"""Check a target directory against the originals.

Every video output is checked in three steps; the first mismatch wins:
  UNREADABLE  ffprobe finds no video stream
  DURATION    runtime deviates beyond duration_tolerance_pct
  PICTURE?    the best PSNR sample stays below visual_psnr_min

Outputs are matched to originals by relative path (the video stage mirrors
the folder structure and keeps the name), falling back to the base name,
since the container may differ (an .avi becomes an .mkv).

A low PSNR is a suspicion, not proof. Without --fix nothing is changed.
--fix deletes the broken outputs and removes their originals from every
video cache in the target tree, so the next run regenerates exactly those;
--run starts that run right away with the list of broken originals.
"""

from __future__ import annotations

import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .. import cache, files, media, proc, safety, ui
from ..config import RC_INTERRUPTED, RC_OK
from ..runlog import log
from ..ui import StyleUI as S
from .base import Context
from .video import BROKEN_LIST, CACHE_NAME, VideoStage, migrate_name

BAD = ("UNREADABLE", "DURATION", "VISUAL")


@dataclass
class Check:
    status: str           # OK, NOSRC, UNREADABLE, DURATION, VISUAL
    out: Path
    src: Path | None


class VerifyStage:
    def __init__(self, ctx: Context, *, keep_dir: Path | None = None, fix: bool = False,
                 run_repair: bool = False):
        self.ctx = ctx
        self.s = ctx.settings
        self.keep_dir = keep_dir
        self.fix = fix
        self.run_repair = run_repair
        assert ctx.output is not None
        self.output: Path = ctx.output

    def _original(self, out: Path) -> Path | None:
        rel = out.relative_to(self.output)
        src = self.ctx.source / rel
        if src.is_file():
            return src
        # Only videos: a photo or subtitle of the same name (IMG_1234.jpg next
        # to IMG_1234.mp4) reports a runtime of 0.04 s, so the output failed
        # as DURATION and --fix deleted it, possibly the only copy left.
        folder = src.parent
        stem = files.stem(out.name)
        is_video = files.extension_matcher(self.s.video_extensions, exclude_parts=True)
        try:
            candidates = sorted(p for p in folder.iterdir()
                                if p.is_file() and files.stem(p.name) == stem and is_video(p.name))
        except OSError:
            return None
        return candidates[0] if candidates else None

    def _check(self, out: Path) -> Check:
        s = self.s
        rel = out.relative_to(self.output)
        src = self._original(out)
        if src is None:
            return Check("NOSRC", out, None)
        if media.probe(out).video_codec is None:
            ui.say("UNREADABLE", S.RED, str(rel), err=True)
            return Check("UNREADABLE", out, src)
        duration = media.verify_video(src, out, s.duration_tolerance_pct)
        if not duration.ok:
            src_d = "?" if duration.source is None else f"{duration.source:.2f}"
            out_d = "?" if duration.output is None else f"{duration.output:.2f}"
            ui.say("DURATION", S.RED, f"   {rel}  (original {src_d}s, output {out_d}s, tolerance "
                                      f"{s.duration_tolerance_pct:g}%)", err=True)
            return Check("DURATION", out, src)
        if s.check_visual:
            # Mirrored below keep_dir: a/v.mp4 and b/v.mp4 overwrote each other's stills.
            keep = self.keep_dir / rel.parent if self.keep_dir is not None else None
            visual = media.verify_visual(src, out, s.visual_psnr_min, s.visual_samples, keep)
            if not visual.ok:
                ui.say("PICTURE?", S.YELLOW, f"    {rel}  (best {visual.best_text} dB; samples: "
                                             f"{visual.samples or 'none'})", err=True)
                return Check("VISUAL", out, src)
        ui.out(f"{S.GREEN}[OK]{S.RESET}       {rel}")
        return Check("OK", out, src)

    def run(self) -> int:
        missing = proc.missing("ffmpeg", "ffprobe")
        if missing:
            ui.say("MISSING", S.RED, f"Required programs not found: {' '.join(missing)}", err=True)
            return 1
        if not self.fix:
            return self._run()
        # --fix deletes outputs and rewrites caches: not while a run writes
        # to the same target. Taken before the check, so a busy target does
        # not cost a full check first.
        self.lock = safety.RunLock(self.output)
        try:
            self.lock.acquire()
        except safety.Busy as exc:
            ui.say("BUSY", S.RED, str(exc), err=True)
            return 1
        try:
            return self._run()
        finally:
            self.lock.release()

    def _run(self) -> int:
        s, ctx = self.s, self.ctx
        if not s.check_visual and self.keep_dir is not None:
            ui.say("NOTE", S.YELLOW, "--keep-samples has no effect without the picture comparison.", err=True)
            self.keep_dir = None
        migrate_name(self.output / ".defekte_videos.txt", self.output / BROKEN_LIST, False)
        broken_list = self.output / BROKEN_LIST

        ui.banner("OUTPUT CHECK (VIDEO)")
        picture = "off"
        if s.check_visual:
            picture = f"{s.visual_psnr_min:g} dB, {s.visual_samples} samples"
            if self.keep_dir:
                picture += f", stills -> {self.keep_dir}"
        ui.out(f"  Source: {ctx.source}\n  Target: {self.output}\n"
               f"  picture check: {picture}, runtime tolerance {s.duration_tolerance_pct:g}%, "
               f"{s.workers()} workers\n")

        match = files.extension_matcher(s.video_extensions, exclude_parts=True)
        outputs = list(files.find_files(self.output, match, include_hidden=s.include_hidden))
        if not outputs:
            ui.out("No matching video files found in the target directory.")
            return RC_OK
        ui.out(f"  checking {len(outputs)} file(s)...\n")

        results: list[Check] = []
        pool = ThreadPoolExecutor(max_workers=s.workers(), thread_name_prefix="verify")
        futures = [pool.submit(self._check, out) for out in outputs]
        try:
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except proc.Stopped:
                    pass
        except KeyboardInterrupt:
            with proc.shielded():
                ui.out()
                ui.say("ABORT", S.RED, "check stopped, nothing was changed.")
                for fut in futures:
                    fut.cancel()
                proc.stop_all()
                pool.shutdown(wait=True)
            return RC_INTERRUPTED
        pool.shutdown(wait=True)

        count = {k: sum(1 for r in results if r.status == k) for k in ("OK", "VISUAL", "DURATION",
                                                                     "UNREADABLE", "NOSRC")}
        ui.box("RESULT", [("Fine:", f"{count['OK']} file(s)"),
                          ("Picture suspicious:", f"{count['VISUAL']} file(s)"),
                          ("Runtime mismatch:", f"{count['DURATION']} file(s)"),
                          ("Unreadable:", f"{count['UNREADABLE']} file(s)"),
                          ("No original:", f"{count['NOSRC']} file(s)")], S.BLUE)
        if count["NOSRC"]:
            ui.out()
            ui.say("NOTE", S.YELLOW, f"{count['NOSRC']} output(s) without a matching original. They are left")
            ui.out("          untouched, since there is nothing to compare or regenerate.")

        bad = [r for r in results if r.status in BAD]
        if not bad:
            ui.out()
            ui.say("DONE", S.GREEN, "No broken files found.")
            broken_list.unlink(missing_ok=True)
            return RC_OK

        originals = sorted({str(r.src) for r in bad if r.src is not None})
        broken_list.write_text("".join(f"{p}\n" for p in originals), encoding="utf-8",
                               errors="surrogateescape")
        ui.out(f"\n{S.BOLD}{len(bad)} broken file(s).{S.RESET} List of affected originals: {broken_list}")

        if not self.fix:
            ui.out()
            ui.say("IMPORTANT", S.YELLOW, "A low PSNR is a suspicion, not proof. First play back a few")
            ui.out("          of the reported files yourself. Store the compared stills\n"
                   "          with: --keep-samples /tmp/samples\n\n"
                   "Nothing changed. Repair only after checking yourself:\n"
                   f"  media-optimizer verify {_q(ctx.source)} {_q(self.output)} --fix --run")
            return 1            # findings, so a caller can react; not an error

        return self._repair(bad, set(originals), broken_list)

    def _repair(self, bad: list[Check], originals: set[str], broken_list: Path) -> int:
        ctx = self.ctx
        # An older Bash version kept the cache in the source directory and the
        # video stage copies it over once. Copy it first, otherwise that later
        # copy would bring back the entries pruned now.
        cache_file = self.output / CACHE_NAME
        legacy = ctx.source / CACHE_NAME
        if not cache_file.exists() and legacy.is_file():
            try:
                shutil.copy2(legacy, cache_file)
            except OSError:
                pass

        removed = 0
        for r in bad:
            try:
                r.out.unlink()
            except OSError:
                continue
            removed += 1
            ui.say("DELETED", S.CYAN, str(r.out.relative_to(self.output)))

        # The video stage takes over the caches of earlier runs on subfolders
        # on every run. Prune those too, or the entries removed here would
        # come back from there.
        caches = [cache_file] if cache_file.is_file() else []
        caches += files.find_state_files(self.output, CACHE_NAME, include_hidden=self.s.include_hidden)
        pruned = 0
        for path in caches:
            pruned += cache.prune(path, originals)       # either format, keeps a .bak
        if caches:
            ui.out(f"\n  {removed} output(s) deleted, {pruned} cache entry/entries removed.")
            if len(caches) == 1:
                ui.out(f"  Cache backup: {caches[0]}.bak")
            else:
                ui.out(f"  Cache backups: {caches[0]}.bak (and {len(caches) - 1} more next to each cache)")
        else:
            ui.out(f"\n  {removed} output(s) deleted. No cache present.")
        log.line("verify", f"fix: {removed} output(s) deleted, {pruned} cache entries removed")

        if not self.run_repair:
            ui.out("\nRegenerate now with:\n"
                   f"  media-optimizer video {_q(ctx.source)} {_q(self.output)} --from-list {_q(broken_list)}")
            return RC_OK

        ui.out(f"\n{S.CYAN}▶ Regenerating {len(originals)} file(s) from the list...{S.RESET}")
        self.lock.release()                 # the video stage takes it itself
        # --from-list instead of a full rerun: only what was reported as
        # broken is repeated, no scan over unrelated files.
        rc = VideoStage(ctx, from_list=str(broken_list)).run()
        if rc == RC_INTERRUPTED:
            return rc
        # Archive the list instead of leaving it: a later --from-list auto
        # would otherwise process a stale selection.
        archive = self.output / f".broken_videos.done-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        try:
            broken_list.rename(archive)
            ui.out(f"\n  List of broken videos archived as {archive.name}")
        except OSError:
            pass
        ui.out(f"\n{S.BOLD}Regeneration finished. Check again with:{S.RESET}\n"
               f"  media-optimizer verify {_q(ctx.source)} {_q(self.output)}")
        return rc


def _q(path) -> str:
    import shlex
    return shlex.quote(str(path))
