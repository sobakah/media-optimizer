"""GIF -> animated WebP, or AVIF with gif_target avif.

INVARIANTS
  - gif2webp encodes losslessly by default. A "-lossless" flag does NOT
    exist; passing it made every call fail for months.
  - With gif_target avif ffmpeg encodes with an AV1 encoder; that path is
    lossy, unlike the WebP one.
  - Results larger than the original are discarded (discard_if_larger).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .. import files, media, proc, safety, ui
from ..journal import journal
from ..runlog import log
from ..ui import StyleUI as S
from .base import BatchStage, FileResult, Kind, verify_image
from .images import nonempty


class GifStage(BatchStage):
    tag = "gif"
    label = "GIF"
    title = "GIF SUMMARY"
    stats_name = ".gif_stats.json"
    pending_name = ".gif_pending_deletes.txt"
    leftovers = ["webp", "avif"]
    dry_noun = "GIF(s)"
    cache_name = ".gif_cache.txt"
    cache_settings = ("gif_target", "gif_method", "gif_kmin", "gif_avif_crf", "discard_if_larger")

    def prepare(self) -> int | None:
        s = self.s
        if s.gif_target == "avif":
            if proc.missing("ffmpeg"):
                ui.say("MISSING", S.RED, "Required programs not found: ffmpeg", err=True)
                return 1
            enc = media.av1_encoder()
            if enc is None:
                ui.say("MISSING", S.RED, "No AV1 encoder for gif_target avif.", err=True)
                return 1
            ui.say("GIF", S.CYAN, f"target: AVIF ({enc[0]}, CRF {s.gif_avif_crf}) - lossy!")
        elif proc.missing("gif2webp"):
            ui.say("MISSING", S.RED, "Required programs not found: gif2webp", err=True)
            return 1
        if self.ctx.dry_run:
            ui.say("DRY-RUN", S.CYAN, "Nothing will be written or deleted.")
        return None

    def files(self) -> Iterator[Path]:
        return files.source_files(self.ctx.source, files.extension_matcher(["gif"]), self.s)

    def summary_rows(self) -> list:
        rows = super().summary_rows()
        if not any(r[0].startswith("Discarded") for r in rows):     # always shown for GIFs
            rows.insert(3, ("Discarded (larger):", f"{self.counters.discarded} file(s)"))
        return rows

    def _encode(self, src: Path, tmp: Path) -> bool:
        s = self.s
        if s.gif_target == "avif":
            encoder, speed = media.av1_encoder(files.gif_width(src))
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-c:v", encoder,
                   "-crf", str(s.gif_avif_crf), *speed, "-pix_fmt", "yuv420p",
                   "-an", "-f", "avif", "-loop", "0", str(tmp)]
            return proc.run(cmd, timeout=media.AV1_TIMEOUT).ok
        cmd = ["gif2webp", "-m", str(s.gif_method), "-kmin", str(s.gif_kmin), "-metadata", "all",
               "-quiet", str(src), "-o", str(tmp)]
        return proc.run(cmd).ok

    def handle(self, src: Path) -> FileResult:
        ctx, s = self.ctx, self.s
        stem = files.stem(src.name)
        ext = "avif" if s.gif_target == "avif" else "webp"
        folder = ctx.target_folder(src)
        dest = folder / f"{stem}.{ext}"

        if nonempty(dest):
            return FileResult(Kind.SKIP)
        if ctx.dry_run:
            ui.say("DRY-RUN", S.CYAN, f"'{src.name}' -> '{dest.name}'")
            return FileResult(Kind.DRY)

        # A photo named .gif: ffmpeg would make a lossy one-frame AVIF of it
        # and the original would go. preflight is off by default.
        real = files.real_type(src)
        if real and real != "gif":
            log.file(self.tag, "UNSUPPORTED", src)
            ui.say("UNSUPPORTED", S.YELLOW, f"'{src.name}' is not a GIF but {real.upper()}, original kept "
                                            "(--preflight corrects the name).", err=True)
            return FileResult(Kind.SKIP, cache="not-a-gif")
        orig = src.stat().st_size
        if s.gif_target == "avif" and media.av1_encoder(files.gif_width(src)) is None:
            ui.say("UNSUPPORTED", S.YELLOW, f"'{src.name}': narrower than {media.SVT_MIN_WIDTH} px, which "
                                            "libsvtav1 cannot encode, and libaom-av1 is missing.", err=True)
            return FileResult(Kind.SKIP, cache="unsupported")
        with safety.Claim(folder, stem, dest) as claim:
            tmp = claim.temp()
            # verify_image checks WebP; for AVIF only the size.
            if not (self._encode(src, tmp) and nonempty(tmp) and verify_image(tmp, s.verify_deep)):
                raise safety.Failed("conversion failed")
            new = tmp.stat().st_size
            if s.discard_if_larger and new >= orig:
                log.file(self.tag, "DISCARDED", src, "", orig, new)
                ui.say("DISCARDED", S.YELLOW,
                       f"'{src.name}': {ext.upper()} would be {ui.format_bytes(new - orig)} larger, "
                       "original kept.")
                return FileResult(Kind.DISCARD, cache="discarded")
            before = journal.snapshot(src)
            safety.commit(src, tmp, dest)
        # gif2webp keeps every frame and its duration (measured); AV1 is lossy.
        journal.converted(self.tag, src, before, dest, s.gif_target == "webp")
        log.file(self.tag, "OK", src, dest.name, orig, new)
        ui.out(f"{S.GREEN}[OK]{S.RESET}   '{src.name}' -> '{dest.name}'")
        self.remove_original(src, dest)
        return FileResult(Kind.SUCCESS, orig, new)
