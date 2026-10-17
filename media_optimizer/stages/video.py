"""Video -> H.265/HEVC (VAAPI, Vulkan or libx265).

The main thread walks the files and decides (cache, size, codec, existing
output, encoder). The work runs in two lanes at the same time: up to
gpu_workers GPU encodes and one CPU encode (x265 already uses every core).
Each job: probe slices, encode, runtime check, picture check, size check,
deletion. Measured on an RX 9070 XT with six 1080p clips: plain ffmpeg
encodes 8.2 s one at a time, 6.1 s two, 5.9 s three; the whole stage
including the picture checks 17.6 s, 12.2 s and 10.9 s.

Counters, the cache, the lists and the progress line are shared between the
jobs and only changed under ``self.lock``.

INVARIANTS
  - Output goes to a temp file and is moved into place, never overwriting,
    only after it passed the runtime check.
  - An original is never deleted before verify_video (and verify_visual when
    on) confirmed the output. It is also kept when the picture looked
    suspicious and for files salvaged from a damaged source.
  - The GPU arguments come from gpu.gpu_args for every file, so probe and
    encode always run with identical arguments.
  - The target container follows the source: MP4 accepts neither Opus nor
    SRT, so "auto" writes MKV for anything that is not MP4/MOV/M4V.
  - With a target directory no state files are written to the source.

The cache file (.video_conversion_cache.txt), the lists and their names are
the same as in the former Bash version (up to 1.2.1), so its caches and lists
are still used.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from .. import cache, files, gpu, media, proc, safety, ui
from ..config import RC_FATAL, RC_INTERRUPTED, RC_OK, RC_PARTIAL
from ..runlog import log
from ..ui import StyleUI as S
from . import clips
from .base import Context, convert_legacy_caches
from .images import nonempty

PROBE_MIN_DURATION = 60
CACHE_NAME = cache.VIDEO_CACHE
# The settings a cached result depends on; changing one makes the files
# count as new again.
CACHE_SETTINGS = ("source_codecs", "min_size_mb", "encoder_mode", "bitrate_threshold_kbps", "gpu_min_pixels",
                  "gpu_qp", "cpu_crf", "cpu_preset", "cpu_x265_params", "gpu_codec", "gpu_rc_mode",
                  "enable_probe", "probe_margin_pct", "probe_duration", "probe_slices", "discard_if_larger",
                  "keep_salvaged_corrupt", "video_container")
# Added later: they count only when changed, so existing caches stay valid.
CACHE_SETTINGS_OPTIONAL = ("gpu_bitrate", "gpu_bf", "gpu_low_power", "gpu_allow_10bit")
BROKEN_LIST = ".broken_videos.txt"

_CONTAINER_ERRORS = re.compile(r"not currently supported in container|could not write header|"
                               r"codec frame size is not set", re.I)
_SALVAGE_SIGNS = re.compile(r"corrupt|partial file|decoding error|invalid nal unit|error splitting", re.I)


def migrate_name(old: Path, new: Path, dry_run: bool) -> None:
    """State files with an older German name are renamed once."""
    if dry_run:
        return
    if old.exists() and not new.exists():
        try:
            safety.move_no_replace(old, new)
        except OSError:
            pass


def target_extension(src: Path, s) -> str:
    """The output container for a source (video_container)."""
    ext = src.suffix.lower().lstrip(".")
    mode = s.video_container
    if mode in ("mp4", "mkv"):
        return mode
    if mode == "keep":
        return ext
    return "mp4" if ext in ("mp4", "m4v", "mov") else "mkv"


def read_list(path: Path) -> Iterator[str]:
    """The lines of a list file (one path per line, # comments)."""
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return iter(())
    return (line for line in text.splitlines() if line and not line.startswith("#"))


class _GpuFailed(Exception):
    """The GPU could not encode this file: it goes to the CPU lane."""


@dataclass
class VideoCounters:
    processed: int = 0
    salvaged: int = 0
    skipped: int = 0
    probe_skipped: int = 0
    discarded: int = 0
    failed: int = 0
    unverified: int = 0
    suspect: int = 0
    verified: int = 0
    gpu: int = 0
    cpu: int = 0
    orig_bytes: int = 0
    new_bytes: int = 0
    elapsed: float = 0.0


@dataclass
class Job:
    """A video that passed the checks and waits for its lane."""
    src: Path
    size: int
    dest: Path
    encoder: str              # "gpu" or "cpu"; the lane it runs in
    reason: str
    args: gpu.GpuArgs | None
    info: media.MediaInfo
    idx: int


class VideoStage:
    tag = "h265"

    def __init__(self, ctx: Context, *, from_list: str | None = None):
        self.ctx = ctx
        self.s = ctx.settings
        self.from_list_arg = from_list
        self.c = VideoCounters()
        self.cache_skipped = 0         # describes this scan, not carried over
        self.dry = 0
        self.fell_back = False
        self.device = ""
        self.encoder_mode = self.s.encoder_mode
        self.skip_existing = True
        self.use_cache = self.s.use_cache      # off for --from-list; the shared settings stay as they are
        self._listed_cache: list | None = None
        self.lock = threading.RLock()
        self.fatal: str | None = None          # a tool vanished: no new jobs
        self.board: dict[int, str] = {}        # running jobs for the progress line
        self.requeue: deque = deque()          # GPU failures waiting for the CPU lane

    # --- setup ------------------------------------------------------------------
    def run(self) -> int:
        ctx, s = self.ctx, self.s
        missing = proc.missing("ffmpeg", "ffprobe")
        if missing:
            ui.say("MISSING", S.RED, f"Required programs not found: {' '.join(missing)}", err=True)
            return 1

        out = ctx.output
        if out is not None:
            migrate_name(out / ".defekte_videos.txt", out / BROKEN_LIST, ctx.dry_run)
        default_list = out / BROKEN_LIST if out is not None else None
        from_list = self.from_list_arg or ""
        if from_list.lower() == "auto" or (not from_list and s.auto_fix_list):
            if default_list is not None and nonempty(default_list):
                from_list = str(default_list)
            else:
                if from_list.lower() == "auto":
                    ui.say("LIST", S.CYAN, "No defect list found, running normally.")
                from_list = ""
        # A list exists but was not requested: only point it out. Silently
        # switching to a subset would hide the rest of the collection.
        if not from_list and default_list is not None and nonempty(default_list):
            n = sum(1 for _ in read_list(default_list))
            ui.say("NOTE", S.YELLOW, f"A defect list with {n} entry/entries exists:")
            ui.out(f"          {default_list}\n          Process only those: --from-list auto\n")
        self.from_list = Path(from_list) if from_list else None
        if self.from_list is not None:
            if not self.from_list.is_file():
                ui.out(f"List not found: {self.from_list}", err=True)
                return 1
            # An explicit list beats cache and existing outputs: the caller
            # wants exactly these files produced again.
            self.use_cache = False
            self.skip_existing = False
            ui.say("LIST", S.CYAN, f"processing only the files from {self.from_list}")
        if ctx.dry_run:
            ui.say("DRY-RUN", S.CYAN, "Nothing will be written or deleted.")

        work = ctx.work_dir
        self.stats_file = work / ".h265_stats.json"
        self.suspect_list = work / ".video_suspect.txt"
        migrate_name(work / ".video_verdaechtig.txt", self.suspect_list, ctx.dry_run)
        cache_file = work / CACHE_NAME
        # An older version kept the cache in the source directory in target
        # mode too. Copy it once (not move: the source is not written).
        legacy = ctx.source / CACHE_NAME
        if out is not None and not ctx.dry_run and legacy.is_file() and not cache_file.exists():
            try:
                shutil.copy2(legacy, cache_file)
            except OSError:
                pass
        self.trash = safety.Trash(None if ctx.dry_run else work / ".video_pending_deletes.txt",
                                  force=s.force_delete)

        lock = safety.RunLock(work)
        if not ctx.dry_run:
            try:
                lock.acquire()
            except safety.Busy as exc:
                ui.say("BUSY", S.RED, str(exc), err=True)
                return 1
            safety.cleanup_leftovers(work)        # any container
        try:
            if self.use_cache:
                convert_legacy_caches(ctx)
            rc = self._setup_encoder()
            if rc is not None:
                return rc
            self.cache = cache.StageCache(cache_file, enabled=self.use_cache, dry_run=ctx.dry_run,
                                          fingerprint=cache.fingerprint(s, CACHE_SETTINGS, CACHE_SETTINGS_OPTIONAL))
            return self._run_locked()
        finally:
            lock.release()

    def _setup_encoder(self) -> int | None:
        s = self.s
        if s.force_gpu:
            self.encoder_mode = "gpu"
            self.device = "/dev/dri/renderD128" if s.vaapi_device.lower() == "auto" else s.vaapi_device
            ui.say("GPU", S.CYAN, f"startup check skipped, using {self.device}.")
        elif self.encoder_mode in ("auto", "gpu"):
            device, error = gpu.detect_device(s)
            if device is not None:
                self.device = device
                ui.say("GPU", S.GREEN, f"{s.gpu_codec} available on {device}.")
            elif self.encoder_mode == "gpu":
                ui.say("ERROR", S.RED, f"{s.gpu_codec} not usable, but --encoder gpu was requested explicitly.",
                       err=True)
                ui.out(f"      reason: {error}", err=True)
                gpu.diagnose(error)
                return 1
            else:
                ui.say("GPU", S.YELLOW, f"{s.gpu_codec} not usable -> falling back to CPU/libx265.")
                ui.out(f"      reason: {error}", err=True)
                gpu.diagnose(error)
                self.encoder_mode = "cpu"
        return None

    # --- targets ------------------------------------------------------------------
    def _targets(self) -> Iterator[Path]:
        if self.from_list is None:
            match = files.extension_matcher(self.s.video_extensions, exclude_parts=True)
            yield from files.source_files(self.ctx.source, match, self.s)
            return
        for line, path, problem in self._listed():
            if path is not None:
                yield path
            else:
                ui.say("LIST", S.YELLOW, f"skipped, {problem}: {line}", err=True)

    def _listed(self) -> list[tuple[str, Path | None, str]]:
        """The files of --from-list as (line, path, problem). A list written
        for the same folder reached another way (a symlink, the real path)
        is mapped onto the source by the resolved path: the mirrored target
        folder is computed relative to it, which failed for every file."""
        if self._listed_cache is None:
            source = self.ctx.source
            real_source = os.path.realpath(source)
            entries = []
            for line in read_list(self.from_list):
                path = Path(line)
                if not path.is_file():
                    entries.append((line, None, "file missing"))
                    continue
                if self.ctx.output is not None and not path.is_relative_to(source):
                    rel = os.path.relpath(os.path.realpath(path.parent), real_source)
                    if rel == ".." or rel.startswith("../") or os.path.isabs(rel):
                        entries.append((line, None, "not inside the source directory"))
                        continue
                    path = source / rel / path.name
                entries.append((line, path, ""))
            self._listed_cache = entries
        return self._listed_cache

    def _count_targets(self) -> int:
        if self.from_list is not None:
            return sum(1 for _, path, _ in self._listed() if path is not None)
        return files.count(self.ctx.source, files.extension_matcher(self.s.video_extensions, exclude_parts=True),
                           self.s)

    # --- the run ------------------------------------------------------------------
    def _run_locked(self) -> int:
        ctx = self.ctx
        if ctx.resuming and self.stats_file.is_file():
            try:
                import json
                data = json.loads(self.stats_file.read_text(encoding="utf-8"))
                self.c = VideoCounters(**{k: v for k, v in data.items() if k in VideoCounters.__dataclass_fields__})
            except (OSError, ValueError, TypeError):
                pass
        elif not ctx.dry_run:
            self.stats_file.unlink(missing_ok=True)
        self.previous = self.c.elapsed
        self.started = time.monotonic()

        self.cache.load()
        if self.use_cache:
            self.cache.merge_subfolders(ctx.work_dir, ctx.source, self.s.include_hidden)
        elif self.from_list is None:
            ui.say("CACHE", S.CYAN, "Ignored for this run: every video is checked again.")

        self.total = self._count_targets()
        ui.out(f"\n{S.BOLD}{self.total} candidate(s) found.{S.RESET}")
        self.idx = 0
        lanes = {"gpu": self.s.gpu_workers, "cpu": 1}
        queued: dict[str, deque] = {"gpu": deque(), "cpu": deque()}
        running: dict[str, set] = {"gpu": set(), "cpu": set()}
        lookahead = self.s.gpu_workers + 2
        pool = ThreadPoolExecutor(max_workers=self.s.gpu_workers + 1, thread_name_prefix="video")
        targets = iter(self._targets())
        exhausted = False
        try:
            while True:
                # Decide ahead, a few files at most: the decision is cheap
                # (one ffprobe), and it is what tells which lane a file needs.
                while not exhausted and self.fatal is None and \
                        len(queued["gpu"]) + len(queued["cpu"]) < lookahead:
                    try:
                        src = next(targets)
                    except StopIteration:
                        exhausted = True
                        break
                    self.idx += 1
                    job = self._decide_guarded(src)
                    if job is not None:
                        queued[job.encoder].append(job)
                # The GPU failed on a real file: everything still waiting for
                # it goes to the CPU instead.
                if self.encoder_mode == "cpu":
                    while queued["gpu"]:
                        job = queued["gpu"].popleft()
                        job.encoder, job.args, job.reason = "cpu", None, "GPU failed earlier"
                        queued["cpu"].append(job)
                # The files the GPU failed on, first in line for the CPU. They
                # used to be encoded on the CPU inside the GPU lane: with two
                # GPU workers that made three x265 encodes at once.
                with self.lock:
                    while self.requeue:
                        queued["cpu"].appendleft(self.requeue.pop())
                if self.fatal is not None:
                    queued["gpu"].clear()
                    queued["cpu"].clear()
                for lane in lanes:
                    while queued[lane] and len(running[lane]) < lanes[lane]:
                        running[lane].add(pool.submit(self._process, queued[lane].popleft()))
                active = running["gpu"] | running["cpu"]
                if not active:
                    if exhausted or self.fatal is not None:
                        break
                    continue
                done, _ = wait(active, timeout=0.5, return_when=FIRST_COMPLETED)
                for lane in lanes:
                    running[lane] -= done
                for fut in done:
                    fut.result()              # _process never raises
        except KeyboardInterrupt:
            with proc.shielded():
                ui.out()
                ui.say("ABORT", S.RED, "video encoding stopped.")
                proc.stop_all()
                pool.shutdown(wait=True)
                self._save_stats()
                self.trash.resolve()
            return RC_INTERRUPTED
        pool.shutdown(wait=True)
        ui.progress_done()
        if self.fatal is not None:
            self._finish()
            return RC_FATAL
        return self._finish()

    # --- progress line ---------------------------------------------------------------
    def _render(self) -> None:
        with self.lock:
            skipped = self.cache_skipped + self.c.skipped + self.c.probe_skipped
            text = f"  [{self.idx}/{self.total}] {skipped} skipped"
            if self.board:
                text += "  │ " + " │ ".join(self.board[k] for k in sorted(self.board))
        ui.progress(text)

    def _tick(self) -> None:
        self._render()

    def _count(self, **amounts) -> None:
        with self.lock:
            for name, n in amounts.items():
                setattr(self.c, name, getattr(self.c, name) + n)

    def _save_stats(self) -> None:
        if self.ctx.dry_run:
            return
        import json
        with self.lock:
            self.c.elapsed = self.previous + time.monotonic() - self.started
            try:
                tmp = self.stats_file.with_name(self.stats_file.name + ".tmp")
                tmp.write_text(json.dumps(asdict(self.c), indent=1), encoding="utf-8")
                os.replace(tmp, self.stats_file)
            except OSError:
                pass

    def _add_dest_to_cache(self, dest: Path) -> None:
        """Only outputs inside the scanned tree: in place the next run finds
        'name_h265.mp4' again and saves an ffprobe; a separate target folder
        is never scanned."""
        if self.ctx.output is None:
            # Valid for any settings: with hevc in source_codecs, a changed
            # setting made an in-place output (renamed to the source name)
            # count as new, and it was encoded a second time.
            self.cache.add(dest, "output", any_settings=True)

    def _decide_guarded(self, src: Path) -> Job | None:
        try:
            return self._decide(src)
        except proc.ToolMissing as exc:
            self._tool_missing(exc.tool)
        except Exception as exc:  # noqa: BLE001 - a bug ends this file, keeps the original
            self._unexpected(src, exc)
        return None

    def _tool_missing(self, tool: str) -> None:
        with self.lock:
            first = self.fatal is None
            self.fatal = self.fatal or tool
            self.c.failed += 1
        if first:
            ui.say("FATAL", S.RED, f"{tool} not found. Aborting the batch.", err=True)
        self._save_stats()

    def _unexpected(self, src: Path, exc: Exception) -> None:
        ui.say("ERROR", S.RED, f"'{src}': unexpected error ({type(exc).__name__}: {exc}), original kept.",
               err=True)
        log.file(self.tag, "ERROR", src)
        self._count(failed=1)
        self._save_stats()

    def _decide(self, src: Path) -> Job | None:
        """The cheap checks, in the main thread. None = nothing to encode."""
        ctx, s = self.ctx, self.s
        if self.cache.lookup(src) is not None:
            with self.lock:
                self.cache_skipped += 1
            self._tick()
            return None
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        if size < s.min_size_mb * 1024 * 1024:
            self._count(skipped=1)
            self.cache.add(src, "small")
            self._tick()
            return None
        info = media.probe(src)
        if info.video_codec not in s.source_codecs:
            self._count(skipped=1)
            self.cache.add(src, "codec")
            self._tick()
            return None

        stem = files.stem(src.name)
        ext = target_extension(src, s)
        if ctx.output is not None:
            dest = ctx.target_folder(src) / f"{stem}.{ext}"
        else:
            dest = src.parent / f"{stem}_h265.{ext}"
        # A short clip already converted by the clip stage counts as done.
        # Named after the SOURCE: in place dest carries the _h265 suffix.
        # Only for a video the clip stage takes: IMG_0001.webp made from
        # IMG_0001.jpg (img_target webp) skipped IMG_0001.mp4 for good.
        dest_anim = dest.parent / f"{stem}.{s.clip_format}"
        if self.skip_existing and (nonempty(dest) or (nonempty(dest_anim) and clips.selected(info, s))):
            self._count(skipped=1)
            self.cache.add(src, "existing")
            self._add_dest_to_cache(dest)
            self._tick()
            return None

        kbps = info.bit_rate // 1000
        encoder, reason = self.encoder_mode, ""
        if self.encoder_mode == "auto":
            if s.gpu_min_pixels > 0 and info.pixels >= s.gpu_min_pixels:
                # Large frames always go to the GPU: libx265 would stall the batch.
                encoder, reason = "gpu", f"{info.width}x{info.height}"
            elif 0 < kbps < s.bitrate_threshold_kbps:
                encoder, reason = "cpu", f"{kbps} kb/s"
            else:
                encoder, reason = "gpu", f"{kbps} kb/s"
        args = gpu.gpu_args(s, self.device, info.pix_fmt) if encoder == "gpu" else None

        if ctx.dry_run:
            ui.progress_done()
            ui.say("DRY-RUN", S.CYAN, f"'{src.name}' ({encoder.upper()}, {kbps} kb/s) -> '{dest.name}'")
            self.dry += 1
            return None
        return Job(src, size, dest, encoder, reason, args, info, self.idx)

    def _process(self, job: Job) -> None:
        """One video in a worker thread. Never raises: every outcome is
        counted here, an interruption is not counted at all."""
        s, src = self.s, job.src
        try:
            if s.enable_probe and job.info.duration and job.info.duration >= PROBE_MIN_DURATION:
                if self._probe_says_skip(src, job.info.duration, job.encoder, job.args):
                    return
            ui.out(f"\n{S.BOLD}>>> [{job.idx}/{self.total}] Processing:{S.RESET} {src} "
                   f"({job.encoder.upper()}{', ' + job.reason if job.reason else ''})")
            try:
                with safety.Claim(job.dest.parent, files.stem(job.dest.name), job.dest) as claim:
                    self._encode_and_keep(job, claim)
            except _GpuFailed:
                with self.lock:
                    self.encoder_mode = "cpu"
                    self.fell_back = True
                    job.encoder, job.args, job.reason = "cpu", None, "GPU failed"
                    self.requeue.append(job)
            except safety.Conflict as exc:
                ui.say("CONFLICT", S.YELLOW, f"'{src.name}': {exc}, original kept.", err=True)
                self._count(skipped=1)
            except safety.Failed as exc:
                ui.say("ERROR", S.RED, f"'{src}': {exc}", err=True)
                log.file(self.tag, "ERROR", src)
                self._count(failed=1)
                self._save_stats()
        except proc.Stopped:
            pass
        except proc.ToolMissing as exc:
            self._tool_missing(exc.tool)
        except Exception as exc:  # noqa: BLE001
            self._unexpected(src, exc)
        finally:
            with self.lock:
                self.board.pop(job.idx, None)

    # --- probe ---------------------------------------------------------------------
    def _probe_says_skip(self, src: Path, duration: float, encoder: str, args) -> bool:
        """Several short slices spread over the runtime instead of one long one
        from the middle: a single slice can land on a title card or an action
        peak. Measured: one slice predicted "74 % larger" for a file that
        actually shrank by 53 %. Original and probe are measured the same
        way: same slices, video only, by file size."""
        s = self.s
        seconds = int(duration)
        slice_len = max(1, s.probe_duration // s.probe_slices)
        probe_bytes = ref_bytes = 0
        points = []
        with tempfile.TemporaryDirectory(prefix="mo_probe_") as tmp:
            for i in range(1, s.probe_slices + 1):
                start = seconds * i // (s.probe_slices + 1)
                start = max(0, min(start, seconds - slice_len))
                out = Path(tmp) / f"probe{i}.mkv"
                cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
                if args:
                    cmd += args.hw
                cmd += ["-reinit_filter", "0", "-ss", str(start), "-t", str(slice_len), "-i", str(src)]
                cmd += list(args.enc) if args else self._x265_args()
                cmd += ["-an", "-map", "0:v:0", str(out)]
                if not proc.run(cmd).ok:
                    return False
                probe_bytes += out.stat().st_size if out.exists() else 0
                ref = Path(tmp) / f"ref{i}.mkv"
                if not proc.run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                                 "-ss", str(start), "-t", str(slice_len), "-i", str(src),
                                 "-map", "0:v:0", "-an", "-c", "copy", str(ref)]).ok:
                    return False
                ref_bytes += ref.stat().st_size if ref.exists() else 0
                points.append(f"{start}s")
        if ref_bytes <= 0 or probe_bytes * 100 < ref_bytes * s.probe_margin_pct:
            return False
        secs = slice_len * s.probe_slices
        probe_kbps = probe_bytes * 8 // secs // 1000
        ref_kbps = ref_bytes * 8 // secs // 1000
        delta = ui.pct_change(ref_bytes, probe_bytes).lstrip("-")
        verdict = (f"would be {delta}% LARGER" if probe_bytes > ref_bytes
                   else f"only {delta}% smaller, threshold {100 - s.probe_margin_pct}%")
        ui.say("SKIPPED", S.YELLOW, f"{src.name} (probe {probe_kbps} vs original {ref_kbps} kb/s over "
                                    f"{s.probe_slices} slices at {', '.join(points)}: {verdict})")
        log.line(self.tag, f"PROBE     {src} ({verdict})")
        self._count(probe_skipped=1)
        self.cache.add(src, "probe")
        return True

    # --- encode ----------------------------------------------------------------------
    def _x265_args(self) -> list[str]:
        s = self.s
        cmd = ["-c:v", "libx265", "-crf", f"{s.cpu_crf:g}", "-preset", s.cpu_preset]
        if s.cpu_x265_params:
            cmd += ["-x265-params", s.cpu_x265_params]
        return cmd

    def _encode(self, job: Job, tmp: Path, encoder: str, args, fallback_streams: bool) -> proc.Result:
        src, duration = job.src, job.info.duration
        s = self.s
        cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "warning", "-nostats",
               "-progress", "pipe:1"]
        if encoder == "gpu":
            cmd += args.hw
        cmd += ["-reinit_filter", "0", "-i", str(src)]
        if encoder == "gpu":
            cmd += [*args.enc, *args.tag]
        else:
            cmd += self._x265_args()
            if s.hevc_tag:
                cmd += ["-tag:v", s.hevc_tag]
        if fallback_streams:
            cmd += ["-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-c:a", "aac", "-b:a", "192k", "-sn"]
        else:
            cmd += ["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map_metadata", "0",
                    "-c:a", "copy", "-c:s", "copy"]
        if s.faststart and tmp.suffix.lower() in (".mp4", ".m4v", ".mov"):
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(tmp))

        started = time.monotonic()
        state: dict[str, str] = {}

        def on_line(line: str) -> None:
            key, _, value = line.partition("=")
            state[key] = value
            if key != "progress":
                return
            try:
                done = int(state.get("out_time_us", "0")) / 1e6
            except ValueError:
                done = 0.0
            speed = state.get("speed", "").strip()
            if speed in ("N/A", "0x"):
                speed = ""
            # No file name: the ">>> [n/total] Processing:" line above names
            # it, and #n refers to it. Short enough for two or three encodes.
            tag = f"#{job.idx} {encoder.upper()}"
            if duration:
                pct = min(100.0, done / duration * 100)
                left = ""
                elapsed = time.monotonic() - started
                if pct > 1:
                    left = f" {ui.format_duration(elapsed / pct * (100 - pct))} left"
                text = f"{tag} {pct:3.0f}% {speed:>5}{left}"
            else:
                text = f"{tag} {ui.format_duration(done)} {speed}"
            with self.lock:
                self.board[job.idx] = text
            self._render()

        return proc.run(cmd, on_line=on_line)

    def _encode_and_keep(self, job: Job, claim: safety.Claim) -> None:
        s = self.s
        src, size, dest, encoder, args = job.src, job.size, job.dest, job.encoder, job.args
        tmp = claim.temp()
        res = self._encode(job, tmp, encoder, args, False)
        errors = res.stderr

        # Copying the audio or subtitle stream into the target container
        # failed: once more with AAC audio and without subtitles. Mainly
        # MKV/WebM sources with Opus or SRT that MP4 refuses.
        if not res.ok and _CONTAINER_ERRORS.search(res.stderr):
            ui.say("CONTAINER", S.YELLOW, f"'{src.name}': stream cannot be copied, retrying with AAC "
                                          "and without subtitles.", err=True)
            tmp.unlink(missing_ok=True)
            res = self._encode(job, tmp, encoder, args, True)
            errors = res.stderr

        # The GPU failed on a real file: this file is retried in the CPU lane
        # (with the container retry there too) and the rest of the run stays
        # on the CPU. A wrong startup check can therefore not derail the run.
        if not res.ok and encoder == "gpu":
            ui.say("GPU", S.YELLOW, f"encoding of '{src.name}' failed. Last messages:", err=True)
            for line in res.stderr.strip().splitlines()[-3:]:
                ui.out(f"        {line}", err=True)
            ui.say("GPU", S.YELLOW, "retrying on CPU and staying there for the rest of the run.", err=True)
            raise _GpuFailed()

        if not res.ok or not tmp.is_file():
            ui.say("ERROR", S.RED, f"'{src}'", err=True)
            for line in res.stderr.strip().splitlines()[-3:]:
                ui.out(f"        {line}", err=True)
            log.file(self.tag, "ERROR", src)
            self._count(failed=1)
            self._save_stats()
            return

        new = tmp.stat().st_size
        salvaged = bool(_SALVAGE_SIGNS.search(errors))

        # Verification before any deletion decision. For salvaged files the
        # runtime legitimately differs; there only readability counts.
        check = media.verify_video(src, tmp, 100 if salvaged else s.duration_tolerance_pct)
        if not check.ok:
            ui.say("INVALID", S.RED, f"'{src.name}': output incomplete or unreadable. Discarded, original kept.",
                   err=True)
            log.file(self.tag, "INVALID", src)
            self._count(unverified=1)
            self._save_stats()
            return

        visual_note = ""
        suspect = False
        if s.verify_visual and not salvaged:
            visual = media.verify_visual(src, tmp, s.visual_psnr_min, s.visual_samples)
            if visual.ok:
                # Reported even when it passed, so a quiet run shows the check ran.
                visual_note = f"{visual.best_text} dB"
                self._count(verified=1)
            else:
                suspect = True
                ui.say("PICTURE?", S.YELLOW, f"'{src.name}': PSNR {visual.best_text} dB below "
                                             f"{s.visual_psnr_min:g} dB (samples: {visual.samples or 'none'})",
                       err=True)
                log.file(self.tag, "PICTURE?", src)
                with self.lock:
                    self.c.suspect += 1
                    try:
                        with open(self.suspect_list, "a", encoding="utf-8", errors="surrogateescape") as fh:
                            fh.write(f"{src}\n")
                    except OSError:
                        pass
                # Warn only by default: the measurement can be wrong, so a
                # human decides. The original is kept either way.
                if s.visual_strict:
                    ui.out("            --strict-visual: discarded, original kept.", err=True)
                    log.file(self.tag, "INVALID", src)
                    self._count(unverified=1)
                    self._save_stats()
                    return
                ui.out("            File is kept, original kept. Check with: media-optimizer verify", err=True)

        src_h, new_h = ui.format_bytes(size), ui.format_bytes(new)
        if new >= size:
            inc_pct = ui.pct_change(size, new)
            inc_h = ui.format_bytes(new - size)
            if salvaged and s.keep_salvaged_corrupt:
                safety.commit(src, tmp, dest)
                self.cache.add(src, "salvaged")
                self._add_dest_to_cache(dest)
                ui.card(S.MAGENTA, [f"{S.BOLD}FILE SALVAGED / REPAIRED:{S.RESET} {src.name}",
                                    f"Original: {src_h} (was damaged)",
                                    f"New:      {new_h} (+{inc_pct}%, +{inc_h})",
                                    "Status:   Kept. Original is NOT deleted."])
                self._count_kept(encoder, size, new, salvaged=True)
                log.file(self.tag, "SALVAGED", src, dest.name, size, new)
                self._save_stats()
                return
            if s.discard_if_larger:
                self.cache.add(src, "discarded")
                ui.card(S.YELLOW, [f"{S.BOLD}FILE DISCARDED:{S.RESET} {src.name}",
                                   f"Original: {src_h} -> new: {new_h}",
                                   f"Status:   Discarded, larger by (+{inc_pct}%, +{inc_h})."])
                log.file(self.tag, "DISCARDED", src, "", size, new)
                self._count(discarded=1)
                self._save_stats()
                return

        safety.commit(src, tmp, dest)
        self._count_kept(encoder, size, new, salvaged=salvaged)
        log.file(self.tag, "SALVAGED" if salvaged else "OK", src, dest.name, size, new)
        lines = [f"{S.BOLD}FILE:{S.RESET}    {src.name}",
                 f"Original: {src_h} -> new: {new_h}"]
        if new < size:
            lines.append(f"Saved: {S.BG_GREEN} {ui.pct_change(size, new)}% {S.RESET} "
                         f"{S.GREEN}{S.BOLD}(-{ui.format_bytes(size - new)}){S.RESET}")
        if visual_note:
            lines.append(f"Picture: verified against the source, PSNR {visual_note}")
        if salvaged:
            lines.append("Status:   salvaged from a damaged source, original is NOT deleted.")
        if suspect:
            lines.append("Status:   picture suspicious, original is NOT deleted.")
        ui.card(S.MAGENTA if salvaged else S.YELLOW if suspect else S.GREEN, lines)

        # Only now, with the output verified and in place. Never for a
        # suspicious picture or a salvaged source.
        if self.ctx.delete and not suspect and not salvaged:
            self.trash.remove(src)
        dest = self._rename_inplace(src, dest)
        self.cache.add(src, "done")
        self._add_dest_to_cache(dest)
        self._save_stats()

    def _count_kept(self, encoder: str, size: int, new: int, *, salvaged: bool) -> None:
        self._count(processed=1, salvaged=int(salvaged), gpu=int(encoder == "gpu"),
                    cpu=int(encoder != "gpu"), orig_bytes=size, new_bytes=new)

    def _rename_inplace(self, src: Path, dest: Path) -> Path:
        """'holiday_h265.mp4' becomes 'holiday.mp4' again: in place only,
        only when the original is really gone, and only when the container
        keeps the extension (an MKV must not be named 'video.avi')."""
        if not self.s.rename_inplace or self.ctx.output is not None:
            return dest
        if dest.suffix.lower() != src.suffix.lower():
            ui.say("RENAME", S.CYAN, f"skipped: target container {dest.suffix} does not match {src.suffix}")
            return dest
        if src.exists():
            ui.say("RENAME", S.YELLOW, f"skipped: '{src.name}' still exists.", err=True)
            return dest
        try:
            safety.move_no_replace(dest, src)
        except OSError:
            ui.say("RENAME", S.YELLOW, f"failed, '{dest.name}' kept.", err=True)
            return dest
        ui.say("RENAME", S.CYAN, f"'{dest.name}' -> '{src.name}'")
        return src

    # --- finish ----------------------------------------------------------------------
    def _finish(self) -> int:
        c = self.c
        if self.ctx.dry_run:
            ui.out()
            ui.say("DRY-RUN", S.CYAN, f"{self.dry} video(s) would be re-encoded.")
            return RC_OK
        c.elapsed = self.previous + time.monotonic() - self.started
        rows = [("Total runtime:", ui.format_duration(c.elapsed)),
                ("Re-encoded:", f"{c.processed} file(s) (GPU: {c.gpu} | CPU: {c.cpu})")]
        if c.salvaged:
            rows.append(("Salvaged (repaired):", f"{c.salvaged} file(s)", S.MAGENTA))
        rows += [("Cache (this scan):", f"{self.cache_skipped} file(s)"),
                 ("Skipped:", f"{c.skipped} file(s)"),
                 ("Probe (no gain):", f"{c.probe_skipped} file(s)"),
                 ("Discarded (larger):", f"{c.discarded} file(s)")]
        if c.verified:
            rows.append(("Picture verified:", f"{c.verified} file(s)"))
        if c.suspect:
            rows.append(("Picture suspicious:", f"{c.suspect} file(s)", S.YELLOW))
        if c.unverified:
            rows.append(("Verification failed:", f"{c.unverified} file(s)", S.RED))
        rows.append(("Failed:", f"{c.failed} file(s)"))
        if c.processed and c.orig_bytes:
            saved = c.orig_bytes - c.new_bytes
            pct = ui.pct_change(c.orig_bytes, c.new_bytes)
            footer = [("Size before:", ui.format_bytes(c.orig_bytes)),
                      ("Size after:", ui.format_bytes(c.new_bytes))]
            footer.append(("Total saved:", f"-{ui.format_bytes(saved)} ({pct}%)", S.GREEN) if saved > 0
                          else ("Increase:", f"+{ui.format_bytes(-saved)} (+{pct}%)", S.YELLOW))
        else:
            footer = ["No new videos accepted."]
        ui.box("SUMMARY", rows, S.BLUE, footer=footer)

        if self.fell_back:
            ui.out()
            ui.say("NOTE", S.YELLOW, "The GPU failed during the run, switched to CPU.")
            ui.out("          See the ffmpeg messages above. Until fixed: --encoder cpu.")
        if c.suspect:
            ui.out()
            ui.say("NOTE", S.YELLOW, f"{c.suspect} output(s) with a suspicious picture comparison. "
                                     "They were KEPT, and so were their originals.")
            ui.out(f"          List: {self.suspect_list}\n"
                   "          Please inspect them yourself; the measurement can be wrong.")
        log.line(self.tag, f"summary: {c.processed} encoded, {c.verified} picture-verified "
                           f"(GPU {c.gpu} / CPU {c.cpu}), {c.skipped} skipped, {c.probe_skipped} by probe, "
                           f"{c.discarded} discarded, {c.failed} failed")
        self.trash.resolve()
        self.stats_file.unlink(missing_ok=True)
        # Failed encodes and outputs that did not pass verification: their
        # originals are kept, and the caller has to learn about it.
        return RC_PARTIAL if c.failed + c.unverified else RC_OK
