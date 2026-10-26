"""The full run: stages, failures, interruption, resume and restart."""

from __future__ import annotations

import json
import os
import time

import pytest
from PIL import Image

from conftest import leftovers, running, snapshot, wait_running
from media_optimizer import orchestrator

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
def test_process_another_directory(folder, tmp_path, pty_run):
    a = folder("ra", "photo.jpg")
    b = folder("rb", "photo.jpg")
    out = pty_run(["y", str(b), str(tmp_path / "rb-out"), "", "n"], a, tmp_path / "ra-out")
    assert "New run" in out, out
    assert (tmp_path / "ra-out" / "photo.jxl").exists() and (tmp_path / "rb-out" / "photo.jxl").exists()


def test_another_directory_does_not_inherit_the_folder_choice(folder, tmp_path, pty_run):
    """keepbest passes its folder choice in the environment; the second
    directory silently got the first one's."""
    a = folder("rsa", "photo.jpg", "sub/photo.jpg=photo.jpg")
    b = folder("rsb", "photo.jpg", "sub/photo.jpg=photo.jpg")
    # another? y, source, target, defaults, the subfolder question (Enter: include), another? n
    out = pty_run(["y", str(b), str(tmp_path / "rsb-out"), "", "", "n"], a, tmp_path / "rsa-out",
                  env={"MO_SUBFOLDERS": "exclude"})
    assert not (tmp_path / "rsa-out" / "sub").exists(), out
    assert (tmp_path / "rsb-out" / "sub" / "photo.jxl").exists(), out


def test_called_from_keepbest(folder, tmp_path, pty_run):
    # keepbest passes the folder on, but the run is interactive: the defaults
    # question is asked, and the closing question says where No leads.
    d = folder("rk", "photo.jpg")
    o = tmp_path / "rk-out"
    out = pty_run([str(o), "", ""], d, env={"MO_CALLER": "keepbest", "MO_HOLD": "0"})
    assert "Use the default settings for all media types?" in out, out
    assert "Process another directory? (No: back to keepbest) [y/N]" in out, out
    assert (o / "photo.jxl").exists(), out


def test_started_in_a_folder_offers_it_as_the_source(folder, tmp_path, pty_run):
    d = folder("rw", "photo.jpg")
    o = tmp_path / "rw-out"
    # source: Enter takes the working directory; target, defaults, another? n
    out = pty_run(["", str(o), "", "n"], cwd=d)
    assert f"Source directory [{d}]:" in out, out
    assert (o / "photo.jxl").exists(), out


def test_home_folder_is_not_offered_as_the_source(folder, tmp_path, pty_run):
    """Started from a launcher, the working directory is the home folder."""
    d = folder("rh", "photo.jpg")
    out = pty_run([""], cwd=d, env={"HOME": d})
    assert "Source directory:" in out and "No source directory given." in out, out
    assert not (d / "photo.jxl").exists()


def test_stage_started_in_a_folder_offers_it_as_the_source(folder, tmp_path, pty_run):
    """With the default settings, a stage without a folder used to end in
    "No source directory given" without asking for one."""
    d = folder("rws", "photo.jpg")
    o = tmp_path / "rws-out"
    out = pty_run(["", "", str(o)], "images", cwd=d)
    assert f"Source directory [{d}]:" in out, out
    assert (o / "photo.jxl").exists(), out


def test_source_on_the_command_line_skips_the_defaults_question(folder, tmp_path, pty_run):
    d = folder("rl", "photo.jpg")
    o = tmp_path / "rl-out"
    out = pty_run([str(o), ""], d)
    assert "Use the default settings" not in out, out
    assert "Process another directory? [y/N]" in out, out
    assert (o / "photo.jxl").exists(), out


def test_discarding_a_stopped_run(folder, tmp_path, pty_run):
    a = folder("rc", "photo.jpg")
    b = folder("rd", "photo.jpg")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps(
        {"status": "INTERRUPTED", "source": str(a), "output": str(tmp_path / "rc-out"), "settings": {},
         "done": {"images": False, "gifs": True, "clips": True, "video": True}}))
    out = pty_run(["n", str(b), str(tmp_path / "rd-out"), "", "n"])
    assert "Starting a new run" in out, out
    assert (tmp_path / "rd-out" / "photo.jxl").exists() and not (tmp_path / "rc-out").exists()
    # Declined before the folder was chosen: a run on another folder used to
    # delete the stopped run's state anyway.
    assert [st.source for st in orchestrator.open_states()] == [str(a)]


def test_declining_and_running_the_same_folder_replaces_its_state(folder, tmp_path, pty_run):
    a = folder("rc2", "photo.jpg")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps(
        {"status": "INTERRUPTED", "source": str(a), "output": str(tmp_path / "rc2-old"), "settings": {},
         "done": {"images": False, "gifs": True, "clips": True, "video": True}}))
    out = pty_run(["n", str(a), str(tmp_path / "rc2-out"), "", "n"])
    assert (tmp_path / "rc2-out" / "photo.jxl").exists(), out
    assert orchestrator.open_states() == [], out


def test_settings_menu(folder, tmp_path, pty_run):
    d = folder("rm", "photo.jpg", "other.png")
    o = tmp_path / "rm-out"
    # source, target, "use defaults?" n, then the menu: preflight, delete, deep, clips, workers,
    # rescan, target format webp, quality, effort, png mode, gif method, encoder, threshold, qp,
    # crf, preset, probe, discard, salvaged, visual, strict, verify-output; another dir: n
    answers = [str(d), str(o), "n", "", "", "", "", "2", "", "webp", "80", "", "", "", "", "", "", "",
               "", "", "", "", "", "", "", "n"]
    out = pty_run(answers, env={"MO_MIN_SIZE_MB": "0"})
    assert (o / "photo.webp").exists() and (o / "other.webp").exists(), out
    assert "Scan all files again" in out
