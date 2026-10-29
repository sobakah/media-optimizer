"""Command line: one program with a subcommand per stage.

    media-optimizer [run] SOURCE [TARGET]     full run (the default command)
    media-optimizer images SOURCE [TARGET]    JPEG/PNG -> JXL or WebP
    media-optimizer gifs SOURCE [TARGET]      GIF -> WebP or AVIF
    media-optimizer clips SOURCE [TARGET]     short silent videos -> AVIF or WebP
    media-optimizer video SOURCE [TARGET]     video -> HEVC
    media-optimizer verify SOURCE TARGET      check outputs against originals
    media-optimizer gpu-selftest FILE         which GPU options work here

Options that change a setting are checked with the same rules as the config
file, so a bad value stops with exit code 2 before anything runs.
"""

from __future__ import annotations

import argparse
import shlex
import signal
import sys
import threading
from pathlib import Path

from . import files, folders, gpu, proc, selfupdate, ui
from .config import (NAME, RC_BAD_SETTING, RC_CANNOT_START, RC_INTERRUPTED, RULES, Settings,
                     SettingsError, __version__, load_settings, parse_value, state_dir)
from .events import events
from .journal import journal
from .results import results
from .runlog import log
from .stages.base import Context, apply_mode_defaults, prompt_delete_original, prompt_rescan, reset_notices
from .ui import StyleUI as S

COMMANDS = ("run", "images", "gifs", "clips", "video", "verify", "gpu-selftest")
_SET = "set__"            # namespace prefix for options that set a setting


def _typed(name: str):
    def convert(text: str):
        try:
            return parse_value(RULES[name], text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"'{text}' is invalid, expected {exc}") from None
    convert.__name__ = name
    return convert


def _value(group, *flags: str, setting: str, metavar: str, help: str) -> None:
    group.add_argument(*flags, dest=_SET + setting, type=_typed(setting), metavar=metavar,
                       default=argparse.SUPPRESS, help=help)


def _flag(group, *flags: str, setting: str, value, help: str) -> None:
    group.add_argument(*flags, dest=_SET + setting, action="store_const", const=value,
                       default=argparse.SUPPRESS, help=help)


def _common(p: argparse.ArgumentParser, *, dirs: str = "optional", workers: bool = True,
            delete: bool = True, folders: bool = True) -> None:
    if dirs != "none":
        p.add_argument("source", nargs="?", help="source directory")
        p.add_argument("target", nargs="?", help="target directory (empty = in place)")
        p.add_argument("-i", "--input", metavar="DIR", help="source directory")
        p.add_argument("-o", "--output", metavar="DIR", help="target directory (empty = in place)")
    g = p.add_argument_group("general")
    if workers:
        _value(g, "-j", "--workers", setting="max_workers", metavar="N", help="parallel workers (default: cores)")
    if delete:
        _flag(g, "--delete", setting="delete_original", value=True,
              help="move originals to the trash after success")
        _flag(g, "--no-delete", setting="delete_original", value=False, help="keep originals")
        _flag(g, "--force-delete", setting="force_delete", value=True,
              help="if the trash fails, delete permanently without asking")
    if folders:
        _value(g, "--subfolders", setting="subfolders", metavar="MODE",
               help="ask | include | exclude (default: ask when there are any; include without a terminal)")
        g.add_argument("--exclude-folder", dest=_SET + "exclude_folders", action="append", metavar="DIR",
                       default=argparse.SUPPRESS,
                       help='leave out the files of this folder (relative to the source, "." = the source '
                            "itself); repeatable")
    g.add_argument("-c", "--config", type=Path, metavar="FILE", help="use this config file")
    g.add_argument("-y", "--yes", action="store_true", help="do not ask, keep the settings as given")
    g.add_argument("-n", "--dry-run", action="store_true", help="preview only, write nothing")
    g.add_argument("--log", dest=_SET + "log_file", metavar="FILE", default=argparse.SUPPRESS,
                   help="log file")
    _flag(g, "--no-log", setting="log", value=False, help="do not write a log")
    _value(g, "--journal", setting="journal_file", metavar="FILE",
           help="file journal for picktag (default: journal.jsonl in the state folder)")
    _flag(g, "--no-journal", setting="journal_file", value=False, help="do not write the file journal")
    if dirs != "none":
        g.add_argument("--events", type=Path, metavar="FILE",
                       help="write the run's progress as JSON lines to FILE (for programs that show it)")
        g.add_argument("--results", type=Path, metavar="FILE",
                       help="write the run's results as JSON to FILE when it ends (for the program that started it)")
    g.add_argument("--hold", dest="hold", action="store_const", const="1", help="keep the window open at the end")
    g.add_argument("--no-hold", dest="hold", action="store_const", const="0", help="never keep the window open")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=NAME, description="Batch-convert images, GIFs and videos to more efficient formats.",
        epilog="Without a command, 'run' is assumed: media-optimizer ~/Pictures")
    parser.add_argument("--version", action="version", version=f"{NAME} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- run ---
    p = sub.add_parser("run", help="full run: images, GIFs, clips (optional), video")
    _common(p)
    g = p.add_argument_group("full run")
    _flag(g, "--verify-deep", setting="verify_deep", value=True, help="fully decode image outputs (slower)")
    _flag(g, "--preflight", setting="preflight", value=True, help="fix file extensions by content beforehand")
    _flag(g, "--verify-visual", setting="verify_visual", value=True,
          help="compare picture content while converting (default)")
    _flag(g, "--no-verify-visual", setting="verify_visual", value=False, help="skip that comparison")
    _flag(g, "--strict-visual", setting="visual_strict", value=True,
          help="discard suspicious video outputs instead of warning")
    _flag(g, "--no-cache", setting="use_cache", value=False, help="ignore the caches for this run")
    _flag(g, "--cache", setting="use_cache", value=True, help="use the caches (default)")
    _flag(g, "--verify-output", setting="enable_verify_output", value=True,
          help="re-check the finished target directory afterwards")
    _flag(g, "--no-verify-output", setting="enable_verify_output", value=False, help="skip that check (default)")
    _flag(g, "--clips", setting="clip_stage", value=True,
          help="extra stage: short silent videos to AVIF (or WebP, clip_format)")
    _value(g, "--clip-format", setting="clip_format", metavar="F", help="avif | webp for that stage")
    _value(g, "--gpu-workers", setting="gpu_workers", metavar="N", help="GPU encodes at the same time (default: 2)")
    _flag(g, "--no-clips", setting="clip_stage", value=False, help="skip that stage (default)")
    g.add_argument("--reset", action="store_true", help="discard the saved state and start over")
    g = p.add_argument_group("browser page")
    g.add_argument("--web", action="store_true", help="ask for the settings and show the run in the browser")
    g.add_argument("--no-browser", action="store_true", help="with --web: do not open a tab, only print the address")
    g.add_argument("--return-url", metavar="URL",
                   help="with --web: the page's last button leads there (only a page on this computer)")
    g.add_argument("--return-name", metavar="NAME", default="",
                   help="with --return-url: the name on that button (default: 'the previous page')")

    # --- images ---
    p = sub.add_parser("images", help="JPEG/PNG -> JPEG XL or WebP")
    _common(p)
    g = p.add_argument_group("images")
    _value(g, "--target", setting="img_target", metavar="F", help="jxl | webp (default: jxl)")
    _value(g, "--webp-quality", setting="img_webp_quality", metavar="Q", help="quality for lossy WebP (default: 85)")
    _flag(g, "--discard-larger", setting="img_discard_if_larger", value=True,
          help="discard the result if larger than the original")
    _value(g, "--copy-metadata", setting="img_copy_metadata", metavar="M",
           help="auto | always | never: copy PNG metadata with exiftool")
    _value(g, "-e", "--effort", setting="jxl_effort", metavar="N", help="JXL effort 1-10 (default: 7)")
    _value(g, "--png-mode", setting="png_mode", metavar="M", help="lossless | lossy (default: lossless)")
    _value(g, "--png-quality", setting="png_quality", metavar="Q", help="with --png-mode lossy, 1-100")
    _value(g, "--cjxl-threads", setting="cjxl_threads", metavar="N", help="threads per cjxl process (default: 1)")
    _flag(g, "--verify-deep", setting="verify_deep", value=True, help="fully decode the output")
    _flag(g, "--no-cache", setting="use_cache", value=False, help="ignore the cache for this run")
    _flag(g, "--cache", setting="use_cache", value=True, help="use the cache (default)")

    # --- gifs ---
    p = sub.add_parser("gifs", help="GIF -> animated WebP or AVIF")
    _common(p)
    g = p.add_argument_group("GIFs")
    _value(g, "-m", "--method", setting="gif_method", metavar="N", help="WebP compression level 0-6 (default: 6)")
    _value(g, "--target", setting="gif_target", metavar="F", help="webp | avif (default: webp)")
    _value(g, "--avif-crf", setting="gif_avif_crf", metavar="N", help="AV1 quality with --target avif (default: 20)")
    _flag(g, "--keep-larger", setting="discard_if_larger", value=False, help="keep results that are larger")
    _flag(g, "--verify-deep", setting="verify_deep", value=True, help="check the output with webpinfo")
    _flag(g, "--no-cache", setting="use_cache", value=False, help="ignore the cache for this run")
    _flag(g, "--cache", setting="use_cache", value=True, help="use the cache (default)")

    # --- avif ---
    p = sub.add_parser("clips", help="short, silent videos -> looping animation (AVIF or WebP)")
    _common(p)
    g = p.add_argument_group("short clips")
    _value(g, "--format", setting="clip_format", metavar="F",
           help="avif | webp (default: avif; webp also animates in GNOME's image viewer)")
    _value(g, "--webp-quality", setting="clip_webp_quality", metavar="Q", help="quality with --format webp (default: 50)")
    _value(g, "--max-seconds", setting="clip_max_seconds", metavar="S", help="only videos shorter than S (default: 10)")
    _value(g, "--extensions", setting="video_extensions", metavar="LIST", help="source extensions, space separated")
    _value(g, "--crf", setting="avif_crf", metavar="N", help="AV1 quality 0-63 (default: 32)")
    _value(g, "--crf-retry", setting="avif_crf_retry", metavar="N", help="penalty for the second attempt (default: 6)")
    _value(g, "--preset", setting="avif_preset", metavar="N", help="SVT-AV1 preset 0-13 (default: 6)")
    _value(g, "--pix-fmt", setting="avif_pix_fmt", metavar="P", help="yuv420p | yuv444p")
    _flag(g, "--allow-audio", setting="clip_require_silent", value=False,
          help="also convert videos with audio (the audio is lost)")
    _flag(g, "--keep-larger", setting="discard_if_larger", value=False, help="keep results that are larger")
    _flag(g, "--no-verify", setting="verify_visual", value=False, help="no picture comparison")
    _flag(g, "--no-cache", setting="use_cache", value=False, help="ignore the cache for this run")
    _flag(g, "--cache", setting="use_cache", value=True, help="use the cache (default)")

    # --- video ---
    p = sub.add_parser("video", help="video -> HEVC on the GPU or CPU")
    _common(p, workers=False)
    g = p.add_argument_group("video")
    _value(g, "--extensions", setting="video_extensions", metavar="LIST", help="source extensions, space separated")
    _value(g, "--container", setting="video_container", metavar="C", help="auto | mp4 | mkv | keep")
    _value(g, "--source-codecs", setting="source_codecs", metavar="LIST", help="source codecs to re-encode")
    _value(g, "--encoder", setting="encoder_mode", metavar="MODE", help="auto | gpu | cpu (default: auto)")
    _value(g, "--threshold", setting="bitrate_threshold_kbps", metavar="KBPS", help="CPU/GPU threshold (default: 3500)")
    _value(g, "--gpu-min-pixels", setting="gpu_min_pixels", metavar="N",
           help="in auto mode, always the GPU from this frame size on")
    _value(g, "--qp", setting="gpu_qp", metavar="N", help="GPU quality (default: 26)")
    _value(g, "--gpu-workers", setting="gpu_workers", metavar="N", help="GPU encodes at the same time (default: 2)")
    _value(g, "--crf", setting="cpu_crf", metavar="N", help="libx265 CRF (default: 22)")
    _value(g, "--preset", setting="cpu_preset", metavar="P", help="libx265 preset (default: medium)")
    _value(g, "--x265-params", setting="cpu_x265_params", metavar="S", help="x265 parameters")
    _value(g, "--min-size", setting="min_size_mb", metavar="MB", help="skip smaller files (default: 5)")
    _flag(g, "--no-probe", setting="enable_probe", value=False, help="no test slices beforehand")
    _value(g, "--probe-margin", setting="probe_margin_pct", metavar="P", help="skip at P%% of the original (default: 90)")
    _value(g, "--probe-duration", setting="probe_duration", metavar="S", help="total test material (default: 6)")
    _value(g, "--probe-slices", setting="probe_slices", metavar="N", help="slices it is split into (default: 3)")
    _flag(g, "--keep-larger", setting="discard_if_larger", value=False, help="keep results that are larger")
    _flag(g, "--no-faststart", setting="faststart", value=False, help="do not move the MP4 index to the front")
    _value(g, "--hevc-tag", setting="hevc_tag", metavar="T", help="container tag, e.g. hvc1")
    _value(g, "--gpu-codec", setting="gpu_codec", metavar="C", help="hevc_vaapi | av1_vaapi | hevc_vulkan")
    _value(g, "--gpu-device", setting="vaapi_device", metavar="PATH", help='render node, or "auto"')
    _flag(g, "--force-gpu", setting="force_gpu", value=True, help="skip the GPU startup check")
    _value(g, "--rc-mode", setting="gpu_rc_mode", metavar="M", help="CQP | VBR | ICQ | QVBR | CBR | AUTO")
    _value(g, "--bf", setting="gpu_bf", metavar="N", help="maximum B-frames on the GPU")
    _flag(g, "--low-power", setting="gpu_low_power", value=True, help="VAAPI low-power encoder")
    _flag(g, "--gpu-10bit", setting="gpu_allow_10bit", value=True, help="encode 10-bit sources in 10 bit")
    _flag(g, "--no-verify-visual", setting="verify_visual", value=False, help="no picture comparison")
    _flag(g, "--strict-visual", setting="visual_strict", value=True, help="discard suspicious outputs")
    _flag(g, "--rename-inplace", setting="rename_inplace", value=True,
          help="drop the _h265 suffix once the original is gone (in place only)")
    _flag(g, "--no-cache", setting="use_cache", value=False, help="ignore the cache for this run")
    _flag(g, "--cache", setting="use_cache", value=True, help="use the cache (default)")
    g.add_argument("--from-list", metavar="FILE|auto",
                   help='only the source files listed there; "auto" = .broken_videos.txt of the target')

    # --- verify ---
    p = sub.add_parser("verify", help="check converted videos against their originals")
    _common(p, delete=False, folders=False)
    g = p.add_argument_group("check")
    _value(g, "--psnr-min", setting="visual_psnr_min", metavar="DB", help="threshold for suspicion (default: 18)")
    _value(g, "--duration-tol", setting="duration_tolerance_pct", metavar="P",
           help="allowed runtime deviation in percent (default: 2)")
    _value(g, "--samples", setting="visual_samples", metavar="N", help="samples per file (default: 3)")
    _value(g, "--extensions", setting="video_extensions", metavar="LIST", help="output extensions to check")
    _flag(g, "--no-visual", setting="check_visual", value=False, help="only readability and runtime")
    g.add_argument("--keep-samples", type=Path, metavar="DIR", help="store the compared stills in DIR")
    g.add_argument("--fix", action="store_true", help="delete broken outputs and drop them from the cache")
    g.add_argument("--run", action="store_true", help="after --fix, regenerate them right away")

    # --- gpu-selftest ---
    p = sub.add_parser("gpu-selftest", help="try GPU option variants on a real file")
    p.add_argument("file", type=Path, help="a video to test with")
    _common(p, dirs="none", workers=False, delete=False, folders=False)
    g = p.add_argument_group("GPU")
    _value(g, "--gpu-device", setting="vaapi_device", metavar="PATH", help='render node, or "auto"')
    _value(g, "--qp", setting="gpu_qp", metavar="N", help="GPU quality (default: 26)")
    return parser


def install_signal_handlers() -> None:
    """Ctrl+C, SIGTERM and a closed terminal all stop the run the same way.
    Installed explicitly: a program started in the background of a
    non-interactive shell inherits SIGINT as ignored, and a handled signal is
    reset to the default in every child, so the encoders can be stopped."""
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, interrupt)


# The folder choice belongs to the directory it was made for (keepbest passes
# its own in the environment); "process another directory" starts without it.
FOLDER_CHOICE = ("subfolders", "exclude_folders")


def _settings(args, *, folder_choice: bool = True, origins: dict[str, str] | None = None) -> Settings:
    """origins, when given, receives where each value came from (config,
    env, cli), for the browser page's presets."""
    ignore = () if folder_choice else FOLDER_CHOICE
    settings, source, warnings = load_settings(args.config, ignore_env=ignore, origins=origins)
    for message in warnings:
        ui.say("WARNING", S.YELLOW, message, err=True)
    for key, value in vars(args).items():
        if key.startswith(_SET) and key[len(_SET):] not in ignore:
            setattr(settings, key[len(_SET):], value)
            if origins is not None:
                origins[key[len(_SET):]] = "cli"
    return settings


def _check_options(parser: argparse.ArgumentParser, args) -> None:
    """Combinations argparse cannot see. As lyrembed's: the return URL only to a page on this computer."""
    from .web import local_page

    if getattr(args, "input", None) and args.source and (args.target or args.output):
        # "-i A B C": C was ignored without a word.
        parser.error("-i with two folders, or with -o: which one is the target?")
    if args.command != "run":
        return
    if not args.web:
        for flag, given in (("--no-browser", args.no_browser), ("--return-url", args.return_url),
                            ("--return-name", args.return_name)):
            if given:
                parser.error(f"{flag} only applies together with --web")
        return
    if args.yes:
        parser.error("--web and -y exclude each other: the page asks for the settings")
    if args.return_name and not args.return_url:
        parser.error("--return-name only applies together with --return-url")
    if args.return_url and not local_page(args.return_url):
        parser.error("--return-url must be a page on this computer, such as http://127.0.0.1:8000/")


def _dirs(args) -> tuple[str | None, str | None]:
    source = args.input or args.source
    output = args.output or args.target
    if args.input and args.source:          # "-i A B": the positional is the target
        output = args.source
    return source, output


def _stage_menu(args, s: Settings, title: str, source: str, output: str) -> tuple[str, str]:
    """The settings menu of a single stage: only with a terminal, without -y,
    and when the source was not given on the command line."""
    ui.out(f"{S.CYAN}{'═' * ui.BOX_WIDTH}{S.RESET}\n{S.BOLD}{title}{S.RESET}\n{S.CYAN}{'═' * ui.BOX_WIDTH}{S.RESET}")
    defaults = ui.prompt_bool("Run with the default settings?", True, indent="")
    # Asked with the defaults too: they ended in "No source directory given".
    source = ui.prompt_text("Source directory", source or files.suggested_source())
    output = ui.prompt_text("Target directory (empty = in place)", output)
    if defaults:
        return source, output
    out_path = Path(output).expanduser() if output else None
    if args.command == "verify":
        s.check_visual = ui.prompt_bool("Compare the picture content as well (PSNR sampling)?", s.check_visual)
        if s.check_visual:
            ui.prompt_setting(s, "visual_psnr_min", "PSNR threshold in dB")
            ui.prompt_setting(s, "visual_samples", "Samples per file")
            if ui.prompt_bool("Store the compared stills for inspection?", False):
                args.keep_samples = Path(ui.prompt_text("Directory for the stills",
                                                        str(args.keep_samples or "/tmp/mo-samples")))
        ui.prompt_setting(s, "duration_tolerance_pct", "Allowed runtime deviation in percent")
        args.fix = ui.prompt_bool("Delete broken outputs and drop them from the cache?", args.fix)
        if args.fix:
            args.run = ui.prompt_bool("Start the repair run afterwards?", args.run)
        ui.out()
        return source, output
    prompt_delete_original(s, out_path)
    if args.command == "images":
        ui.prompt_setting(s, "jxl_effort", "JXL effort (1-10)")
        ui.prompt_setting(s, "png_mode", "PNG mode (lossless/lossy)")
    elif args.command == "gifs":
        ui.prompt_setting(s, "max_workers", "Parallel workers (empty = all cores)")
        ui.prompt_setting(s, "gif_method", "Compression level (0-6)")
    elif args.command == "video":
        ui.prompt_setting(s, "encoder_mode", "Encoder (auto/gpu/cpu)")
    prompt_rescan(s)
    return source, output


TITLES = {
    "images": "Image converter: JPG/PNG -> {target}",
    "gifs": "Batch converter: GIF to animated {target}",
    "clips": "Short silent clips to animated {target}",
    "video": "Batch video re-encoder (H.264 -> H.265 / HEVC)",
    "verify": "Output check: compare converted videos with their originals",
}


def _run_stage(args, s: Settings) -> int:
    from .stages.clips import ClipStage
    from .stages.gifs import GifStage
    from .stages.images import ImageStage
    from .stages.verify import VerifyStage
    from .stages.video import VideoStage

    source, output = _dirs(args)
    if ui.interactive() and not args.yes and not source:
        target = {"images": "JXL (with WebP fallback)" if s.img_target == "jxl" else "WebP",
                  "gifs": "WebP" if s.gif_target == "webp" else "AVIF (lossy)",
                  "clips": s.clip_format.upper()}.get(args.command, "")
        source, output = _stage_menu(args, s, TITLES[args.command].format(target=target),
                                     source or "", output or "")
    if args.command == "verify" and (not source or not output):
        ui.out("Both source and target directory are required.", err=True)
        return RC_BAD_SETTING
    if not source:
        ui.out("No source directory given.", err=True)
        return RC_CANNOT_START
    src = Path(source).expanduser().absolute()
    if not src.is_dir():
        ui.out(f"Source directory not found: {src}", err=True)
        return RC_CANNOT_START
    out = Path(output).expanduser().absolute() if output else None
    if args.command == "verify":
        if not out.is_dir():
            ui.out(f"Target directory not found: {out}", err=True)
            return RC_CANNOT_START
        if out == src:
            ui.out("Source and target are identical. The check is not meant for in-place mode.", err=True)
            return RC_BAD_SETTING
    elif out == src:
        out = None
    if args.command != "verify":
        folders.resolve(src, s, assume_yes=args.yes)
    if out is not None and not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)

    ctx = Context(s, src, out, dry_run=args.dry_run, assume_yes=args.yes)
    if args.command != "verify":
        apply_mode_defaults(ctx, with_rename=args.command == "video")
    tag = {"images": "img", "gifs": "gif", "clips": "clips", "video": "h265", "verify": "verify"}[args.command]
    log.open(enabled=s.log, log_file=s.log_file, max_kb=s.log_max_kb, dry_run=args.dry_run,
             call=f"{NAME} {shlex.join(sys.argv[1:])}", source=src, target=out, tag=tag)
    if args.command != "verify" or args.fix:            # a plain verify changes nothing
        journal.start(s.journal_file, dry_run=args.dry_run, source=src, target=out)
    events.run_start(args.command, src, out, args.dry_run)
    events.emit("stage_start", stage=args.command, total=None)     # the command names are the stage names
    if args.command == "images":
        stage = ImageStage(ctx)
    elif args.command == "gifs":
        stage = GifStage(ctx)
    elif args.command == "clips":
        stage = ClipStage(ctx)
    elif args.command == "video":
        stage = VideoStage(ctx, from_list=args.from_list)
    else:
        stage = VerifyStage(ctx, keep_dir=args.keep_samples, fix=args.fix, run_repair=args.run)
    rc = stage.run()
    events.stage_end(args.command, rc, stage)
    log.close(rc)
    journal.end(rc)
    events.run_end(rc)
    return rc


# How long a full run waits for the update check before it starts (it goes on
# in the background and is remembered for the next start).
UPDATE_WAIT = 3.0


def _checks_for_updates(args, s: Settings) -> bool:
    """Only a full run started by a person checks: not one started by keepbest
    (it checks itself) and not the run the page starts (--events)."""
    return s.check_for_updates and not ui.caller() and getattr(args, "events", None) is None \
        and not getattr(args, "return_url", None)


def _newer_version() -> str | None:
    found: list[str | None] = [None]

    def check() -> None:
        found[0] = selfupdate.available()

    thread = threading.Thread(target=check, name="mo-update-check", daemon=True)
    thread.start()
    thread.join(UPDATE_WAIT)
    return found[0]


def _offer_update(version: str, assume_yes: bool) -> None:
    """Before a full run in the terminal: install a newer version and start it
    with the same command, or show the command."""
    how = selfupdate.how_installed(version)
    ui.say("UPDATE", S.CYAN, f"{NAME} {version} is available (this is {__version__}).")
    if how.commands and ui.interactive() and not assume_yes:
        if not ui.prompt_bool("Install it now? The run then starts with the new version", False, indent=""):
            ui.out()
            return
        code, _ = selfupdate.install(how.commands, state_dir() / "update.log", show=True)
        if code == 0:
            ui.say("UPDATE", S.GREEN, f"{NAME} {version} is installed; starting it.")
            selfupdate.restart(selfupdate.restart_argv())
        ui.say("UPDATE", S.RED, f"The update didn't work (exit code {code}); this run uses {__version__}.")
    ui.out("  To update:")
    for line in how.command.splitlines():
        ui.out(f"    {line}")
    ui.out()


def _run_web(args) -> int:
    from . import web

    origins: dict[str, str] = {}
    s = _settings(args, origins=origins)
    s.validate()
    source, output = _dirs(args)
    # Without a folder: the current one, by the rule of the terminal's question.
    source = source or files.suggested_source() or None
    session = web.Session(s, origins, Path(source).expanduser().absolute() if source else None,
                          Path(output).expanduser().absolute() if output else None,
                          (args.return_url, args.return_name) if args.return_url else None,
                          dry_run=args.dry_run, reset=args.reset, results=args.results)
    return web.serve(session, open_browser=not args.no_browser, check_updates=_checks_for_updates(args, s))


def _run_full(args) -> int:
    from . import orchestrator

    if args.web:
        return _run_web(args)
    source, output = _dirs(args)
    first = True
    while True:
        s = _settings(args, folder_choice=first)
        s.validate()
        if first and _checks_for_updates(args, s):
            version = _newer_version()
            if version is not None:
                _offer_update(version, args.yes)
        req = orchestrator.Request(settings=s, source=source if first else None,
                                   output=output if first else None,
                                   dry_run=args.dry_run, assume_yes=args.yes, reset=args.reset and first,
                                   call=f"{NAME} {shlex.join(sys.argv[1:])}")
        rc, again = orchestrator.run(req)
        if not again:
            return rc
        first = False
        proc.reset()
        reset_notices()
        ui.out(f"\n{S.CYAN}─── New run ───{S.RESET}\n")


# The options of _common that take a value, to find a command after them.
def value_options(parser: argparse.ArgumentParser) -> set[str]:
    """Every option of any command that takes a value. All of them, not only
    the general ones: '--gpu-workers 2 video DIR' was a full run of a folder
    called "video" into DIR."""
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {o for p in sub.choices.values() for a in p._actions if a.nargs != 0 for o in a.option_strings}


def _command_first(argv: list[str], parser: argparse.ArgumentParser) -> list[str]:
    """Options before the command belong to it: '-c cfg images DIR' was a full
    run of a source folder called "images". Without a command: run."""
    taking = value_options(parser)
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        i += 2 if argv[i] in taking else 1
    if i < len(argv) and argv[i] in COMMANDS:
        return [argv[i], *argv[:i], *argv[i + 1:]]
    return ["run", *argv]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "avif" and not Path("avif").is_dir():
        ui.out("The command 'avif' was renamed to 'clips' (it also writes WebP): "
               "media-optimizer clips --help", err=True)
        return RC_BAD_SETTING
    parser = build_parser()
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv = _command_first(argv, parser)
    args = parser.parse_args(argv)
    _check_options(parser, args)
    install_signal_handlers()
    proc.reset()
    reset_notices()
    if getattr(args, "events", None) is not None:
        events.open(args.events)
    if getattr(args, "results", None) is not None:
        results.open(args.results)

    rc = 0
    try:
        if args.command == "run":
            rc = _run_full(args)
        else:
            s = _settings(args)
            s.validate()
            if args.command == "gpu-selftest":
                rc = gpu.selftest(s, args.file)
            else:
                rc = _run_stage(args, s)
    except SettingsError as exc:
        for line in exc.messages:
            ui.out(f"{S.RED}{line}{S.RESET}", err=True)
        rc = RC_BAD_SETTING
    except KeyboardInterrupt:
        with proc.shielded():
            proc.stop_all()
        ui.out("\nStopped.", err=True)
        rc = RC_INTERRUPTED
    finally:
        log.close(rc)
        journal.end(rc)             # Ctrl+C, SIGTERM, a closed terminal: best effort
        events.run_end(rc)
        events.close()
        results.close()
    ui.hold_open(rc, getattr(args, "hold", None))
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
