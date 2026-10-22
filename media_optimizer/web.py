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

import hmac
import json
import logging
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import NAME, RC_OK, Settings, __version__

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
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --- what the page shows ---------------------------------------------------------
class Session:
    """The page's state. Every change gets a new revision; the page asks for
    the state whenever the revision it saw is old."""

    def __init__(self, settings: Settings, origins: dict[str, str], source: Path | None, target: Path | None,
                 back: tuple[str, str] | None = None) -> None:
        self.settings = settings
        self.origins = origins
        self.source = source
        self.target = target
        self.back_url, self.back_name = back if back else ("", "")
        self.phase = "setup"              # setup, running, finished
        self.code: int | None = None      # the exit code once finished
        self._cond = threading.Condition()
        # From 1: the page's first request (since=0) is answered at once and
        # every later one waits; starting at 0 made the page ask in a loop.
        self._rev = 1

    def bump(self) -> None:
        with self._cond:
            self._rev += 1
            self._cond.notify_all()

    def wait_for_change(self, since: int, timeout: float) -> None:
        with self._cond:
            if since >= self._rev and timeout > 0:
                self._cond.wait(timeout)

    def snapshot(self) -> dict:
        with self._cond:
            return {"rev": self._rev, "version": __version__, "phase": self.phase, "code": self.code,
                    "source": str(self.source) if self.source else "",
                    "target": str(self.target) if self.target else "",
                    "returnUrl": self.back_url, "returnName": self.back_name or "the previous page"}

    def quit(self) -> None:
        """The page's "Close" before or after a run."""
        with self._cond:
            if self.code is None:
                self.code = RC_OK
            self.phase = "finished"
        self.bump()


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
                self._send_json(exc.status, {"error": exc.message})
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
            # Long poll: wait briefly for something new, so progress shows
            # without the page asking ten times a second.
            session.wait_for_change(since_rev, timeout=5.0 if since_rev else 0)
            return session.snapshot()
        if method == "POST" and path == "/api/quit":
            session.quit()
            return {"ok": True}
        if method == "POST" and path in ("/api/ping", "/api/closing"):
            return {"ok": True}
        raise ApiError(404, "Not found.")


def _say(message: str) -> None:
    """A line for the terminal media-optimizer was started in."""
    print(message, flush=True)


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
    try:
        while not server.stop.wait(1.0):
            if server.page_gone():
                break
    except KeyboardInterrupt:
        session.code = 130 if session.code is None else session.code
    finally:
        server.shutdown()
        server.server_close()
    return RC_OK if session.code is None else session.code
