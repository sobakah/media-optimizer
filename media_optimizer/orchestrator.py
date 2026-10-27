"""The full run: count the media types and run the matching stages in order
(images, GIFs, AVIF, video), with resume after an interruption.

Differences to the Bash orchestrator, on purpose:
  - Stages run in-process instead of as child scripts; there is nothing to
    export and no exit code to translate.
  - The resume state stores every setting, not a selection (Bash open issue
    3), as JSON in the state folder.
  - "Process another directory?" loops instead of re-executing itself, so
    the restart cannot fail on how the program was started.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import files, folders, proc, questions, safety, ui
from .config import (RC_BAD_SETTING, RC_CANNOT_START, RC_INTERRUPTED, RC_OK, RC_PARTIAL, Settings,
                     state_dir)
from .events import events
from .journal import journal
from .runlog import log
from .stages.clips import ClipStage
from .stages.base import Context, apply_mode_defaults, resolve_legacy_caches
from .stages.gifs import GifStage
from .stages.images import ImageStage
from .stages.verify import VerifyStage
from .stages.video import VideoStage
from .ui import StyleUI as S

STAGES = ("images", "gifs", "clips", "video")
IMAGE_EXTS = ("jpg", "jpeg", "png")


LEGACY_STATE = "state.json"       # one file for every folder, up to 0.5


def state_file(source: Path | str) -> Path:
    """One resume state per source folder, so a run on another folder neither
    resumes nor overwrites it. Keyed by the folder with symlinks resolved."""
    key = hashlib.sha1(os.fsencode(os.path.realpath(source))).hexdigest()[:16]
    return state_dir() / f"state-{key}.json"


def _alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class RunState:
    status: str = "RUNNING"
    source: str = ""
    output: str | None = None
    settings: dict = field(default_factory=dict)
    done: dict = field(default_factory=lambda: {name: False for name in STAGES})
    pid: int = 0
    # Stage -> candidates when the run started: a resumed run still numbers
    # and lists a stage it finished, although its files are gone by now.
    planned: dict = field(default_factory=dict)
    loaded_from: Path | None = field(default=None, repr=False, compare=False)

    def save(self, dry_run: bool) -> None:
        if dry_run or not self.source:
            return
        path = state_file(self.source)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.pid = os.getpid()
        data = {k: v for k, v in self.__dict__.items() if k != "loaded_from"}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        if self.loaded_from is not None and self.loaded_from != path:
            self.loaded_from.unlink(missing_ok=True)       # taken over from state.json
        self.loaded_from = path

    def remove(self) -> None:
        if self.source:
            state_file(self.source).unlink(missing_ok=True)
        if self.loaded_from is not None:
            self.loaded_from.unlink(missing_ok=True)

    @property
    def running_elsewhere(self) -> bool:
        """Saved by a run that is still going (another terminal, a timer)."""
        return self.status == "RUNNING" and _alive(self.pid)

    @classmethod
    def load(cls, path: Path) -> "RunState | None":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            done = data.get("done") or {}
            if "avif" in done:                      # stage key before the rename to "clips"
                done["clips"] = done.pop("avif")
            state = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k != "loaded_from"})
        except (OSError, ValueError, TypeError):
            return None
        state.loaded_from = path
        return state


def open_states() -> list[RunState]:
    """Every interrupted run, the most recent first."""
    folder = state_dir()
    paths = sorted(folder.glob("state-*.json")) + [folder / LEGACY_STATE]
    found = []
    for path in paths:
        state = RunState.load(path) if path.is_file() else None
        if state is not None and state.status in ("INTERRUPTED", "RUNNING") and state.source:
            found.append((path.stat().st_mtime, state))
    found.sort(key=lambda item: item[0], reverse=True)
    return [state for _, state in found]


def _same_folder(a: str | Path, b: str | Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _pick_state(req: Request) -> tuple[RunState | None, str | None]:
    """The interrupted run this invocation may resume, and an error message
    when a run on the same folder is still going. With a source on the
    command line only that folder's state counts: a run started for
    /folder/B (by a timer, with -y) used to resume /folder/A instead."""
    states = open_states()
    if req.source:
        source = Path(req.source).expanduser().absolute()
        states = [st for st in states if _same_folder(st.source, source)]
        for st in states:
            if st.running_elsewhere:
                return None, (f"Another run is working on '{st.source}' (process {st.pid}).\n"
                              "If there is none, start over with --reset.")
    states = [st for st in states if not st.running_elsewhere]
    return (states[0] if states else None), None


@dataclass
class Request:
    """What the command line asked for."""
    settings: Settings
    source: str | None
    output: str | None
    dry_run: bool = False
    assume_yes: bool = False
    reset: bool = False
    call: str = ""


def _settings_menu(s: Settings, output: Path | None) -> None:
    questions.ask(s, output)          # the same table the browser page shows


def _preflight(source: Path, output: Path | None, s: Settings, dry_run: bool) -> None:
    """Correct extensions that do not match the content. In place only; with
    a target directory the source stays untouched and the stages put the
    result under the correct name in the target."""
    ui.out(f"\n{S.CYAN}Checking file extensions against the content...{S.RESET}")
    match = files.extension_matcher(["jpg", "jpeg", "png", "gif", "webp", "jxl", "mp4"])
    for path in list(files.source_files(source, match, s)):
        real = files.real_type(path)
        ext = files.extension(path)
        if not real or real == ext:
            continue
        if output is not None:
            ui.out(f"  {ui.badge('NOTE', S.YELLOW)} '{path.name}' is actually {real} (source left unchanged)")
            continue
        target = files.unique_name(path.parent, files.stem(path.name), real)
        ui.out(f"  {ui.badge('CORRECTED', S.YELLOW)} '{path.name}' ({real}) -> '{target.name}'")
        if not dry_run:
            try:
                safety.move_no_replace(path, target)
            except OSError as exc:
                ui.say("ERROR", S.RED, f"could not rename '{path.name}': {exc.strerror}", err=True)
            else:
                journal.renamed(path, target, "extension")


def _collision_warning(source: Path, s: Settings) -> None:
    """Sources that map to the same output name: foo.jpg and foo.png both
    become foo.jxl, foo.mp4 and foo.mov both foo.mp4. The workers lock the
    target name, but it is better to surface this beforehand. foo.jpg and
    foo.gif only clash when both formats end up the same."""
    from .stages.video import target_extension

    image = {"jpg": s.img_target, "jpeg": s.img_target, "png": s.img_target,
             "gif": "avif" if s.gif_target == "avif" else "webp"}
    groups: dict[str, list[str]] = defaultdict(list)
    match = files.extension_matcher([*image, *s.video_extensions], exclude_parts=True)
    for path in files.source_files(source, match, s, report=False):
        ext = path.suffix.lower().lstrip(".")
        out = image[ext] if ext in image else target_extension(path, s)
        groups[str(path.parent / f"{files.stem(path.name)}.{out}")].append(path.name)
    clashes = sorted(k for k, names in groups.items() if len(names) > 1)
    if not clashes:
        return
    ui.out()
    ui.say("WARNING", S.YELLOW, "Files would get the same output name.")
    ui.out("          Only the first is converted, the others are left untouched:")
    for name in clashes[:10]:
        ui.out(f"            {name}  ({', '.join(groups[name])})")
    if len(clashes) > 10:
        ui.out(f"            ... and {len(clashes) - 10} more")


def run(req: Request) -> tuple[int, bool]:
    """One full run. Returns (exit code, whether to process another directory)."""
    s = req.settings
    ui.banner("MEDIA OPTIMIZER")
    if req.reset and not req.dry_run:
        # The given folder's state; without one, every saved state.
        for st in open_states():
            if not req.source or _same_folder(st.source, Path(req.source).expanduser().absolute()):
                st.remove()

    resuming = False
    declined: RunState | None = None
    state, busy = _pick_state(req)
    if busy:
        ui.out(f"{S.RED}{busy}{S.RESET}", err=True)
        return RC_CANNOT_START, False
    if req.reset and req.dry_run:
        state = None                    # kept for later, but the preview starts over
    if state is not None:
        ui.box("INCOMPLETE PREVIOUS RUN DETECTED", [], S.YELLOW)
        done = state.done
        ui.out(f"  Source: {S.BOLD}{state.source}{S.RESET} | images: {_open(done, 'images')} | "
               f"GIFs: {_open(done, 'gifs')} | video: {_open(done, 'video')}\n")
        if ui.interactive() and not req.assume_yes and \
                not ui.prompt_bool("Resume the last run with its saved settings?", True, indent=""):
            # Removed once the new run turns out to be on the same folder:
            # a run on another folder must leave it (README).
            ui.out("-> Starting a new run.\n")
            declined, state = state, None
        else:
            resuming = True
            ui.out(f"-> {S.GREEN}Settings loaded, resuming.{S.RESET}\n")
            saved = Settings.from_dict(state.settings)
            # Logging belongs to this invocation, not the interrupted one:
            # --no-log or --log FILE given now must apply. The journal too.
            for name in ("log", "log_file", "log_max_kb", "journal_file"):
                setattr(saved, name, getattr(s, name))
            s = saved
            errors = s.errors()
            if errors:
                for line in errors:
                    ui.out(f"{S.RED}{line}{S.RESET} (in the saved state)", err=True)
                return RC_BAD_SETTING, False

    if resuming:
        source = Path(state.source)
        output = Path(state.output) if state.output else None
        if not source.is_dir():
            ui.out(f"{S.RED}Directory not found: {source}{S.RESET}", err=True)
            return RC_CANNOT_START, False
    else:
        src_text = req.source or ""
        if not src_text and ui.interactive():
            src_text = ui.prompt_text("Source directory", files.suggested_source(), indent="")
        if not src_text:
            ui.out(f"{S.RED}No source directory given.{S.RESET}", err=True)
            return RC_CANNOT_START, False
        source = Path(src_text).expanduser()
        if not source.is_dir():
            ui.out(f"{S.RED}Directory not found: {source}{S.RESET}", err=True)
            return RC_CANNOT_START, False
        out_text = req.output or ""
        if not out_text and ui.interactive() and req.output is None and not req.assume_yes:
            out_text = ui.ask("Target directory (Enter = in place): ")
        output = Path(out_text).expanduser() if out_text else None
        # The settings menu is for interactive use: not when the source was
        # given on the command line, unless another program (keepbest) passed
        # it on for a run the user started by hand.
        if ui.interactive() and not req.assume_yes and (not req.source or ui.caller()):
            ui.out()
            if not ui.prompt_bool("Use the default settings for all media types?", True, indent=""):
                _settings_menu(s, output)
        # Before the state is saved: a resumed run keeps the folder choice.
        folders.resolve(source, s, assume_yes=req.assume_yes)
        state = RunState(source=str(source), output=str(output) if output else None)

    source = source.absolute()
    if output is not None:
        output = output.absolute()
        if output == source:
            output = None
        elif not req.dry_run:
            output.mkdir(parents=True, exist_ok=True)
    if not resuming:
        # Absolute, so a resume from another working directory finds them.
        state.source, state.output = str(source), str(output) if output else None
        other = RunState.load(state_file(source))          # a folder typed in at the prompt
        if other is not None and other.running_elsewhere:
            ui.out(f"{S.RED}Another run is working on '{source}' (process {other.pid}).{S.RESET}\n"
                   "If there is none, start over with --reset.", err=True)
            return RC_CANNOT_START, False
        if declined is not None and _same_folder(declined.source, source) and not req.dry_run:
            declined.remove()

    ctx = Context(s, source, output, dry_run=req.dry_run, resuming=resuming, assume_yes=req.assume_yes)
    apply_mode_defaults(ctx, with_rename=True)
    state.settings = s.to_dict()

    log.open(enabled=s.log, log_file=s.log_file, max_kb=s.log_max_kb, dry_run=req.dry_run,
             call=req.call, source=source, target=output, tag="run")
    log.settings(s.to_dict())

    missing = proc.missing("cjxl", "cwebp", "gif2webp", "ffmpeg", "ffprobe")
    if missing:
        ui.say("MISSING", S.RED, f"Required programs not found: {' '.join(missing)}", err=True)
        ui.out(f"{S.RED}Please install the missing packages (libjxl-tools, webp, ffmpeg).{S.RESET}", err=True)
        log.close(RC_CANNOT_START)
        return RC_CANNOT_START, False
    if not safety.trash_available():
        ui.say("NOTE", S.YELLOW, "No trash available (send2trash, gio, trash-put). Originals are only "
                                 "deleted after a prompt.")
    if req.dry_run:
        ui.out()
        ui.say("DRY-RUN", S.CYAN, "No writes, no deletions.")

    journal.start(s.journal_file, dry_run=req.dry_run, source=source, target=output)
    events.run_start("run", source, output, req.dry_run)
    if s.preflight and not resuming:
        _preflight(source, output, s, req.dry_run)
    _collision_warning(source, s)

    count_img = files.count(source, files.extension_matcher(IMAGE_EXTS), s)
    count_gif = files.count(source, files.extension_matcher(["gif"]), s)
    count_vid = files.count(source, files.extension_matcher(s.video_extensions, exclude_parts=True), s)
    counts = {"images": count_img, "gifs": count_gif, "clips": count_vid, "video": count_vid}
    wanted = {"images": count_img > 0, "gifs": count_gif > 0,
              "clips": count_vid > 0 and s.clip_stage, "video": count_vid > 0}
    if state.planned:
        for name, n in state.planned.items():
            if name in wanted and not wanted[name] and state.done.get(name):
                wanted[name], counts[name] = True, n
    else:
        state.planned = {name: counts[name] for name in STAGES if wanted[name]}
    total = sum(wanted.values())
    for name in STAGES:
        if not wanted[name]:
            state.done[name] = True
    _plan(wanted, counts, state)
    state.status = "RUNNING"
    state.save(req.dry_run)
    if count_vid:
        resolve_legacy_caches(ctx)        # asked now, converted by the video stage

    titles = {
        "images": ("Images", f"Starting image conversion ({count_img} files)...", ImageStage),
        "gifs": ("GIFs", f"Starting GIF conversion ({count_gif} files)...", GifStage),
        # The clip stage runs BEFORE HEVC: short silent clips should end up as
        # AVIF/WebP. The video stage then skips sources that already have one.
        "clips": (f"Clips ({s.clip_format.upper()})", f"Starting clip stage: short silent videos to "
                  f"{s.clip_format.upper()}...",
                 ClipStage),
        "video": ("Videos", f"Starting video encoding ({count_vid} files)...", VideoStage),
    }
    failures: list[str] = []
    number = 0
    try:
        for name in STAGES:
            if not wanted[name]:
                continue
            number += 1
            if state.done[name]:
                continue
            label, message, stage_cls = titles[name]
            ui.out(f"\n{S.CYAN}▶ [{number}/{total}] {message}{S.RESET}")
            log.line("start", f"stage {label} ({counts[name]} candidates)")
            events.emit("stage_start", stage=name, total=counts[name])
            stage = stage_cls(ctx)
            rc = stage.run()
            events.stage_end(name, rc, stage)
            if rc == RC_INTERRUPTED:
                raise KeyboardInterrupt
            if rc == RC_PARTIAL:
                ui.out()
                ui.say("WARNING", S.YELLOW, f"Stage '{label}' finished, but some files failed.", err=True)
                failures.append(label)
            elif rc != RC_OK:
                ui.out()
                ui.say("ERROR", S.RED, f"Stage '{label}' exited with code {rc}.", err=True)
                ui.out(f"{S.CYAN}The state is kept, resume after fixing the problem.{S.RESET}", err=True)
                log.close(rc)
                journal.end(rc)
                events.run_end(rc)
                return rc, False
            state.done[name] = True
            if name == "images" and not wanted["gifs"] and output is None and not req.dry_run:
                # In place the image stage renames a GIF named .png to .gif;
                # counted before, the GIF stage would have been left out.
                count_gif = files.count(source, files.extension_matcher(["gif"]), s)
                if count_gif:
                    wanted["gifs"], state.done["gifs"], counts["gifs"] = True, False, count_gif
                    total += 1
                    titles["gifs"] = ("GIFs", f"Starting GIF conversion ({count_gif} files)...", GifStage)
                    _plan(wanted, counts, state)
            state.save(req.dry_run)
    except KeyboardInterrupt:
        with proc.shielded():
            proc.stop_all()
            ui.out(f"\n\n{S.YELLOW}[INTERRUPTED] Aborted by user.{S.RESET}")
            log.line("abort", "Aborted by user")
            log.close(RC_INTERRUPTED)
            journal.end(RC_INTERRUPTED)
            events.run_end(RC_INTERRUPTED)
            state.status = "INTERRUPTED"
            state.save(req.dry_run)
            ui.out(f"{S.CYAN}Progress and settings have been saved.{S.RESET}\n"
                   f"{S.CYAN}The next start will resume where this stopped.{S.RESET}")
        return RC_INTERRUPTED, False

    # Final check: only with a target directory, against the untouched
    # originals. Read-only; findings print the repair command.
    if s.enable_verify_output and not req.dry_run:
        if output is None:
            ui.out()
            ui.say("NOTE", S.YELLOW, "verify-output needs a target directory; the originals")
            ui.out("       would already be gone in place. Skipping the final check.")
        elif count_vid == 0:
            ui.out()
            ui.say("NOTE", S.YELLOW, "No videos in the source, nothing to re-check.")
        else:
            ui.out(f"\n{S.CYAN}▶ Final check of the target directory...{S.RESET}")
            log.line("verify", f"final check of {output}")
            events.emit("stage_start", stage="verify", total=None)
            stage = VerifyStage(ctx)
            rc = stage.run()
            events.stage_end("verify", rc, stage)
            if rc == RC_INTERRUPTED:
                log.close(rc)
                journal.end(rc)
                events.run_end(rc)
                return rc, False
            if rc != RC_OK:
                ui.say("NOTE", S.YELLOW, "The check reported findings. Nothing was changed;")
                ui.out(f"       repair with: media-optimizer verify {source} {output} --fix --run")

    if not req.dry_run:
        state.remove()
    rc = RC_PARTIAL if failures else RC_OK
    log.close(rc)
    journal.end(rc)
    events.run_end(rc)
    if failures:
        ui.box("ALL STAGES COMPLETED, WITH FAILURES", [("Failed files in:", " ".join(failures))], S.YELLOW)
    else:
        ui.box("ALL STAGES COMPLETED SUCCESSFULLY", [], S.GREEN)

    another = False
    if ui.interactive() and not req.assume_yes:
        ui.out()
        caller = ui.caller()
        question = f"Process another directory? (No: back to {caller})" if caller else "Process another directory?"
        another = ui.prompt_bool(question, False)
    return rc, another


def _plan(wanted: dict, counts: dict, state: RunState) -> None:
    """The stages this run will go through, for the events file; a resumed
    run marks the ones it finished before."""
    events.emit("plan", stages=[{"stage": name, "total": counts[name], "done": bool(state.done.get(name))}
                                for name in STAGES if wanted[name]])


def _open(done: dict, name: str) -> str:
    return "done" if done.get(name) else "open"
