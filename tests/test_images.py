"""The image stage against real files, including forced failures."""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from conftest import leftovers, running, snapshot, wait_running
from media_optimizer import files, safety

BASIC = ("photo.jpg", "meta.png", "fake.jpg", "isgif.png", "garbage.png", "other.png", "other.webp")


def _decode(jxl: Path, out: Path) -> Path:
    subprocess.run(["djxl", str(jxl), str(out), "--quiet"], check=True)
    return out


def _same_pixels(a: Path, b: Path) -> bool:
    with Image.open(a) as x, Image.open(b) as y:
        return ImageChops.difference(x.convert("RGB"), y.convert("RGB")).getbbox() is None


def _exif(path: Path, *tags: str) -> str:
    return subprocess.run(["exiftool", "-s", "-s", "-s", *tags, str(path)], stdout=subprocess.PIPE,
                          text=True).stdout


def test_in_place(folder, media, tmp_path, run_cli):
    d = folder("r1", *BASIC)
    out = run_cli("images", d, "--delete", "--force-delete", "--png-mode", "lossless", "-j", "4")
    assert out.rc == 3, out                                      # garbage.png fails
    for name, src in (("fake", "fake.jpg"), ("meta", "meta.png"), ("other", "other.png")):
        assert _same_pixels(media / src, _decode(d / f"{name}.jxl", tmp_path / f"{name}.png")), name
    _decode(d / "photo.jxl", tmp_path / "rec.jpg")
    assert (tmp_path / "rec.jpg").read_bytes() == (media / "photo.jpg").read_bytes(), "bit-exact JPEG"
    assert "Max" in _exif(d / "meta.jxl", "-Artist") and "48" in _exif(d / "meta.jxl", "-GPSLatitude")
    names = {p.name for p in d.iterdir()}
    assert not names & {"photo.jpg", "fake.jpg", "meta.png", "other.png"}, "originals of successes gone"
    assert "garbage.png" in names
    assert "isgif.gif" in names, "GIF named .png gets its extension"
    assert "other.jxl" in names, "an unrelated other.webp does not block other.jxl"
    assert not leftovers(d)


def test_target_mode_leaves_source_untouched(folder, tmp_path, run_cli):
    d = folder("r2", *BASIC)
    before = snapshot(d)
    o = tmp_path / "r2out"
    out = run_cli("images", d, o, "--png-mode", "lossless")
    assert snapshot(d) == before, "source byte-identical incl. names and mtimes"
    assert {"photo.jxl", "fake.jxl", "isgif.gif"} <= {p.name for p in o.iterdir()}, out
    assert not leftovers(o)
    assert not list(d.glob(".img_*")), "no state files in the source"


def test_output_keeps_mtime(folder, tmp_path, run_cli):
    d = folder("r2b", "photo.jpg")
    os.utime(d / "photo.jpg", (1_000_000_000, 1_000_000_000))
    run_cli("images", d, tmp_path / "o")
    assert (tmp_path / "o" / "photo.jxl").stat().st_mtime == 1_000_000_000


def test_jpeg_metadata_bit_exact(folder, tmp_path, run_cli):
    d = folder("r3", "e.jpg=exif.jpg")
    run_cli("images", d, d / "out")
    _decode(d / "out" / "e.jxl", tmp_path / "rec.jpg")
    assert (tmp_path / "rec.jpg").read_bytes() == (d / "e.jpg").read_bytes()
    assert "Max" in _exif(d / "out" / "e.jxl", "-Artist"), "metadata readable without Brotli support"


def test_cmyk_unsupported_truncated_failed(folder, run_cli):
    d = folder("r4", "c.jpg=cmyk.jpg", "t.jpg=truncated.jpg")
    out = run_cli("images", d, "--delete", "--force-delete")
    assert "[UNSUPPORTED] 'c.jpg': CMYK" in out, out
    assert "damaged" in out and "t.jpg" in out
    assert out.rc == 3
    assert (d / "c.jpg").exists() and (d / "t.jpg").exists()
    d = folder("r4b", "c.jpg=cmyk.jpg")
    assert run_cli("images", d).rc == 0, "a known limitation alone is not a failure"


def test_arithmetic_jpeg_unsupported(folder, media, run_cli):
    if not (media / "arith.jpg").exists():
        pytest.skip("magick could not write an arithmetic-coded JPEG")
    d = folder("r4c", "a.jpg=arith.jpg")
    out = run_cli("images", d, "--delete", "--force-delete")
    assert "[UNSUPPORTED] 'a.jpg': arithmetic-coded JPEG" in out, out
    assert out.rc == 0 and (d / "a.jpg").exists()


# --- forced failures (in-process, one step patched) ------------------------------------------
def test_race_before_claim(folder, main, monkeypatch):
    """Another worker finished foo.jxl between the unlocked check and the
    claim: both originals kept, the foreign output untouched."""
    d = folder("r5a", "photo.jpg", "meta.png")
    real = files.real_type

    def racing(path):
        path.with_suffix(".jxl").write_text("FROM-OTHER-WORKER")
        return real(path)
    monkeypatch.setattr(files, "real_type", racing)
    from media_optimizer import proc
    calls = []
    run = proc.run

    def spy(cmd, **kw):
        calls.append(cmd[0])
        return run(cmd, **kw)
    monkeypatch.setattr(proc, "run", spy)
    out = main("images", d, "--delete", "--force-delete", "--png-mode", "lossless", "-j", "2")
    assert (d / "photo.jpg").exists() and (d / "meta.png").exists(), out
    assert (d / "photo.jxl").read_text() == "FROM-OTHER-WORKER"
    assert "CONFLICT" in out
    assert calls.count("cjxl") == 1, "only the startup check; the claim stopped both before encoding"
    assert not leftovers(d)


def test_race_before_commit(folder, main, monkeypatch):
    """The name is taken right before the move: never overwritten."""
    d = folder("r5b", "photo.jpg")
    move = safety.move_no_replace

    def racing(src, dest):
        if ".part." in src.name:
            dest.write_text("FROM-OTHER-WORKER")
        return move(src, dest)
    monkeypatch.setattr(safety, "move_no_replace", racing)
    out = main("images", d, "--delete", "--force-delete")
    assert (d / "photo.jpg").exists(), out
    assert (d / "photo.jxl").read_text() == "FROM-OTHER-WORKER"
    assert not leftovers(d)


def test_move_failure_keeps_original(folder, main, monkeypatch):
    d = folder("r5c", "photo.jpg", "meta.png")

    def failing(src, dest):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(safety, "move_no_replace", failing)
    out = main("images", d, "--delete", "--force-delete", "--png-mode", "lossless")
    assert out.rc == 3, out
    assert (d / "photo.jpg").exists() and (d / "meta.png").exists()
    assert not leftovers(d)


def test_exiftool_failure_keeps_original(folder, run_cli, stub):
    d = folder("r5d", "photo.jpg", "meta.png")
    path = stub("exiftool", '[[ "$*" == *tagsFromFile* ]] && exit 1; PATH="${PATH#*:}" exec exiftool "$@"\n')
    out = run_cli("images", d, "--delete", "--force-delete", "--png-mode", "lossless", path=path)
    assert out.rc == 3, out
    assert (d / "meta.png").exists() and not (d / "meta.jxl").exists()
    assert "could not copy metadata" in out
    assert not leftovers(d)


def test_unexpected_error_keeps_original(folder, main, monkeypatch):
    d = folder("r5e", "photo.jpg", "meta.png")
    real = files.real_type

    def broken(path):
        if path.name == "photo.jpg":
            raise RuntimeError("simulated bug")
        return real(path)
    monkeypatch.setattr(files, "real_type", broken)
    out = main("images", d, "--delete", "--force-delete", "--png-mode", "lossless")
    assert (d / "photo.jpg").exists() and "unexpected error" in out, out
    assert (d / "meta.jxl").exists(), "the other file still converted"
    assert out.rc == 3
    assert not leftovers(d)


def test_trash_failure_queues_instead_of_deleting(folder, main):
    """Without a trash and without --force-delete nothing is deleted; the
    path is queued and kept when nobody can be asked."""
    d = folder("r5f", "photo.jpg")
    out = main("images", d, "--delete")
    assert (d / "photo.jpg").exists() and (d / "photo.jxl").exists(), out
    assert "[TRASH] failed, original kept" in out
    assert "Cannot prompt" in out
    assert "photo.jpg" in (d / ".img_pending_deletes.txt").read_text()


# --- settings ----------------------------------------------------------------------------
@pytest.mark.parametrize("args", [["--copy-metadata", "false"], ["-j", "0"], ["-e", "11"],
                                  ["--png-quality", "abc"], ["--output"]])
def test_invalid_options_exit_2(folder, run_cli, args):
    d = folder("r6", "photo.jpg")
    out = run_cli("images", "-i", d, *args)
    assert out.rc == 2, out
    assert sorted(p.name for p in d.iterdir()) == ["photo.jpg"]


def test_effort_10_accepted(folder, run_cli):
    d = folder("r6b", "photo.jpg")
    assert run_cli("images", d, "-e", "10", "-n").rc == 0


def test_invalid_environment_exits_2(folder, run_cli):
    d = folder("r6c", "photo.jpg")
    out = run_cli("images", d, env={"MO_DELETE_ORIGINAL": "yes"})
    assert out.rc == 2 and "MO_DELETE_ORIGINAL" in out, out
    assert sorted(p.name for p in d.iterdir()) == ["photo.jpg"]


# --- scanning ---------------------------------------------------------------------------------
def test_hidden_and_system_folders(folder, tmp_path, run_cli):
    d = folder("r7", "a.jpg=photo.jpg", ".hid/b.jpg=photo.jpg", ".Trash-1000/c.jpg=photo.jpg",
               "@eaDir/d.jpg=photo.jpg", "._e.jpg=photo.jpg", ".f.jpg=photo.jpg", "sub/g.jpg=photo.jpg")
    o = tmp_path / "r7out"

    def outputs():
        return sorted(str(p.relative_to(o)) for p in o.rglob("*.jxl"))
    run_cli("images", d, o)
    assert outputs() == ["a.jxl", "sub/g.jxl"]
    import shutil
    shutil.rmtree(o)
    run_cli("images", d, o, env={"MO_INCLUDE_HIDDEN": "true"})
    assert outputs() == [".f.jxl", ".hid/b.jxl", "a.jxl", "sub/g.jxl"]


def test_unreadable_subfolder_is_not_a_failure(folder, tmp_path, run_cli):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 folder")
    d = folder("r7b", "a.jpg=photo.jpg", "locked/x.jpg=photo.jpg")
    (d / "locked").chmod(0)
    try:
        out = run_cli("images", d, tmp_path / "r7bout")
    finally:
        (d / "locked").chmod(0o755)
    assert out.rc == 0 and (tmp_path / "r7bout" / "a.jxl").exists(), out


def test_dry_run_changes_nothing(folder, tmp_path, run_cli):
    d = folder("r8", "photo.jpg", "meta.png")
    (d / "x.jxl.part.1.2.jxl").touch()
    (d / ".x.molock").mkdir()
    (d / ".img_stats.json").write_text("{}")
    log = tmp_path / "dry.log"
    log.write_text("")
    before = snapshot(d)
    out = run_cli("images", d, "-n", "--log", log, env={"MO_LOG": "true"})
    assert out.rc == 0, out
    assert snapshot(d) == before
    assert log.read_text() == ""
    out = run_cli("images", d, "-n", "--target", "webp")
    assert "'photo.jpg' -> 'photo.webp'" in out


def test_log_written(folder, tmp_path, run_cli):
    d = folder("r8b", "photo.jpg")
    log = tmp_path / "run.log"
    run_cli("images", d, tmp_path / "o", "--log", log, env={"MO_LOG": "true"})
    text = log.read_text()
    assert "started" in text and "OK        " in text and "photo.jxl" in text and "finished" in text


# --- concurrency and stopping ------------------------------------------------------------------
def test_busy_directory(folder, run_cli):
    d = folder("r9", "photo.jpg")
    fd = os.open(d, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        out = run_cli("images", d)
    finally:
        os.close(fd)
    assert out.rc == 1 and "BUSY" in out, out
    assert not (d / "photo.jxl").exists()


def test_target_folder_cannot_be_created(folder, tmp_path, run_cli):
    d = folder("r9b", "sub/photo.jpg=photo.jpg")
    o = tmp_path / "r9bout"
    o.mkdir()
    (o / "sub").write_text("a file where the folder should be")
    out = run_cli("images", d, o)
    assert "cannot create target folder" in out, out
    assert out.rc == 3


def test_encoder_vanishes(folder, run_cli, vanishing_path):
    d = folder("r10", "a.jpg=photo.jpg", "b.jpg=photo.jpg", "c.jpg=photo.jpg")
    out = run_cli("images", d, "-j", "1", "--delete", "--force-delete", path=vanishing_path("cjxl"))
    assert out.rc == 255, out
    assert sorted(p.name for p in d.iterdir() if p.suffix == ".jpg") == ["a.jpg", "b.jpg", "c.jpg"]


@pytest.mark.slow
def test_sigterm_stops_at_once(folder, media, tmp_path, start_cli):
    d = folder("r11")
    for n in range(40):
        Image.effect_mandelbrot((1200, 900), (-2 + n * 0.01, -1.2, 1, 1.2), 100).convert("RGB") \
            .save(d / f"p{n}.png")
    p = start_cli("images", d, "-j", "2", "-e", "9", "--png-mode", "lossless", out=tmp_path / "term.out")
    assert wait_running("cjxl", str(d))
    time.sleep(1)
    t0 = time.monotonic()
    p.terminate()
    rc = p.wait(timeout=30)
    took = time.monotonic() - t0
    output = (tmp_path / "term.out").read_text()
    assert rc == 130, output
    assert took < 3, f"took {took:.1f}s"
    assert (d / ".img_stats.json").exists(), "counters saved for resume"
    assert not leftovers(d)
    time.sleep(0.3)
    assert not running("cjxl", str(d))


@pytest.mark.slow
def test_sigint_while_started_with_sigint_ignored(folder, tmp_path):
    """A program started in the background of a non-interactive shell
    inherits SIGINT as ignored. It must still be stoppable, and its encoders
    with it (the Bash version needed SIGTERM for that)."""
    import sys
    from conftest import PYDIR
    d = folder("r11b")
    for n in range(20):
        Image.effect_mandelbrot((1200, 900), (-2 + n * 0.01, -1.2, 1, 1.2), 100).convert("RGB") \
            .save(d / f"p{n}.png")
    env = dict(os.environ, PYTHONPATH=str(PYDIR), PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.Popen([sys.executable, "-m", "media_optimizer", "images", str(d), "-j", "2", "-e", "9",
                          "--png-mode", "lossless"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=env,
                         preexec_fn=lambda: __import__("signal").signal(2, __import__("signal").SIG_IGN))
    assert wait_running("cjxl", str(d))
    time.sleep(1)
    p.send_signal(2)
    assert p.wait(timeout=10) == 130
    time.sleep(0.3)
    assert not running("cjxl", str(d))
    assert not leftovers(d)


# --- prompts --------------------------------------------------------------------------------
def test_no_menu_when_source_given(folder, tmp_path, pty_run):
    d = folder("r12", "photo.jpg")
    out = pty_run([], "images", d, tmp_path / "o")
    assert "Run with the default settings" not in out, out


def test_explicit_no_delete_is_not_asked_again(folder, pty_run):
    d = folder("r12b", "photo.jpg")
    out = pty_run([], "images", d, "--no-delete")
    assert "Keep these settings" not in out, out
    assert "_h265" not in out
    assert (d / "photo.jpg").exists()


def test_interactive_no_to_delete_is_honoured(folder, pty_run):
    d = folder("r12c", "photo.jpg")
    out = pty_run(["n", str(d), "", "n", "", "", ""], "images")
    assert (d / "photo.jpg").exists() and (d / "photo.jxl").exists(), out


def test_in_place_notice_offers_change(folder, pty_run):
    d = folder("r12d", "photo.jpg")
    out = pty_run(["n", "n"], "images", d)
    assert "Keep these settings" in out, out
    assert (d / "photo.jpg").exists() and (d / "photo.jxl").exists()


def test_invalid_answer_is_asked_again(folder, pty_run):
    d = folder("r12e", "photo.jpg")
    out = pty_run(["n", str(d), "", "n", "11", "9", "", ""], "images")
    assert "'11' is not valid here" in out, out
    assert (d / "photo.jxl").exists()


def test_renamed_source_is_not_counted_twice(folder, run_cli):
    """In place, a kept fake.jpg (really a PNG) becomes fake.png after its
    conversion. Each folder is listed completely before its files are
    processed, so the new name is not found again (Bash open issue 1)."""
    d = folder("r13", "fake.jpg", "sub/fake.jpg=fake.jpg", "photo.jpg")
    out = run_cli("images", d, "--no-delete", "--png-mode", "lossless", "-j", "1")
    assert (d / "fake.png").exists() and (d / "sub" / "fake.png").exists(), out
    assert "Converted:            3 file(s)" in out and "Skipped:              0 file(s)" in out, out
