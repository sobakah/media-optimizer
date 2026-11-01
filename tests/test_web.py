"""The browser page's server: the security rules copied from cdimport and
lyrembed, the options keepbest's hand-off relies on, and the address line
it reads. No browser here; the page itself is clicked through outside the
suite."""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import PYDIR, _env
from media_optimizer import web
from media_optimizer.config import Settings

TOKEN = "test-token"
# cdimport/web.py's _HANDOFF, verbatim: keepbest reads the address the same way.
HANDOFF = re.compile(r"is running at (http://(?:127\.0\.0\.1|localhost):\d+/\?token=[A-Za-z0-9_-]+)")


@pytest.fixture
def server(tmp_path):
    session = web.Session(Settings(), {}, tmp_path, None, ("http://127.0.0.1:9/", "keepbest"))
    srv = web._Server(session, TOKEN)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _http(srv, method: str, path: str, body=None, headers: dict | None = None, cookie: bool = True):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    h = {"Host": f"127.0.0.1:{srv.port}"}
    if cookie:
        h["Cookie"] = f"{web.cookie_name(srv.port)}={TOKEN}"
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.update({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{srv.port}"})
    h.update(headers or {})
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), raw


def test_token_becomes_a_cookie_named_after_the_port(server):
    status, headers, _ = _http(server, "GET", f"/?token={TOKEN}", cookie=False)
    assert status == 303 and headers["Location"] == "/"
    assert headers["Set-Cookie"] == f"media_optimizer_token_{server.port}={TOKEN}; HttpOnly; SameSite=Strict; Path=/"
    assert _http(server, "GET", "/?token=wrong", cookie=False)[0] == 403
    assert _http(server, "GET", "/api/state", cookie=False)[0] == 403
    assert _http(server, "GET", "/api/state", headers={"Cookie": f"media_optimizer_token_1={TOKEN}"},
                 cookie=False)[0] == 403, "another port's cookie does not count"


def test_index_has_the_csp_and_the_security_headers(server):
    status, headers, body = _http(server, "GET", "/")
    assert status == 200 and b'id="app"' in body
    assert headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    status, headers, _ = _http(server, "GET", "/static/app.js")
    assert status == 200 and "Content-Security-Policy" not in headers
    assert headers["Content-Type"].startswith("text/javascript")


def test_wrong_host_is_refused(server):
    """DNS rebinding: a page elsewhere reaching 127.0.0.1 under its own name."""
    assert _http(server, "GET", "/api/state", headers={"Host": f"evil.example:{server.port}"})[0] == 400
    assert _http(server, "GET", "/api/state", headers={"Host": f"localhost:{server.port}"})[0] == 200


def test_posts_need_the_page_origin_and_json(server):
    assert _http(server, "POST", "/api/ping", {}, headers={"Origin": "http://evil.example"})[0] == 403
    assert _http(server, "POST", "/api/ping", {}, headers={"Content-Type": "text/plain"})[0] == 415
    assert _http(server, "POST", "/api/ping", b"[1]")[0] == 400
    assert _http(server, "POST", "/api/ping", b"x" * (web.MAX_BODY + 1))[0] == 413
    # The closing beacon is sent without a JSON type (navigator.sendBeacon).
    assert _http(server, "POST", "/api/closing", b"", headers={"Content-Type": "text/plain"})[0] == 200


@pytest.mark.parametrize("path", ["/static/../web.py", "/static/%2e%2e/web.py", "/static/vendor/../../cli.py",
                                  "/static/nothing.js", "/elsewhere"])
def test_only_static_files_are_served(server, path):
    assert _http(server, "GET", path)[0] == 404


def test_no_way_out_of_the_static_folder(server, tmp_path, monkeypatch):
    """A served suffix just outside static/: only the folder check stops it."""
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "ok.txt").write_text("fine")
    (tmp_path / "secret.txt").write_text("secret")
    monkeypatch.setattr(web, "STATIC_DIR", tmp_path / "static")
    assert _http(server, "GET", "/static/ok.txt")[2] == b"fine"
    assert _http(server, "GET", "/static/../secret.txt")[0] == 404


def test_state_and_long_poll(server):
    status, _, body = _http(server, "GET", "/api/state")
    state = json.loads(body)
    assert status == 200 and state["phase"] == "setup" and state["returnName"] == "keepbest"
    started = time.monotonic()
    threading.Timer(0.3, server.session.bump).start()
    status, _, body = _http(server, "GET", f"/api/state?since={state['rev']}")
    assert json.loads(body)["rev"] == state["rev"] + 1 and time.monotonic() - started < 3, \
        "a change answers the waiting request at once"


def test_quit_sets_the_stop_flag_after_the_reply(server):
    status, _, body = _http(server, "POST", "/api/quit", {})
    assert status == 200 and json.loads(body) == {"ok": True}, "the reply arrived in full"
    assert server.stop.wait(2)
    assert server.session.phase == "finished" and server.session.code == 0


def test_page_gone(server, monkeypatch):
    assert not server.page_gone(), "never before the first contact"
    server.contact()
    server.closing_at = time.monotonic() - web.CLOSE_GRACE - 1
    assert server.page_gone()
    server.contact()
    assert not server.page_gone(), "a reload pings again"
    server.last_contact = time.monotonic() - web.HEARTBEAT_TIMEOUT - 1
    assert server.page_gone()


@pytest.mark.parametrize("url, ok", [("http://127.0.0.1:8000/", True), ("http://localhost:8000/x", True),
                                     ("http://127.0.0.1/", False), ("https://127.0.0.1:8000/", False),
                                     ("http://example.com:8000/", False), ("http://127.0.0.1.evil:80/", False),
                                     ("javascript:alert(1)", False), ("http://[::1:80/", False)])
def test_return_url_only_to_this_computer(url, ok):
    assert web.local_page(url) is ok


@pytest.mark.parametrize("args, message", [
    (["--web", "--return-url", "http://example.com:80/"], "must be a page on this computer"),
    (["--web", "--return-name", "keepbest"], "only applies together with --return-url"),
    (["--no-browser"], "only applies together with --web"),
    (["--return-url", "http://127.0.0.1:8000/"], "only applies together with --web"),
    (["--web", "-y"], "exclude each other"),
])
def test_web_option_rules(run_cli, tmp_path, args, message):
    out = run_cli(tmp_path, *args)
    assert out.rc == 2 and message in out, out


def test_help_lists_the_return_url():
    """keepbest decides by this whether to hand its tab over (cdimport's can_return)."""
    out = subprocess.run([sys.executable, "-m", "media_optimizer", "--help"], capture_output=True, text=True,
                         env=_env(None, None))
    assert out.returncode == 0
    run_help = subprocess.run([sys.executable, "-m", "media_optimizer", "run", "--help"], capture_output=True,
                              text=True, env=_env(None, None)).stdout
    assert "--return-url" in run_help and "--no-browser" in run_help and "--web" in run_help


def test_address_line_is_found_like_cdimport_finds_lyrembed(tmp_path):
    """The real program: the first line is the address, flushed (read from
    a pipe), in the form keepbest looks for; quitting from the page ends the
    process with 0."""
    p = subprocess.Popen([sys.executable, "-m", "media_optimizer", str(tmp_path), "--web", "--no-browser"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         env=_env(None, None))
    try:
        line = p.stdout.readline()
        match = HANDOFF.search(line)
        assert match, line
        port = int(re.search(r":(\d+)/", match.group(1)).group(1))
        token = match.group(1).split("token=")[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/api/quit", body=b"{}", headers={
            "Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json",
            "Cookie": f"{web.cookie_name(port)}={token}"})
        assert conn.getresponse().status == 200
        assert p.wait(timeout=10) == 0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        p.stdout.close()


def test_static_files_are_packaged():
    """pyproject's package-data covers every file of the page, or a pipx
    install ships it without them."""
    text = (PYDIR / "pyproject.toml").read_text()
    globs = json.loads(re.search(r"media_optimizer = (\[[^\]]*\])", text).group(1))
    static = PYDIR / "media_optimizer" / "static"
    files = [p.relative_to(static.parent).as_posix() for p in static.rglob("*") if p.is_file()]
    assert files
    uncovered = [f for f in files if not any(Path(f).match(g) and f.count("/") == g.count("/") for g in globs)]
    assert not uncovered


# --- the session: setting up, running, stopping ------------------------------------------
def _session(source, target=None, **kw):
    from media_optimizer.config import load_settings
    origins: dict[str, str] = {}
    s, _, _ = load_settings(origins=origins)
    return web.Session(s, origins, source, target, **kw)


def _wait_finished(session, timeout=120):
    end = time.monotonic() + timeout
    while session.phase != "finished" and time.monotonic() < end:
        time.sleep(0.1)
    assert session.phase == "finished", session.log.lines[-20:]


def _interrupted_state(tmp_path, source, output, **extra):
    state = {"status": "INTERRUPTED", "source": str(source), "output": str(output) if output else None,
             "settings": {"img_target": "webp"}, "done": {"images": False, "gifs": True, "clips": True,
                                                         "video": True}, **extra}
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))


def test_folder_info(folder, tmp_path):
    assert _session(tmp_path / "missing").folder_info()["error"].startswith("Folder not found")
    assert _session(None).folder_info()["error"] == "Choose the folder to convert."
    d = folder("w1", "photo.jpg", "a/b/x.jpg=photo.jpg", "a/l.mp4=long.mp4")
    info = _session(d).folder_info()
    assert info["error"] is None and info["resume"] is None and info["busy"] is None
    assert info["rootCounts"] == [1, 0]
    assert info["tree"] == [{"rel": "a", "images": 0, "videos": 1}, {"rel": "a/b", "images": 1, "videos": 0}]
    _interrupted_state(tmp_path, d, tmp_path / "w1out")
    resume = _session(d).folder_info()["resume"]
    assert resume["changed"] == {"img_target": "webp"} and resume["done"]["images"] is False
    _interrupted_state(tmp_path, d, None, status="RUNNING", pid=os.getppid())     # a live process, not ours
    assert "Another run is working" in _session(d).folder_info()["busy"]


def test_start_checks_the_values_and_the_resume_choice(folder, tmp_path):
    d = folder("w2", "photo.jpg")
    session = _session(d)
    with pytest.raises(web.ApiError) as exc:
        session.start({"settings": {"jxl_effort": 99, "nope": 1, "png_mode": "half"}})
    assert exc.value.status == 400 and set(exc.value.extra["fields"]) == {"jxl_effort", "nope", "png_mode"}
    _interrupted_state(tmp_path, d, None)
    with pytest.raises(web.ApiError) as exc:
        _session(d).start({})
    assert exc.value.status == 409 and "resume it or start over" in exc.value.message
    assert session.phase == "setup" and session.child is None


def _form(setup: dict) -> dict:
    """The folder choice as the form gets it: (value, where it came from)."""
    fields = {f["name"]: f for g in setup["schema"]["groups"] for f in g["fields"]}
    return {n: (fields[n]["value"], fields[n].get("origin")) for n in ("subfolders", "exclude_folders")}


def test_the_folder_choice_stays_with_its_folder(folder, tmp_path, monkeypatch):
    """keepbest passes its choice in the environment. "Use these folders" on
    the form silently kept it for the next folder, where "skipme" or "." mean
    something else; the terminal's "another directory" drops it."""
    from media_optimizer import cli
    a, b = folder("fa", "photo.jpg"), folder("fb", "photo.jpg")
    monkeypatch.setenv("MO_SUBFOLDERS", "include")
    monkeypatch.setenv("MO_EXCLUDE_FOLDERS", '[".", "skipme"]')
    config = tmp_path / "config.json"
    config.write_text('{"subfolders": "exclude"}')             # the config stays, as in the terminal
    sessions = []
    monkeypatch.setattr(web, "serve", lambda session, **kw: sessions.append(session) or 0)
    cli._run_web(cli.build_parser().parse_args(
        ["run", str(a), "--web", "--no-browser", "--exclude-folder", "cli", "--config", str(config)]))
    session = sessions[0]
    given = _form(session.setup())
    assert given["exclude_folders"] == (["cli"], "cli") and given["subfolders"][0] == "include"
    assert _form(session.set_folder(str(b), ""))["exclude_folders"][0] == []
    assert _form(session.setup())["subfolders"][0] == "exclude"
    assert _form(session.set_folder(str(a), "")) == given       # back: it is that folder's again


def test_a_run_from_the_page(folder, tmp_path):
    """The form's values reach the child through MO_*, the folder choice
    included; the progress and the colourless log come back."""
    d = folder("w3", "photo.jpg", "anim.gif", "sub/x.jpg=photo.jpg")
    o = tmp_path / "w3out"
    session = _session(d, o)
    session.start({"settings": {"img_target": "webp", "exclude_folders": ["sub"], "subfolders": "include"}})
    assert session.phase == "running"
    _wait_finished(session)
    assert session.code == 0
    assert (o / "photo.webp").exists() and (o / "anim.webp").exists() and not (o / "sub").exists()
    snap = session.snapshot()
    stages = {s["stage"]: s for s in snap["progress"]["stages"]}
    assert stages["images"]["done"] == stages["images"]["total"] == 1 and stages["images"]["state"] == "done"
    assert stages["images"]["outcomes"] == {"converted": 1} and stages["gifs"]["after"] > 0
    assert any("ALL STAGES COMPLETED" in line for line in snap["log"]["lines"])
    assert snap["run"] == 1


def test_start_over_passes_reset(folder, tmp_path):
    d = folder("w4", "photo.jpg")
    _interrupted_state(tmp_path, d, None)          # its saved settings would make WebP
    session = _session(d, tmp_path / "w4out")
    session.start({"mode": "reset", "settings": {}})
    _wait_finished(session)
    assert session.code == 0 and (tmp_path / "w4out" / "photo.jxl").exists(), session.log.lines[-10:]
    assert not any("resuming" in line for line in session.log.lines)


def test_quit_and_again_follow_the_phase(folder, tmp_path):
    session = _session(folder("w5", "photo.jpg"))
    with pytest.raises(web.ApiError):
        session.again()
    session.phase = "running"
    with pytest.raises(web.ApiError):
        session.quit()
    session.phase = "finished"
    session.again()
    assert session.phase == "setup"


@pytest.mark.slow
def test_stop_saves_the_state(folder, tmp_path):
    """Stop is Ctrl+C for the run: 130, and the next start offers to resume."""
    d = folder("w6", "s.mp4=slow.mp4")
    session = _session(d, tmp_path / "w6out")
    session.start({"settings": {"min_size_mb": 0, "enable_probe": False, "encoder_mode": "cpu",
                                "cpu_preset": "slow"}})
    end = time.monotonic() + 30
    while not session.progress.encodes and time.monotonic() < end:
        time.sleep(0.2)
    assert session.progress.encodes, session.log.lines[-10:]
    assert session.stop()
    _wait_finished(session, 60)
    assert session.code == 130
    assert _session(d).folder_info()["resume"] is not None


def test_log_keeps_the_newest_state_of_a_redrawn_line():
    log = web.Log()
    log.feed(b"one\n  [1/3] 0 skipped\r  [2/3] 0 skipped\r\x1b[K")
    log.feed("two é".encode()[:-1])            # a character split across reads
    log.feed("two é".encode()[-1:] + b"\nthree")
    assert log.lines == ["one", "\x1b[Ktwo é"] and log.partial == "three"
    assert log.since(1) == {"from": 1, "lines": ["\x1b[Ktwo é"], "partial": "three"}


def test_log_keeps_a_line_whose_line_break_is_split_across_reads():
    """The pty sends \\r\\n; a read ending between the two dropped the line as
    a redraw."""
    log = web.Log()
    log.feed(b"[ERROR] x.mp4\r")
    assert log.lines == [] and log.since(0)["partial"] == "[ERROR] x.mp4"
    log.feed(b"\nnext\r")
    log.feed(b"redrawn\n")
    assert log.lines == ["[ERROR] x.mp4", "redrawn"]


def test_the_last_events_of_a_run_are_read(tmp_path):
    """A run that writes its last events and ends between an empty read and
    the check whether it has ended."""
    events_file = tmp_path / "events.jsonl"
    events_file.touch()

    class Child:
        def poll(self):
            if not events_file.read_text():
                with open(events_file, "a") as fh:
                    fh.write(json.dumps({"event": "stage_start", "stage": "images", "total": 1}) + "\n")
                    fh.write(json.dumps({"event": "stage_end", "stage": "images", "code": 0, "counters": {}}) + "\n")
            return 0

    session = web.Session(Settings(), {}, tmp_path, None)
    session._read_events(events_file, Child())
    assert session.progress.snapshot()["stages"][0]["state"] == "done"


def test_one_start_at_a_time(folder):
    """Two starts at once (a double click, two tabs) both got past the check."""
    session = _session(folder("w7", "photo.jpg"))
    session._starting = True
    with pytest.raises(web.ApiError) as exc:
        session.start({})
    assert exc.value.status == 409 and session.child is None


def test_a_preview_of_starting_over_ignores_the_saved_run(folder, tmp_path):
    """--reset -n kept the state (a dry run removes nothing) and then resumed
    it: the preview showed the saved settings, not the form's."""
    d = folder("w8", "photo.jpg")
    _interrupted_state(tmp_path, d, None)          # its saved settings would make WebP
    session = _session(d)
    session.start({"mode": "reset", "dry_run": True, "settings": {}})
    _wait_finished(session)
    log = "\n".join(session.log.lines)
    assert session.code == 0 and "photo.jxl" in log and "photo.webp" not in log, log
    assert "resuming" not in log
    assert _session(d).folder_info()["resume"] is not None, "the state stays for a real run"


def test_a_preview_can_be_run_for_real_with_the_same_settings(folder, tmp_path):
    """The form's values only exist in the run: going through the form again
    lost them, and a "rerun" used other settings than the preview showed."""
    d = folder("wr", "photo.jpg")
    o = tmp_path / "wrout"
    session = _session(d, o)
    session.start({"dry_run": True, "settings": {"img_target": "webp"}})
    _wait_finished(session)
    assert session.code == 0 and not o.exists()
    assert session.snapshot()["realRun"] == {"target": str(o), "deletes": False}
    session.run_for_real()
    _wait_finished(session)
    assert session.code == 0 and (o / "photo.webp").exists() and not (o / "photo.jxl").exists()
    assert session.run_number == 2 and session.snapshot()["realRun"] is None, "only after a preview"
    with pytest.raises(web.ApiError) as exc:
        session.run_for_real()
    assert exc.value.status == 409


def test_a_run_for_real_says_when_it_deletes(folder):
    d = folder("wd", "photo.jpg")
    session = _session(d)
    session.start({"dry_run": True, "settings": {}})
    _wait_finished(session)
    assert session.snapshot()["realRun"] == {"target": "", "deletes": True}, "in place: originals go"


def test_a_stopped_preview_offers_no_real_run(folder):
    session = _session(folder("ws", "photo.jpg"))
    session._last = {"settings": {}, "mode": "new", "dry_run": True}
    session.phase, session.code = "finished", 130
    assert session.snapshot()["realRun"] is None
    with pytest.raises(web.ApiError):
        session.run_for_real()


def test_back_to_the_form_keeps_what_the_preview_ran_with(folder, tmp_path):
    d = folder("wf", "photo.jpg", "sub/x.jpg=photo.jpg")
    session = _session(d, tmp_path / "wfout")
    session.start({"dry_run": True, "settings": {"img_target": "webp", "subfolders": "include",
                                                 "exclude_folders": ["sub"]}})
    _wait_finished(session)
    session.back_to_form()
    assert session.phase == "setup"
    fields = {f["name"]: f for g in session.setup()["schema"]["groups"] for f in g["fields"]}
    assert fields["img_target"]["value"] == "webp" and fields["exclude_folders"]["value"] == ["sub"]
    assert fields["img_target"]["origin"] == "default", "chosen on the form, not from a config file"
    assert session.setup()["folder"]["dryRun"] is True, "still a preview until that is switched off"


def test_log_drops_old_lines_with_stable_indexes(monkeypatch):
    monkeypatch.setattr(web, "LOG_LINES", 3)
    log = web.Log()
    log.feed(b"".join(b"%d\n" % i for i in range(5)))
    assert log.lines == ["2", "3", "4"] and log.dropped == 2
    assert log.since(0)["from"] == 2 and log.since(4)["lines"] == ["4"]


def test_a_new_run_sends_its_log_from_the_start(tmp_path):
    session = web.Session(Settings(), {}, tmp_path, None)
    session.phase, session.run_number = "running", 2
    session.log.feed(b"a\nb\n")
    assert session.snapshot(log_from=5, log_run=1)["log"]["lines"] == ["a", "b"], "lines of run 1 counted"
    assert session.snapshot(log_from=1, log_run=2)["log"]["lines"] == ["b"]


def test_progress_folds_the_events():
    p = web.Progress()
    for ev in [{"event": "run_start", "dry_run": False},
               {"event": "plan", "stages": [{"stage": "images", "total": 3, "done": False},
                                            {"stage": "video", "total": 1, "done": True}]},
               {"event": "stage_start", "stage": "images", "total": 3},
               {"event": "file", "stage": "images", "status": "OK", "src": "/a.jpg", "dest": "a.jxl", "before": 100,
                "after": 60},
               {"event": "done", "stage": "images", "src": "/a.jpg", "outcome": "converted"},
               {"event": "done", "stage": "images", "src": "/b.jpg", "outcome": "skipped", "reason": None},
               {"event": "message", "level": "error", "tag": "ERROR", "text": "boom"},
               {"event": "message", "level": "info", "tag": "CACHE", "text": "ignored"},
               {"event": "job_start", "stage": "video", "idx": 1, "total": 1, "src": "/v.mp4", "encoder": "gpu"},
               {"event": "encode", "stage": "video", "idx": 1, "percent": 40.0, "speed": "3x", "eta": 12}]:
        p.apply(ev)
    snap = p.snapshot()
    images, video = snap["stages"]
    assert images == {**images, "total": 3, "done": 2, "state": "running", "before": 100, "after": 60,
                      "outcomes": {"converted": 1, "skipped": 1}}
    assert video["state"] == "earlier"
    assert snap["messages"] == [{"level": "error", "tag": "ERROR", "text": "boom", "stage": "images"}]
    assert snap["encodes"] == [{"idx": 1, "total": 1, "src": "/v.mp4", "encoder": "gpu", "percent": 40.0,
                                "speed": "3x", "eta": 12}]
    p.finish_job("/v.mp4")
    assert p.snapshot()["encodes"] == []


def test_progress_keeps_the_newest_files_of_each_stage(monkeypatch):
    """A long run: each stage sends only its newest files, and says how
    many there were; the totals are the results file's."""
    monkeypatch.setattr(web, "RECENT_FILES", 3)
    p = web.Progress()
    p.apply({"event": "stage_start", "stage": "images", "total": 10})
    p.apply({"event": "file", "stage": "images", "status": "ERROR", "src": "/bad.png"})
    for i in range(9):
        p.apply({"event": "file", "stage": "images", "status": "OK", "src": f"/{i}.jpg", "dest": f"{i}.jxl",
                 "before": 10, "after": 4})
    p.apply({"event": "stage_start", "stage": "gifs", "total": 1})
    p.apply({"event": "file", "stage": "gifs", "status": "OK", "src": "/a.gif", "before": 5, "after": 5})
    images, gifs = p.snapshot()["stages"]
    assert [f["src"] for f in images["files"]] == ["/6.jpg", "/7.jpg", "/8.jpg"] and images["listed"] == 10
    assert [f["src"] for f in gifs["files"]] == ["/a.gif"] and gifs["listed"] == 1
    assert p.snapshot()["totals"] == {"converted": 0, "failed": 0, "before": 95, "after": 41}
    json.dumps(p.snapshot())


# --- closing the tab, the background, the notification ----------------------------------
_FAKE_RUN = """
import json, signal, sys, time
from pathlib import Path
events, release = sys.argv[1], Path(sys.argv[2])
def emit(**ev):
    with open(events, "a") as fh:
        fh.write(json.dumps({"v": 1, "ts": "", **ev}) + "\\n")
def stop(*args):
    print("stopped, state saved", flush=True)
    emit(event="run_end", code=130)
    sys.exit(130)
signal.signal(signal.SIGINT, stop)
emit(event="run_start", command="run", source="/x", target=None, dry_run=False, pid=0)
emit(event="plan", stages=[{"stage": "images", "total": 1, "done": False}])
emit(event="stage_start", stage="images", total=1)
print("working", flush=True)
while not release.exists():
    time.sleep(0.05)
emit(event="file", stage="images", status="OK", src="/x/a.jpg", dest="a.jxl", before=3000, after=1000)
emit(event="done", stage="images", src="/x/a.jpg", outcome="converted")
emit(event="stage_end", stage="images", code=0, counters={})
emit(event="run_end", code=0)
"""


class _FakeServer:
    def __init__(self):
        self.gone = False
        self.last_contact = None

    def page_gone(self):
        return self.gone


@pytest.fixture
def fake_run(tmp_path):
    """A session whose run is a stand-in: it waits until released, or ends
    with 130 on Ctrl+C."""
    script = tmp_path / "fake_run.py"
    script.write_text(_FAKE_RUN)
    release = tmp_path / "release"
    session = web.Session(Settings(), {}, tmp_path, None)
    session.command = lambda mode, dry_run, events_file: [sys.executable, str(script), str(events_file), str(release)]
    session.start({})
    end = time.monotonic() + 10
    while not any("working" in line for line in session.log.lines) and time.monotonic() < end:
        time.sleep(0.05)
    session.release = lambda: release.touch()
    yield session
    release.touch()
    if session.child is not None and session.child.poll() is None:
        session.child.kill()


def test_closing_the_tab_stops_the_run(fake_run):
    server = _FakeServer()
    watch = web.Watch(server, fake_run, "http://127.0.0.1:1/?token=x", notify=lambda *a, **k: pytest.fail("notified"))
    assert not watch.tick(), "the page is there"
    server.gone = True
    assert not watch.tick() and fake_run.stopping, "Ctrl+C for the run"
    _wait_finished(fake_run, 10)
    assert fake_run.code == 130 and any("state saved" in line for line in fake_run.log.lines)
    assert watch.tick(), "then the program ends"


def test_a_background_run_outlives_the_tab(fake_run, monkeypatch):
    sent = []
    server = _FakeServer()
    watch = web.Watch(server, fake_run, "http://127.0.0.1:1/?token=x", notify=lambda *a, **k: sent.append((a, k)))
    fake_run.keep_in_background()
    server.gone = True
    assert not watch.tick() and not fake_run.stopping
    fake_run.release()
    _wait_finished(fake_run, 10)
    assert fake_run.code == 0
    assert not watch.tick(), "waits for the page to be opened again"
    assert len(sent) == 1
    (title, body), options = sent[0]
    assert title == "media-optimizer finished" and body == "1 file converted, 2.0 KB saved."
    assert options["action"] == "Open" and callable(options["on_action"])
    assert not watch.tick() and len(sent) == 1, "once"
    monkeypatch.setattr(web, "BACKGROUND_WAIT", 0.0)
    assert watch.tick(), "not opened again in time: the program ends with the run's code"


def test_a_page_opened_after_the_background_run_keeps_it_up(fake_run):
    server = _FakeServer()
    watch = web.Watch(server, fake_run, "http://127.0.0.1:1/?token=x", notify=lambda *a, **k: None)
    fake_run.keep_in_background()
    server.gone = True
    fake_run.release()
    _wait_finished(fake_run, 10)
    assert not watch.tick()
    server.gone, server.last_contact = False, time.monotonic() + 1      # the notification's "Open"
    assert not watch.tick() and not fake_run.background, "back to the usual rules"
    server.gone = True
    assert watch.tick()


def test_a_second_background_run_gets_its_notification(tmp_path):
    """The time the first background run ended was kept: the second one
    ended without a notification and the program ended at once."""
    from types import SimpleNamespace

    sent = []
    server = _FakeServer()
    session = SimpleNamespace(phase="running", background=True, stopping=False, code=None,
                              summary=lambda: "done.")
    watch = web.Watch(server, session, "http://127.0.0.1:1/?token=x", notify=lambda *a, **k: sent.append(a))
    server.gone = True
    session.phase, session.code = "finished", 0
    assert not watch.tick() and len(sent) == 1
    time.sleep(0.01)
    server.gone, server.last_contact = False, time.monotonic()      # opened again
    assert not watch.tick() and not session.background
    session.phase, session.background, session.code = "running", True, None      # another folder
    server.gone = True
    assert not watch.tick()
    time.sleep(0.01)
    session.phase, session.code = "finished", 0
    assert not watch.tick() and not watch.tick(), "waits for the page again"
    assert len(sent) == 2 and session.background


def test_background_only_while_running(tmp_path):
    with pytest.raises(web.ApiError):
        web.Session(Settings(), {}, tmp_path, None).keep_in_background()


def test_notification_with_a_button(stub, tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    path = stub("notify-send", f'printf "%s\\n" "$@" > "{calls}"\necho open\n')
    monkeypatch.setenv("PATH", path)
    from media_optimizer import notify
    clicked = threading.Event()
    assert notify.notify("Done", "3 files", icon="/i.svg", action="Open", on_action=clicked.set)
    assert clicked.wait(5)
    assert calls.read_text().split("\n")[:5] == ["--app-name=media-optimizer", "--icon=/i.svg",
                                                 "--action=open=Open", "Done", "3 files"]


def test_notification_falls_back_to_gdbus_and_never_raises(tmp_path, monkeypatch):
    from media_optimizer import notify
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    (bindir / "gdbus").write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{calls}"\n')
    (bindir / "gdbus").chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert notify.notify("Done", "3 files", action="Open", on_action=lambda: None)
    args = calls.read_text().split("\n")
    assert "org.freedesktop.Notifications.Notify" in args and "Done" in args and "3 files" in args
    assert "[]" in args, "no button without notify-send"
    (bindir / "gdbus").write_text("#!/bin/bash\nexit 1\n")
    assert notify.notify("Done", "x") is False
    (bindir / "gdbus").write_text("#!/nonexistent/interpreter\n")
    assert notify.notify("Done", "x") is False, "a program that cannot even start"
    (bindir / "gdbus").unlink()
    assert notify.notify("Done", "x") is False, "nothing to notify with: nothing happens"


# --- files earlier runs handled ----------------------------------------------------------------
def _cache_entry(path, cache_file, keys, optional=(), settings=None, outcome="larger"):
    """An entry as a stage writes it, for *settings* (default: the defaults)."""
    from media_optimizer import cache
    stage = cache.StageCache(cache_file, enabled=True, dry_run=False,
                             fingerprint=cache.fingerprint(settings or Settings(), keys, optional))
    stage.add(path, outcome)


def test_the_folder_tree_counts_what_the_caches_skip(folder):
    from media_optimizer.stages import images, video
    d = folder("c1", "photo.jpg", "other.png", "a/l.mp4=long.mp4", "a/b/x.jpg=photo.jpg")
    _cache_entry(d / "photo.jpg", d / ".img_cache.txt", images.ImageStage.cache_settings)
    _cache_entry(d / "a/l.mp4", d / ".video_conversion_cache.txt", video.CACHE_SETTINGS,
                 video.CACHE_SETTINGS_OPTIONAL, outcome="codec")
    with open(d / ".img_cache.txt", "a") as fh:      # stale: a load that may write would compact it
        fh.write(f"1\t1\tabc\tlarger\t{d / 'gone.png'}\n")
    before = {p: p.read_bytes() for p in d.glob(".*_cache*.txt")}
    session = _session(d)
    info = session.folder_info()
    assert info["cached"] == {"root": [1, 0], "tree": {"a": [0, 1]}}
    assert {p: p.read_bytes() for p in d.glob(".*_cache*.txt")} == before   # only read
    # Another target format: the image's cached result no longer holds, the video's does.
    assert session.cached_for({"img_target": "webp"}) == {"root": [0, 0], "tree": {"a": [0, 1]}}
    assert session.cached_for({"img_target": 7}) == info["cached"]          # invalid: the setting stays
    os.utime(d / "photo.jpg", ns=(1, 1))                                     # changed since: handled again
    assert _session(d).folder_info()["cached"] == {"root": [0, 0], "tree": {"a": [0, 1]}}


def test_a_video_the_video_stage_skipped_is_marked(folder, run_cli):
    """End to end: what a real run put into its cache is what the page marks."""
    d = folder("c2", "a/l.mp4=long.mp4")
    out = run_cli("video", d, "--no-delete")                 # below min_size_mb: cached as "small"
    assert out.rc == 0, out.text
    assert "\tsmall\t" in (d / ".video_conversion_cache.txt").read_text()
    session = _session(d)
    assert session.folder_info()["cached"] == {"root": [0, 0], "tree": {"a": [0, 1]}}
    assert session.cached_for({"min_size_mb": 0.0})["tree"] == {}           # would be converted now


def test_the_page_asks_for_the_cached_counts(server):
    status, _, raw = _http(server, "POST", "/api/cached", {"settings": {"img_target": "webp"}})
    assert status == 200 and json.loads(raw) == {"root": [0, 0], "tree": {}}
    status, _, _ = _http(server, "POST", "/api/cached", {"settings": {}}, headers={"Origin": "http://evil"})
    assert status == 403


# --- a preview's files -----------------------------------------------------------------
def test_a_preview_lists_every_file_it_would_convert(folder, tmp_path):
    d = folder("pv", "a.jpg=photo.jpg", "sub/b.jpg=photo.jpg", "sub/deep/c.jpg=photo.jpg")
    session = _session(d)
    session.start({"settings": {}, "dry_run": True})
    _wait_finished(session)
    assert session.planned("images") == {"files": ["a.jpg", "sub/b.jpg", "sub/deep/c.jpg"]}
    assert session.planned("gifs") == {"files": []} and session.planned("nonsense") == {"files": []}
    assert "planned" not in json.dumps(session.snapshot()), "not in what the page polls"


def test_the_preview_list_holds_more_than_a_stage_card_shows():
    progress = web.Progress()
    for i in range(web.RECENT_FILES * 3):
        progress.apply({"event": "done", "stage": "images", "src": f"/s/{i}.png", "outcome": "dry"})
    progress.apply({"event": "done", "stage": "images", "src": "/s/skipped.png", "outcome": "skipped"})
    assert len(progress.planned["images"]) == web.RECENT_FILES * 3


def test_a_real_run_lists_nothing_as_planned(folder):
    d = folder("pr", "a.jpg=photo.jpg")
    session = _session(d)
    session.start({"settings": {"delete_original": False}, "dry_run": False})
    _wait_finished(session)
    assert session.planned("images") == {"files": []}


def test_the_preview_list_needs_the_page(server):
    assert _http(server, "GET", "/api/planned?stage=images")[0] == 200
    assert _http(server, "GET", "/api/planned?stage=images", cookie=False)[0] == 403


# --- the defaults ----------------------------------------------------------------------
def _config(tmp_path, data=None) -> tuple[Path, "web.Session"]:
    path = tmp_path / "config.json"
    if data is not None:
        path.write_text(json.dumps(data))
    session = web.Session(Settings(), {}, tmp_path, None, config=path if data is not None else None)
    return path, session


def test_saved_defaults_keep_the_file_and_only_what_differs(tmp_path):
    path, session = _config(tmp_path, {"_about": "mine", "_png_mode": "why", "png_mode": "lossy", "mystery": 1,
                                       "cpu_crf": 21})
    done = session.save_defaults({"gif_target": "avif", "png_mode": "lossless", "cpu_crf": 21, "gpu_qp": 26})
    data = json.loads(path.read_text())
    assert data["gif_target"] == "avif" and sorted(done["changed"]) == ["gif_target", "png_mode"]
    assert "png_mode" not in data, "back at its default"
    assert data["_about"] == "mine" and data["mystery"] == 1 and data["cpu_crf"] == 21 and "gpu_qp" not in data
    assert json.loads(path.with_name("config.json.bak").read_text())["png_mode"] == "lossy"
    assert session.settings.gif_target == "avif" and session.origins["gif_target"] == "config"


def test_saved_defaults_reach_the_next_start(tmp_path, monkeypatch):
    from media_optimizer.config import load_settings
    path, session = _config(tmp_path, {})
    session.save_defaults({"gif_target": "avif", "visual_psnr_min": 20.0})
    s, _, _ = load_settings(path)
    assert s.gif_target == "avif" and s.visual_psnr_min == 20


def test_a_first_config_file_is_created_where_it_is_read(tmp_path, monkeypatch):
    from media_optimizer.config import config_file, load_settings
    monkeypatch.delenv("MO_CONFIG", raising=False)
    _, session = _config(tmp_path)
    path = config_file()
    assert not path.exists()
    assert session.save_defaults({"gif_target": "avif"})["path"] == str(path.resolve())
    assert load_settings()[0].gif_target == "avif"
    assert not path.with_name("config.json.bak").exists()


def test_a_symlinked_config_stays_a_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "mo.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / "config.json"
    link.symlink_to(real)
    web.Session(Settings(), {}, tmp_path, None, config=link).save_defaults({"gif_target": "avif"})
    assert link.is_symlink() and json.loads(real.read_text()) == {"gif_target": "avif"}


def test_the_defaults_are_the_files_not_this_starts(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_GIF_TARGET", "avif")
    path, session = _config(tmp_path, {"png_mode": "lossy"})
    session.settings.gif_target = "avif"
    session.origins.update(gif_target="env")
    data = session.defaults()
    fields = {f["name"]: f for g in data["schema"]["groups"] for f in g["fields"]}
    assert fields["gif_target"]["value"] == "webp" and fields["png_mode"]["origin"] == "config"
    assert data["overridden"] == ["gif_target"] and data["path"] == str(path)
    session.save_defaults({"gif_method": 4})
    assert "gif_target" not in json.loads(path.read_text()), "what the environment says is not saved"


def test_what_this_start_overrides_stays_on_the_form(tmp_path):
    path, session = _config(tmp_path, {})
    session.settings.cpu_crf = 19
    session.origins["cpu_crf"] = "cli"
    session.save_defaults({"cpu_crf": 23})
    assert json.loads(path.read_text())["cpu_crf"] == 23
    assert session.settings.cpu_crf == 19 and session.origins["cpu_crf"] == "cli"


def test_a_bad_value_or_a_broken_file_saves_nothing(tmp_path):
    path, session = _config(tmp_path, {"png_mode": "lossy"})
    with pytest.raises(web.ApiError) as bad:
        session.save_defaults({"gpu_qp": 999, "gif_target": "avif"})
    assert bad.value.status == 400 and "gpu_qp" in bad.value.extra["fields"]
    assert json.loads(path.read_text()) == {"png_mode": "lossy"}
    path.write_text("{ not json")
    with pytest.raises(web.ApiError):
        session.save_defaults({"gif_target": "avif"})
    assert path.read_text() == "{ not json"


def test_the_folder_choice_is_not_saved(tmp_path):
    path, session = _config(tmp_path, {})
    session.save_defaults({"exclude_folders": ["x"], "subfolders": "exclude"})
    assert json.loads(path.read_text()) == {}


def test_defaults_are_refused_during_a_run_and_need_the_page(server, tmp_path):
    server.session.phase = "running"
    origin = {"Origin": f"http://127.0.0.1:{server.port}", "Content-Type": "application/json"}
    assert _http(server, "POST", "/api/defaults", {"settings": {"gif_target": "avif"}}, origin)[0] == 409
    server.session.phase = "setup"
    evil = {"Origin": "http://evil.example"}
    assert _http(server, "POST", "/api/defaults", {"settings": {}}, evil)[0] == 403
    assert _http(server, "POST", "/api/defaults", {"settings": {}}, cookie=False)[0] == 403
    assert _http(server, "GET", "/api/defaults")[0] == 200
