"""The settings questionnaire of a full run, as data: the browser page shows
it as a form (``schema``).

A question belongs to a setting. Its condition is data too, not code, so
the page can show and hide it: ``when=("png_mode", "lossy")``
asks only while png_mode is one of the listed values; the name ``target``
stands for "a target directory is given".

Two questions are special: ``delete`` (its default follows the mode, and
the answer always counts as chosen) and ``rescan`` (asked the other way
round than use_cache). Everything else is a yes/no question for a bool
setting and a value with its preset for the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import DOCS, RULES, Settings, groups

DELETE_TEXT = "Move originals to the trash after a successful conversion?"
RESCAN_TEXT = "Scan all files again, ignoring the caches of earlier runs (slower)?"


@dataclass(frozen=True)
class Question:
    setting: str
    text: str
    when: tuple = ()              # (setting or "target", allowed value, ...)
    special: str = ""             # "delete" or "rescan"


@dataclass(frozen=True)
class Section:
    label: str
    questions: tuple[Question, ...]
    hint: str = ""                # a section with one starts folded on the page


FULL_RUN = (
    Section("Global settings", (
        Question("preflight", "Correct wrong file extensions by content first?"),
        Question("delete_original", DELETE_TEXT, special="delete"),
        Question("verify_deep", "Fully decode image outputs to verify them (slower)?"),
        Question("clip_stage", "Also convert short silent videos to animations?"),
        Question("clip_format", "Format for them (avif, or webp for viewers that show AVIF as a still)", ("clip_stage", True)),
        Question("max_workers", "Parallel workers for images/GIFs (empty = all cores)"),
        Question("use_cache", RESCAN_TEXT, special="rescan"),
        Question("cache_write", "Remember this run's results, so the next run can skip them?", ("use_cache", True)),
    )),
    Section("Images & GIFs", (
        Question("img_target", "Image target format (jxl or webp)"),
        Question("img_webp_quality", "WebP quality, higher is better (0-100)", ("img_target", "webp")),
        Question("jxl_effort", "JXL effort, higher is slower and smaller (1-10)"),
        Question("png_mode", "PNG mode (lossless or lossy)"),
        Question("png_quality", "JXL quality for PNG, higher is better (1-100)", ("png_mode", "lossy")),
        Question("gif_method", "WebP compression level for GIFs (0-6)"),
    ), hint="Format, quality and compression"),
    Section("Video (H.265)", (
        Question("encoder_mode", "Video encoder (auto, gpu or cpu)"),
        Question("bitrate_threshold_kbps", "Bitrate above which the GPU is used (kb/s)", ("encoder_mode", "auto")),
        Question("gpu_qp", "GPU quality, lower is better (24-28)", ("encoder_mode", "auto", "gpu")),
        Question("cpu_crf", "CPU quality, lower is better (20-24)", ("encoder_mode", "auto", "cpu")),
        Question("cpu_preset", "CPU preset (medium or slow)", ("encoder_mode", "auto", "cpu")),
        Question("enable_probe", "Encode a few short test slices first to predict the saving?"),
        Question("discard_if_larger", "Discard a video if the result is larger than the original?"),
        Question("keep_salvaged_corrupt", "Keep videos salvaged from damaged sources even if they grew?"),
        Question("verify_visual", "Compare the picture against the source while converting?"),
        Question("visual_strict", "Discard a video when that comparison looks wrong?", ("verify_visual", True)),
        Question("enable_verify_output", "Re-check the finished target directory at the end?", ("target", True)),
    ), hint="Encoder, quality and checks"),
)


def shown(q: Question, s: Settings, output: Path | None) -> bool:
    """Is the question asked, given the answers so far?"""
    if not q.when:
        return True
    name, *allowed = q.when
    value = output is not None if name == "target" else getattr(s, name)
    return value in allowed


# --- for the page -------------------------------------------------------------------
def _field(name: str, s: Settings, origins: dict[str, str]) -> dict:
    rule = RULES[name]
    nullable = rule[0] == "opt"
    base = rule[1] if nullable else rule
    field = {"name": name, "kind": base[0], "nullable": nullable, "doc": DOCS[name],
             "default": getattr(Settings(), name), "value": getattr(s, name),
             "origin": origins.get(name, "default")}
    if base[0] == "choice":
        field["choices"] = list(base[1:])
    elif base[0] in ("int", "num"):
        field["min"], field["max"] = base[1], base[2]
    return field


def schema(s: Settings, origins: dict[str, str] | None = None, sections=FULL_RUN) -> dict:
    """Everything the page needs to draw the form: the questionnaire with
    its conditions, and every setting by topic with its rule, default,
    current value and where that value came from (default, config, env,
    cli). JSON-serialisable."""
    origins = origins or {}
    return {
        "questionnaire": [{"title": sec.label, "hint": sec.hint,
                           "questions": [{"setting": q.setting, "text": q.text, "when": list(q.when),
                                          "special": q.special or None} for q in sec.questions]}
                          for sec in sections],
        "groups": [{"title": title, "fields": [_field(n, s, origins) for n in names]}
                   for title, names in groups()],
    }
