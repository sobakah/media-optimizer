"""Settings: defaults, validation, config file and environment.

Every setting is a field of ``Settings`` with its rule next to it, so the
default, the check and the documentation live in one place. Values come from,
in rising precedence: built-in default < config file < environment
(``MO_<NAME>``) < interactive prompt < command line.

``delete_original`` and ``rename_inplace`` default to ``None``: "not chosen".
The mode then decides (in place: true, with a target directory: false). Any
value from the config file, the environment, a prompt or a flag is a
deliberate choice and is never overwritten by the mode.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

__version__ = "2.4"
NAME = "media-optimizer"
PROJECT_URL = "https://github.com/sobakah/media-optimizer"
ENV_PREFIX = "MO_"

# Exit codes, shared by every command.
RC_OK = 0
RC_CANNOT_START = 1      # missing tool, busy directory, missing folder
RC_BAD_SETTING = 2       # invalid option or setting
RC_PARTIAL = 3           # finished, but some files failed (originals kept)
RC_INTERRUPTED = 130     # stopped, resumable
RC_FATAL = 255           # batch aborted, e.g. an encoder vanished

CPU_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
               "slow", "slower", "veryslow", "placebo")


# Settings renamed in 0.1.x: the short-clip stage was called "avif" although
# it can write WebP. Old names give a clear message instead of "unknown key",
# and a saved resume state with old names still loads.
RENAMED = {
    "enable_avif_stage": "clip_stage",
    "avif_max_seconds": "clip_max_seconds",
    "avif_require_silent": "clip_require_silent",
    "avif_source_codecs": "clip_source_codecs",
    "anim_format": "clip_format",
    "anim_webp_quality": "clip_webp_quality",
    "anim_webp_quality_retry": "clip_webp_quality_retry",
}


class SettingsError(Exception):
    """One or more settings are invalid; ``messages`` has one line per value."""

    def __init__(self, messages: list[str]):
        super().__init__("\n".join(messages))
        self.messages = messages


# --- rules ---------------------------------------------------------------------
# A rule is a tuple: ("int", lo, hi), ("num", lo, hi), ("bool",),
# ("choice", word, ...), ("text",), ("words",) or ("paths",) (a list of
# strings that may contain spaces; ":"-separated or a JSON list as text). ("opt", rule) also allows
# None, written as "null" in JSON and as an empty value in the environment.
# ("file_or_off",) is a file path, false (off) or None (the default file).

def _setting(default: Any, rule: tuple, doc: str, *, factory=None):
    meta = {"rule": rule, "doc": doc}
    if factory is not None:
        return field(default_factory=factory, metadata=meta)
    return field(default=default, metadata=meta)


def _describe(rule: tuple) -> str:
    kind = rule[0]
    if kind == "opt":
        return _describe(rule[1]) + " (or empty)"
    if kind == "int":
        return f"a whole number from {rule[1]} to {rule[2]}"
    if kind == "num":
        return f"a number from {rule[1]} to {rule[2]}"
    if kind == "bool":
        return "true or false"
    if kind == "choice":
        return "one of: " + ", ".join(rule[1:])
    if kind == "words":
        return "a list of words"
    if kind == "paths":
        return "a list of folder paths"
    if kind == "file_or_off":
        return "a file path, false (off) or empty (the default file)"
    return "text"


def check_value(rule: tuple, value: Any) -> bool:
    """Is a typed value (from JSON or already parsed) valid for the rule?"""
    kind = rule[0]
    if kind == "opt":
        return value is None or check_value(rule[1], value)
    if kind == "bool":
        return isinstance(value, bool)
    if kind in ("int", "num"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if kind == "int" and not float(value).is_integer():
            return False
        return rule[1] <= value <= rule[2]
    if kind == "choice":
        return isinstance(value, str) and value in rule[1:]
    if kind == "words":
        return isinstance(value, list) and all(isinstance(w, str) and w and " " not in w for w in value)
    if kind == "paths":
        return isinstance(value, list) and all(isinstance(w, str) and w.strip() for w in value)
    if kind == "file_or_off":
        return value is None or value is False or (isinstance(value, str) and bool(value.strip()))
    return isinstance(value, str)


def parse_value(rule: tuple, text: str) -> Any:
    """Turn text from the environment, a prompt or the command line into a
    typed value. Raises ValueError with the expectation on bad input."""
    kind = rule[0]
    text = text.strip()
    if kind == "opt":
        return None if text == "" else parse_value(rule[1], text)
    if kind == "file_or_off":
        return None if text == "" else False if text == "false" else text
    try:
        if kind == "bool":
            # Strict on purpose: "yes" or "1" in a config is more likely a
            # mistake than a wish, and this tool deletes files.
            if text in ("true", "false"):
                return text == "true"
            raise ValueError
        if kind == "int":
            value: Any = int(text)
        elif kind == "num":
            value = float(text)
        elif kind == "words":
            value = text.split()
        elif kind == "paths":
            # A JSON list for names that contain ":" (keepbest passes one).
            if text.startswith("["):
                value = json.loads(text)
            else:
                value = [p for p in text.split(os.pathsep) if p.strip()]
        else:
            value = text
    except ValueError:
        raise ValueError(_describe(rule)) from None
    if not check_value(rule, value):
        raise ValueError(_describe(rule))
    return value


# --- the settings --------------------------------------------------------------
@dataclass
class Settings:
    # general
    max_workers: int | None = _setting(None, ("opt", ("int", 1, 256)),
        "Parallel workers for images, GIFs, AVIF and checks. null = number of CPU cores.")
    delete_original: bool | None = _setting(None, ("opt", ("bool",)),
        "Move originals to the trash after a verified conversion. null = follow the mode: "
        "true in place, false with a target directory.")
    rename_inplace: bool | None = _setting(None, ("opt", ("bool",)),
        "In place, rename 'name_h265.mp4' back to 'name.mp4' once the original is gone. "
        "null = follow the mode like delete_original.")
    force_delete: bool = _setting(False, ("bool",),
        "If the trash fails, delete permanently without asking.")
    include_hidden: bool = _setting(False, ("bool",),
        "Also process hidden files and folders. Trash and NAS system folders are always skipped.")
    subfolders: str = _setting("ask", ("choice", "ask", "include", "exclude"),
        "Subfolders of the source. ask = ask before a run when there are subfolders with images or "
        "videos (Enter: include; without a terminal or with -y: include). exclude = only the files "
        "directly in the source.")
    exclude_folders: list[str] = _setting(None, ("paths",),
        "Folders left out, relative to the source, e.g. [\"Archive\", \"2019/raw\"]; \".\" = the files "
        "directly in the source. Each entry covers only that folder's own files, not its subfolders. "
        "The custom choice of the subfolder question fills it for one run.", factory=list)
    verify_deep: bool = _setting(False, ("bool",),
        "Fully decode every image output before accepting it (slower).")
    preflight: bool = _setting(False, ("bool",),
        "Before the run, correct file extensions that do not match the content (in place only).")
    use_cache: bool = _setting(True, ("bool",),
        "Remember files that need no work next time: finished videos, and results without an output "
        "(discarded as larger, unsupported, not selected). Each stage has its own cache file; entries "
        "store size, modification time and the settings they depend on. false = check everything again.")
    log: bool = _setting(True, ("bool",), "Write the run log.")
    log_file: str | None = _setting(None, ("opt", ("text",)),
        "Path of the run log. null = media-optimizer.log in the project folder (checkout) "
        "or the state folder (installed).")
    log_max_kb: int = _setting(5120, ("int", 1, 10_000_000),
        "The log is renamed to .log.1 at the start of a run once it is larger than this.")
    journal_file: str | bool | None = _setting(None, ("file_or_off",),
        "Machine-readable journal of converted, renamed, copied and removed files (JSON Lines), "
        "read by picktag. null = journal.jsonl in the state folder, false = no journal. "
        "Independent of the run log.")

    # images
    img_target: str = _setting("jxl", ("choice", "jxl", "webp"),
        "Target format for JPEG and PNG. jxl keeps JPEG bit-exact; webp re-encodes JPEG lossily.")
    img_webp_quality: int = _setting(85, ("int", 0, 100), "Quality for lossy WebP.")
    img_discard_if_larger: bool = _setting(False, ("bool",),
        "Discard an image result that is not smaller than the original.")
    img_copy_metadata: str = _setting("auto", ("choice", "auto", "always", "never"),
        "Copy PNG metadata with exiftool. auto = when installed, always = refuse to start without it.")
    jxl_effort: int = _setting(7, ("int", 1, 10), "cjxl effort, higher is slower and smaller.")
    png_mode: str = _setting("lossless", ("choice", "lossless", "lossy"), "How PNG is converted.")
    png_quality: int = _setting(90, ("int", 1, 100), "JXL quality for PNG with png_mode lossy.")
    cjxl_threads: int = _setting(1, ("int", 0, 256),
        "Threads per cjxl process. 1 avoids N workers x N threads.")

    # GIFs
    gif_target: str = _setting("webp", ("choice", "webp", "avif"),
        "webp is lossless, avif about half the size but lossy.")
    gif_method: int = _setting(6, ("int", 0, 6), "WebP compression level for GIFs.")
    gif_kmin: int = _setting(0, ("int", 0, 100_000),
        "Minimum keyframe distance in animated WebP. 0 = no keyframes, smallest files.")
    gif_avif_crf: int = _setting(20, ("int", 0, 63), "AV1 quality for gif_target avif.")

    # shared by GIFs, clips and video
    discard_if_larger: bool = _setting(True, ("bool",),
        "Discard a GIF, clip or video result that is not smaller than the original.")

    # short silent clips -> looping animation (the "clips" command)
    clip_stage: bool = _setting(False, ("bool",),
        "Run the clip stage (short silent videos to AVIF or WebP) in a full run.")
    clip_format: str = _setting("avif", ("choice", "avif", "webp"),
        "Format for short silent videos. avif is about a third of the size, but GNOME's image "
        "viewer (glycin 2.1, Fedora 44) shows only its first frame; webp animates everywhere, "
        "and is often larger than an H.264 source.")
    clip_max_seconds: float = _setting(10.0, ("num", 0.1, 86400),
        "Only videos shorter than this go to the clip stage.")
    clip_require_silent: bool = _setting(True, ("bool",),
        "Only videos without audio. Neither AVIF nor animated WebP can store sound.")
    clip_source_codecs: list[str] = _setting(None, ("words",),
        "Source codecs the clip stage accepts.",
        factory=lambda: ["h264", "hevc", "vp8", "vp9", "av1", "mpeg4"])
    clip_webp_quality: int = _setting(50, ("int", 1, 100), "Quality for clip_format webp.")
    clip_webp_quality_retry: int = _setting(25, ("int", 0, 99),
        "Subtracted from the WebP quality for one more attempt when the result is too large.")
    avif_crf: int = _setting(32, ("int", 0, 63), "AV1 quality for clip_format avif, lower is better.")
    avif_crf_retry: int = _setting(6, ("int", 0, 63),
        "Added to the CRF for one more attempt when the result is too large.")
    avif_preset: int = _setting(6, ("int", 0, 13), "SVT-AV1 preset, lower is slower.")
    avif_pix_fmt: str = _setting("yuv420p", ("choice", "yuv420p", "yuv444p"), "AVIF pixel format.")

    # video
    video_extensions: list[str] = _setting(None, ("words",), "Video file extensions.",
        factory=lambda: ["mp4", "m4v", "mov", "mkv", "webm", "avi", "ts", "m2ts", "wmv", "flv"])
    video_container: str = _setting("auto", ("choice", "auto", "mp4", "mkv", "keep"),
        "auto = mp4 for mp4/m4v/mov sources, mkv for the rest (MP4 cannot hold Opus or SRT).")
    source_codecs: list[str] = _setting(None, ("words",),
        "Source codecs that get re-encoded. HEVC, VP9 and AV1 are already efficient.",
        factory=lambda: ["h264", "mpeg4", "msmpeg4v3", "wmv3", "vc1", "mpeg2video"])
    min_size_mb: float = _setting(5.0, ("num", 0, 10_000_000), "Skip smaller videos.")
    encoder_mode: str = _setting("auto", ("choice", "auto", "gpu", "cpu"),
        "auto picks the GPU above the bitrate threshold or from gpu_min_pixels on.")
    bitrate_threshold_kbps: int = _setting(3500, ("int", 0, 10_000_000),
        "In auto mode: below this bitrate the CPU encodes (better density).")
    gpu_min_pixels: int = _setting(2_073_600, ("int", 0, 1_000_000_000),
        "In auto mode the GPU always encodes from this frame size on. 0 disables it.")
    gpu_workers: int = _setting(2, ("int", 1, 8),
        "GPU encodes running at the same time; a CPU encode runs alongside in any case. "
        "Measured on an RX 9070 XT, six 1080p clips including the picture check: 1 = 17.6 s, "
        "2 = 12.2 s, 3 = 10.9 s.")
    gpu_qp: int = _setting(26, ("int", 0, 255), "GPU quality, lower is better.")
    cpu_crf: float = _setting(22, ("num", 0, 51), "libx265 CRF, lower is better.")
    cpu_preset: str = _setting("medium", ("choice", *CPU_PRESETS), "libx265 preset.")
    cpu_x265_params: str = _setting("aq-mode=3:no-sao=1", ("text",), "Extra x265 parameters.")
    vaapi_device: str = _setting("auto", ("text",), "VAAPI render node, or auto to try them all.")
    gpu_codec: str = _setting("hevc_vaapi", ("choice", "hevc_vaapi", "av1_vaapi", "hevc_vulkan"),
        "GPU encoder. See gpu-selftest for what works on this machine.")
    vulkan_device: str = _setting("0", ("text",), "Vulkan device for hevc_vulkan.")
    gpu_rc_mode: str = _setting("CQP", ("choice", "CQP", "VBR", "ICQ", "QVBR", "CBR", "AUTO"),
        "GPU rate control.")
    gpu_bf: int | None = _setting(None, ("opt", ("int", 0, 16)),
        "Maximum B-frames on the GPU. null = ffmpeg decides.")
    gpu_low_power: bool = _setting(False, ("bool",), "VAAPI low-power encoder.")
    gpu_async_depth: int | None = _setting(None, ("opt", ("int", 1, 64)), "VAAPI async depth.")
    gpu_bitrate: str = _setting("8M", ("text",), "Bitrate for gpu_rc_mode CBR.")
    gpu_allow_10bit: bool = _setting(False, ("bool",),
        "Encode 10-bit sources in 10 bit on the GPU (p010 + main10).")
    hevc_tag: str = _setting("", ("text",),
        "Container tag such as hvc1. Empty = ffmpeg default. Check with gpu-selftest first.")
    force_gpu: bool = _setting(False, ("bool",), "Skip the GPU startup check.")
    faststart: bool = _setting(True, ("bool",), "Move the MP4 index to the front.")
    enable_probe: bool = _setting(True, ("bool",),
        "Encode short test slices first and skip videos that would not shrink enough.")
    probe_margin_pct: int = _setting(90, ("int", 1, 100),
        "Skip when the probe reaches this percentage of the original. 100 = only when it grows.")
    probe_duration: int = _setting(6, ("int", 1, 3600), "Total seconds of test material.")
    probe_slices: int = _setting(3, ("int", 1, 100), "Number of slices the test material is split into.")
    keep_salvaged_corrupt: bool = _setting(True, ("bool",),
        "Keep a video salvaged from a damaged source even when it grew. The original is always kept.")
    duration_tolerance_pct: float = _setting(2, ("num", 0, 100),
        "Allowed runtime difference between original and output.")
    auto_fix_list: bool = _setting(False, ("bool",),
        "Process only .broken_videos.txt from the target directory when it exists.")

    # verification
    verify_visual: bool = _setting(True, ("bool",),
        "Compare the picture of every video output with its source (PSNR samples).")
    visual_psnr_min: float = _setting(18, ("num", 1, 100),
        "Below this the picture counts as suspicious. Sound re-encodes measure 31-38 dB.")
    visual_samples: int = _setting(3, ("int", 1, 100), "Picture samples per video.")
    visual_strict: bool = _setting(False, ("bool",),
        "Discard a suspicious video output instead of only warning.")
    enable_verify_output: bool = _setting(False, ("bool",),
        "After a full run with a target directory, check every video output again.")
    check_visual: bool = _setting(True, ("bool",), "The final check also compares pictures.")

    # --- helpers ---------------------------------------------------------------
    def set_text(self, name: str, text: str) -> None:
        """Set a value given as text (prompt, environment). Raises ValueError."""
        setattr(self, name, parse_value(RULES[name], text))

    def errors(self) -> list[str]:
        out = []
        for f in fields(self):
            value = getattr(self, f.name)
            if not check_value(f.metadata["rule"], value):
                out.append(f"[SETTING] {f.name}={json.dumps(value)} is invalid, "
                           f"expected {_describe(f.metadata['rule'])}.")
        return out

    def validate(self) -> None:
        problems = self.errors()
        if problems:
            raise SettingsError(problems)

    def workers(self) -> int:
        if self.max_workers:
            return self.max_workers
        count = os.process_cpu_count() if hasattr(os, "process_cpu_count") else os.cpu_count()
        return count or 4

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        s = cls()
        for name, value in data.items():
            name = RENAMED.get(name, name)
            if name in RULES:
                setattr(s, name, value)
        return s


RULES: dict[str, tuple] = {f.name: f.metadata["rule"] for f in fields(Settings)}
DOCS: dict[str, str] = {f.name: f.metadata["doc"] for f in fields(Settings)}

# Topics for the browser page's "All settings": the first setting of each,
# in the order of the fields above.
GROUP_STARTS = {"max_workers": "General", "log": "Log and journal", "img_target": "Images",
                "gif_target": "GIFs", "discard_if_larger": "GIFs, clips and video", "clip_stage": "Short clips",
                "video_extensions": "Video", "verify_visual": "Picture check"}


def groups() -> list[tuple[str, list[str]]]:
    """Every setting once, by topic, in field order."""
    out: list[tuple[str, list[str]]] = []
    for name in RULES:
        if name in GROUP_STARTS:
            out.append((GROUP_STARTS[name], []))
        out[-1][1].append(name)
    return out


def format_value(rule: tuple, value: Any) -> str:
    """A value as text that parse_value reads back to the same value: for
    MO_* variables (the browser page hands every setting to its run that
    way)."""
    if rule[0] == "opt":
        return "" if value is None else format_value(rule[1], value)
    if rule[0] == "file_or_off":
        return "" if value is None else "false" if value is False else value
    if rule[0] == "bool":
        return "true" if value else "false"
    if rule[0] == "paths":
        return json.dumps(value)                  # names may contain ":" and spaces
    if rule[0] == "words":
        return " ".join(value)
    if rule[0] == "num":
        return repr(float(value))
    return str(value)


def to_env(settings: "Settings") -> dict[str, str]:
    """Every setting as MO_<NAME>: the environment beats a config file, so
    inherited MO_* variables (keepbest's folder choice) cannot override it."""
    return {ENV_PREFIX + name.upper(): format_value(rule, getattr(settings, name)) for name, rule in RULES.items()}


# --- locations -----------------------------------------------------------------
def project_dir() -> Path | None:
    """The project folder when running from a source checkout, else None."""
    root = Path(__file__).resolve().parent.parent
    return root if (root / "pyproject.toml").is_file() else None


def config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def state_dir() -> Path:
    """Where the resume state of a full run and, when installed, the log live."""
    override = os.environ.get("MO_STATE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / NAME


def default_journal_file() -> Path:
    """Always the state folder, also from a checkout: picktag looks there."""
    return state_dir() / "journal.jsonl"


def default_log_file() -> Path:
    project = project_dir()
    return (project if project is not None else state_dir()) / "media-optimizer.log"


def config_search_paths() -> list[Path]:
    paths = []
    env = os.environ.get("MO_CONFIG")
    if env:
        return [Path(env)]
    project = project_dir()
    if project is not None:
        paths.append(project / "config.json")
    paths.append(config_home() / NAME / "config.json")
    return paths


# --- loading -------------------------------------------------------------------
def load_settings(explicit: Path | None = None, *, ignore_env: tuple[str, ...] = (),
                  origins: dict[str, str] | None = None) -> tuple[Settings, str, list[str]]:
    """Defaults, then the first config file found, then MO_* environment
    variables (except the settings in ignore_env). Returns (settings, source
    description, warnings). origins, when given, receives "config" or "env"
    for every value that did not come from the defaults.

    Invalid values raise SettingsError: a typo in a tool that deletes files
    should stop it, not be replaced by a guess. Unknown keys only warn.
    """
    settings = Settings()
    warnings: list[str] = []
    errors: list[str] = []
    source = "built-in defaults"

    paths = [explicit] if explicit else config_search_paths()
    for path in paths:
        if not path.is_file():
            if explicit is not None or os.environ.get("MO_CONFIG"):
                errors.append(f"[SETTING] Config file not found: {path}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"[SETTING] {path} is not valid JSON: {exc}")
            break
        if not isinstance(data, dict):
            errors.append(f"[SETTING] {path} must contain a JSON object.")
            break
        for key, value in data.items():
            if key.startswith("_"):
                continue                      # comments
            if key in RENAMED:
                errors.append(f"[SETTING] '{key}' in {path.name} was renamed to '{RENAMED[key]}'.")
                continue
            if key not in RULES:
                warnings.append(f"Unknown key '{key}' in {path.name} ignored.")
                continue
            if isinstance(value, int) and not isinstance(value, bool) and RULES[key][0] == "num":
                value = float(value)
            if not check_value(RULES[key], value):
                errors.append(f"[SETTING] {key}={json.dumps(value)} in {path.name} is invalid, "
                              f"expected {_describe(RULES[key])}.")
                continue
            setattr(settings, key, value)
            if origins is not None:
                origins[key] = "config"
        source = str(path)
        break

    for old, new in RENAMED.items():
        if ENV_PREFIX + old.upper() in os.environ:
            errors.append(f"[SETTING] {ENV_PREFIX}{old.upper()} was renamed to {ENV_PREFIX}{new.upper()}.")
    for name, rule in RULES.items():
        env = os.environ.get(ENV_PREFIX + name.upper())
        if env is None or name in ignore_env:
            continue
        try:
            setattr(settings, name, parse_value(rule, env))
            if origins is not None:
                origins[name] = "env"
        except ValueError as exc:
            errors.append(f"[SETTING] {ENV_PREFIX}{name.upper()}='{env}' is invalid, expected {exc}.")

    if errors:
        raise SettingsError(errors)
    return settings, source, warnings


def example_config() -> dict[str, Any]:
    """Every setting with its default and a comment, for config.example.json."""
    out: dict[str, Any] = {
        "_about": [
            f"{NAME} does NOT read this file. It lists every setting with its built-in default.",
            f"To change settings, create ~/.config/{NAME}/config.json (or pass --config FILE,",
            "or set MO_CONFIG) with only the keys you want to change. Keys starting with an",
            "underscore are comments. Every key can also be set as an environment variable",
            "MO_<KEY>, e.g. MO_MIN_SIZE_MB=0.",
        ]
    }
    for name, value in Settings().to_dict().items():
        out["_" + name] = DOCS[name]
        out[name] = value
    return out
