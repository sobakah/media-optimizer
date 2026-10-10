"""Folders processed by the former Bash version (up to 1.2.1).

Its caches, lists and leftovers use the same names and formats, so a folder
it worked on continues seamlessly: finished videos are not probed again,
half-written temp files are cleaned up, and its run lock (flock on the
directory) is respected the same way.
"""

from __future__ import annotations

import shutil
import subprocess
import time

import pytest


def test_cache_written_by_bash_is_used(folder, tmp_path, run_cli):
    """Bash wrote one absolute source path per line."""
    d = folder("c1", "l.mp4=long.mp4")
    o = tmp_path / "c1out"
    o.mkdir()
    (o / ".video_conversion_cache.txt").write_text(f"{d}/l.mp4\n")
    out = run_cli("video", d, o, "--encoder", "cpu", "--min-size", "0", "--no-probe")
    assert "Cache (this scan):    1 file(s)" in out, out
    assert not (o / "l.mp4").exists()


def test_leftovers_of_bash_runs_are_removed(folder, run_cli):
    d = folder("c2", "photo.jpg")
    (d / "photo.jxl.part.4711.12345.jxl").write_text("half")      # Bash temp naming
    (d / ".photo.molock").mkdir()
    out = run_cli("images", d, "--no-delete")
    assert "leftover(s) from an aborted run removed" in out, out
    assert (d / "photo.jxl").exists()
    assert not (d / "photo.jxl.part.4711.12345.jxl").exists()


def test_old_german_list_names_are_renamed(folder, tmp_path, run_cli):
    d = folder("c3", "l.mp4=long.mp4")
    o = tmp_path / "c3out"
    o.mkdir()
    (o / ".defekte_videos.txt").write_text(f"{d}/l.mp4\n")
    run_cli("video", d, o, "--encoder", "cpu", "--min-size", "0", "--no-probe", "-n")
    assert (o / ".defekte_videos.txt").exists(), "a dry run renames nothing"
    run_cli("video", d, o, "--encoder", "cpu", "--min-size", "0", "--no-probe", "--preset", "ultrafast")
    assert (o / ".broken_videos.txt").exists() and not (o / ".defekte_videos.txt").exists()


@pytest.mark.skipif(shutil.which("flock") is None, reason="needs flock(1)")
def test_any_flock_on_the_directory_keeps_a_run_out(folder, run_cli):
    """The Bash scripts locked with `flock <dir>`; the same lock is honoured."""
    d = folder("c4", "photo.jpg")
    holder = subprocess.Popen(["flock", str(d), "sleep", "5"])
    try:
        time.sleep(0.3)
        out = run_cli("images", d)
    finally:
        holder.kill()
        holder.wait()
    assert out.rc == 1 and "BUSY" in out, out
