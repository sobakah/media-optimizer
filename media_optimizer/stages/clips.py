"""Short, silent clips -> looping animations: AVIF, or WebP with clip_format webp.

The command is "clips", not "avif", since the format is a setting.

A video is selected only if all criteria match: codec in clip_source_codecs,
runtime below clip_max_seconds, no audio track (neither format can store
sound; clip_require_silent false converts anyway and drops the audio).

Which format, measured on Fedora 44 (glycin 2.1.5, the loader behind GNOME's
image viewer and the file manager's thumbnails): animated WebP, GIF and APNG
play; animated AVIF and animated JPEG XL show their first frame only.
Browsers play both. Sizes against an H.264 source of the same clip: AVIF
CRF 32 12-106 %, WebP quality 50 32-144 % (smaller only for noisy,
high-bitrate sources such as phone footage). So with discard_if_larger many
WebP results are discarded; --keep-larger keeps them for compatibility.

INVARIANTS
  - A result larger than the original gets exactly one more attempt (AVIF:
    CRF + avif_crf_retry, WebP: quality - clip_webp_quality_retry) before it
    is discarded.
  - The picture is compared with the source before the original is touched.
    A suspicious result is kept, but its original is never deleted.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Iterator

from .. import files, media, proc, safety, ui
from ..runlog import log
from ..ui import StyleUI as S
from .base import BatchStage, FileResult, Kind
from .images import nonempty


class ClipStage(BatchStage):
    tag = "clips"
    label = "Short clip"
    stats_name = ".clip_stats.json"
    pending_name = ".clip_pending_deletes.txt"
    leftovers = ["*.part.*.avif", "*.part.*.webp"]
    dry_noun = "video(s)"

    @property
    def ext(self) -> str:
        return self.s.clip_format

    @property
    def title(self) -> str:          # type: ignore[override]
        return f"{self.ext.upper()} SUMMARY"

    def prepare(self) -> int | None:
        s = self.s
        missing = proc.missing("ffmpeg", "ffprobe")
        if missing:
            ui.say("MISSING", S.RED, f"Required programs not found: {' '.join(missing)}", err=True)
            return 1
        if self.ext == "webp":
            if not media.has_encoder("libwebp_anim"):
                ui.say("MISSING", S.RED, "ffmpeg has no libwebp_anim encoder for clip_format webp.", err=True)
                return 1
            # ffmpeg cannot decode animated WebP; the picture check needs Pillow.
            if s.verify_visual and importlib.util.find_spec("PIL") is None:
                ui.say("MISSING", S.RED, "clip_format webp needs Pillow for the picture check "
                                         "(pip install Pillow), or --no-verify.", err=True)
                return 1
            ui.say("CLIPS", S.CYAN, f"format: animated WebP, quality {s.clip_webp_quality}")
        else:
            enc = media.av1_encoder()
            if enc is None:
                ui.say("MISSING", S.RED, "No AV1 encoder (libsvtav1 or libaom-av1) in ffmpeg.", err=True)
                return 1
            self.av1, self.av1_speed = enc
            if self.av1 == "libsvtav1":
                self.av1_speed = ["-preset", str(s.avif_preset)]
            ui.say("AVIF", S.CYAN, f"encoder: {self.av1}, CRF {s.avif_crf}, {s.avif_pix_fmt}")
        if self.ctx.dry_run:
            ui.say("DRY-RUN", S.CYAN, "Nothing will be written or deleted.")
        return None

    def files(self) -> Iterator[Path]:
        return files.find_files(self.ctx.source,
                                files.extension_matcher(self.s.video_extensions, exclude_parts=True),
                                include_hidden=self.s.include_hidden)

    def summary_rows(self) -> list:
        rows = super().summary_rows()
        if not any(r[0].startswith("Discarded") for r in rows):
            rows.insert(3, ("Discarded (larger):", f"{self.counters.discarded} file(s)"))
        return rows

    def _encode(self, src: Path, tmp: Path, level: int) -> bool:
        """level: CRF for AVIF, quality for WebP."""
        if self.ext == "webp":
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-c:v", "libwebp_anim",
                   "-quality", str(level), "-compression_level", "6", "-loop", "0", "-pix_fmt", "yuv420p",
                   "-an", "-f", "webp", str(tmp)]
        else:
            # -map_metadata 0 carries creation_time, title and location over;
            # without it the AVIF would lose every container tag.
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-c:v", self.av1,
                   "-crf", str(level), *self.av1_speed, "-pix_fmt", self.s.avif_pix_fmt, "-an",
                   "-map_metadata", "0", "-f", "avif", "-loop", "0", str(tmp)]
        return proc.run(cmd).ok and nonempty(tmp)

    def _retry_level(self, level: int) -> int:
        if self.ext == "webp":
            return max(1, level - self.s.clip_webp_quality_retry)
        return min(63, level + self.s.avif_crf_retry)

    def handle(self, src: Path) -> FileResult:
        ctx, s = self.ctx, self.s
        info = media.probe(src)
        if info.video_codec not in s.clip_source_codecs:
            return FileResult(Kind.SKIP)
        if info.duration is None or not info.duration < s.clip_max_seconds:
            return FileResult(Kind.SKIP)
        if s.clip_require_silent and info.has_audio:
            return FileResult(Kind.SKIP)

        stem = files.stem(src.name)
        folder = ctx.target_folder(src)
        dest = folder / f"{stem}.{self.ext}"
        if nonempty(dest):
            return FileResult(Kind.SKIP)
        if ctx.dry_run:
            ui.say("DRY-RUN", S.CYAN, f"'{src.name}' ({int(info.duration)}s, {info.video_codec}) -> '{dest.name}'")
            return FileResult(Kind.DRY)

        orig = src.stat().st_size
        with safety.Claim(folder, stem, dest) as claim:
            tmp = claim.temp()
            level = s.clip_webp_quality if self.ext == "webp" else s.avif_crf
            if not self._encode(src, tmp, level):
                raise safety.Failed("conversion failed")
            new = tmp.stat().st_size
            if s.discard_if_larger and new >= orig:
                # Too large: one more attempt at a lower quality before discarding.
                level = self._retry_level(level)
                what = f"quality {level}" if self.ext == "webp" else f"CRF {level}"
                ui.say("RETRY", S.YELLOW, f"'{src.name}': {ui.format_bytes(new)} too large, retrying with {what}")
                tmp.unlink(missing_ok=True)
                if not self._encode(src, tmp, level):
                    raise safety.Failed("conversion failed")
                new = tmp.stat().st_size
            if s.discard_if_larger and new >= orig:
                log.file(self.tag, "DISCARDED", src, "", orig, new)
                ui.say("DISCARDED", S.YELLOW, f"'{src.name}': {self.ext.upper()} would be "
                                              f"{ui.format_bytes(new - orig)} larger, original kept.")
                return FileResult(Kind.DISCARD)

            note = ""
            suspect = False
            if s.verify_visual:
                visual = media.verify_visual(src, tmp, s.visual_psnr_min, s.visual_samples)
                if visual.ok:
                    note = f", picture {visual.best_text} dB"
                else:
                    suspect = True
            safety.commit(src, tmp, dest)

        if suspect:
            ui.say("PICTURE?", S.YELLOW, f"'{src.name}': PSNR {visual.best_text} dB below "
                                         f"{s.visual_psnr_min:g} dB ({visual.samples or 'no samples'}). "
                                         "Kept, original kept.", err=True)
        log.file(self.tag, "OK", src, dest.name, orig, new)
        if not suspect:
            self.remove_original(src)
        pct = new * 100 // orig if orig else 0
        ui.out(f"{S.GREEN}[{self.ext.upper()}]{S.RESET} '{src.name}' -> '{dest.name}'  "
               f"({ui.format_bytes(new)}, {pct}%{note})")
        return FileResult(Kind.SUCCESS, orig, new, suspect=suspect)
