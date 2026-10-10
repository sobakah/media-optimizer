"""The full run: stages, failures, interruption, resume and restart."""

from __future__ import annotations

import json
import os
import time

import pytest
from PIL import Image

from conftest import leftovers, running, snapshot, wait_running

FAST_VIDEO = {"MO_MIN_SIZE_MB": "0", "MO_ENCODER_MODE": "cpu", "MO_ENABLE_PROBE": "false",
              "MO_CPU_PRESET": "ultrafast"}


def _state(tmp_path):
    path = tmp_path / "state" / "state.json"
    return json.loads(path.read_text()) if path.exists() else None


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
    assert "share a base name" in out and str(d / "foo") in out, out


def test_missing_tool(folder, tmp_path, run_cli, vanishing_path):
    d = folder("o6", "photo.jpg")
    path = vanishing_path("gif2webp")
    os.remove(os.path.join(path, "gif2webp"))
    out = run_cli("-y", d, tmp_path / "o6out", path=path)
    assert out.rc == 1 and "gif2webp" in out, out


def test_in_place_with_yes_keeps_settings(folder, run_cli):
    d = folder("o7", "photo.jpg")
    out = run_cli("-y", d)
    assert "IN-PLACE MODE" in out and "Cannot prompt" in out, out


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


def test_reset_discards_state(folder, tmp_path, run_cli):
    d = folder("o10", "photo.jpg")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps(
        {"status": "INTERRUPTED", "source": str(tmp_path / "gone"), "output": None, "settings": {},
         "done": {}}))
    out = run_cli("-y", "--reset", d, tmp_path / "o10out")
    assert out.rc == 0 and (tmp_path / "o10out" / "photo.jxl").exists(), out


# --- restart ------------------------------------------------------------------------------
def test_process_another_directory(folder, tmp_path, pty_run):
    a = folder("ra", "photo.jpg")
    b = folder("rb", "photo.jpg")
    out = pty_run(["y", str(b), str(tmp_path / "rb-out"), "", "n"], a, tmp_path / "ra-out")
    assert "New run" in out, out
    assert (tmp_path / "ra-out" / "photo.jxl").exists() and (tmp_path / "rb-out" / "photo.jxl").exists()


def test_discarding_a_stopped_run(folder, tmp_path, pty_run):
    a = folder("rc", "photo.jpg")
    b = folder("rd", "photo.jpg")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps(
        {"status": "INTERRUPTED", "source": str(a), "output": str(tmp_path / "rc-out"), "settings": {},
         "done": {"images": False, "gifs": True, "clips": True, "video": True}}))
    out = pty_run(["n", str(b), str(tmp_path / "rd-out"), "", "n"])
    assert "Discarding state" in out, out
    assert (tmp_path / "rd-out" / "photo.jxl").exists() and not (tmp_path / "rc-out").exists()


def test_settings_menu(folder, tmp_path, pty_run):
    d = folder("rm", "photo.jpg", "other.png")
    o = tmp_path / "rm-out"
    # source, target, "use defaults?" n, then the menu: preflight, delete, deep, avif, workers,
    # target format webp, quality, effort, png mode, gif method, encoder, threshold, qp, crf,
    # preset, probe, rescan, discard, salvaged, visual, strict, verify-output; another dir: n
    answers = [str(d), str(o), "n", "", "", "", "", "2", "webp", "80", "", "", "", "", "", "", "", "",
               "", "", "", "", "", "", "", "n"]
    out = pty_run(answers, env={"MO_MIN_SIZE_MB": "0"})
    assert (o / "photo.webp").exists() and (o / "other.webp").exists(), out
    assert "Scan all videos again" in out
