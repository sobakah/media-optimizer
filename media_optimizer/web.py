"""The browser page (``media-optimizer DIR --web``): a thin layer around the
program. The page asks for the settings, starts the real program as a child
(``run -y``) and shows its progress; the terminal stays the default and is
not changed by any of this.

Copied from cdimport and lyrembed, including every rule that was found by a
failure there:
  - 127.0.0.1 only, a port the system picks, no framework, no build step.
  - The printed address carries a token; the first visit turns it into an
    HttpOnly, SameSite=Strict cookie whose name includes the port (two pages
    on 127.0.0.1 overwrote each other's token under one name).
  - The Host header must name this server (DNS rebinding); a POST needs the
    page's own Origin and a JSON body; static files only from static/; a CSP.
  - The page asks for what is new with a revision number (long polling).
  - The stop flag is set only after the reply is written; a closed tab's
    BrokenPipeError/ConnectionResetError is ignored.
  - The page counts as gone after its "closing" beacon plus CLOSE_GRACE (a
    reload sends the beacon too, then pings again), or after
    HEARTBEAT_TIMEOUT without a ping (Chrome runs a hidden tab's timers once a
    minute).
  - The address is flushed: whoever started us may read it from a pipe.
"""

from __future__ import annotations

import codecs
import copy
import fcntl
import hmac
import json
import logging
import os
import re
import secrets
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import folders, questions
from .config import NAME, RC_OK, RULES, Settings, __version__, check_value, to_env
from .config import _describe as describe

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",              # media-optimizer's own icon only
}
_CSP = ("default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'none'")
COOKIE = "media_optimizer_token"
HEARTBEAT_TIMEOUT = 90.0
CLOSE_GRACE = 10.0
# After a run kept going in the background has ended: how long to wait for
# the page to be opened again (the notification's "Open") before exiting.
BACKGROUND_WAIT = 15 * 60.0
MAX_BODY = 1024 * 1024
# keepbest and cdimport find a page's address in its output with
# r"is running at (http://(?:127\.0\.0\.1|localhost):\d+/\?token=[A-Za-z0-9_-]+)": keep the wording.
ADDRESS_LINE = "media-optimizer is running at {url}"


def cookie_name(port: int) -> str:
    return f"{COOKIE}_{port}"


def local_page(url: str) -> bool:
    """A page on this computer: the only kind "Back to ..." may lead to, so
    media-optimizer cannot be used to send a tab elsewhere (as lyrembed)."""
    try:
        parts = urlsplit(url)
        return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost") and parts.port is not None
    except ValueError:
        return False


class ApiError(Exception):
    def __init__(self, status: int, message: str, extra: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


# --- the child's output ------------------------------------------------------------
_LINE_BREAK = re.compile(r"\r\n|\n|\r")
LOG_LINES = 4000


class Log:
    """The child's terminal output, colours included (the page turns them
    into spans). A line a progress display redraws with \r keeps only its
    newest state, as in cdimport."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.dropped = 0                  # lines cut off the front: indexes stay stable
        self.partial = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes) -> None:
        text = self.partial + self._decoder.decode(data)
        pieces = _LINE_BREAK.split(text)
        for piece, brk in zip(pieces, _LINE_BREAK.findall(text)):
            if brk != "\r":              # a piece before a lone \r is redrawn by what follows
                self.lines.append(piece)
        self.partial = pieces[-1]
        if len(self.lines) > LOG_LINES:
            cut = len(self.lines) - LOG_LINES
            del self.lines[:cut]
            self.dropped += cut

    def since(self, index: int) -> dict:
        start = max(index, self.dropped)
        return {"from": start, "lines": self.lines[start - self.dropped:], "partial": self.partial}


# --- the events, folded into what the page draws ---------------------------------------
STAGE_TITLES = {"images": "Images", "gifs": "GIFs", "clips": "Short clips", "video": "Videos",
                "verify": "Final check"}
RECENT_FILES = 300
MAX_MESSAGES = 500


class Progress:
    """The events of a run (events.py), folded into a small state: per stage
    its counts and sizes, the newest files, the messages, the running
    encodes. A run over 100 000 files must not send 100 000 events each time."""

    def __init__(self) -> None:
        self.stages: dict[str, dict] = {}
        self.order: list[str] = []
        self.current: str | None = None
        self.files: deque = deque(maxlen=RECENT_FILES)
        self.messages: list[dict] = []
        self.encodes: dict[int, dict] = {}
        self.dry_run = False

    def _stage(self, name: str) -> dict:
        if name not in self.stages:
            self.stages[name] = {"stage": name, "title": STAGE_TITLES.get(name, name), "total": None, "done": 0,
                                 "outcomes": {}, "before": 0, "after": 0, "state": "waiting", "code": None,
                                 "counters": None}
            self.order.append(name)
        return self.stages[name]

    def apply(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "run_start":
            self.dry_run = bool(ev.get("dry_run"))
        elif kind == "plan":
            for entry in ev.get("stages", []):
                st = self._stage(entry["stage"])
                st["total"] = entry.get("total")
                if entry.get("done"):
                    st["state"] = "earlier"           # finished by the interrupted run
        elif kind == "stage_start":
            st = self._stage(ev["stage"])
            st["state"] = "running"
            if ev.get("total") is not None:
                st["total"] = ev["total"]
            self.current = ev["stage"]
        elif kind == "stage_end":
            st = self._stage(ev["stage"])
            st["state"] = "done"
            st["code"], st["counters"] = ev.get("code"), ev.get("counters")
            self.encodes.clear()
        elif kind == "done":
            st = self._stage(ev["stage"])
            st["done"] += 1
            outcome = ev.get("outcome") or "finished"
            st["outcomes"][outcome] = st["outcomes"].get(outcome, 0) + 1
        elif kind == "file":
            st = self._stage(ev["stage"])
            if ev.get("status") in ("OK", "SALVAGED") and ev.get("before") and ev.get("after"):
                st["before"] += ev["before"]
                st["after"] += ev["after"]
            self.files.append({k: ev.get(k) for k in ("stage", "status", "src", "dest", "before", "after")})
        elif kind == "message" and ev.get("level") in ("error", "warning") and len(self.messages) < MAX_MESSAGES:
            self.messages.append({"level": ev["level"], "tag": ev.get("tag"), "text": ev.get("text"),
                                  "stage": self.current})
        elif kind == "job_start":
            self.encodes[ev["idx"]] = {"idx": ev["idx"], "total": ev.get("total"), "src": ev.get("src"),
                                       "encoder": ev.get("encoder"), "percent": None, "speed": None, "eta": None}
        elif kind == "encode" and ev.get("idx") in self.encodes:
            self.encodes[ev["idx"]].update(percent=ev.get("percent"), speed=ev.get("speed"), eta=ev.get("eta"))

    def finish_job(self, src: str) -> None:
        for idx, job in list(self.encodes.items()):
            if job["src"] == src:
                del self.encodes[idx]

    def snapshot(self) -> dict:
        return {"stages": [self.stages[n] for n in self.order], "current": self.current,
                "files": list(self.files), "messages": self.messages, "encodes": list(self.encodes.values()),
                "dryRun": self.dry_run}


# --- what the page shows ---------------------------------------------------------
class Session:
    """The page's state: the folder and settings while setting up, then the
    child run and its progress. Every change gets a new revision; the page
    asks for the state whenever the revision it saw is old."""

    def __init__(self, settings: Settings, origins: dict[str, str], source: Path | None, target: Path | None,
                 back: tuple[str, str] | None = None, *, dry_run: bool = False, reset: bool = False) -> None:
        self.settings = settings
        self.origins = origins
        self.source = source
        self.target = target
        self.back_url, self.back_name = back if back else ("", "")
        self.dry_run = dry_run            # presets of the form, from -n and --reset
        self.reset = reset
        self.phase = "setup"              # setup, running, finished
        self.code: int | None = None      # the exit code of the last run
        self.child: subprocess.Popen | None = None
        self.log = Log()
        self.progress = Progress()
        self.stopping = False
        self.background = False           # the run goes on when the page is gone
        self.run_number = 0               # the page's log belongs to one run
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._cond = threading.Condition()
        # From 1: the page's first request (since=0) is answered at once and
        # every later one waits; starting at 0 made the page ask in a loop.
        self._rev = 1

    # revisions
    def bump(self) -> None:
        with self._cond:
            self._rev += 1
            self._cond.notify_all()

    def wait_for_change(self, since: int, timeout: float) -> None:
        with self._cond:
            if since >= self._rev and timeout > 0:
                self._cond.wait(timeout)

    def snapshot(self, log_from: int = 0, log_run: int = 0) -> dict:
        with self._cond:
            data = {"rev": self._rev, "version": __version__, "phase": self.phase, "code": self.code,
                    "source": str(self.source) if self.source else "",
                    "target": str(self.target) if self.target else "",
                    "returnUrl": self.back_url, "returnName": self.back_name or "the previous page",
                    "stopping": self.stopping, "background": self.background, "run": self.run_number}
            if self.phase != "setup":
                data["progress"] = self.progress.snapshot()
                # A request that still counts lines of an earlier run gets this one from the start.
                data["log"] = self.log.since(log_from if log_run == self.run_number else 0)
            return data

    # setting up
    def folder_info(self) -> dict:
        """What the page shows about the chosen folder: whether it can be
        used, an interrupted run to resume, the subfolders, notes."""
        from . import cache, orchestrator, safety

        info: dict = {"source": str(self.source) if self.source else "",
                      "target": str(self.target) if self.target else "", "error": None, "resume": None,
                      "busy": None, "tree": [], "rootCounts": [0, 0], "notes": [], "dryRun": self.dry_run,
                      "reset": self.reset}
        if self.source is None:
            info["error"] = "Choose the folder to convert."
            return info
        if not self.source.is_dir():
            info["error"] = f"Folder not found: {self.source}"
            return info
        if self.target is not None and self.target == self.source:
            self.target = None
            info["target"] = ""
        state, busy = orchestrator._pick_state(orchestrator.Request(self.settings, str(self.source), None))
        info["busy"] = busy
        if state is not None:
            info["resume"] = {"output": state.output, "done": state.done,
                              "changed": {k: v for k, v in state.settings.items()
                                          if k in RULES and getattr(Settings(), k) != v}}
        found: list = []
        counts = folders.media_counts(self.source, self.settings, found)
        info["rootCounts"] = list(counts.get(folders.ROOT, (0, 0)))
        info["tree"] = [{"rel": rel, "images": counts.get(rel, (0, 0))[0], "videos": counts.get(rel, (0, 0))[1]}
                        for rel in folders.subfolder_entries(counts)]
        work = self.target if self.target is not None else self.source
        # Files an earlier run handled are skipped while the caches are used.
        self._cache_view = folders.CacheView(self.source, work, found, self.settings.include_hidden)
        info["cached"] = self.cached(self.settings)
        if work.is_dir() and cache.find_legacy(work, self.settings.include_hidden):
            info["notes"].append("Video caches of an older version were found; this run converts them to the "
                                 "new format (the old file is kept as .v1.bak).")
        if not safety.trash_available():
            info["notes"].append("No trash is available (send2trash, gio, trash-put). Originals are kept and "
                                 "listed instead of being deleted.")
        return info

    def cached(self, settings: Settings) -> dict:
        """Per folder, the (images, videos) the stages' caches would skip with
        *settings* (whether the caches are used at all is the page's to show)."""
        view = getattr(self, "_cache_view", None)
        counts = view.counts(settings) if view is not None else {}
        return {"root": list(counts.get(folders.ROOT, (0, 0))),
                "tree": {rel: list(c) for rel, c in counts.items() if rel != folders.ROOT}}

    def cached_for(self, values: dict) -> dict:
        """cached() with the form's values; invalid ones keep their setting."""
        s, errors = _settings_from(self.settings, values)
        if errors:
            s, _ = _settings_from(self.settings, {k: v for k, v in values.items() if k not in errors})
        return self.cached(s)

    def setup(self) -> dict:
        return {"folder": self.folder_info(), "schema": questions.schema(self.settings, self.origins)}

    def set_folder(self, source: str, target: str) -> dict:
        with self._cond:
            if self.phase != "setup":
                raise ApiError(409, "A run is going on.")
        self.source = Path(source).expanduser().absolute() if source.strip() else None
        self.target = Path(target).expanduser().absolute() if target.strip() else None
        self.bump()
        return self.setup()

    # running
    def start(self, body: dict) -> None:
        with self._cond:
            if self.phase == "running":
                raise ApiError(409, "A run is going on.")
        info = self.folder_info()
        if info["error"]:
            raise ApiError(400, info["error"])
        if info["busy"]:
            raise ApiError(409, info["busy"])
        mode = body.get("mode", "new")
        if info["resume"] and mode not in ("resume", "reset"):
            raise ApiError(409, "This folder has an interrupted run: resume it or start over.")
        s, errors = _settings_from(self.settings, body.get("settings") or {})
        if errors:
            raise ApiError(400, "Some settings are not valid.", {"fields": errors})
        self._tmp = tempfile.TemporaryDirectory(prefix="mo_web_")
        events_file = Path(self._tmp.name) / "events.jsonl"
        events_file.touch()
        cmd = self.command(mode, bool(body.get("dry_run")), events_file)
        env = {**os.environ, **to_env(s), "MO_HOLD": "0", "COLUMNS": str(LOG_COLUMNS)}
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, LOG_COLUMNS, 0, 0))
        except OSError:
            pass
        try:
            child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, env=env,
                                     close_fds=True)
        except OSError as exc:
            os.close(master)
            os.close(slave)
            raise ApiError(500, f"Could not start the run: {exc}") from exc
        os.close(slave)
        with self._cond:
            self.child, self.phase, self.code, self.stopping, self.background = child, "running", None, False, False
            self.log, self.progress = Log(), Progress()
            self.run_number += 1
        self.bump()
        reader = threading.Thread(target=self._read_output, args=(master,), name="mo-web-output", daemon=True)
        tail = threading.Thread(target=self._read_events, args=(events_file, child), name="mo-web-events",
                                daemon=True)
        reader.start()
        tail.start()
        threading.Thread(target=self._wait, args=(child, reader, tail), name="mo-web-wait", daemon=True).start()

    def command(self, mode: str, dry_run: bool, events_file: Path) -> list[str]:
        """The run the page starts: the real program, asking nothing."""
        cmd = [sys.executable, "-m", "media_optimizer", "run", "-y", str(self.source)]
        if self.target is not None:
            cmd.append(str(self.target))
        if mode == "reset":
            cmd.append("--reset")
        if dry_run:
            cmd.append("-n")
        return cmd + ["--events", str(events_file)]

    def keep_in_background(self) -> None:
        with self._cond:
            if self.phase != "running":
                raise ApiError(409, "No run is going on.")
            self.background = True
        self.bump()

    def summary(self) -> str:
        """One line for the notification."""
        converted = before = after = failed = 0
        for st in self.progress.stages.values():
            converted += st["outcomes"].get("converted", 0)
            failed += st["outcomes"].get("failed", 0)
            before += st["before"]
            after += st["after"]
        if self.code == 130:
            return "Stopped; the next start offers to resume."
        parts = [f"{converted} file{'s' if converted != 1 else ''} converted"]
        if before > after:
            parts.append(f"{_bytes(before - after)} saved")
        if failed:
            parts.append(f"{failed} failed")
        if self.code not in (0, 3):
            parts.append(f"exit code {self.code}")
        return ", ".join(parts) + "."

    def _read_output(self, master: int) -> None:
        try:
            while True:
                try:
                    data = os.read(master, 65536)
                except OSError:           # EIO: the child closed the terminal
                    break
                if not data:
                    break
                with self._cond:
                    self.log.feed(data)
                self.bump()
        finally:
            os.close(master)

    def _read_events(self, path: Path, child: subprocess.Popen) -> None:
        with open(path, "rb") as fh:
            pending = b""
            while True:
                chunk = fh.read()
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    with self._cond:
                        for line in lines:
                            try:
                                ev = json.loads(line)
                            except ValueError:
                                continue
                            self.progress.apply(ev)
                            if ev.get("event") == "done" and ev.get("stage") == "video":
                                self.progress.finish_job(ev.get("src"))
                    self.bump()
                elif child.poll() is not None:
                    break
                else:
                    time.sleep(0.2)

    def _wait(self, child: subprocess.Popen, reader: threading.Thread, tail: threading.Thread) -> None:
        code = child.wait()
        reader.join(timeout=5)
        tail.join(timeout=5)
        with self._cond:
            self.code, self.phase, self.stopping = code, "finished", False
            if self._tmp is not None:
                self._tmp.cleanup()
                self._tmp = None
        self.bump()

    def stop(self) -> bool:
        """Ctrl+C for the run: it saves its state and ends with 130."""
        with self._cond:
            child = self.child
            if self.phase != "running" or child is None or child.poll() is not None:
                return False
            self.stopping = True
        try:
            child.send_signal(signal.SIGINT)
        except OSError:
            return False
        self.bump()
        return True

    def again(self) -> None:
        """Back to the form for another folder."""
        with self._cond:
            if self.phase != "finished":
                raise ApiError(409, "The run has not finished.")
            self.phase = "setup"
        self.bump()

    def quit(self) -> None:
        """The page's "Close" before or after a run."""
        with self._cond:
            if self.phase == "running":
                raise ApiError(409, "A run is going on: stop it first.")
            if self.code is None:
                self.code = RC_OK
            self.phase = "finished"
        self.bump()


LOG_COLUMNS = 110


def _bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _settings_from(base: Settings, values: dict) -> tuple[Settings, dict[str, str]]:
    """The form's values, checked by the same rules as a config file."""
    s = copy.deepcopy(base)
    errors: dict[str, str] = {}
    for name, value in values.items():
        if name not in RULES:
            errors[name] = "unknown setting"
            continue
        rule = RULES[name]
        base_kind = rule[1][0] if rule[0] == "opt" else rule[0]
        if base_kind == "num" and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not check_value(rule, value):
            errors[name] = f"expected {describe(rule)}"
            continue
        setattr(s, name, value)
    return s, errors


# --- HTTP ------------------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, session: Session, token: str, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.session = session
        self.token = token
        self.stop = threading.Event()
        self.last_contact: float | None = None
        self.closing_at: float | None = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    def contact(self) -> None:
        self.last_contact = time.monotonic()
        self.closing_at = None

    def page_gone(self) -> bool:
        now = time.monotonic()
        if self.closing_at is not None and now - self.closing_at > CLOSE_GRACE:
            return True
        return self.last_contact is not None and now - self.last_contact > HEARTBEAT_TIMEOUT


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = NAME

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - the base class's name
        log.debug("web: " + format, *args)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _host_ok(self) -> bool:
        # A page elsewhere could reach 127.0.0.1 through a name it controls
        # (DNS rebinding); the Host header then carries that name.
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _cookie_token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == cookie_name(self.server.port):
                return value
        return ""

    def _authorized(self) -> bool:
        return hmac.compare_digest(self._cookie_token().encode(), self.server.token.encode())

    def _body(self, json_required: bool) -> dict:
        if self.headers.get("Origin", "") != f"http://{self.headers.get('Host', '')}":
            raise ApiError(403, "Requests must come from media-optimizer's own page.")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "Request too large.")
        raw = self.rfile.read(length) if length else b""
        if not json_required:
            return {}
        # A form on another site cannot send this type without asking first.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ApiError(415, "Expected JSON.")
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ApiError(400, f"Invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "Expected a JSON object.")
        return data

    def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, data) -> None:
        # ensure_ascii: file names that are not valid UTF-8 (surrogates) still encode.
        self._send(status, json.dumps(data, ensure_ascii=True).encode("ascii"), "application/json; charset=utf-8")

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                raise ApiError(400, "Unexpected host.")
            url = urlsplit(self.path)
            if method == "GET" and url.path == "/":
                self._index(url.query)
                return
            if not self._authorized():
                raise ApiError(403, "Open media-optimizer through the address it printed in the terminal.")
            if method == "GET" and url.path.startswith("/static/"):
                self._static(url.path[len("/static/"):])
                return
            if not url.path.startswith("/api/"):
                raise ApiError(404, "Not found.")
            body = self._body(json_required=url.path != "/api/closing") if method == "POST" else {}
            self.server.contact()
            self._send_json(200, self._api(method, url.path, parse_qs(url.query), body))
            # Only once the answer is out: stopping first could end the
            # program while the answer is still being written.
            if url.path == "/api/closing":
                self.server.closing_at = time.monotonic()
            elif url.path == "/api/quit":
                self.wfile.flush()
                self.server.stop.set()
        except (BrokenPipeError, ConnectionResetError):
            return  # the page went away mid-answer (a closed tab, a reload)
        except ApiError as exc:
            if self.path.startswith("/api/"):
                self._send_json(exc.status, {"error": exc.message, **exc.extra})
            else:
                self._send(exc.status, exc.message.encode("utf-8"), "text/plain; charset=utf-8")
        except Exception as exc:  # a bug must not kill the server thread silently
            log.exception("web request failed: %s %s", method, self.path)
            self._send_json(500, {"error": f"Internal error: {exc}"})

    def _index(self, query: str) -> None:
        given = (parse_qs(query).get("token") or [""])[0]
        if given and hmac.compare_digest(given.encode(), self.server.token.encode()):
            self._send(303, b"", "text/plain", {
                "Location": "/",
                "Set-Cookie": f"{cookie_name(self.server.port)}={self.server.token}; HttpOnly; SameSite=Strict; Path=/",
            })
            return
        if not self._authorized():
            raise ApiError(403, "Open media-optimizer through the address it printed in the terminal.")
        self._static("index.html")

    def _static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            raise ApiError(404, "Not found.")
        content_type = _CONTENT_TYPES.get(target.suffix)
        if content_type is None:
            raise ApiError(404, "Not found.")
        headers = {"Content-Security-Policy": _CSP} if target.suffix == ".html" else None
        self._send(200, target.read_bytes(), content_type, headers)

    def _api(self, method: str, path: str, query: dict, body: dict):
        session = self.server.session
        if method == "GET" and path == "/api/state":
            since = (query.get("since") or ["0"])[0]
            since_rev = int(since) if since.isdigit() else 0
            log_from = (query.get("log") or ["0"])[0]
            log_run = (query.get("run") or ["0"])[0]
            # Long poll: wait briefly for something new, so progress shows
            # without the page asking ten times a second.
            session.wait_for_change(since_rev, timeout=5.0 if since_rev else 0)
            return session.snapshot(int(log_from) if log_from.isdigit() else 0,
                                    int(log_run) if log_run.isdigit() else 0)
        if method == "GET" and path == "/api/setup":
            return session.setup()
        if method == "POST" and path == "/api/folder":
            return session.set_folder(str(body.get("source") or ""), str(body.get("target") or ""))
        if method == "POST" and path == "/api/cached":
            values = body.get("settings")
            return session.cached_for(values if isinstance(values, dict) else {})
        if method == "POST" and path == "/api/start":
            session.start(body)
            return {"ok": True}
        if method == "POST" and path == "/api/stop":
            return {"stopped": session.stop()}
        if method == "POST" and path == "/api/background":
            session.keep_in_background()
            return {"ok": True}
        if method == "POST" and path == "/api/again":
            session.again()
            return {"ok": True}
        if method == "POST" and path == "/api/quit":
            session.quit()
            return {"ok": True}
        if method == "POST" and path in ("/api/ping", "/api/closing"):
            return {"ok": True}
        raise ApiError(404, "Not found.")


def _say(message: str) -> None:
    """A line for the terminal media-optimizer was started in."""
    print(message, flush=True)


class Watch:
    """What happens when the page goes away (checked once a second):
      - before or after a run: the program ends;
      - during a run: the run gets Ctrl+C (it saves its state, 130), unless
        "Keep running in the background" was chosen;
      - a background run that has ended: a notification with "Open", then
        wait for the page; seen again, the usual rules apply; not seen
        within BACKGROUND_WAIT, the program ends with the run's code."""

    def __init__(self, server: _Server, session: Session, url: str, notify=None) -> None:
        self.server = server
        self.session = session
        self.url = url
        self.notify = notify
        self.finished_at: float | None = None

    def tick(self) -> bool:
        """True when the program should end."""
        session, server = self.session, self.server
        gone = server.page_gone()
        if session.phase == "running":
            if gone and not session.background and not session.stopping:
                session.stop()
            return False
        if session.background and session.phase == "finished":
            now = time.monotonic()
            if self.finished_at is None:
                self.finished_at = now
                self._notify()
            if server.last_contact is not None and server.last_contact > self.finished_at:
                session.background = False            # seen again: back to the usual rules
                return False
            return now - self.finished_at > BACKGROUND_WAIT
        return gone

    def _notify(self) -> None:
        from . import notify

        send = self.notify or notify.notify
        title = "media-optimizer finished" if self.session.code in (0, 3) else "media-optimizer stopped"
        send(title, self.session.summary(), icon=str(STATIC_DIR / "icon.svg"), action="Open",
             on_action=lambda: webbrowser.open(self.url))


def serve(session: Session, open_browser: bool = True, port: int = 0) -> int:
    """Run the page until it is closed; returns the exit code."""
    token = secrets.token_urlsafe(24)
    server = _Server(session, token, port)
    url = f"http://127.0.0.1:{server.port}/?token={token}"
    thread = threading.Thread(target=server.serve_forever, name="media-optimizer-web", daemon=True)
    thread.start()
    _say(ADDRESS_LINE.format(url=url))
    opened = open_browser and webbrowser.open(url)
    if session.back_url:
        _say(f"When the run is done, its page leads back to {session.back_name or 'the previous page'}.")
    else:
        if not opened:
            _say("Open that address in your browser.")
        _say("Close the page when done, or press Ctrl+C here.")
    sys.stdout.flush()
    watch = Watch(server, session, url)
    try:
        while not server.stop.wait(1.0):
            if watch.tick():
                break
    except KeyboardInterrupt:
        # The run is in our process group and got the Ctrl+C too: it saves
        # its state and ends with 130. Wait for it.
        child = session.child
        if child is not None and child.poll() is None:
            try:
                child.wait(timeout=120)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            session.code = child.returncode
        elif session.code is None:
            session.code = 130
    finally:
        server.shutdown()
        server.server_close()
    return RC_OK if session.code is None else session.code
