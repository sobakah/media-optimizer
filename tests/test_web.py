"""The browser page's server: the security rules copied from cdimport and
lyrembed, the options keepbest's hand-off relies on, and the address line
it reads. No browser here; the page itself is clicked through outside the
suite."""

from __future__ import annotations

import http.client
import json
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
