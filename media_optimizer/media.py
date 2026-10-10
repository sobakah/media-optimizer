"""ffprobe/ffmpeg helpers: stream info, runtime check, picture comparison.

One ``ffprobe -of json`` call per file gives codec, pixel format, size,
bitrate, duration and audio at once; the Bash version needed five calls and
string splitting for that.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import proc


@dataclass
class MediaInfo:
    readable: bool = False
    duration: float | None = None
    video_codec: str | None = None
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    bit_rate: int = 0             # bits per second, video stream or container
    has_audio: bool = False
    streams: list = field(default_factory=list)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def main_video(self) -> int:
        """Index among the video streams of the one that carries the motion.
        An animated AVIF has two: a single still (the primary image) first,
        the animation second. Comparing the still found no frame at any
        sample time after 0 s, so the check measured nothing and passed."""
        videos = [s for s in self.streams if s.get("codec_type") == "video"]
        if len(videos) < 2:
            return 0
        frames = [int(s.get("nb_frames") or 0) if str(s.get("nb_frames") or "").isdigit() else 0
                  for s in videos]
        return frames.index(max(frames))


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN


def probe(path: Path) -> MediaInfo:
    res = proc.run(["ffprobe", "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", str(path)])
    if not res.ok:
        return MediaInfo()
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        return MediaInfo()
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    info = MediaInfo(readable=True, streams=streams)
    info.duration = _num(fmt.get("duration"))
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is not None:
        info.video_codec = video.get("codec_name")
        info.pix_fmt = video.get("pix_fmt") or ""
        info.width = int(video.get("width") or 0)
        info.height = int(video.get("height") or 0)
        rate = _num(video.get("bit_rate"))
        if rate is None:
            rate = _num(fmt.get("bit_rate"))
        info.bit_rate = int(rate or 0)
    info.has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return info


@functools.lru_cache(maxsize=None)
def encoders() -> str:
    """ffmpeg's encoder list, read once."""
    try:
        return proc.run(["ffmpeg", "-hide_banner", "-encoders"]).stdout
    except proc.ToolMissing:
        return ""


def has_encoder(name: str) -> bool:
    return f" {name} " in encoders()


# Measured with SVT-AV1 3.1.2 (Fedora 44): frames narrower than about 31 px
# (24 hung, 31 worked; the height does not matter) make libsvtav1 hang
# forever, and only SIGKILL ends it. libaom-av1 encodes them fine.
SVT_MIN_WIDTH = 32
# Safety net for the short-content AV1 encodes (GIFs, clips): a hang that is
# not known yet must not block a worker forever.
AV1_TIMEOUT = 600


def av1_encoder(width: int | None = None) -> tuple[str, list[str]] | None:
    """(encoder, speed arguments). libsvtav1 is considerably faster than
    libaom, but hangs on very narrow frames."""
    if has_encoder("libsvtav1") and (width is None or width >= SVT_MIN_WIDTH):
        return "libsvtav1", ["-preset", "6"]
    if has_encoder("libaom-av1"):
        return "libaom-av1", ["-cpu-used", "6"]
    return None


# --- runtime check -----------------------------------------------------------------
@dataclass
class DurationCheck:
    ok: bool
    source: float | None
    output: float | None


def verify_video(src: Path, dst: Path, tolerance_pct: float) -> DurationCheck:
    """The output must report a runtime, and match the source's within the
    tolerance. An unreadable source leaves only the output's readability."""
    try:
        if dst.stat().st_size == 0:
            return DurationCheck(False, None, None)
    except OSError:
        return DurationCheck(False, None, None)
    out = probe(dst).duration
    if out is None:
        return DurationCheck(False, None, None)
    source = probe(src).duration
    if source is None or source <= 0:
        return DurationCheck(True, source, out)
    return DurationCheck(abs(out - source) / source * 100 <= tolerance_pct, source, out)


# --- picture comparison ------------------------------------------------------------------
# One ffmpeg call per sample compares both files in memory. Seeking both inside
# a single process aligns the frames far better than writing stills first,
# which removed the false alarms variable frame rates used to produce.
# The BEST sample decides: a single bad measurement can come from a seek
# offset, a genuinely broken picture is bad everywhere. Measured separation:
# sound re-encodes 31-38 dB, CRF 51 still 24 dB, broken colour formats 9-12 dB.
# If this method changes, the threshold has to be measured again.
_PSNR = re.compile(r"average:(inf|\d+\.\d+)")      # inf: identical pictures
_FILTER = ("[0:v]select=eq(n\\,0),setpts=0[x];[1:v]select=eq(n\\,0),setpts=0[y];"
           "[x][y]scale2ref=flags=bilinear[a][b];[a][b]psnr")


@dataclass
class Visual:
    ok: bool                       # fine, or not measurable
    best: float | None = None      # best sample in dB
    samples: str = ""              # every sample, for the message

    @property
    def best_text(self) -> str:
        if self.best is None:
            return "n/a"
        return "inf" if self.best == float("inf") else f"{int(self.best)}"


def _filter(dst_stream: int, src_stream: int) -> str:
    return _FILTER.replace("[0:v]", f"[0:v:{dst_stream}]").replace("[1:v]", f"[1:v:{src_stream}]")


def _sample_times(duration: float, samples: int) -> list[str]:
    return [f"{duration * (100 * i // (samples + 1)) / 100:.2f}" for i in range(1, samples + 1)]


def verify_visual(src: Path, dst: Path, min_psnr: float, samples: int,
                  keep_dir: Path | None = None) -> Visual:
    """Not measurable at all (source without a runtime) passes. A known
    runtime where not a single sample could be compared does NOT pass: that
    is how the AVIF check silently compared nothing for every file."""
    src_info = probe(src)
    duration = src_info.duration
    if duration is None:
        return Visual(True)
    if dst.suffix.lower() == ".webp":
        return _verify_webp(src, dst, duration, min_psnr, samples, keep_dir)
    lavfi = _filter(probe(dst).main_video, src_info.main_video)
    best = None
    parts = []
    for t in _sample_times(duration, samples):
        res = proc.run(["ffmpeg", "-nostdin", "-hide_banner", "-noautorotate", "-ss", t, "-i", str(dst),
                        "-noautorotate", "-ss", t, "-i", str(src), "-lavfi", lavfi,
                        "-frames:v", "1", "-f", "null", "-"])
        match = _PSNR.search(res.stderr)
        if match:
            value = float(match.group(1))
            parts.append(f"{t}s={match.group(1) if value == float('inf') else int(value)} dB")
            best = value if best is None or value > best else best
        if keep_dir is not None:
            _keep_stills(src, dst, t, keep_dir)
    if best is None:
        return Visual(False, None, "no sample could be compared")
    return Visual(best >= min_psnr, best, ", ".join(parts))


# --- animated WebP --------------------------------------------------------------------------
# ffmpeg (8.1) cannot decode animated WebP: it skips the ANIM/ANMF chunks and
# finds no picture. Pillow decodes it through libwebp, including the blending
# of partial frames. The comparison runs in YCbCr with the planes weighted
# 4:1:1 like ffmpeg's 4:2:0 "average", so the same threshold applies.
# Measured with this method: correct WebPs 25-44 dB (quality 25-75, 640x360),
# 23.7 dB for a small 320x240 test pattern at quality 50; a WebP of different
# content 10-11 dB. The margin above 18 dB is smaller than for video, so a
# very low quality can be flagged as suspicious; the original is then kept.

def _webp_frame_at(path: Path, t: float):
    from PIL import Image

    with Image.open(path) as im:
        pos = 0.0
        for i in range(getattr(im, "n_frames", 1)):
            im.seek(i)
            im.load()                       # the frame duration is only known after load()
            duration = (im.info.get("duration") or 0) / 1000
            if pos + duration > t + 1e-6:
                return im.convert("RGB")
            pos += duration
        return im.convert("RGB")


def _psnr_ycbcr(a, b) -> float:
    import math

    from PIL import ImageChops, ImageStat

    mses = []
    for x, y in zip(a.convert("YCbCr").split(), b.convert("YCbCr").split()):
        rms = ImageStat.Stat(ImageChops.difference(x, y)).rms[0]
        mses.append(rms * rms)
    mse = (4 * mses[0] + mses[1] + mses[2]) / 6
    return float("inf") if mse == 0 else 10 * math.log10(255 * 255 / mse)


def _verify_webp(src: Path, dst: Path, duration: float, min_psnr: float, samples: int,
                 keep_dir: Path | None) -> Visual:
    import tempfile

    from PIL import Image

    best = None
    parts = []
    with tempfile.TemporaryDirectory(prefix="mo_webp_") as tmp:
        for t in _sample_times(duration, samples):
            still = Path(tmp) / f"src_{t}.png"
            proc.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-noautorotate", "-ss", t, "-i", str(src),
                      "-map", f"0:v:{probe(src).main_video}", "-frames:v", "1", str(still)])
            try:
                frame = _webp_frame_at(dst, float(t))
                with Image.open(still) as ref:
                    value = _psnr_ycbcr(frame, ref.convert("RGB").resize(frame.size))
            except (OSError, ValueError):
                continue
            parts.append(f"{t}s={'inf' if value == float('inf') else int(value)} dB")
            best = value if best is None or value > best else best
            if keep_dir is not None:
                try:
                    keep_dir.mkdir(parents=True, exist_ok=True)
                    frame.save(keep_dir / f"{dst.stem}_{t}s_target.png")
                    Image.open(still).save(keep_dir / f"{dst.stem}_{t}s_source.png")
                except OSError:
                    pass
    if best is None:
        return Visual(False, None, "no sample could be compared")
    return Visual(best >= min_psnr, best, ", ".join(parts))


def _keep_stills(src: Path, dst: Path, t: str, keep_dir: Path) -> None:
    try:
        keep_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    base = dst.stem
    for which, path in (("source", src), ("target", dst)):
        proc.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-noautorotate", "-ss", t, "-i", str(path),
                  "-map", f"0:v:{probe(path).main_video}", "-frames:v", "1",
                  str(keep_dir / f"{base}_{t}s_{which}.png")])
