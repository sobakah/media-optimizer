"""The GIF stage and the clip stage (short silent videos)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import leftovers, snapshot
from media_optimizer import media as mo_media
from media_optimizer import safety


# --- GIF -----------------------------------------------------------------------------
def test_gif_target_mode(folder, tmp_path, run_cli):
    d = folder("g1", "a.gif=anim.gif", "b.gif=anim.gif", ".hid/h.gif=anim.gif")
    (d / "broken.gif").write_bytes(os.urandom(3000))
    before = snapshot(d)
    o = tmp_path / "g1out"
    out = run_cli("gifs", d, o)
    assert snapshot(d) == before, "source unchanged"
    assert (o / "a.webp").exists() and (o / "b.webp").exists() and not (o / ".hid").exists(), out
    assert out.rc == 3, "broken.gif fails"
    assert not list(d.glob(".gif_*"))


def test_gif_lossless(folder, tmp_path, run_cli):
    """gif2webp is lossless by default; a '-lossless' flag does not exist."""
    d = folder("g1b", "a.gif=anim.gif")
    out = run_cli("gifs", d, tmp_path / "o", "--keep-larger")
    assert out.rc == 0 and (tmp_path / "o" / "a.webp").exists(), out


@pytest.mark.parametrize("target", ["avif", "webp"])
def test_photo_named_gif_is_not_converted(folder, run_cli, target):
    """With gif_target avif, ffmpeg made a lossy one-frame AVIF of a JPEG
    named .gif and the original went to the trash."""
    d = folder(f"g1p{target}", "p.gif=photo.jpg")
    out = run_cli("gifs", d, "--target", target, "--delete", "--force-delete")
    assert out.rc == 0 and "[UNSUPPORTED]" in out and "not a GIF but JPG" in out, out
    assert (d / "p.gif").exists() and not (d / f"p.{target}").exists()


def test_gif_bad_method(folder, run_cli):
    d = folder("g1c", "a.gif=anim.gif")
    assert run_cli("gifs", d, "-m", "9").rc == 2


def test_gif_race(folder, main, monkeypatch):
    d = folder("g2", "a.gif=anim.gif")
    claim_enter = safety.Claim.__enter__

    def racing(self):
        self.dest.write_text("FROM-OTHER-WORKER")
        return claim_enter(self)
    monkeypatch.setattr(safety.Claim, "__enter__", racing)
    out = main("gifs", d, "--delete", "--force-delete", "--keep-larger")
    assert (d / "a.gif").exists(), out
    assert (d / "a.webp").read_text() == "FROM-OTHER-WORKER"
    assert not leftovers(d)


def test_gif_move_failure(folder, main, monkeypatch):
    d = folder("g2b", "a.gif=anim.gif")

    def failing(src, dest):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(safety, "move_no_replace", failing)
    out = main("gifs", d, "--delete", "--force-delete", "--keep-larger")
    assert out.rc == 3 and (d / "a.gif").exists(), out
    assert not leftovers(d)


def test_gif_encoder_vanishes(folder, run_cli, vanishing_path):
    d = folder("g3", "a.gif=anim.gif", "b.gif=anim.gif", "c.gif=anim.gif")
    out = run_cli("gifs", d, "-j", "1", "--delete", "--force-delete", path=vanishing_path("gif2webp"))
    assert out.rc == 255, out
    assert len(list(d.glob("*.gif"))) == 3


# --- AVIF ----------------------------------------------------------------------------------
def test_avif_selection(folder, tmp_path, run_cli):
    d = folder("a1", "c.mp4=clip.mp4", "w.mp4=withaudio.mp4", "l.mp4=long.mp4")
    before = snapshot(d)
    o = tmp_path / "a1out"
    out = run_cli("clips", d, o, "--max-seconds", "5")
    assert (o / "c.avif").exists(), out
    assert not (o / "w.avif").exists(), "audio clip skipped"
    assert not (o / "l.avif").exists(), "long clip skipped"
    assert snapshot(d) == before
    assert out.rc == 0


def test_avif_picture_is_really_compared(folder, tmp_path, run_cli):
    """An animated AVIF has a still first and the animation second. The Bash
    version compared the still, found no frame at any sample time and
    passed every file unmeasured ("picture n/a dB")."""
    d = folder("a1b", "c.mp4=clip.mp4")
    out = run_cli("clips", d, tmp_path / "o")
    assert "picture n/a dB" not in out, out
    assert " dB)" in out


def test_avif_wrong_picture_is_detected(media, tmp_path):
    """The other direction: an AVIF with different content scores far below
    the threshold (measured 7 dB against 42-45 dB for the real one)."""
    import subprocess
    other = tmp_path / "other.avif"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                    "-t", "3", "-c:v", "libsvtav1", "-crf", "32", "-pix_fmt", "yuv420p", "-f", "avif",
                    str(other)], check=True, stderr=subprocess.DEVNULL)
    result = mo_media.verify_visual(media / "clip.mp4", other, 18, 3)
    assert not result.ok and result.best is not None and result.best < 18


@pytest.mark.parametrize("fmt", ["avif", "webp"])
def test_portrait_clip_passes_the_picture_check(folder, tmp_path, run_cli, fmt):
    """Same as for the video stage: upright output, sideways comparison."""
    d = folder(f"a1p{fmt}", "p.mp4=portrait.mp4")
    out = run_cli("clips", d, tmp_path / "o", "--format", fmt, "--keep-larger")
    assert (tmp_path / "o" / f"p.{fmt}").exists() and "[PICTURE?]" not in out, out


def test_avif_suspicious_picture_keeps_original(folder, main, monkeypatch):
    d = folder("a1c", "c.mp4=clip.mp4")
    monkeypatch.setattr(mo_media, "verify_visual",
                        lambda *a, **k: mo_media.Visual(False, 9.0, "1.00s=9 dB"))
    out = main("clips", d, "--delete", "--force-delete")
    assert "[PICTURE?]" in out, out
    assert (d / "c.mp4").exists() and (d / "c.avif").exists()


def test_avif_claimed_name_is_a_conflict(folder, main, monkeypatch):
    d = folder("a2", "c.mp4=clip.mp4")
    probe = mo_media.probe

    def claiming(path):
        if path.suffix == ".mp4":
            (path.parent / ".c.molock").mkdir(exist_ok=True)
        return probe(path)
    monkeypatch.setattr(mo_media, "probe", claiming)
    out = main("clips", d, "--no-delete")
    assert "[CONFLICT] 'c.mp4'" in out, out


def test_avif_bad_crf(folder, run_cli):
    d = folder("a3", "c.mp4=clip.mp4")
    assert run_cli("clips", d, "--crf", "99").rc == 2


# --- why a file was left alone ---------------------------------------------------------
@pytest.mark.parametrize("info, why", [
    (dict(video_codec="h264", duration=30.0), "clip-long"),
    (dict(video_codec="h264", duration=3.0, has_audio=True), "clip-sound"),
    (dict(video_codec="h264", duration=30.0, has_audio=True), "clip-long+sound"),
    (dict(video_codec="wmv3", duration=3.0), "clip-codec"),
    (dict(video_codec="wmv3", duration=None, has_audio=True), "clip-codec+long+sound"),
])
def test_why_a_video_is_no_short_clip(info, why):
    from media_optimizer.config import Settings
    from media_optimizer.stages import clips
    s, video = Settings(), mo_media.MediaInfo(**info)
    assert not clips.selected(video, s) and clips.why_not(video, s) == why


def test_the_events_say_why_a_gif_was_skipped(folder, tmp_path, run_cli):
    """The same file three ways: the result exists, not a GIF, and that remembered."""
    d = folder("gw", "a.gif=anim.gif", "p.gif=photo.jpg")
    (d / "a.webp").write_bytes(b"x")
    ev = tmp_path / "ev.jsonl"

    def reasons():
        lines = [json.loads(line) for line in ev.read_text().splitlines()]
        return {Path(e["src"]).name: (e["outcome"], e["reason"]) for e in lines if e["event"] == "done"}
    assert run_cli("gifs", d, "--events", ev).rc == 0
    assert reasons() == {"a.gif": ("skipped", "existing"), "p.gif": ("skipped", "not-a-gif")}
    assert run_cli("gifs", d, "--events", ev).rc == 0
    assert reasons()["p.gif"] == ("cached", "not-a-gif")


@pytest.mark.parametrize("retry, attempts", [("0", ["0"]), ("6", ["0", "6"])])
def test_a_clip_too_large_gets_a_second_encode_only_when_asked(folder, tmp_path, run_cli, stub, retry, attempts):
    """0 used to encode the same file a second time at the same CRF."""
    crfs = tmp_path / "crfs.txt"
    path = stub("ffmpeg", f"""
        for a in "$@"; do [ "$prev" = "-crf" ] && echo "$a" >> {crfs}; prev="$a"; done
        PATH="${{PATH#*:}}" exec ffmpeg "$@"
        """)
    d = folder("c9")
    # Starved, so that even CRF 0 ends up larger than it.
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "2",
                    "-c:v", "libx264", "-b:v", "30k", "-pix_fmt", "yuv420p", str(d / "a.mp4")], check=True)
    r = run_cli("clips", d, "-y", "--no-log", path=path,
                env={"MO_AVIF_CRF": "0", "MO_AVIF_CRF_RETRY": retry, "MO_AVIF_PRESET": "12"})
    assert r.rc == 0 and "DISCARDED" in str(r)
    assert crfs.read_text().split() == attempts
    assert (d / "a.mp4").exists() and not (d / "a.avif").exists()


# --- how many AV1 encoders run ------------------------------------------------------------
@pytest.fixture
def slots(monkeypatch):
    monkeypatch.setattr(mo_media, "av1_slots", mo_media._AV1Slots())
    monkeypatch.setattr(mo_media.os, "process_cpu_count", lambda: 16, raising=False)
    monkeypatch.setattr(mo_media, "available_memory", lambda: 100 * 2 ** 30)
    return mo_media.av1_slots


def _hold(slots, pixels, entered, release):
    def run():
        with slots.slot(pixels):
            entered.release()
            release.wait(20)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _running_after(slots, count, pixels):
    entered, release = threading.Semaphore(0), threading.Event()
    threads = [_hold(slots, pixels, entered, release) for _ in range(count)]
    time.sleep(0.3)
    running = slots.running
    release.set()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads) and slots.running == 0 and slots.reserved == 0
    return running


def test_av1_encoders_share_the_cores(slots):
    assert _running_after(slots, 9, 300_000) == 4


def test_av1_encoders_stay_within_the_free_memory(slots, monkeypatch):
    monkeypatch.setattr(mo_media, "available_memory", lambda: 20 * 2 ** 30)      # a budget of 14 GB
    assert _running_after(slots, 6, 8_300_000) == 2                              # 2160p: 5.2 GB each


def test_a_frame_too_large_for_the_memory_gets_its_turn_alone(slots, monkeypatch):
    monkeypatch.setattr(mo_media, "available_memory", lambda: 2 ** 30)
    assert _running_after(slots, 3, 8_300_000) == 1


def test_without_a_figure_for_the_memory_only_the_cores_count(slots, monkeypatch):
    monkeypatch.setattr(mo_media, "available_memory", lambda: None)
    assert _running_after(slots, 6, 8_300_000) == 4


def test_memory_taken_by_something_else_counts_too(slots, monkeypatch):
    """Within the budget, but the memory has gone elsewhere since it was set."""
    entered, release = threading.Semaphore(0), threading.Event()
    first = _hold(slots, 300_000, entered, release)
    assert entered.acquire(timeout=5)
    monkeypatch.setattr(mo_media, "available_memory", lambda: 2 ** 20)
    second = _hold(slots, 300_000, entered, release)
    time.sleep(0.3)
    assert slots.running == 1
    release.set()
    first.join(10)
    second.join(10)


def test_a_stop_reaches_an_encode_that_is_waiting(slots, monkeypatch):
    from media_optimizer import proc

    entered, release = threading.Semaphore(0), threading.Event()
    holders = [_hold(slots, 300_000, entered, release) for _ in range(4)]
    assert all(entered.acquire(timeout=5) for _ in holders)
    outcome = []

    def waiter():
        try:
            with slots.slot(300_000):
                outcome.append("ran")
        except proc.Stopped:
            outcome.append("stopped")

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    proc.stopping.set()
    try:
        thread.join(5)
    finally:
        proc.stopping.clear()
        release.set()
    assert outcome == ["stopped"]


def test_av1_encodes_are_told_their_threads(folder, tmp_path, run_cli, stub):
    args = tmp_path / "args.txt"
    path = stub("ffmpeg", f"""
        echo "$*" >> {args}
        PATH="${{PATH#*:}}" exec ffmpeg "$@"
        """)
    d = folder("c10", "a.mp4=clip.mp4")
    assert run_cli("clips", d, tmp_path / "out", "-y", "--no-log", path=path).rc == 0
    assert (tmp_path / "out" / "a.avif").exists()
    assert f"lp={mo_media.AV1_THREADS}" in args.read_text()


# --- a name taken by something else --------------------------------------------------------
def _picture(path, colour, size=(96, 64)):
    from PIL import Image, ImageDraw

    im = Image.new("RGB", size, colour)
    ImageDraw.Draw(im).ellipse((8, 8, 60, 50), fill=(255 - colour[0], 40, 255 - colour[2]))
    im.save(path)


def test_different_pictures_with_one_name_both_get_converted(tmp_path, run_cli):
    d = tmp_path / "n1"
    d.mkdir()
    _picture(d / "foo.jpg", (200, 30, 30))
    _picture(d / "foo.png", (10, 200, 90))
    r = run_cli("images", d, "-y", "--no-log", "--delete", "--force-delete")
    assert r.rc == 0, r
    assert sorted(p.name for p in d.iterdir() if p.suffix == ".jxl") == ["foo.jxl", "foo_1.jxl"]
    assert not (d / "foo.jpg").exists() and not (d / "foo.png").exists()


def test_a_kept_original_is_not_converted_again_and_again(tmp_path, run_cli):
    """foo.png's result under a numbered name is found again, by its time."""
    d = tmp_path / "n2"
    d.mkdir()
    _picture(d / "foo.png", (10, 200, 90))
    _picture(d / "foo.webp", (200, 30, 30))
    (d / "foo.jxl").write_bytes((d / "foo.webp").read_bytes())       # a different picture under the name
    for _ in range(3):
        r = run_cli("images", d, "-y", "--no-log", "--no-delete")
        assert r.rc == 0, r
    assert sorted(p.name for p in d.iterdir()) == ["foo.jxl", "foo.png", "foo.webp", "foo_1.jxl"]


def test_the_same_picture_in_another_format_stays_as_it_is(tmp_path, run_cli):
    from PIL import Image

    d = tmp_path / "n3"
    d.mkdir()
    _picture(d / "foo.png", (10, 200, 90))
    Image.open(d / "foo.png").save(d / "foo.webp", quality=90)
    r = run_cli("images", d, "-y", "--no-log", "--delete", "--force-delete", env={"MO_IMG_TARGET": "webp"})
    assert r.rc == 0, r
    assert sorted(p.name for p in d.iterdir()) == ["foo.png", "foo.webp"]


def test_the_same_picture_at_another_size_stays_as_it_is(tmp_path, run_cli):
    d = tmp_path / "n4"
    d.mkdir()
    _picture(d / "foo.png", (10, 200, 90))
    from PIL import Image

    Image.open(d / "foo.png").resize((192, 128)).save(d / "foo.webp", quality=90)
    assert run_cli("images", d, "-y", "--no-log", env={"MO_IMG_TARGET": "webp"}).rc == 0
    assert not (d / "foo_1.webp").exists()


def test_what_cant_be_compared_stays_as_it_is(tmp_path, run_cli):
    d = tmp_path / "n5"
    d.mkdir()
    _picture(d / "foo.png", (10, 200, 90))
    (d / "foo.webp").write_text("not a picture")
    assert run_cli("images", d, "-y", "--no-log", env={"MO_IMG_TARGET": "webp"}).rc == 0
    assert sorted(p.name for p in d.iterdir()) == ["foo.png", "foo.webp"]
    assert (d / "foo.webp").read_text() == "not a picture"


def test_a_preview_names_the_numbered_output_and_writes_nothing(tmp_path, run_cli):
    d = tmp_path / "n6"
    d.mkdir()
    _picture(d / "foo.png", (10, 200, 90))
    _picture(d / "other.png", (200, 30, 30))
    (d / "foo.jxl").write_bytes(b"")
    r = run_cli("images", d, "-y", "--no-log", "-n")
    assert r.rc == 0 and "'foo.png' -> 'foo.jxl'" in str(r)


def test_a_clip_beside_a_different_picture_gets_converted(folder, tmp_path, run_cli):
    d = folder("n7", "foo.mp4=clip.mp4")
    _picture(d / "x.png", (200, 30, 30))
    (d / "x.png").rename(d / "foo.avif")          # decoded by content: a different picture
    r = run_cli("clips", d, "-y", "--no-log", "--no-delete")
    assert r.rc == 0, r
    assert (d / "foo_1.avif").exists()
    run_cli("clips", d, "-y", "--no-log", "--no-delete")
    assert not (d / "foo_2.avif").exists()


def test_a_gif_beside_a_different_webp_gets_converted(folder, tmp_path, run_cli):
    d = folder("n8", "foo.gif=anim.gif")
    _picture(d / "foo.webp", (200, 30, 30))
    assert run_cli("gifs", d, "-y", "--no-log", "--no-delete").rc == 0
    assert (d / "foo_1.webp").exists()
