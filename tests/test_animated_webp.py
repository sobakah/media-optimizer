"""media-optimizer-webp: which WebPs came from GIFs, AVIF for those, and the
WebPs that are labelled animated but aren't. Real files, real encoders."""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest
from PIL import Image, ImageDraw

from conftest import check_journal, leftovers, snapshot
from media_optimizer import animated_webp as tool


def _frames(count=12, mode="RGB", size=(121, 91), noise=False):
    out = []
    for i in range(count):
        im = Image.new(mode, size, (0, 0, 0, 0) if mode == "RGBA" else (30, 60, 90))
        ImageDraw.Draw(im).ellipse((i * 7, 10, i * 7 + 40, 50), fill=(240, 200, 30))
        if noise:
            im = Image.blend(im.convert("RGB"), Image.effect_noise(size, 90).convert("RGB"), 0.4)
        out.append(im)
    return out


def _gif(path, frames, duration=(70, 70, 130), **kw):
    # A palette per image, as a GIF has; saved through gif2webp, as the gif stage does.
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[duration[i % len(duration)]
                   for i in range(len(frames))], loop=0, **kw)


def from_gif(folder_path, name, frames=None, **kw):
    """A WebP as the gif stage writes it, and only that in the folder."""
    work = folder_path.parent / f"{name}.work"
    work.mkdir(exist_ok=True)
    gif = work / f"{name}.gif"
    _gif(gif, frames or _frames(noise=True), **kw)
    dest = folder_path / f"{name}.webp"
    subprocess.run(["gif2webp", "-m", "6", "-kmin", "0", "-quiet", str(gif), "-o", str(dest)], check=True)
    return dest


def lossy_clip(path):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=1:s=120x90:r=10",
                    "-c:v", "libwebp_anim", "-quality", "50", "-loop", "0", "-pix_fmt", "yuv420p", "-f", "webp",
                    str(path)], check=True)


def ui_plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


@pytest.fixture
def d(tmp_path):
    path = tmp_path / "pics"
    path.mkdir()
    return path


@pytest.fixture
def run(monkeypatch, capsys):
    def go(*args, force=True):
        if force:
            monkeypatch.setenv("MO_FORCE_DELETE", "true")
        rc = tool.main([str(a) for a in args])
        captured = capsys.readouterr()
        return rc, captured.out + captured.err
    return go


# --- reading the container ---------------------------------------------------------
def test_the_container_of_a_gif_webp(d):
    info = tool.read_container(from_gif(d, "a"))
    assert info.flagged and len(info.frames) == 12 and not info.metadata and not info.damaged
    assert all(f.lossless for f in info.frames)
    assert [f.duration for f in info.frames[:3]] == [70, 70, 130]


def test_the_container_of_a_lossy_clip(d):
    lossy_clip(d / "clip.webp")
    info = tool.read_container(d / "clip.webp")
    assert info.flagged and len(info.frames) == 10 and not any(f.lossless for f in info.frames)


def test_a_lossless_frame_behind_an_alpha_chunk_counts_as_lossy_only_when_it_is(d):
    """An ALPH chunk comes before a lossy frame's VP8; its size has to be skipped right."""
    frames = _frames(4, "RGBA", noise=False)
    frames[0].save(d / "x.webp", save_all=True, append_images=frames[1:], duration=100, lossless=False, quality=60)
    info = tool.read_container(d / "x.webp")
    assert len(info.frames) == 4 and not any(f.lossless for f in info.frames)


def test_not_a_webp_and_a_cut_webp(d):
    (d / "no.webp").write_bytes(b"not a webp at all")
    assert tool.read_container(d / "no.webp") is None
    good = from_gif(d, "a").read_bytes()
    (d / "cut.webp").write_bytes(good[:len(good) // 2])
    assert tool.read_container(d / "cut.webp").damaged


# --- the report --------------------------------------------------------------------
def test_the_report_changes_nothing_and_tells_them_apart(d, run):
    from_gif(d, "gif")
    lossy_clip(d / "clip.webp")
    _frames(1)[0].save(d / "still.webp", lossless=True)
    before = snapshot(d)
    rc, out = run(d)
    assert rc == 0 and snapshot(d) == before, out
    assert "1 animated WebP(s) came from GIFs: 0 by the journal, 1 by their look." in out
    assert "gif.webp" in out and "still.webp" not in out
    assert "1 more have a GIF's timing" in out and "clip.webp" in out and "--include-maybe" in out
    assert not list(d.parent.glob("pics/.webp_avif*"))


def test_more_colours_than_a_gif_has_is_not_a_gif(d, run):
    frames = [Image.effect_noise((120, 90), 80).convert("RGB").resize((120, 90)) for _ in range(3)]
    rich = [Image.merge("RGB", (f.getchannel(0), Image.effect_noise((120, 90), 60), Image.effect_noise((120, 90), 40)))
            for f in frames]
    rich[0].save(d / "rich.webp", save_all=True, append_images=rich[1:], duration=100, lossless=True)
    rc, out = run(d)
    assert rc == 0 and "0 animated WebP(s) came from GIFs" in out and "1 more have a GIF's timing" in out, out
    assert "(many colours)" in ui_plain(out)


def test_timing_a_gif_cant_have_is_not_a_gif(d, run):
    frames = _frames(4)
    frames[0].save(d / "ms.webp", save_all=True, append_images=frames[1:], duration=33, lossless=True)
    rc, out = run(d)
    assert rc == 0 and "0 animated WebP(s) came from GIFs" in out and "1 other animated" in out, out


def test_maybes_are_converted_only_when_asked(d, run):
    lossy_clip(d / "clip.webp")
    rc, out = run(d, "--convert", "--keep-larger")
    assert rc == 0 and (d / "clip.webp").exists() and not (d / "clip.avif").exists(), out
    rc, out = run(d, "--convert", "--keep-larger", "--include-maybe")
    assert rc == 0 and not (d / "clip.webp").exists() and (d / "clip.avif").exists(), out


def test_the_journal_decides_also_after_a_rename(d, tmp_path, run, monkeypatch):
    """Lossy: nothing about it looks like a GIF, but the journal knows it."""
    moved = d / "renamed.webp"
    lossy_clip(moved)
    st = moved.stat()
    journal_file = tmp_path / "other-journal.jsonl"
    monkeypatch.setenv("MO_JOURNAL_FILE", str(journal_file))
    journal_file.write_text(json.dumps({"event": "converted", "stage": "gif", "dest": "/old/place/a.webp",
                                        "dest_size": st.st_size, "dest_mtime_ns": st.st_mtime_ns}) + "\n{broken\n")
    rc, out = run(d)
    assert rc == 0 and "1 by the journal" in out and "renamed.webp" in out, out


def _chunk(kind: bytes, data: bytes) -> bytes:
    return kind + len(data).to_bytes(4, "little") + data + b"\0" * (len(data) & 1)


def labelled_animated(path, frames: int, size=(40, 30)):
    """A WebP with the animation flag and these frames, all the same picture.
    Encoders refuse to write this; other tools do."""
    still = path.with_suffix(".still")
    Image.new("RGB", size, (200, 40, 40)).save(still, "WEBP", lossless=True)
    coded = still.read_bytes()[12:]
    still.unlink()
    w, h = (size[0] - 1).to_bytes(3, "little"), (size[1] - 1).to_bytes(3, "little")
    frame = _chunk(b"ANMF", b"\0" * 6 + w + h + (100).to_bytes(3, "little") + b"\0" + coded)
    body = b"WEBP" + _chunk(b"VP8X", bytes([tool.ANIMATION_FLAG, 0, 0, 0]) + w + h) \
        + _chunk(b"ANIM", b"\0" * 6) + frame * frames
    path.write_bytes(b"RIFF" + len(body).to_bytes(4, "little") + body)


@pytest.mark.parametrize("frames, why", [(0, "no frames"), (1, "one frame"), (5, "5 frames, all the same picture")])
def test_labelled_animated_but_not(d, run, frames, why):
    labelled_animated(d / "fake.webp", frames)
    info = tool.read_container(d / "fake.webp")
    assert info.flagged and len(info.frames) == frames
    before = snapshot(d)
    rc, out = run(d, "--convert")
    assert rc == 0 and snapshot(d) == before, out
    assert "1 WebP(s) are labelled animated, but aren't:" in out and f"fake.webp  ({why})" in ui_plain(out), out


def test_a_photo_named_webp_and_a_cut_one_are_left_alone(d, run):
    (d / "no.webp").write_bytes(b"not a webp at all")
    good = from_gif(d, "a")
    data = good.read_bytes()
    (d / "cut.webp").write_bytes(data[:len(data) // 2])
    good.unlink()
    before = snapshot(d)
    rc, out = run(d, "--convert")
    assert rc == 0 and snapshot(d) == before, out
    assert "1 WebP(s) could not be read" in out and "cut.webp" in out


# --- converting --------------------------------------------------------------------
def test_convert_replaces_the_webp_and_journals_it(d, tmp_path, run):
    webp = from_gif(d, "a")
    stamp = 1_700_000_000_000_000_000
    os.utime(webp, ns=(stamp, stamp))
    rc, out = run(d, "--convert", "--keep-larger")
    dest = d / "a.avif"
    assert rc == 0 and dest.is_file() and not webp.exists(), out
    assert dest.stat().st_mtime_ns == stamp, "the time goes along"
    assert not leftovers(d) and "picture" in out
    events = check_journal(tmp_path / "state" / "journal.jsonl")
    converted = next(e for e in events if e["event"] == "converted")
    assert converted["stage"] == "webp-avif" and converted["lossless"] is False
    assert any(e["event"] == "removed" and e["reason"] == "converted" for e in events)


def test_the_animation_survives_with_its_timing(d, run):
    from media_optimizer import media
    webp = from_gif(d, "a")
    total = sum(f.duration for f in tool.read_container(webp).frames) / 1000
    rc, out = run(d, "--convert", "--keep-larger", "--keep-webp")
    assert rc == 0 and webp.exists() and (d / "a.avif").exists(), out
    info = media.probe(d / "a.avif")
    videos = [s for s in info.streams if s.get("codec_type") == "video"]
    assert videos[info.main_video]["nb_frames"] == "12"
    assert abs(info.duration - total) < 0.15


def test_keep_webp_journals_no_removal(d, tmp_path, run):
    webp = from_gif(d, "a")
    rc, out = run(d, "--convert", "--keep-larger", "--keep-webp")
    assert rc == 0 and webp.exists(), out
    assert not any(e["event"] == "removed" for e in check_journal(tmp_path / "state" / "journal.jsonl"))


def test_a_narrow_animation_does_not_hang(d, run):
    """libsvtav1 hangs below 31 px; the WebP's canvas picks the encoder."""
    frames = [Image.effect_noise((16, 40), 60 + i * 9).convert("RGB") for i in range(6)]
    from_gif(d, "thin", frames, duration=(100,))
    rc, out = run(d, "--convert", "--keep-larger")
    assert rc == 0 and (d / "thin.avif").exists(), out


def test_a_larger_result_is_discarded_and_remembered(d, run, monkeypatch):
    webp = from_gif(d, "a")
    real = tool.AnimatedWebpStage._encode

    def grown(self, src, tmp, width):
        ok = real(self, src, tmp, width)
        with open(tmp, "ab") as fh:
            fh.write(b"\0" * (webp.stat().st_size + 1))
        return ok
    monkeypatch.setattr(tool.AnimatedWebpStage, "_encode", grown)
    rc, out = run(d, "--convert")
    assert rc == 0 and webp.exists() and not (d / "a.avif").exists() and not leftovers(d), out
    assert "[DISCARDED]" in out and (d / ".webp_avif_cache.txt").is_file()
    rc, out = run(d, "--convert")
    assert rc == 0 and "[DISCARDED]" not in out, out


def test_transparency_stays_a_webp(d, run):
    """yuv420p flattens it without an error: the WebP may not go for that."""
    frames = _frames(6, "RGBA")
    webp = from_gif(d, "clear", frames, disposal=2)
    before = snapshot(d)
    rc, out = run(d, "--convert", "--keep-larger")
    assert rc == 0 and snapshot(d) == before and "(transparent)" in out and webp.exists(), out


def test_metadata_stays_a_webp(d, run):
    webp = from_gif(d, "meta")
    xmp = d.parent / "x.xmp"
    xmp.write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>")
    subprocess.run(["webpmux", "-set", "xmp", str(xmp), str(webp), "-o", str(webp)], check=True)
    assert tool.read_container(webp).metadata
    rc, out = run(d, "--convert", "--keep-larger")
    assert rc == 0 and webp.exists() and "has EXIF or XMP" in out, out


def test_an_existing_avif_is_never_overwritten(d, run):
    webp = from_gif(d, "a")
    (d / "a.avif").write_text("MINE")
    rc, out = run(d, "--convert", "--keep-larger")
    assert rc == 0 and webp.exists() and (d / "a.avif").read_text() == "MINE", out
    assert "a.avif exists" in out


def test_only_journal_leaves_lookalikes_alone(d, run):
    webp = from_gif(d, "a")
    rc, out = run(d, "--convert", "--only-journal", "--keep-larger")
    assert rc == 0 and webp.exists() and not (d / "a.avif").exists() and "not in the journal" in out, out


def test_a_picture_that_does_not_match_keeps_the_webp(d, run, monkeypatch):
    """Broken on purpose: the check's answer decides whether the WebP goes."""
    webp = from_gif(d, "a")
    monkeypatch.setattr(tool.media, "verify_from_webp",
                        lambda *a, **k: tool.media.Visual(False, 7.0, "1s=7 dB"))
    rc, out = run(d, "--convert", "--keep-larger")
    assert rc == 3 and webp.exists() and not (d / "a.avif").exists() and not leftovers(d), out
    assert "picture check: 1s=7 dB" in out


def test_a_picture_check_that_measured_nothing_fails(d, tmp_path):
    webp = from_gif(d, "a")
    (d / "empty.avif").write_bytes(b"")
    from media_optimizer import media
    result = media.verify_from_webp(webp, d / "empty.avif", 12, 18, 3)
    assert not result.ok and result.samples == "no sample could be compared"


def test_a_real_comparison_tells_right_from_wrong(d, run):
    from media_optimizer import media
    webp = from_gif(d, "a")
    rc, out = run(d, "--convert", "--keep-larger", "--keep-webp")
    assert rc == 0, out
    right = media.verify_from_webp(webp, d / "a.avif", 12, 18, 3)
    other = from_gif(d, "b", [Image.effect_noise((121, 91), 120 + i).convert("RGB") for i in range(12)])
    wrong = media.verify_from_webp(other, d / "a.avif", 12, 18, 3)
    assert right.ok and right.best > 18
    assert wrong.best < right.best


def test_a_still_as_the_result_keeps_the_webp(d, run, monkeypatch):
    """An AVIF with only the first frame compares well: the frames are counted first."""
    webp = from_gif(d, "a")
    real = tool.AnimatedWebpStage._encode

    def one_frame(self, src, tmp, width):
        ok = real(self, src, tmp, width)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-c:v",
                        "libsvtav1", "-f", "avif", str(tmp)], check=True, capture_output=True)
        return ok
    monkeypatch.setattr(tool.AnimatedWebpStage, "_encode", one_frame)
    rc, out = run(d, "--convert", "--keep-larger")
    assert rc == 3 and webp.exists() and not (d / "a.avif").exists(), out
    assert "frames instead of 12" in out


def test_a_bad_quality_is_refused(d, run):
    rc, out = run(d, "--crf", "99")
    assert rc == 2 and "gif_avif_crf" in out


def test_a_missing_folder(d, run):
    rc, out = run(d / "nowhere")
    assert rc == 1 and "not found" in out
