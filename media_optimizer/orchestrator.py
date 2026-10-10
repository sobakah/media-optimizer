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

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import files, proc, safety, ui
from .config import (RC_BAD_SETTING, RC_CANNOT_START, RC_INTERRUPTED, RC_OK, RC_PARTIAL, Settings,
                     state_dir)
from .runlog import log
from .stages.clips import ClipStage
from .stages.base import (Context, apply_mode_defaults, prompt_delete_original, prompt_rescan,
                          resolve_legacy_caches)
from .stages.gifs import GifStage
from .stages.images import ImageStage
from .stages.verify import VerifyStage
from .stages.video import VideoStage
from .ui import StyleUI as S

STAGES = ("images", "gifs", "clips", "video")
IMAGE_EXTS = ("jpg", "jpeg", "png")


def state_file() -> Path:
    return state_dir() / "state.json"


@dataclass
class RunState:
    status: str = "RUNNING"
    source: str = ""
    output: str | None = None
    settings: dict = field(default_factory=dict)
    done: dict = field(default_factory=lambda: {name: False for name in STAGES})

    def save(self, dry_run: bool) -> None:
        if dry_run or not self.source:
            return
        path = state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls) -> "RunState | None":
        try:
            data = json.loads(state_file().read_text(encoding="utf-8"))
            done = data.get("done") or {}
            if "avif" in done:                      # stage key before the rename to "clips"
                done["clips"] = done.pop("avif")
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return None


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
    ui.section("GLOBAL SETTINGS")
    s.preflight = ui.prompt_bool("Correct wrong file extensions by content first?", s.preflight)
    prompt_delete_original(s, output)
    s.verify_deep = ui.prompt_bool("Fully decode image outputs to verify them (slower)?", s.verify_deep)
    s.clip_stage = ui.prompt_bool("Also convert short silent videos to animations?", s.clip_stage)
    if s.clip_stage:
        ui.prompt_setting(s, "clip_format", "Format for them (avif, or webp to animate in GNOME's viewer)")
    ui.prompt_setting(s, "max_workers", "Parallel workers for images/GIFs (empty = all cores)")
    prompt_rescan(s)

    ui.section("IMAGES & GIFS")
    ui.prompt_setting(s, "img_target", "Image target format (jxl or webp)")
    if s.img_target == "webp":
        ui.prompt_setting(s, "img_webp_quality", "WebP quality, higher is better (0-100)")
    ui.prompt_setting(s, "jxl_effort", "JXL effort, higher is slower and smaller (1-10)")
    ui.prompt_setting(s, "png_mode", "PNG mode (lossless or lossy)")
    if s.png_mode == "lossy":
        ui.prompt_setting(s, "png_quality", "JXL quality for PNG, higher is better (1-100)")
    ui.prompt_setting(s, "gif_method", "WebP compression level for GIFs (0-6)")

    ui.section("VIDEO (H.265)")
    ui.prompt_setting(s, "encoder_mode", "Video encoder (auto, gpu or cpu)")
    if s.encoder_mode == "auto":
        ui.prompt_setting(s, "bitrate_threshold_kbps", "Bitrate above which the GPU is used (kb/s)")
    if s.encoder_mode in ("auto", "gpu"):
        ui.prompt_setting(s, "gpu_qp", "GPU quality, lower is better (24-28)")
    if s.encoder_mode in ("auto", "cpu"):
        ui.prompt_setting(s, "cpu_crf", "CPU quality, lower is better (20-24)")
        ui.prompt_setting(s, "cpu_preset", "CPU preset (medium or slow)")
    s.enable_probe = ui.prompt_bool("Encode a few short test slices first to predict the saving?",
                                    s.enable_probe)
    s.discard_if_larger = ui.prompt_bool("Discard a video if the result is larger than the original?",
                                         s.discard_if_larger)
    s.keep_salvaged_corrupt = ui.prompt_bool("Keep videos salvaged from damaged sources even if they grew?",
                                             s.keep_salvaged_corrupt)
    s.verify_visual = ui.prompt_bool("Compare the picture against the source while converting?",
                                     s.verify_visual)
    if s.verify_visual:
        s.visual_strict = ui.prompt_bool("Discard a video when that comparison looks wrong?", s.visual_strict)
    if output is not None:
        s.enable_verify_output = ui.prompt_bool("Re-check the finished target directory at the end?",
                                                s.enable_verify_output)


def _preflight(source: Path, output: Path | None, s: Settings, dry_run: bool) -> None:
    """Correct extensions that do not match the content. In place only; with
    a target directory the source stays untouched and the stages put the
    result under the correct name in the target."""
    ui.out(f"\n{S.CYAN}Checking file extensions against the content...{S.RESET}")
    match = files.extension_matcher(["jpg", "jpeg", "png", "gif", "webp", "jxl", "mp4"])
    for path in list(files.find_files(source, match, include_hidden=s.include_hidden)):
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


def _collision_warning(source: Path, s: Settings) -> None:
    """foo.jpg and foo.png both map to foo.jxl. The workers lock the target
    name, but it is better to surface this beforehand."""
    groups: dict[str, int] = defaultdict(int)
    match = files.extension_matcher(["jpg", "jpeg", "png", "gif"])
    for path in files.find_files(source, match, include_hidden=s.include_hidden, report=False):
        groups[str(path.with_suffix(""))] += 1
    clashes = sorted(k for k, n in groups.items() if n > 1)
    if not clashes:
        return
    ui.out()
    ui.say("WARNING", S.YELLOW, "Files share a base name with different extensions.")
    ui.out("          Only the first is converted, the others are left untouched:")
    for name in clashes[:10]:
        ui.out(f"            {name}")
    if len(clashes) > 10:
        ui.out(f"            ... and {len(clashes) - 10} more")


def run(req: Request) -> tuple[int, bool]:
    """One full run. Returns (exit code, whether to process another directory)."""
    s = req.settings
    ui.banner("MEDIA OPTIMIZER")
    if req.reset:
        state_file().unlink(missing_ok=True)

    resuming = False
    state = RunState.load()
    if state is not None and state.status in ("INTERRUPTED", "RUNNING"):
        ui.box("INCOMPLETE PREVIOUS RUN DETECTED", [], S.YELLOW)
        done = state.done
        ui.out(f"  Source: {S.BOLD}{state.source}{S.RESET} | images: {_open(done, 'images')} | "
               f"GIFs: {_open(done, 'gifs')} | video: {_open(done, 'video')}\n")
        if ui.interactive() and not req.assume_yes and \
                not ui.prompt_bool("Resume the last run with its saved settings?", True, indent=""):
            ui.out("-> Discarding state.\n")
            state_file().unlink(missing_ok=True)
            state = None
        else:
            resuming = True
            ui.out(f"-> {S.GREEN}Settings loaded, resuming.{S.RESET}\n")
            saved = Settings.from_dict(state.settings)
            # Logging belongs to this invocation, not the interrupted one:
            # --no-log or --log FILE given now must apply.
            for name in ("log", "log_file", "log_max_kb"):
                setattr(saved, name, getattr(s, name))
            s = saved
            errors = s.errors()
            if errors:
                for line in errors:
                    ui.out(f"{S.RED}{line}{S.RESET} (in the saved state)", err=True)
                return RC_BAD_SETTING, False
    else:
        state = None

    if resuming:
        source = Path(state.source)
        output = Path(state.output) if state.output else None
        if not source.is_dir():
            ui.out(f"{S.RED}Directory not found: {source}{S.RESET}", err=True)
            return RC_CANNOT_START, False
    else:
        src_text = req.source or ""
        if not src_text and ui.interactive():
            src_text = ui.ask("Source directory: ")
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
        state = RunState(source=str(source), output=str(output) if output else None)

    source = source.absolute()
    if output is not None:
        output = output.absolute()
        if output == source:
            output = None
        elif not req.dry_run:
            output.mkdir(parents=True, exist_ok=True)

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

    if s.preflight and not resuming:
        _preflight(source, output, s, req.dry_run)
    _collision_warning(source, s)

    hidden = s.include_hidden
    count_img = files.count(source, files.extension_matcher(IMAGE_EXTS), hidden)
    count_gif = files.count(source, files.extension_matcher(["gif"]), hidden)
    count_vid = files.count(source, files.extension_matcher(s.video_extensions, exclude_parts=True), hidden)
    counts = {"images": count_img, "gifs": count_gif, "clips": count_vid, "video": count_vid}
    wanted = {"images": count_img > 0, "gifs": count_gif > 0,
              "clips": count_vid > 0 and s.clip_stage, "video": count_vid > 0}
    total = sum(wanted.values())
    for name in STAGES:
        if not wanted[name]:
            state.done[name] = True
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
            rc = stage_cls(ctx).run()
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
                return rc, False
            state.done[name] = True
            state.save(req.dry_run)
    except KeyboardInterrupt:
        with proc.shielded():
            proc.stop_all()
            ui.out(f"\n\n{S.YELLOW}[INTERRUPTED] Aborted by user.{S.RESET}")
            log.line("abort", "Aborted by user")
            log.close(RC_INTERRUPTED)
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
            rc = VerifyStage(ctx).run()
            if rc == RC_INTERRUPTED:
                log.close(rc)
                return rc, False
            if rc != RC_OK:
                ui.say("NOTE", S.YELLOW, "The check reported findings. Nothing was changed;")
                ui.out(f"       repair with: media-optimizer verify {source} {output} --fix --run")

    state_file().unlink(missing_ok=True)
    rc = RC_PARTIAL if failures else RC_OK
    log.close(rc)
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


def _open(done: dict, name: str) -> str:
    return "done" if done.get(name) else "open"
