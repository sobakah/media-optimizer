"""The GIF stage and the clip stage (short silent videos)."""

from __future__ import annotations

import os

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
