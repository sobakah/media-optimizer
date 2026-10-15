"""JPEG/PNG -> JPEG XL, or WebP with img_target webp.

Per file:
  1. detect the real type from the content
  2. claim the target name (lock), then check again for an existing output
  3. encode into a temp file, copy PNG metadata into that temp file
  4. verify and size-check the temp file, which is now the final artefact
  5. move it into place without overwriting, then remove the original

INVARIANTS
  - The encoder settings follow the real type, never the extension. A PNG
    named .jpg would otherwise get the JPEG settings, which for cjxl means
    lossy.
  - JPEG is transcoded bit-exactly in the JXL path; png_mode only affects
    PNG. There is deliberately no pixel-based fallback for JPEGs cjxl cannot
    transcode: for a truncated JPEG it would "succeed" and replace the
    damaged original with an equally damaged copy.
  - With a target directory the source directory is never modified, not even
    to correct an extension.
  - Metadata goes into the temp file before verification, so the checks see
    the file exactly as it is kept. If exiftool fails, the original is kept.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .. import files, proc, safety, ui
from ..runlog import log
from ..ui import StyleUI as S
from .base import BatchStage, FileResult, Kind, verify_image

WEBP_METHOD = 6
TAKE_OVER = ("webp", "jxl", "gif")      # already efficient, only misnamed


def nonempty(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


class ImageStage(BatchStage):
    tag = "img"
    label = "image"
    title = "IMAGE SUMMARY"
    stats_name = ".img_stats.json"
    pending_name = ".img_pending_deletes.txt"
    leftovers = ["jxl", "webp"]
    cache_name = ".img_cache.txt"
    cache_settings = ("img_target", "img_webp_quality", "img_discard_if_larger", "png_mode", "png_quality",
                      "jxl_effort", "img_copy_metadata")

    def prepare(self) -> int | None:
        s = self.s
        missing = proc.missing("cjxl", "cwebp")
        if missing:
            ui.say("MISSING", S.RED, f"Required programs not found: {' '.join(missing)}", err=True)
            return 1
        if s.img_copy_metadata == "always" and not proc.which("exiftool"):
            ui.say("MISSING", S.RED, "img_copy_metadata always needs exiftool, which was not found.",
                   err=True)
            return 1
        self.want_metadata = s.img_copy_metadata != "never" and proc.which("exiftool") is not None
        # cjxl 0.11 Brotli-compresses the EXIF/XMP boxes, which many readers
        # cannot decode. Uncompressed only when this cjxl knows the option: an
        # unknown option would make every file fail. Captured, not piped into
        # grep -q: grep exiting early made the Bash check fail 3 times in 300.
        help_text = proc.run(["cjxl", "-h", "-v", "-v", "-v"], merge_stderr=True).stdout
        self.cjxl_meta = ["--compress_boxes=0"] if "--compress_boxes" in help_text else []
        if s.img_target == "webp":
            ui.say("IMAGES", S.CYAN, f"target: WebP (JPEG lossy at q{s.img_webp_quality}, PNG {s.png_mode})")
        if self.ctx.dry_run:
            ui.say("DRY-RUN", S.CYAN, "Nothing will be written or deleted.")
        return None

    def files(self) -> Iterator[Path]:
        return files.source_files(self.ctx.source, files.extension_matcher(["jpg", "jpeg", "png"]), self.s)

    # --- one file ---------------------------------------------------------------
    def handle(self, src: Path) -> FileResult:
        ctx, s = self.ctx, self.s
        stem = files.stem(src.name)
        ext = files.extension(src)
        folder = ctx.target_folder(src)
        dest = folder / f"{stem}.{s.img_target}"

        # Cheap check without the lock, for resumed runs. Only the configured
        # target format counts: an unrelated foo.webp must not stop foo.png
        # from becoming foo.jxl.
        if nonempty(dest):
            return FileResult(Kind.SKIP)
        if ctx.dry_run:
            ui.say("DRY-RUN", S.CYAN, f"'{src.name}' -> '{dest.name}'")
            return FileResult(Kind.DRY)

        real = files.real_type(src)
        orig = src.stat().st_size

        with safety.Claim(folder, stem, dest) as claim:
            if real in TAKE_OVER:
                return self._take_over(src, real, folder, stem)
            if real not in ("jpg", "png"):
                raise safety.Failed(f"not a JPEG or PNG ({files.describe(src)})")
            if real != ext and ctx.output is not None:
                ui.say("CORRECTED", S.MAGENTA,
                       f"'{src.name}' is actually {real}, converted as such, source unchanged")
            try:
                result = self._encode(src, real, dest, orig, claim)
            except safety.Failed as exc:
                result = self.fail(src, str(exc))
            except safety.Conflict as exc:
                result = self.conflict(src, str(exc))

        # In place, a kept original gets the extension that matches its
        # content. After the conversion, so the worker never follows a
        # renamed source. Not after a success with deletion: if the trash
        # failed, the original sits in the pending list under its current
        # name, and a rename would make that entry miss it.
        doomed = result.kind is Kind.SUCCESS and ctx.delete
        if real != ext and ctx.output is None and not doomed and src.exists():
            target = files.unique_name(src.parent, stem, real)
            try:
                safety.move_no_replace(src, target)
            except OSError:
                pass
            else:
                ui.say("CORRECTED", S.MAGENTA, f"'{src.name}' ({real}) -> '{target.name}'")
                log.file(self.tag, "CORRECTED", src, target.name)
        return result

    def _take_over(self, src: Path, real: str, folder: Path, stem: str) -> FileResult:
        """Already WebP, JXL or GIF under a wrong extension: nothing to
        convert. In place it gets the right extension; with a target
        directory a correctly named copy goes there, the source stays."""
        import filecmp
        import shutil

        if self.ctx.output is not None:
            # Taken over by an earlier run already: the same content under the
            # correct name. Without this check every run added another copy
            # (isgif.gif, isgif_1.gif, ...), in the Bash version too.
            same = folder / f"{stem}.{real}"
            if same.is_file() and filecmp.cmp(src, same, shallow=False):
                return FileResult(Kind.SKIP, cache="taken-over")
            target = files.unique_name(folder, stem, real)
            try:
                shutil.copy2(src, target)
            except OSError as exc:
                raise safety.Failed(f"could not copy to '{target}' ({exc.strerror})") from None
            ui.say("CORRECTED", S.MAGENTA, f"'{src.name}' is {real} -> target '{target.name}', source unchanged")
        else:
            target = files.unique_name(src.parent, stem, real)
            try:
                safety.move_no_replace(src, target)
            except OSError as exc:
                raise safety.Failed(f"could not rename to '{target.name}' ({exc.strerror})") from None
            ui.say("CORRECTED", S.MAGENTA, f"'{src.name}' is {real} -> '{target.name}'")
        log.file(self.tag, "CORRECTED", src, target.name)
        return FileResult(Kind.SKIP, cache="taken-over" if self.ctx.output is not None else None)

    def _unsupported(self, src: Path, reason: str) -> FileResult:
        """A known limitation, not an error: skipped with the reason, so it
        does not fail every run again."""
        log.file(self.tag, "UNSUPPORTED", src)
        ui.say("UNSUPPORTED", S.YELLOW, f"'{src.name}': {reason}, original kept.", err=True)
        return FileResult(Kind.SKIP, cache="unsupported")

    def _encode(self, src: Path, real: str, dest: Path, orig: int, claim: safety.Claim) -> FileResult:
        s = self.s
        tmp = claim.temp()
        if s.img_target == "webp":
            cmd = ["cwebp", "-quiet", "-m", str(WEBP_METHOD), "-metadata", "all"]
            if real == "png" and s.png_mode == "lossless":
                cmd += ["-lossless", "-z", "9"]
                label = "[PNG->WEBP-LOSSLESS]"
            else:
                # JPEG: lossless WebP would be larger than the source.
                cmd += ["-q", str(s.img_webp_quality)]
                label = f"[{real.upper()}->WEBP-q{s.img_webp_quality}]"
            cmd += [str(src), "-o", str(tmp)]
        else:
            cmd = ["cjxl", str(src), str(tmp), "-e", str(s.jxl_effort),
                   f"--num_threads={s.cjxl_threads}", "--quiet"]
            if real == "jpg":
                cmd.append("--lossless_jpeg=1")
                label = "[JXL-LOSSLESS]"
            elif s.png_mode == "lossy":
                cmd += ["-q", str(s.png_quality)]
                label = f"[PNG->JXL-q{s.png_quality}]"
            else:
                cmd += ["-d", "0"]
                label = "[PNG->JXL-LOSSLESS]"
            cmd += self.cjxl_meta

        if proc.run(cmd).ok and nonempty(tmp):
            return self._finish(src, real, tmp, dest, orig, label)
        tmp.unlink(missing_ok=True)

        if real == "jpg":
            # Known limitations of the encoders, measured: cjxl and cwebp
            # cannot read CMYK; cjxl cannot read arithmetic-coded JPEGs.
            # Anything else (truncated, damaged) stays a failure.
            info = files.jpeg_info(src)
            if info is not None and info.components == 4:
                return self._unsupported(src, f"CMYK JPEG, {cmd[0]} cannot convert it")
            if info is not None and info.arithmetic and cmd[0] == "cjxl":
                return self._unsupported(src, "arithmetic-coded JPEG, cjxl cannot convert it")
            raise safety.Failed("JPEG could not be decoded (damaged or an unsupported variant)")

        # A PNG cjxl cannot handle: lossless WebP including metadata.
        if s.img_target == "jxl":
            wdest = dest.with_suffix(".webp")
            if wdest.exists():
                # Most likely this fallback's own output from an earlier run
                # with the original kept: a CONFLICT on every run, after
                # another failed cjxl each time. Remembered instead.
                ui.say("SKIPPED", S.YELLOW, f"'{src.name}': JXL failed, '{wdest.name}' already exists, "
                                            "original kept.")
                log.file(self.tag, "SKIPPED", src)
                return FileResult(Kind.SKIP, cache="jxl-failed")
            ui.say("FALLBACK", S.YELLOW, f"'{src.name}' failed -> trying WebP...")
            wtmp = claim.temp(wdest)
            cmd = ["cwebp", "-quiet", "-lossless", "-z", "9", "-m", str(WEBP_METHOD), "-metadata", "all",
                   str(src), "-o", str(wtmp)]
            if proc.run(cmd).ok and nonempty(wtmp):
                return self._finish(src, real, wtmp, wdest, orig, "[PNG->WEBP]")
        raise safety.Failed("conversion failed")

    def _finish(self, src: Path, real: str, tmp: Path, dest: Path, orig: int, label: str) -> FileResult:
        # Metadata first, so verification and the size check see the file as
        # it will be kept. -all, not -all:all: the latter drops PNG text
        # chunks (Artist, Copyright). -m keeps a harmless "[minor]" warning
        # from turning into exit code 1.
        if real == "png" and self.want_metadata:
            res = proc.run(["exiftool", "-m", "-overwrite_original", "-quiet", "-quiet",
                            "-tagsFromFile", str(src), "-all", str(tmp)])
            if not res.ok:
                raise safety.Failed("could not copy metadata (exiftool)")

        if not verify_image(tmp, self.s.verify_deep):
            raise safety.Failed("output not verifiable")

        new = tmp.stat().st_size
        if self.s.img_discard_if_larger and orig > 0 and new >= orig:
            log.file(self.tag, "DISCARDED", src, "", orig, new)
            ui.say("DISCARDED", S.YELLOW, f"'{src.name}': result would be larger, original kept.")
            return FileResult(Kind.DISCARD, cache="discarded")

        safety.commit(src, tmp, dest)
        log.file(self.tag, "OK", src, dest.name, orig, new)
        ui.out(f"{S.GREEN}{label}{S.RESET} '{src.name}' -> '{dest.name}'")
        self.remove_original(src)
        return FileResult(Kind.SUCCESS, orig, new)
