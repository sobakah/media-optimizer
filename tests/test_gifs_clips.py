"""The GIF stage and the clip stage (short silent videos)."""

from __future__ import annotations

import json
import os
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
