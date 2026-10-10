"""The per-stage caches: what is remembered, when an entry counts, and the
conversion of the old video cache format."""

from __future__ import annotations

import os
import shutil

from conftest import snapshot
from media_optimizer import cache

FAST = ["--encoder", "cpu", "--min-size", "0", "--no-probe", "--preset", "ultrafast"]
V2 = "# media-optimizer cache v2"


def _entries(path):
    lines = path.read_text().splitlines()
    assert lines[0] == V2, lines[:2]
    return {line.split("\t", 4)[4]: line.split("\t", 4)[3] for line in lines[1:] if line}


def _counting_stub(stub, tool, log):
    """A stand-in that records each call's last argument, then runs the tool."""
    return stub(tool, f'echo "${{@: -1}}" >> "{log}"; PATH="${{PATH#*:}}" exec {tool} "$@"\n')


# --- what is remembered -------------------------------------------------------------
def test_unsupported_image_is_not_retried(folder, run_cli, stub, tmp_path):
    d = folder("k1", "c.jpg=cmyk.jpg")
    out = run_cli("images", d)
    assert "[UNSUPPORTED]" in out and _entries(d / ".img_cache.txt") == {f"{d}/c.jpg": "unsupported"}
    log = tmp_path / "cjxl.calls"
    out = run_cli("images", d, path=_counting_stub(stub, "cjxl", log))
    assert "[UNSUPPORTED]" not in out and "Cache (this scan):    1 file(s)" in out, out
    assert str(d / "c.jpg") not in (log.read_text() if log.exists() else ""), "no encode attempt"


def test_replaced_file_counts_as_new(folder, media, run_cli):
    d = folder("k2", "c.jpg=cmyk.jpg")
    run_cli("images", d, "--no-delete")
    shutil.copy(media / "photo.jpg", d / "c.jpg")        # same name, other file
    out = run_cli("images", d, "--no-delete")
    assert (d / "c.jxl").exists(), out


def _tiny_gif(path):
    """321 bytes; as AVIF it grows (container overhead), so it is discarded."""
    import random

    from PIL import Image
    random.seed(1)
    Image.frombytes("L", (8, 8), bytes(random.randrange(256) for _ in range(64))).convert("P").save(path)


def test_discarded_gif_is_not_reencoded_until_settings_change(folder, run_cli, stub, tmp_path):
    d = folder("k3")
    _tiny_gif(d / "a.gif")
    out = run_cli("gifs", d, "--no-delete", "--target", "avif")
    assert "[DISCARDED]" in out, out
    assert _entries(d / ".gif_cache.txt") == {f"{d}/a.gif": "discarded"}
    log = tmp_path / "ffmpeg.calls"
    path = _counting_stub(stub, "ffmpeg", log)

    def encodes() -> int:
        return log.read_text().count(".part.") if log.exists() else 0
    out = run_cli("gifs", d, "--no-delete", "--target", "avif", path=path)
    assert "[DISCARDED]" not in out and encodes() == 0, out
    out = run_cli("gifs", d, "--no-delete", "--target", "avif", "--avif-crf", "30", path=path)
    assert "[DISCARDED]" in out and encodes() == 1, "a changed setting makes it count as new"


def test_no_cache_checks_everything_again(folder, run_cli):
    d = folder("k4")
    _tiny_gif(d / "a.gif")
    run_cli("gifs", d, "--no-delete", "--target", "avif")
    out = run_cli("gifs", d, "--no-delete", "--target", "avif", "--no-cache")
    assert "[DISCARDED]" in out and "Ignored for this run" in out, out


def test_clips_not_selected_are_not_probed_again(folder, run_cli, stub, tmp_path):
    d = folder("k5", "l.mp4=long.mp4")
    run_cli("clips", d, "--max-seconds", "5")
    assert _entries(d / ".clip_cache.txt") == {f"{d}/l.mp4": "not-selected"}
    log = tmp_path / "ffprobe.calls"
    out = run_cli("clips", d, "--max-seconds", "5", path=_counting_stub(stub, "ffprobe", log))
    assert not log.exists(), (log.read_text(), out.text)


def test_clips_existing_output_is_checked_before_probing(folder, run_cli, stub, tmp_path):
    d = folder("k6", "c.mp4=clip.mp4")
    (d / "c.avif").write_bytes(b"x" * 100)
    log = tmp_path / "ffprobe.calls"
    run_cli("clips", d, "--no-cache", path=_counting_stub(stub, "ffprobe", log))
    assert not log.exists()


def test_misnamed_file_is_taken_over_once(folder, tmp_path, run_cli):
    """Target mode copied a GIF named .png on every run (isgif_1.gif, ...)."""
    d = folder("k7", "isgif.png")
    o = tmp_path / "k7out"
    for _ in range(2):
        run_cli("images", d, o)
    run_cli("images", d, o, "--no-cache")
    assert sorted(p.name for p in o.iterdir() if not p.name.startswith(".")) == ["isgif.gif"]


def test_failures_are_not_cached(folder, run_cli):
    d = folder("k8", "garbage.png")
    run_cli("images", d)
    assert not (d / ".img_cache.txt").exists() or "garbage" not in (d / ".img_cache.txt").read_text()


def test_dry_run_reads_but_never_writes_the_cache(folder, run_cli):
    d = folder("k9", "c.jpg=cmyk.jpg")
    out = run_cli("images", d, "-n")
    assert not (d / ".img_cache.txt").exists(), out
    run_cli("images", d)
    out = run_cli("images", d, "-n")
    assert "c.jpg" not in out.text.split("DRY-RUN")[-1] or "would be converted" in out


# --- the old video cache format -------------------------------------------------------------
def _old_cache(folder, tmp_path, run_cli, name):
    """A target folder with a converted video and a cache in the 1.x format."""
    d = folder(name, "l.mp4=long.mp4")
    o = tmp_path / f"{name}out"
    run_cli("video", d, o, *FAST)
    (o / ".video_conversion_cache.txt").write_text(f"{d}/l.mp4\n{d}/gone.mp4\n")
    return d, o


def test_old_cache_converted_after_yes(folder, tmp_path, run_cli, pty_run):
    d, o = _old_cache(folder, tmp_path, run_cli, "m1")
    out = pty_run(["y"], "video", d, o, *FAST)
    assert "in the old format" in out and "1 entries kept, 1 for missing files dropped" in out, out
    assert _entries(o / ".video_conversion_cache.txt") == {f"{d}/l.mp4": "done"}
    assert (o / ".video_conversion_cache.txt.v1.bak").read_text() == f"{d}/l.mp4\n{d}/gone.mp4\n"
    assert "Cache (this scan):    1 file(s)" in out


def test_old_cache_kept_after_no(folder, tmp_path, run_cli, pty_run):
    d, o = _old_cache(folder, tmp_path, run_cli, "m2")
    out = pty_run(["n"], "video", d, o, *FAST)
    assert "Kept as they are" in out, out
    assert (o / ".video_conversion_cache.txt").read_text() == f"{d}/l.mp4\n{d}/gone.mp4\n"
    assert "Cache (this scan):    1 file(s)" in out, "still used, by path as before"
    out = pty_run(["n"], "video", d, o, *FAST)
    assert "in the old format" in out, "asked again next run"


def test_old_cache_without_terminal_is_converted(folder, tmp_path, run_cli):
    d, o = _old_cache(folder, tmp_path, run_cli, "m3")
    out = run_cli("video", d, o, *FAST)
    assert "Converting (default; no terminal to ask)" in out, out
    assert _entries(o / ".video_conversion_cache.txt") == {f"{d}/l.mp4": "done"}


def test_old_cache_in_dry_run_is_used_not_touched(folder, tmp_path, run_cli):
    d, o = _old_cache(folder, tmp_path, run_cli, "m4")
    before = snapshot(o)
    out = run_cli("video", d, o, *FAST, "-n")
    assert "old format" not in out and snapshot(o) == before, out
    assert "would be re-encoded" in out and "0 video(s)" in out


def test_old_subfolder_caches_are_converted_too(folder, tmp_path, run_cli):
    d = folder("m5", "sub/l.mp4=long.mp4")
    run_cli("video", d / "sub", *FAST, "--no-delete")
    (d / "sub" / ".video_conversion_cache.txt").write_text(f"{d}/sub/l.mp4\n")
    out = run_cli("video", d, *FAST, "--no-delete")
    assert "converted" in out and "entries taken over" in out, out
    assert _entries(d / "sub" / ".video_conversion_cache.txt") == {f"{d}/sub/l.mp4": "done"}


def test_full_run_asks_before_the_first_stage(folder, tmp_path, run_cli):
    d = folder("m6", "photo.jpg", "l.mp4=long.mp4")
    o = tmp_path / "m6out"
    o.mkdir()
    (o / ".video_conversion_cache.txt").write_text(f"{d}/l.mp4\n")
    out = run_cli("-y", d, o, env={"MO_MIN_SIZE_MB": "0", "MO_ENCODER_MODE": "cpu", "MO_ENABLE_PROBE": "false"})
    assert out.text.index("in the old format") < out.text.index("Starting image conversion"), out
    assert _entries(o / ".video_conversion_cache.txt")[f"{d}/l.mp4"] == "done"


# --- the file format ------------------------------------------------------------------------
def test_later_lines_win_and_are_compacted(tmp_path):
    f = tmp_path / "x.jpg"
    f.write_text("x")
    st = os.stat(f)
    c = tmp_path / ".img_cache.txt"
    c.write_text(f"{V2}\n{st.st_size}\t{st.st_mtime_ns}\tfp\tdiscarded\t{f}\n"
                 f"{st.st_size}\t{st.st_mtime_ns}\tfp\tunsupported\t{f}\n")
    sc = cache.StageCache(c, enabled=True, dry_run=False, fingerprint="fp")
    sc.load()
    assert sc.lookup(f) == "unsupported"
    assert len(c.read_text().splitlines()) == 2, "compacted to header + one entry"
    assert cache.StageCache(c, enabled=True, dry_run=False, fingerprint="other").lookup(f) is None


def test_prune_handles_both_formats(tmp_path):
    old = tmp_path / "old.txt"
    old.write_text("/a\n/b\n")
    assert cache.prune(old, {"/a"}) == 1 and old.read_text() == "/b\n"
    new = tmp_path / "new.txt"
    new.write_text(f"{V2}\n1\t2\t*\tdone\t/a\n1\t2\t*\tdone\t/b\n")
    assert cache.prune(new, {"/a"}) == 1 and new.read_text() == f"{V2}\n1\t2\t*\tdone\t/b\n"


# --- AV1 on very narrow frames ---------------------------------------------------------------
def test_narrow_gif_does_not_hang(folder, run_cli):
    """libsvtav1 (3.1.2) hangs forever on frames narrower than ~31 px; such
    files go to libaom-av1. Before, this test never finished."""
    import time
    d = folder("n1")
    _tiny_gif(d / "a.gif")
    t0 = time.monotonic()
    out = run_cli("gifs", d, "--no-delete", "--target", "avif", "--keep-larger", timeout=120)
    assert out.rc == 0 and (d / "a.avif").exists(), out
    assert time.monotonic() - t0 < 60


def test_timeout_stops_a_stuck_tool(tmp_path):
    import time
    from media_optimizer import proc
    t0 = time.monotonic()
    res = proc.run(["sleep", "30"], timeout=1)
    assert res.returncode == 124 and "timed out" in res.stderr
    assert time.monotonic() - t0 < 15
