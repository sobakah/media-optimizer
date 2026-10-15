"""What every stage shares: the run context, per-file results, counters that
survive an interruption, the in-place notice, and the parallel batch runner
used by the image, GIF and clip stages.

The batch runner replaces ``find | xargs -P`` with a thread pool. Workers are
threads because the work happens in the encoder processes; a worker returns
its result instead of appending a token to a shared file, and ends a file
early by raising an ``Outcome`` (Failed, Conflict) instead of relying on
``set -e`` and an EXIT trap. Any other exception is a failure of that file:
the original is kept, because deletion is the last step of a worker.
"""

from __future__ import annotations

import enum
import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from .. import cache, proc, safety, ui
from ..config import RC_FATAL, RC_INTERRUPTED, RC_OK, RC_PARTIAL, Settings
from ..runlog import log
from ..ui import StyleUI as S


@dataclass
class Context:
    settings: Settings
    source: Path
    output: Path | None
    dry_run: bool = False
    resuming: bool = False
    assume_yes: bool = False
    legacy_cache: str = "ask"     # old-format caches: ask, convert, keep, none, done

    @property
    def work_dir(self) -> Path:
        """State, temp and lock files live here: the target directory, or the
        source directory in place. The source is never written in target mode."""
        return self.output if self.output is not None else self.source

    def target_folder(self, src: Path) -> Path:
        if self.output is None:
            return src.parent
        return self.output / src.parent.relative_to(self.source)

    @property
    def delete(self) -> bool:
        return bool(self.settings.delete_original)


class Kind(enum.Enum):
    SUCCESS = "SUCCESS"
    SKIP = "SKIP"
    FAIL = "FAIL"
    DISCARD = "DISCARD"
    COLLISION = "COLLISION"
    DRY = "DRY"
    STOPPED = "STOPPED"     # interrupted, not counted; tried again next run
    CACHED = "CACHED"       # nothing to do according to the cache; counted per scan only


@dataclass
class FileResult:
    kind: Kind
    orig: int = 0
    new: int = 0
    suspect: bool = False
    cache: str | None = None      # outcome to remember: nothing to do next time


@dataclass
class Counters:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    collision: int = 0
    discarded: int = 0
    suspect: int = 0
    orig_bytes: int = 0
    new_bytes: int = 0
    elapsed: float = 0.0
    dry: int = field(default=0, metadata={"transient": True})

    def add(self, r: FileResult) -> None:
        if r.kind is Kind.SUCCESS:
            self.processed += 1
            self.orig_bytes += r.orig
            self.new_bytes += r.new
        elif r.kind is Kind.SKIP:
            self.skipped += 1
        elif r.kind is Kind.FAIL:
            self.failed += 1
        elif r.kind is Kind.COLLISION:
            self.collision += 1
        elif r.kind is Kind.DISCARD:
            self.discarded += 1
        elif r.kind is Kind.DRY:
            self.dry += 1
        if r.suspect:
            self.suspect += 1

    def save(self, path: Path) -> None:
        data = asdict(self)
        data.pop("dry", None)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "Counters":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return cls()


# --- defaults that depend on the mode -------------------------------------------------
_inplace_notice_shown = False


def reset_notices() -> None:
    global _inplace_notice_shown
    _inplace_notice_shown = False


def apply_mode_defaults(ctx: Context, *, with_rename: bool) -> None:
    """delete_original/rename_inplace that were not chosen follow the mode:
    with a target directory false, in place true. In place that is
    destructive, so a notice shows the values and offers to change them;
    nothing is asked when both were chosen already, with -y, or without a
    terminal. Shown once per run, not per stage."""
    global _inplace_notice_shown
    s = ctx.settings
    if ctx.output is not None:
        if s.delete_original is None:
            s.delete_original = False
        if s.rename_inplace is None:
            s.rename_inplace = False
        return
    delete_chosen = s.delete_original is not None
    rename_chosen = s.rename_inplace is not None
    if s.delete_original is None:
        s.delete_original = True
    if s.rename_inplace is None:
        s.rename_inplace = True
    if _inplace_notice_shown:
        return
    _inplace_notice_shown = True

    ui.out(f"\n{S.YELLOW}╔{'═' * ui.BOX_WIDTH}╗{S.RESET}\n"
           f"{S.YELLOW}║{S.RESET}  {S.BOLD}IN-PLACE MODE: originals will be modified{S.RESET}"
           f"{' ' * 19}{S.YELLOW}║{S.RESET}\n"
           f"{S.YELLOW}╚{'═' * ui.BOX_WIDTH}╝{S.RESET}")
    ui.out("  No target directory given, so:")
    ui.out(f"    Move originals to trash  : {S.BOLD}{_tf(s.delete_original)}{S.RESET}")
    if with_rename:
        ui.out(f"    Remove _h265 suffix      : {S.BOLD}{_tf(s.rename_inplace)}{S.RESET}")
    if s.delete_original:
        ui.out("  Originals go to the trash and can be restored from there.")
    if delete_chosen and (not with_rename or rename_chosen):
        ui.out()
        return
    if not ui.interactive():
        ui.out(f"  {S.CYAN}Cannot prompt (not interactive).{S.RESET}")
        return
    if ctx.assume_yes:
        ui.out(f"  {S.CYAN}-y given, settings are kept.{S.RESET}")
        return
    if not ui.prompt_bool("Keep these settings?", True, indent="  "):
        s.delete_original = ui.prompt_bool("Move originals to the trash after a successful conversion?",
                                           s.delete_original)
        if with_rename:
            s.rename_inplace = ui.prompt_bool("Rename the result to the original name after deleting it?",
                                              s.rename_inplace)
    ui.out()


def _tf(value) -> str:
    return "true" if value else "false"


def prompt_delete_original(settings: Settings, output: Path | None) -> None:
    """The interactive "delete originals?" question. The default shown is the
    one the mode would apply, so Enter changes nothing; the answer counts as
    chosen and is never overwritten by the mode."""
    default = settings.delete_original
    if default is None:
        default = output is None
    settings.delete_original = ui.prompt_bool(
        "Move originals to the trash after a successful conversion?", default)


def prompt_rescan(settings: Settings) -> None:
    """Asked the other way round than use_cache, because ignoring the caches
    is the deliberate choice."""
    rescan = ui.prompt_bool("Scan all files again, ignoring the caches of earlier runs (slower)?",
                            not settings.use_cache)
    settings.use_cache = not rescan


def resolve_legacy_caches(ctx: Context) -> None:
    """Ask once per run whether old-format video caches are converted. Only
    the answer is taken here; the video stage converts under its run lock.
    A dry run writes nothing, so it neither asks nor converts."""
    if ctx.legacy_cache not in ("ask", "none"):
        return
    if ctx.dry_run or not ctx.settings.use_cache or not ctx.work_dir.is_dir():
        ctx.legacy_cache = "none"
        return
    paths = cache.find_legacy(ctx.work_dir, ctx.settings.include_hidden)
    if not paths:
        ctx.legacy_cache = "none"
        return
    convert = cache.ask_convert(paths, interactive=ui.interactive(), assume_yes=ctx.assume_yes)
    ctx.legacy_cache = "convert" if convert else "keep"
    if not convert:
        ui.out("          Kept as they are and used as before (paths only); asked again next run.")


def convert_legacy_caches(ctx: Context) -> None:
    """Carry out a "convert" answer. Call while holding the run lock."""
    resolve_legacy_caches(ctx)          # a cache copied over from the source just now
    if ctx.legacy_cache != "convert":
        return
    for path in cache.find_legacy(ctx.work_dir, ctx.settings.include_hidden):
        try:
            kept, dropped = cache.convert(path)
        except OSError as exc:
            ui.say("CACHE", S.RED, f"could not convert {path}: {exc.strerror}", err=True)
            continue
        ui.say("CACHE", S.CYAN, f"converted {path}: {kept} entries kept, {dropped} for missing files dropped.")
    ctx.legacy_cache = "done"


# --- the parallel batch stage ----------------------------------------------------------
class BatchStage:
    """Base for the image, GIF and clip stages. Subclasses set the class
    attributes and implement ``prepare``, ``files`` and ``handle``."""

    tag = ""                  # log tag
    label = ""                # "image", for "image conversion stopped"
    title = ""                # summary box title
    stats_name = ""
    pending_name = ""
    leftovers: list[str] = []      # output extensions whose temp files a hard abort left
    dry_noun = "file(s)"
    cache_name = ""
    cache_settings: tuple[str, ...] = ()      # the settings a cached outcome depends on

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.s = ctx.settings
        self.counters = Counters()
        self.cache = cache.StageCache(ctx.work_dir / self.cache_name, enabled=False, dry_run=True,
                                      fingerprint="")
        self.trash = safety.Trash(None if ctx.dry_run else ctx.work_dir / self.pending_name,
                                  force=self.s.force_delete)
        self.started = time.monotonic()

    # subclass hooks
    def prepare(self) -> int | None:
        """Tool checks and announcements; return an exit code to stop."""
        return None

    def files(self) -> Iterator[Path]:
        raise NotImplementedError

    def handle(self, src: Path) -> FileResult:
        raise NotImplementedError

    def summary_rows(self) -> list:
        c = self.counters
        rows = [("Total runtime:", ui.format_duration(c.elapsed)),
                ("Converted:", f"{c.processed} file(s)")]
        if self.cache.hits:
            rows.append(("Cache (this scan):", f"{self.cache.hits} file(s)"))
        rows.append(("Skipped:", f"{c.skipped} file(s)"))
        if c.discarded:
            rows.append(("Discarded (larger):", f"{c.discarded} file(s)"))
        if c.suspect:
            rows.append(("Picture suspicious:", f"{c.suspect} file(s)"))
        if c.collision:
            rows.append(("Name conflict:", f"{c.collision} file(s)"))
        rows.append(("Failed:", f"{c.failed} file(s)"))
        return rows

    # shared helpers for workers
    def fail(self, src: Path, reason: str) -> FileResult:
        log.file(self.tag, "ERROR", src)
        ui.say("ERROR", S.RED, f"{reason}: '{src.name}' (original kept)", err=True)
        return FileResult(Kind.FAIL)

    def conflict(self, src: Path, message: str) -> FileResult:
        log.file(self.tag, "CONFLICT", src)
        ui.say("CONFLICT", S.YELLOW, f"'{src.name}': {message}, original kept.", err=True)
        return FileResult(Kind.COLLISION)

    def remove_original(self, src: Path) -> None:
        """Only ever called after the output is verified and in place."""
        if self.ctx.delete:
            self.trash.remove(src)

    # the run
    def _guarded(self, src: Path) -> tuple[FileResult, str | None]:
        try:
            if self.cache.lookup(src) is not None:
                return FileResult(Kind.CACHED), None
            result = self.handle(src)
            if result.cache and result.kind is not Kind.FAIL:
                self.cache.add(src, result.cache)
            return result, None
        except proc.Stopped:
            return FileResult(Kind.STOPPED), None
        except proc.ToolMissing as exc:
            self.fail(src, "conversion failed")
            return FileResult(Kind.FAIL), exc.tool
        except safety.Failed as exc:
            return self.fail(src, str(exc)), None
        except safety.Conflict as exc:
            return self.conflict(src, str(exc)), None
        except Exception as exc:  # noqa: BLE001 - any bug ends this file, keeps the original
            return self.fail(src, f"unexpected error ({type(exc).__name__}: {exc})"), None

    def run(self) -> int:
        ctx = self.ctx
        rc = self.prepare()
        if rc is not None:
            return rc
        lock = safety.RunLock(ctx.work_dir)
        if not ctx.dry_run:
            try:
                lock.acquire()
            except safety.Busy as exc:
                ui.say("BUSY", S.RED, str(exc), err=True)
                return 1
            safety.cleanup_leftovers(ctx.work_dir, self.leftovers)
        try:
            return self._run_locked()
        finally:
            lock.release()

    def _run_locked(self) -> int:
        ctx = self.ctx
        stats = ctx.work_dir / self.stats_name
        if ctx.resuming and stats.is_file():
            self.counters = Counters.load(stats)
        elif not ctx.dry_run:
            stats.unlink(missing_ok=True)
        previous = self.counters.elapsed
        s = self.s
        self.cache = cache.StageCache(ctx.work_dir / self.cache_name, enabled=s.use_cache,
                                      dry_run=ctx.dry_run, fingerprint=cache.fingerprint(s, self.cache_settings))
        self.cache.load()
        self.cache.merge_subfolders(ctx.work_dir, ctx.source, s.include_hidden)
        if not s.use_cache:
            ui.say("CACHE", S.CYAN, "Ignored for this run: every file is checked again.")

        interrupted = False
        fatal: str | None = None
        workers = self.s.workers()
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=self.tag)
        pending: set = set()
        try:
            paths = iter(self.files())
            exhausted = False
            while True:
                while not exhausted and fatal is None and len(pending) < workers * 2:
                    try:
                        src = next(paths)
                    except StopIteration:
                        exhausted = True
                        break
                    pending.add(pool.submit(self._guarded, src))
                if not pending:
                    break
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    result, missing_tool = fut.result()
                    self.counters.add(result)
                    if missing_tool and fatal is None:
                        fatal = missing_tool
                        ui.say("FATAL", S.RED, f"{missing_tool} not found. Aborting the batch.", err=True)
        except KeyboardInterrupt:
            interrupted = True
            with proc.shielded():
                ui.out()
                ui.say("ABORT", S.RED, f"{self.label} conversion stopped.")
                proc.stop_all()
                for fut in pending:
                    result, _ = fut.result()
                    self.counters.add(result)
        finally:
            pool.shutdown(wait=True)

        self.counters.elapsed = previous + time.monotonic() - self.started
        if interrupted:
            with proc.shielded():
                if not ctx.dry_run:
                    self.counters.save(stats)
                self.trash.resolve()
            return RC_INTERRUPTED

        if not ctx.dry_run:
            stats.unlink(missing_ok=True)
        if ctx.dry_run:
            ui.out()
            ui.say("DRY-RUN", S.CYAN, f"{self.counters.dry} {self.dry_noun} would be converted, "
                                      f"{self.counters.skipped} skipped.")
            return RC_OK
        if fatal:
            ui.out()
            ui.say("ABORT", S.RED, "Batch stopped: a required encoder is missing.", err=True)
        self.print_summary()
        self.trash.resolve()
        if fatal:
            return RC_FATAL
        return RC_PARTIAL if self.counters.failed else RC_OK

    def print_summary(self) -> None:
        c = self.counters
        footer = []
        if c.processed and c.orig_bytes:
            saved = c.orig_bytes - c.new_bytes
            pct = ui.pct_change(c.orig_bytes, c.new_bytes)
            footer = [("Size before:", ui.format_bytes(c.orig_bytes)),
                      ("Size after:", ui.format_bytes(c.new_bytes))]
            if saved > 0:
                footer.append(("Total saved:", f"-{ui.format_bytes(saved)} ({pct}%)", S.GREEN))
            else:
                footer.append(("Increase:", f"+{ui.format_bytes(-saved)} (+{pct}%)", S.YELLOW))
        ui.box(self.title, self.summary_rows(), S.BLUE, footer=footer or None)
        log.line(self.tag, f"summary: {c.processed} converted, {c.skipped} skipped, "
                           f"{c.discarded} discarded, {c.failed} failed, "
                           f"{ui.format_bytes(c.orig_bytes)} -> {ui.format_bytes(c.new_bytes)}")


def verify_image(path: Path, deep: bool) -> bool:
    """Light: not empty. Deep: a full decode (djxl / webpinfo)."""
    try:
        if path.stat().st_size == 0:
            return False
    except OSError:
        return False
    if not deep:
        return True
    ext = path.suffix.lower()
    import tempfile

    if ext == ".jxl" and proc.which("djxl"):
        with tempfile.TemporaryDirectory(prefix="mo_verify_") as tmp:
            return proc.run(["djxl", str(path), os.path.join(tmp, "out.ppm")]).ok
    if ext == ".webp" and proc.which("webpinfo"):
        return proc.run(["webpinfo", "-quiet", str(path)]).ok
    return True
