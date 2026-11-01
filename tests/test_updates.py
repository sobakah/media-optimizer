"""A newer media-optimizer: the check (git tags with two or three parts, at
every start), the command for each kind of install, the question before a full run
in the terminal, and installing it from the page, which then starts again in
the same process and tab. The releases are a local repository, never GitHub."""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest

from conftest import PYDIR, _env, releases
from media_optimizer import selfupdate, web
from media_optimizer.config import Settings, __version__

MAJOR = int(__version__.split(".")[0])
NEWER = f"{MAJOR + 1}.1"


@pytest.fixture
def newer_release(tmp_path, monkeypatch):
    """GitHub has a release newer than this media-optimizer."""
    url = releases(tmp_path / "releases", "0.8.3", __version__, f"{MAJOR + 1}.0.9", NEWER, "not-a-version")
    monkeypatch.setenv(selfupdate.REPO_ENV, url)
    return url


def write_metadata(prefix, package_or_url, injected=(), venv_args=(), pinned=False):
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "pipx_metadata.json").write_text(json.dumps({
        "main_package": {"package": "media-optimizer", "package_or_url": package_or_url, "pip_args": [],
                         "pinned": pinned},
        "injected_packages": {name: {"package": name, "package_or_url": name} for name in injected},
        "venv_args": list(venv_args)}))
    return prefix


# --- the check ----------------------------------------------------------------------
def test_tags_with_two_or_three_parts_count(newer_release):
    assert selfupdate.latest() == NEWER                     # 2.1 is newer than 2.0.9
    assert selfupdate.newer("1.0.1", "1.0") and selfupdate.newer("1.1", "1.0.9")
    assert not selfupdate.newer("1.0.0", "1.0") and not selfupdate.newer(None)


def test_a_repository_that_cant_be_read_is_an_error(tmp_path):
    with pytest.raises(selfupdate.UpdateError):
        selfupdate.latest(str(tmp_path / "missing"))
    with pytest.raises(selfupdate.UpdateError, match="no releases"):
        selfupdate.latest(releases(tmp_path / "none"))


def test_github_is_asked_at_every_start_and_the_answer_kept(newer_release, monkeypatch):
    """A release made a minute ago shows at the next start; the answer is kept
    for a start that can't wait (known) or can't reach GitHub."""
    asked = []
    real = selfupdate.latest
    monkeypatch.setattr(selfupdate, "latest", lambda: asked.append(1) or real())
    assert selfupdate.known() is None                       # nothing found yet
    assert selfupdate.available() == NEWER
    assert selfupdate.available() == NEWER
    assert len(asked) == 2                                  # asked again, not taken from the last time
    assert selfupdate.known() == NEWER
    monkeypatch.setenv(selfupdate.REPO_ENV, "/nowhere")
    assert selfupdate.available() == NEWER                  # offline: the release found last time


def test_a_failed_check_is_quiet_and_tried_again(tmp_path):
    assert selfupdate.available() is None                   # the tests' repository doesn't exist
    assert not (tmp_path / "state" / selfupdate.STATE_FILE).exists()


# --- how to update ------------------------------------------------------------------
def test_a_pipx_install_from_the_repository_is_upgraded(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate.shutil, "which", lambda name: f"/bin/{name}")
    venv = write_metadata(tmp_path / "venv", "git+https://github.com/sobakah/media-optimizer")
    how = selfupdate.how_installed(NEWER, venv)
    assert how.kind == "pipx" and how.command == "pipx upgrade media-optimizer"
    assert how.commands == [["/bin/pipx", "upgrade", "media-optimizer"]]


def test_a_pinned_install_moves_to_the_new_release_and_injects_again(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate.shutil, "which", lambda name: f"/bin/{name}")
    spec = "git+https://github.com/sobakah/media-optimizer@1.0"
    venv = write_metadata(tmp_path / "venv", spec, injected=["pytest"])
    how = selfupdate.how_installed(NEWER, venv)
    assert how.commands == [["/bin/pipx", "install", "--force", spec.replace("@1.0", f"@{NEWER}")],
                            ["/bin/pipx", "inject", "media-optimizer", "pytest"]]


def test_an_ssh_address_is_not_taken_for_a_pinned_release(tmp_path):
    venv = write_metadata(tmp_path / "venv", "git+ssh://git@github.com/sobakah/media-optimizer.git")
    assert selfupdate.how_installed(NEWER, venv).command == "pipx upgrade media-optimizer"


def test_held_back_and_other_installs_get_the_command_only(tmp_path):
    how = selfupdate.how_installed(NEWER, write_metadata(tmp_path / "venv", "media-optimizer", pinned=True))
    assert how.commands is None and "pipx unpin media-optimizer" in how.command
    how = selfupdate.how_installed(NEWER, tmp_path / "no-venv")
    assert how.kind == "checkout" and how.command == f"git -C {PYDIR} pull" and how.commands is None
    package = tmp_path / "site-packages" / "media_optimizer"
    package.mkdir(parents=True)
    how = selfupdate.how_installed(NEWER, tmp_path / "no-venv", package)
    assert how.kind == "other" and how.commands is None


def test_installing_stops_at_the_first_failure_and_keeps_a_log(tmp_path):
    log = tmp_path / "update.log"
    code, tail = selfupdate.install([["sh", "-c", "echo one"], ["sh", "-c", "echo broke >&2; exit 3"],
                                     ["sh", "-c", f"touch {tmp_path / 'never'}"]], log)
    assert code == 3 and tail.splitlines()[-1] == "broke"
    assert "echo one" in log.read_text() and not (tmp_path / "never").exists()


def test_starting_again_uses_the_same_command(monkeypatch, tmp_path):
    script = tmp_path / "bin" / "media-optimizer"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(script), "run", "pics"])
    assert selfupdate.restart_argv() == [str(script), "run", "pics"]
    monkeypatch.setattr(sys, "argv", [str(PYDIR / "media_optimizer" / "__main__.py"), "pics"])
    assert selfupdate.restart_argv() == [sys.executable, "-m", "media_optimizer", "pics"]


# --- a full run in the terminal -----------------------------------------------------
@pytest.mark.parametrize("args", [["run", "-y", "-n"], ["images", "-n"]])
def test_only_the_page_checks(newer_release, run_cli, tmp_path, args):
    """A script with -y doesn't reach out, and the run the page starts neither."""
    (tmp_path / "src").mkdir()
    out = run_cli(*args, tmp_path / "src")
    assert "[UPDATE]" not in out
    assert not (tmp_path / "state" / selfupdate.STATE_FILE).exists()


@pytest.fixture
def pipx_install(monkeypatch, newer_release):
    """A pipx install with stand-ins for pipx and for starting again."""
    calls = {"installed": [], "restarted": []}
    how = selfupdate.Install("pipx", "pipx upgrade media-optimizer", [["pipx", "upgrade", "media-optimizer"]])
    monkeypatch.setattr(selfupdate, "how_installed", lambda version: how)
    outcome = {"code": 0, "tail": ""}

    def install(commands, log):
        calls["installed"].append(commands)
        return outcome["code"], outcome["tail"]

    class Restarted(Exception):
        pass

    def restart(argv):
        calls["restarted"].append((argv, os.environ.pop(web.RESTART_ENV, None)))
        raise Restarted

    monkeypatch.setattr(selfupdate, "install", install)
    monkeypatch.setattr(selfupdate, "restart", restart)
    calls["outcome"], calls["Restarted"] = outcome, Restarted
    assert selfupdate.available() == NEWER    # an earlier start found it: offered before the run
    return calls


# --- on the page --------------------------------------------------------------------
def _page(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    headers = {"Host": f"127.0.0.1:{srv.port}", "Cookie": f"{web.cookie_name(srv.port)}={srv.token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{srv.port}"})
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    status, raw = resp.status, resp.read()
    conn.close()
    return status, json.loads(raw or b"{}")


def offered(get_state, state="available", ready=True, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        update = get_state()["update"]
        if update and update["state"] == state and update["ready"] == ready:
            return update
        time.sleep(0.05)
    raise AssertionError(f"no update in state {state}: {get_state()['update']}")


@pytest.fixture
def served(tmp_path, pipx_install, monkeypatch):
    """serve() in a thread with the update check on: -> (server, result)."""
    servers, result = [], {}
    real = web._Server

    def remember(*args, **kw):
        srv = real(*args, **kw)
        servers.append(srv)
        return srv
    monkeypatch.setattr(web, "_Server", remember)
    monkeypatch.setattr(web, "_say", lambda message: result.setdefault("said", []).append(message))
    session = web.Session(Settings(), {}, tmp_path, None)

    def run():
        try:
            result["code"] = web.serve(session, open_browser=False, check_updates=True)
        except pipx_install["Restarted"]:
            result["restarted"] = True
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while not servers:
        time.sleep(0.01)
    result["thread"], result["session"] = thread, session
    yield servers[0], result
    servers[0].stop.set()
    thread.join(timeout=10)


def test_the_page_offers_the_update_and_starts_again(served, pipx_install):
    srv, result = served
    update = offered(lambda: _page(srv, "GET", "/api/state")[1])
    assert update["version"] == NEWER and update["current"] == __version__ and update["installable"]
    assert update["notes"].endswith(f"/releases/tag/{NEWER}")
    assert _page(srv, "POST", "/api/update", {})[0] == 200
    result["thread"].join(timeout=20)
    assert result.get("restarted") and pipx_install["installed"] == [[["pipx", "upgrade", "media-optimizer"]]]
    [(argv, address)] = pipx_install["restarted"]
    assert argv == selfupdate.restart_argv() and address == f"{srv.port} {srv.token}"
    assert f"Starting media-optimizer {NEWER}." in result["said"]


def test_no_update_during_a_run_and_no_run_during_the_update(served, pipx_install):
    srv, result = served
    session = result["session"]
    offered(lambda: _page(srv, "GET", "/api/state")[1])
    session.phase = "running"
    status, data = _page(srv, "POST", "/api/update", {})
    assert status == 409 and "run is going on" in data["error"] and pipx_install["installed"] == []
    assert _page(srv, "GET", "/api/state")[1]["update"]["ready"] is False
    session.phase = "setup"
    release = threading.Event()
    pipx_install["outcome"]["code"] = 1
    real_install = selfupdate.install
    selfupdate.install = lambda commands, log: (release.wait(10), real_install(commands, log))[1]
    try:
        assert _page(srv, "POST", "/api/update", {})[0] == 200
        status, data = _page(srv, "POST", "/api/start", {"mode": "new"})
        assert status == 409 and "being updated" in data["error"]
        release.set()
        update = offered(lambda: _page(srv, "GET", "/api/state")[1], state="failed")
        assert update["code"] == 1
    finally:
        selfupdate.install = real_install
    assert pipx_install["restarted"] == []


DRIVER = """
import sys
sys.path.insert(0, {repo!r})
from media_optimizer import cli, selfupdate
# a pipx install whose update is a stand-in: the program really starts again (exec)
selfupdate.how_installed = lambda version: selfupdate.Install(
    "pipx", "pipx upgrade media-optimizer", [["sh", "-c", "echo installed >> {marker}"]])
sys.exit(cli.main())
"""


def test_after_the_update_the_same_process_and_tab_go_on(newer_release, tmp_path):
    marker = tmp_path / "installed"
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER.format(repo=str(PYDIR), marker=marker))
    proc = subprocess.Popen([sys.executable, str(driver), "run", str(tmp_path), "--web", "--no-browser"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=_env(None, None))
    out = []
    reader = threading.Thread(target=lambda: out.extend(iter(proc.stdout.readline, "")), daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 30
        while not any("is running at" in line for line in out) and time.monotonic() < deadline:
            time.sleep(0.02)
        url = next(line for line in out if "is running at" in line)
        port, token = re.search(r":(\d+)/\?token=([\w-]+)", url).groups()

        class Srv:
            pass
        srv = Srv()
        srv.port, srv.token = int(port), token

        def state():
            return _page(srv, "GET", "/api/state")[1]
        boot = state()["boot"]
        offered(state)
        assert _page(srv, "POST", "/api/update", {})[0] == 200
        now = None
        while time.monotonic() < deadline:     # the same address answers again, from the new program
            try:
                now = state()
            except (ConnectionError, OSError):
                time.sleep(0.1)
                continue
            if now["boot"] != boot:
                break
            time.sleep(0.1)
        assert now["boot"] != boot and now["phase"] == "setup"
        assert proc.poll() is None and marker.read_text() == "installed\n"
        assert _page(srv, "POST", "/api/quit", {})[0] == 200
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        reader.join(timeout=5)
    said = "".join(out)
    assert said.count(url.strip()) == 2 and f"Starting media-optimizer {NEWER}." in said
