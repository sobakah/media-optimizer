"""media-optimizer-webp: a helper next to the program, for animated WebPs.

    media-optimizer-webp FOLDER             report, change nothing
    media-optimizer-webp FOLDER --convert   WebPs that came from GIFs -> AVIF

It finds the animated WebPs that came from GIFs, turns those into animated
AVIF, and lists WebPs that are labelled animated but aren't. A stage like the
others (BatchStage): the same lock, temp names, trash, journal and cache.

WHICH WEBPS CAME FROM A GIF
  - "journal": a converted event of the gif stage has this file's size and
    time. Certain, and it survives a move or a rename.
  - "look": a run before the journal, or another tool. gif2webp leaves marks
    a GIF can't avoid (measured): every frame lossless, every duration a
    multiple of 10 ms (a GIF counts in hundredths), at most 256 colours per
    frame, each pixel fully opaque or fully transparent. The clip stage's
    WebPs are lossy and don't qualify.
  - "maybe": the timing fits, the rest doesn't prove it. In a real collection of
    27 000 WebPs: 8501 lossy ones at 70-100 ms a frame (a lossy conversion of
    a GIF leaves no other mark, but a video at 25 fps looks the same), and 23
    of 195 lossless ones above 256 colours (a GIF's local palettes add up).
    Listed, and converted only with --include-maybe.

LABELLED ANIMATED, BUT NOT
  The animation flag in the VP8X chunk is set, and there is no frame, one
  frame, or every frame shows the same picture. Only reported: nothing about
  such a file is wrong enough to touch it, and an AVIF of it would be a still.

INVARIANTS
  - Without --convert nothing is written (it is a dry run).
  - AV1 is lossy, a GIF's WebP is not, so a result has to pass: every frame
    there, the runtime right, the picture compared with the WebP
    (media.verify_from_webp). Otherwise the WebP stays and the result goes.
  - A WebP with transparency or metadata stays as it is: the AVIF command
    keeps neither. yuv420p flattens transparency, without any error.
  - A result that isn't smaller is discarded (discard_if_larger).
"""

from __future__ import annotations

import argparse
import json
import shlex
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import files, folders, media, proc, safety, ui
from .cli import install_signal_handlers
from .config import (NAME, RC_BAD_SETTING, RC_CANNOT_START, Settings, SettingsError, __version__,
                     default_journal_file, load_settings)
from .journal import journal
from .runlog import log
from .stages.base import BatchStage, Context, FileResult, Kind
from .stages.images import nonempty
from .ui import StyleUI as S

ANIMATION_FLAG = 0x02            # in the VP8X chunk's flags
MAX_GIF_COLOURS = 256
SHOWN = 30                       # of a list in the report, without --verbose


# --- reading a file --------------------------------------------------------------------
@dataclass
class Frame:
    duration: int                # milliseconds
    lossless: bool


@dataclass
class Container:
    """What the RIFF chunks say, without decoding a picture."""
    flagged: bool = False        # labelled animated
    frames: list[Frame] = field(default_factory=list)
    metadata: bool = False       # EXIF or XMP
    damaged: bool = False        # a chunk runs past the end of the file


def read_container(path: Path) -> Container | None:
    """None for a file that isn't a WebP."""
    with open(path, "rb") as fh:
        head = fh.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
            return None
        end = struct.unpack("<I", head[4:8])[0] + 8
        info = Container()
        pos = 12
        while pos + 8 <= end:
            fh.seek(pos)
            chunk = fh.read(8)
            if len(chunk) < 8:
                info.damaged = True
                break
            size = struct.unpack("<I", chunk[4:8])[0]
            if pos + 8 + size > end:
                info.damaged = True
                break
            kind = chunk[:4]
            if kind == b"VP8X":
                info.flagged = bool(fh.read(1)[0] & ANIMATION_FLAG) if size else False
            elif kind == b"ANMF":
                # 16 bytes of header, then VP8 or VP8L, in lossy frames maybe after an ALPH chunk.
                start = fh.read(min(size, 24))
                coded = start[16:20]
                if coded == b"ALPH":
                    alpha = int.from_bytes(start[20:24], "little")
                    fh.seek(pos + 8 + 24 + alpha + (alpha & 1))
                    coded = fh.read(4)
                info.frames.append(Frame(int.from_bytes(start[12:15], "little"), coded == b"VP8L"))
            elif kind in (b"EXIF", b"XMP "):
                info.metadata = True
            pos += 8 + size + (size & 1)
        return info


@dataclass
class Look:
    """What the decoded frames show."""
    identical: bool
    transparent: bool
    gif_pixels: bool             # few colours, no partial transparency


def look_at(path: Path) -> Look:
    from PIL import Image

    identical = gif_pixels = True
    transparent = False
    first = None
    with Image.open(path) as im:
        for i in range(getattr(im, "n_frames", 1)):
            im.seek(i)
            frame = im.convert("RGBA")
            data = frame.tobytes()
            first = data if first is None else first
            identical = identical and data == first
            low, high = frame.getchannel("A").getextrema()
            transparent = transparent or low < 255
            alpha = frame.getchannel("A").getcolors(256) or []
            gif_pixels = (gif_pixels and frame.getcolors(MAX_GIF_COLOURS) is not None
                          and all(value in (0, 255) for _, value in alpha))
    return Look(identical, transparent, gif_pixels)


def not_really_animated(info: Container, picture: Look | None) -> str:
    """Why a file labelled animated isn't; empty when it is."""
    if not info.flagged:
        return ""
    if not info.frames:
        return "no frames"
    if len(info.frames) == 1:
        return "one frame"
    if picture is not None and picture.identical:
        return f"{len(info.frames)} frames, all the same picture"
    return ""


def looks_like_a_gif(info: Container, picture: Look) -> bool:
    return (all(f.lossless for f in info.frames) and all(f.duration % 10 == 0 for f in info.frames)
            and picture.gif_pixels)


def gif_outputs(path: Path) -> set[tuple[int, int]]:
    """(size, mtime_ns) of every WebP the gif stage wrote. The journal's way
    of recognising a file: it holds after a move or a rename."""
    found: set[tuple[int, int]] = set()
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (event.get("event") == "converted" and event.get("stage") == "gif"
                        and str(event.get("dest", "")).lower().endswith(".webp")):
                    found.add((event.get("dest_size"), event.get("dest_mtime_ns")))
    except OSError:
        pass
    return found


# --- the stage -------------------------------------------------------------------------
class AnimatedWebpStage(BatchStage):
    tag = "webp-avif"
    label = "WebP"
    title = "WEBP TO AVIF SUMMARY"
    stats_name = ".webp_avif_stats.json"
    pending_name = ".webp_avif_pending_deletes.txt"
    leftovers = ["avif"]
    dry_noun = "WebP(s)"
    cache_name = ".webp_avif_cache.txt"
    cache_settings = ("gif_avif_crf", "discard_if_larger")

    def __init__(self, ctx: Context, *, known: set[tuple[int, int]], only_journal: bool = False,
                 include_maybe: bool = False, verbose: bool = False):
        super().__init__(ctx)
        self.known = known
        self.only_journal = only_journal
        self.include_maybe = include_maybe
        self.verbose = verbose
        self.from_gif: list[tuple[Path, str]] = []       # (file, "journal" or "look")
        self.maybe: list[tuple[Path, str]] = []          # the timing fits, nothing else proves it
        self.fake: list[tuple[Path, str]] = []           # labelled animated, but not
        self.kept: list[tuple[Path, str]] = []           # from a GIF, and stays a WebP
        self.damaged: list[Path] = []
        self.other = 0                                   # really animated, not from a GIF

    def prepare(self) -> int | None:
        missing = proc.missing("ffmpeg", "ffprobe")
        if missing:
            ui.say("MISSING", S.RED, f"Required programs not found: {' '.join(missing)}", err=True)
            return 1
        if not self.ctx.dry_run:
            if media.av1_encoder() is None:
                ui.say("MISSING", S.RED, "No AV1 encoder (libsvtav1 or libaom-av1) in ffmpeg.", err=True)
                return 1
            if " webp_anim " not in proc.run(["ffmpeg", "-hide_banner", "-decoders"]).stdout:
                ui.say("MISSING", S.RED, "This ffmpeg can't decode animated WebP (9.0 can).", err=True)
                return 1
        return None

    def files(self) -> Iterator[Path]:
        return files.source_files(self.ctx.source, files.extension_matcher(["webp"], exclude_parts=True), self.s)

    def handle(self, src: Path) -> FileResult:
        info = read_container(src)
        if info is None:
            return FileResult(Kind.SKIP)
        if info.damaged:
            self.damaged.append(src)
            return FileResult(Kind.SKIP)
        if not info.flagged:
            return FileResult(Kind.SKIP)
        picture = None
        if info.frames:
            try:
                picture = look_at(src)
            except (OSError, ValueError, EOFError):
                self.damaged.append(src)
                return FileResult(Kind.SKIP)
        reason = not_really_animated(info, picture)
        if reason:
            self.fake.append((src, reason))
            return FileResult(Kind.SKIP)

        st = src.stat()
        basis = "journal" if (st.st_size, st.st_mtime_ns) in self.known else \
            "look" if looks_like_a_gif(info, picture) else ""
        if basis:
            self.from_gif.append((src, basis))
            if basis != "journal" and self.only_journal:
                self.kept.append((src, "not in the journal"))
                return FileResult(Kind.SKIP)
        elif all(f.duration % 10 == 0 for f in info.frames):
            self.maybe.append((src, "many colours" if all(f.lossless for f in info.frames) else "lossy"))
            if not self.include_maybe:
                return FileResult(Kind.SKIP)
        else:
            self.other += 1
            return FileResult(Kind.SKIP)
        if picture.transparent:
            self.kept.append((src, "transparent"))
            return FileResult(Kind.SKIP)
        if info.metadata:
            self.kept.append((src, "has EXIF or XMP"))
            return FileResult(Kind.SKIP)
        dest = src.with_name(f"{files.stem(src.name)}.avif")
        if dest.exists() or dest.is_symlink():
            self.kept.append((src, f"{dest.name} exists"))
            return FileResult(Kind.SKIP)
        if self.ctx.dry_run:
            return FileResult(Kind.DRY)
        return self._convert(src, dest, info)

    def _encode(self, src: Path, tmp: Path, width: int) -> bool:
        encoder, speed = media.av1_encoder(width)
        # vfr: a GIF's frames rarely all last the same; the default fills them up to a fixed rate.
        return proc.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-fps_mode", "vfr",
                         "-c:v", encoder, "-crf", str(self.s.gif_avif_crf), *speed, "-pix_fmt", "yuv420p",
                         "-an", "-f", "avif", "-loop", "0", str(tmp)], timeout=media.AV1_TIMEOUT).ok

    def _check(self, src: Path, tmp: Path, info: Container) -> tuple[str, str]:
        """(what is wrong, the picture's dB). The frames and the runtime first:
        an AVIF of one still would compare well with the first frame."""
        result = media.probe(tmp)
        videos = [v for v in result.streams if v.get("codec_type") == "video"]
        count = videos[result.main_video].get("nb_frames") if videos else None
        if str(count) != str(len(info.frames)):
            return f"{count} frames instead of {len(info.frames)}", ""
        total = sum(f.duration for f in info.frames) / 1000
        if result.duration is None or abs(result.duration - total) > max(info.frames[-1].duration / 1000, 0.05) \
                + total * self.s.duration_tolerance_pct / 100:
            return f"runs {result.duration} s instead of {total:g} s", ""
        visual = media.verify_from_webp(src, tmp, len(info.frames), self.s.visual_psnr_min, self.s.visual_samples)
        if not visual.ok:
            return f"picture check: {visual.samples or 'nothing compared'}", ""
        return "", visual.best_text

    def _convert(self, src: Path, dest: Path, info: Container) -> FileResult:
        s = self.s
        orig = src.stat().st_size
        width = _canvas_width(src)
        if media.av1_encoder(width) is None:
            self.kept.append((src, f"narrower than {media.SVT_MIN_WIDTH} px, and no libaom-av1"))
            return FileResult(Kind.SKIP, cache="unsupported")
        with safety.Claim(src.parent, files.stem(src.name), dest) as claim:
            tmp = claim.temp()
            if not (self._encode(src, tmp, width) and nonempty(tmp)):
                raise safety.Failed("conversion failed")
            new = tmp.stat().st_size
            if s.discard_if_larger and new >= orig:
                log.file(self.tag, "DISCARDED", src, "", orig, new)
                ui.say("DISCARDED", S.YELLOW, f"'{src.name}': AVIF would be {ui.format_bytes(new - orig)} larger, "
                                              "WebP kept.")
                return FileResult(Kind.DISCARD, cache="discarded")
            problem, picture = self._check(src, tmp, info)
            if problem:
                raise safety.Failed(f"not kept, {problem}")
            before = journal.snapshot(src)
            safety.commit(src, tmp, dest)
        journal.converted(self.tag, src, before, dest, False)
        log.file(self.tag, "OK", src, dest.name, orig, new)
        pct = new * 100 // orig if orig else 0
        ui.out(f"{S.GREEN}[AVIF]{S.RESET} '{src.name}' -> '{dest.name}'  "
               f"({ui.format_bytes(new)}, {pct}%, picture {picture} dB)")
        self.remove_original(src, dest)
        return FileResult(Kind.SUCCESS, orig, new)

    def report(self) -> None:
        root = self.ctx.source

        def show(rows: list[tuple[Path, str]]) -> None:
            rows = sorted(rows)
            for path, note in rows if self.verbose else rows[:SHOWN]:
                ui.out(f"  {path.relative_to(root)}  {S.CYAN}({note}){S.RESET}")
            if not self.verbose and len(rows) > SHOWN:
                ui.out(f"  ... and {len(rows) - SHOWN} more (--verbose lists them)")

        by_journal = sum(1 for _, basis in self.from_gif if basis == "journal")
        ui.out()
        ui.say("FROM GIF", S.CYAN, f"{len(self.from_gif)} animated WebP(s) came from GIFs: {by_journal} by the "
                                   f"journal, {len(self.from_gif) - by_journal} by their look.")
        show(self.from_gif)
        if self.maybe:
            ui.say("MAYBE", S.CYAN, f"{len(self.maybe)} more have a GIF's timing, but nothing proves it "
                                    f"({'converted too' if self.include_maybe else '--include-maybe converts them'}):")
            show(self.maybe)
        if self.kept:
            ui.say("KEPT", S.YELLOW, f"{len(self.kept)} of them stay WebP:")
            show(self.kept)
        ui.say("NOT ANIMATED", S.YELLOW if self.fake else S.CYAN,
               f"{len(self.fake)} WebP(s) are labelled animated, but aren't:")
        show(self.fake)
        if self.damaged:
            ui.say("DAMAGED", S.RED, f"{len(self.damaged)} WebP(s) could not be read:")
            show([(p, "not read") for p in self.damaged])
        ui.out(f"{self.other} other animated WebP(s) don't have a GIF's timing and were left alone.")


def _canvas_width(path: Path) -> int | None:
    from PIL import Image

    with Image.open(path) as im:
        return im.width


# --- the command line ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{NAME}-webp", description="Find animated WebPs that came from GIFs and "
                                     "convert them to AVIF; list WebPs labelled animated that aren't. Without "
                                     "--convert nothing changes.")
    parser.add_argument("source", type=Path, help="folder to look through, with its subfolders")
    parser.add_argument("--convert", action="store_true", help="convert them; the WebPs go to the trash")
    parser.add_argument("--keep-webp", action="store_true", help="with --convert: keep the WebPs too")
    parser.add_argument("--only-journal", action="store_true",
                        help="with --convert: only WebPs the journal says came from a GIF")
    parser.add_argument("--include-maybe", action="store_true",
                        help="with --convert: also WebPs that only have a GIF's timing")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every file in the report")
    parser.add_argument("--crf", type=int, metavar="N", help="AV1 quality, lower is better (default: gif_avif_crf, 20)")
    parser.add_argument("--keep-larger", action="store_true", help="keep an AVIF that isn't smaller")
    parser.add_argument("-c", "--config", type=Path, metavar="FILE", help="use this config file")
    parser.add_argument("--version", action="version", version=f"{NAME} {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    install_signal_handlers()
    try:
        settings, _, _ = load_settings(args.config)
        if args.crf is not None:
            settings.gif_avif_crf = args.crf
        settings.discard_if_larger = settings.discard_if_larger and not args.keep_larger
        settings.delete_original = args.convert and not args.keep_webp
        settings.validate()
    except SettingsError as exc:
        for line in exc.messages:
            ui.out(f"{S.RED}{line}{S.RESET}", err=True)
        return RC_BAD_SETTING
    source = args.source.expanduser().absolute()
    if not source.is_dir():
        ui.out(f"Source directory not found: {source}", err=True)
        return RC_CANNOT_START
    folders.resolve(source, settings, assume_yes=True)
    return _run(args, settings, source)


def _run(args, s: Settings, source: Path) -> int:
    ctx = Context(s, source, None, dry_run=not args.convert, assume_yes=True)
    journal_path = Path(s.journal_file).expanduser() if isinstance(s.journal_file, str) else default_journal_file()
    stage = AnimatedWebpStage(ctx, known=gif_outputs(journal_path), only_journal=args.only_journal,
                              include_maybe=args.include_maybe, verbose=args.verbose)
    log.open(enabled=s.log, log_file=s.log_file, max_kb=s.log_max_kb, dry_run=ctx.dry_run,
             call=f"{NAME}-webp {shlex.join(sys.argv[1:])}", source=source, target=None, tag=stage.tag)
    journal.start(s.journal_file, dry_run=ctx.dry_run, source=source, target=None)
    rc = 1
    try:
        rc = stage.run()
        stage.report()
    finally:
        log.close(rc)
        journal.end(rc)
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
