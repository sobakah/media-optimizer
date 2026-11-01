"""The full run: stages, failures, interruption, resume and restart."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from PIL import Image

from conftest import _env, leftovers, running, snapshot, wait_running

FAST_VIDEO = {"MO_MIN_SIZE_MB": "0", "MO_ENCODER_MODE": "cpu", "MO_ENABLE_PROBE": "false",
              "MO_CPU_PRESET": "ultrafast"}


def _state(tmp_path):
    """The one saved resume state (one file per source folder), or None."""
    paths = sorted((tmp_path / "state").glob("state*.json"))
    assert len(paths) <= 1, paths
    return json.loads(paths[0].read_text()) if paths else None


def _interrupted(tmp_path, source, output, **extra):
    """A stopped run as an older version saved it (state.json)."""
    state = {"status": "INTERRUPTED", "source": str(source), "output": str(output), "settings": {},
             "done": {"images": False, "gifs": True, "clips": True, "video": True}, **extra}
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))


def test_failure_in_one_stage_does_not_stop_the_next(folder, tmp_path, run_cli):
    d = folder("o1", "photo.jpg", "garbage.png", "anim.gif")
    o = tmp_path / "o1out"
    out = run_cli("-y", d, o)
    assert out.rc == 3, out
    assert "WITH FAILURES" in out and (o / "anim.webp").exists() and (o / "photo.jxl").exists()
    assert _state(tmp_path) is None, "state removed after a finished run"


def test_dry_run_writes_nothing(folder, tmp_path, run_cli):
    d = folder("o2", "photo.jpg", "anim.gif", "l.mp4=long.mp4")
    before = snapshot(d)
    out = run_cli("-y", "-n", d, tmp_path / "o2out", env=FAST_VIDEO)
    assert out.rc == 0, out
    assert snapshot(d) == before
    assert not (tmp_path / "o2out").exists()
    assert _state(tmp_path) is None


def test_avif_runs_before_video(folder, tmp_path, run_cli):
    d = folder("o3", "c.mp4=clip.mp4")
    o = tmp_path / "o3out"
    out = run_cli("-y", "--clips", d, o, env=FAST_VIDEO)
    assert (o / "c.avif").exists() and not (o / "c.mp4").exists(), out
    assert "[3/4]" not in out and "[1/2] Starting clip stage: short silent videos to AVIF" in out


def test_preflight_in_place_and_target(folder, tmp_path, run_cli):
    d = folder("o4", "fake.jpg", "isgif.png")
    out = run_cli("-y", "--preflight", d, tmp_path / "o4out")
    assert "is actually png (source left unchanged)" in out, out
    assert (d / "fake.jpg").exists()
    d = folder("o4b", "fake.jpg", "isgif.png")
    out = run_cli("-y", "--preflight", "--no-delete", d)
    assert (d / "fake.png").exists() and (d / "isgif.gif").exists(), out


def test_collision_warning(folder, tmp_path, run_cli):
    d = folder("o5", "foo.jpg=photo.jpg", "foo.png=other.png")
    out = run_cli("-y", d, tmp_path / "o5out")
    assert "same output name" in out and f"{d / 'foo.jxl'}  (foo.jpg, foo.png)" in out, out


def test_collision_warning_follows_the_output_names(folder, tmp_path, run_cli):
    """foo.jpg -> foo.jxl and foo.gif -> foo.webp do not clash; foo.mp4 and
    foo.mov both become foo.mp4."""
    d = folder("o5b", "foo.jpg=photo.jpg", "foo.gif=anim.gif", "v.mp4=clip.mp4", "v.mov=clip.mp4")
    out = run_cli("-y", "-n", d, tmp_path / "o5bout")
    listed = [line.strip() for line in out.text.splitlines() if line.startswith(f"            {d}")]
    assert listed == [f"{d / 'v.mp4'}  (v.mov, v.mp4)"], out


def test_gif_named_png_is_converted_in_the_same_run(folder, run_cli):
    """In place the image stage renames isgif.png to isgif.gif, but the GIFs
    were counted before, so the GIF stage was left out."""
    d = folder("o5c", "isgif.png")
    out = run_cli("-y", "--no-delete", d, env={"MO_DISCARD_IF_LARGER": "false"})
    assert out.rc == 0 and "Starting GIF conversion (1 files)" in out, out
    assert (d / "isgif.webp").exists()


def test_missing_tool(folder, tmp_path, run_cli, vanishing_path):
    d = folder("o6", "photo.jpg")
    path = vanishing_path("gif2webp")
    os.remove(os.path.join(path, "gif2webp"))
    out = run_cli("-y", d, tmp_path / "o6out", path=path)
    assert out.rc == 1 and "gif2webp" in out, out


def test_in_place_with_yes_keeps_settings(folder, run_cli):
    d = folder("o7", "photo.jpg")
    out = run_cli("-y", d)
    assert "IN-PLACE MODE" in out, out


# --- interruption and resume --------------------------------------------------------------
def _pngs(d, n):
    for i in range(n):
        Image.effect_mandelbrot((1000, 800), (-2 + i * 0.01, -1.2, 1, 1.2), 100).convert("RGB") \
            .save(d / f"p{i}.png")


@pytest.mark.slow
def test_sigterm_saves_state_and_resume_finishes(folder, tmp_path, start_cli, run_cli):
    d = folder("o8")
    _pngs(d, 30)
    o = tmp_path / "o8out"
    env = {"MO_JXL_EFFORT": "9", "MO_PNG_MODE": "lossless", "MO_MAX_WORKERS": "2"}
    p = start_cli("-y", d, o, "--no-verify-visual", env=env, out=tmp_path / "orch.out")
    assert wait_running("cjxl", str(d))
    time.sleep(1)
    t0 = time.monotonic()
    p.terminate()
    rc = p.wait(timeout=30)
    took = time.monotonic() - t0
    assert rc == 130 and took < 5, (rc, took, (tmp_path / "orch.out").read_text())
    time.sleep(0.3)
    assert not running("cjxl", str(d))
    state = _state(tmp_path)
    assert state and state["status"] == "INTERRUPTED"
    assert state["settings"]["verify_visual"] is False, "every setting is saved, not a selection"
    assert (o / ".img_stats.json").exists()
    assert not leftovers(o)

    # Resume without the flags: the saved settings apply.
    out = run_cli("-y")
    assert out.rc == 0, out
    assert "Settings loaded, resuming" in out
    assert len(list(o.glob("*.jxl"))) == 30
    assert _state(tmp_path) is None


def test_resume_uses_saved_settings(folder, tmp_path, run_cli, monkeypatch):
    d = folder("o9", "photo.jpg")
    o = tmp_path / "o9out"
    state = {"status": "INTERRUPTED", "source": str(d), "output": str(o),
             "settings": {"img_target": "webp", "delete_original": False, "rename_inplace": False},
             "done": {"images": False, "gifs": True, "clips": True, "video": True}}
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))
    out = run_cli("-y")
    assert out.rc == 0 and (o / "photo.webp").exists(), out


def test_dry_run_keeps_the_saved_state(folder, tmp_path, run_cli):
    """A preview resumed the stopped run and then deleted its state."""
    a = folder("o11", "photo.jpg")
    _interrupted(tmp_path, a, tmp_path / "o11out")
    out = run_cli("-y", "-n")
    assert out.rc == 0 and "resuming" in out, out
    assert not (tmp_path / "o11out").exists()
    assert _state(tmp_path)["source"] == str(a)


def test_other_source_does_not_resume_a_stopped_run(folder, tmp_path, run_cli):
    """With -y (or without a terminal) a run for B used to resume A instead,
    with A's settings, and B was never processed."""
    a = folder("o12a", "photo.jpg")
    b = folder("o12b", "photo.jpg")
    _interrupted(tmp_path, a, tmp_path / "o12a-out")
    out = run_cli("-y", b, tmp_path / "o12b-out")
    assert out.rc == 0 and "INCOMPLETE PREVIOUS RUN" not in out, out
    assert (tmp_path / "o12b-out" / "photo.jxl").exists() and not (tmp_path / "o12a-out").exists()
    assert _state(tmp_path)["source"] == str(a), "A's state is left for later"
    out = run_cli("-y")
    assert "resuming" in out and (tmp_path / "o12a-out" / "photo.jxl").exists(), out
    assert _state(tmp_path) is None


def test_same_source_given_resumes(folder, tmp_path, run_cli):
    a = folder("o13", "photo.jpg")
    _interrupted(tmp_path, a, tmp_path / "o13out", settings={"img_target": "webp"})
    out = run_cli("-y", a, tmp_path / "elsewhere")
    assert "resuming" in out and (tmp_path / "o13out" / "photo.webp").exists(), out
    assert not (tmp_path / "elsewhere").exists()


def test_run_still_going_on_the_folder_is_not_taken_over(folder, tmp_path, run_cli):
    a = folder("o14", "photo.jpg")
    # This test process stands in for the other run: it is alive.
    _interrupted(tmp_path, a, tmp_path / "o14out", status="RUNNING", pid=os.getpid())
    out = run_cli("-y", a, tmp_path / "o14out")
    assert out.rc == 1 and "Another run is working on" in out, out
    out = run_cli("-y")
    assert "INCOMPLETE PREVIOUS RUN" not in out, "not offered for resuming either"
    out = run_cli("-y", "--reset", a, tmp_path / "o14out")
    assert out.rc == 0 and (tmp_path / "o14out" / "photo.jxl").exists(), out


def test_reset_discards_state(folder, tmp_path, run_cli):
    d = folder("o10", "photo.jpg")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps(
        {"status": "INTERRUPTED", "source": str(tmp_path / "gone"), "output": None, "settings": {},
         "done": {}}))
    out = run_cli("-y", "--reset", d, tmp_path / "o10out")
    assert out.rc == 0 and (tmp_path / "o10out" / "photo.jxl").exists(), out


# --- restart ------------------------------------------------------------------------------
def test_preview_with_y_asks_nothing(folder, tmp_path, run_cli):
    out = run_cli("-y", "-n", folder("rt", "photo.jpg"), tmp_path / "rt-out")
    assert "for real" not in out.text, out


# --- the page is the default -----------------------------------------------------------
def _page_start(argv, env, cwd=None):
    """The address media-optimizer printed, with the process still running."""
    proc = subprocess.Popen([sys.executable, "-m", "media_optimizer", *map(str, argv)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=cwd)
    line = proc.stdout.readline()
    proc.terminate()
    proc.wait(10)
    return proc.returncode, line


@pytest.mark.parametrize("argv", [["--no-browser"], ["run", "--no-browser", "--web"]])
def test_a_full_run_opens_the_page_without_y(folder, argv):
    d = folder("p1", "photo.jpg")
    env = _env({"DISPLAY": ":99"}, None)
    code, line = _page_start([*argv, d], env)
    assert "is running at http://127.0.0.1:" in line
    assert (d / "photo.jpg").exists() and not (d / "photo.jxl").exists()


def test_without_a_display_it_prints_the_address(folder, tmp_path, monkeypatch):
    """Over SSH: no tab to open, webbrowser would start a text browser."""
    from media_optimizer import cli, web

    opened = []
    monkeypatch.setattr(web, "serve", lambda session, open_browser, check_updates: opened.append(open_browser) or 0)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    assert cli.main(["run", str(folder("p2", "photo.jpg"))]) == 0
    assert opened == [False]


def test_a_timer_without_y_stops_instead_of_waiting_for_a_page(folder, run_cli):
    """No display, no terminal: nobody could ever open it. It used to run."""
    d = folder("p3", "photo.jpg")
    proc = subprocess.run([sys.executable, "-m", "media_optimizer", str(d)], stdin=subprocess.DEVNULL, text=True,
                          capture_output=True, env=_env(None, None), timeout=30)
    assert proc.returncode == 1 and "-y runs without it" in proc.stderr
    assert (d / "photo.jpg").exists()


def test_y_runs_without_a_page_and_says_what_happens_in_place(folder, run_cli):
    d = folder("p4", "photo.jpg")
    out = run_cli("run", "-y", d, "--force-delete")
    assert out.rc == 0 and "IN-PLACE MODE" in out and "Move originals to trash  : true" in out
    assert (d / "photo.jxl").exists() and not (d / "photo.jpg").exists()


def test_the_page_does_not_preset_the_home_folder(tmp_path, monkeypatch):
    from media_optimizer import cli, web

    seen = []
    monkeypatch.setattr(web, "serve", lambda session, **kw: seen.append(session.source) or 0)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run"]) == 0
    assert seen == [None]


@pytest.mark.parametrize("flags", [["--web"], ["--no-browser"], ["--return-url", "http://127.0.0.1:9/"]])
def test_the_pages_options_exclude_y(folder, run_cli, flags):
    assert run_cli("run", "-y", folder("p5", "photo.jpg"), *flags).rc == 2


def test_events_need_y(folder):
    d = folder("p6", "photo.jpg")
    proc = subprocess.run([sys.executable, "-m", "media_optimizer", "run", str(d), "--events", str(d / "e")],
                          stdin=subprocess.DEVNULL, capture_output=True, env=_env(None, None), timeout=30)
    assert proc.returncode == 2
