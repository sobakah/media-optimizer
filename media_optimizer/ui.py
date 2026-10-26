"""Terminal output: colours, badges, boxes, prompts and the progress line.

Plain ANSI via StyleUI, like the owner's other projects (keepbest, lyrembed).
Workers print from several threads, so every line goes through ``say`` /
``out``, which hold one lock and first clear a progress line that may be
standing on the terminal.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import unicodedata
from pathlib import Path

_ANSI = {
    "RESET": "\033[0m", "BOLD": "\033[1m", "DIM": "\033[2m",
    "RED": "\033[1;31m", "GREEN": "\033[1;32m", "YELLOW": "\033[1;33m",
    "BLUE": "\033[1;34m", "MAGENTA": "\033[1;35m", "CYAN": "\033[1;36m",
    "BG_GREEN": "\033[42;30m",
}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class Color(str):
    """A colour code that knows its name, also when colours are off and it
    is empty: say() reports red as an error and yellow as a warning to the
    events file either way."""

    def __new__(cls, code: str, name: str):
        obj = super().__new__(cls, code)
        obj.name = name
        return obj


class StyleUI:
    RESET = BOLD = DIM = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = BG_GREEN = ""


def colors_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None or os.environ.get("TERM", "") == "dumb":
        return False
    return sys.stdout.isatty()


def apply_color_settings() -> None:
    enabled = colors_enabled()
    for name, code in _ANSI.items():
        setattr(StyleUI, name, Color(code if enabled else "", name))


apply_color_settings()

_lock = threading.RLock()
_progress_open = False


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0                          # accents, zero-width joiner, variation selectors
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def fit_width(text: str, width: int) -> str:
    """Cut text to at most width terminal columns. CJK characters and most
    emoji take two columns: cutting by characters let such a line wrap, and
    every update of the progress line then left a line behind."""
    used = 0
    for i, ch in enumerate(text):
        used += _char_width(ch)
        if used > width:
            return text[:i]
    return text


def visible_width(text: str) -> int:
    """Terminal columns the text takes, without colour codes."""
    return sum(_char_width(ch) for ch in _ANSI_RE.sub("", text))


def pad_width(text: str, width: int) -> str:
    """Pad to width terminal columns (ljust counts characters)."""
    return text + " " * max(0, width - visible_width(text))


def _clear_progress() -> None:
    global _progress_open
    if _progress_open:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        _progress_open = False


def out(text: str = "", *, err: bool = False) -> None:
    """Print one line (or a block) without interleaving with other threads."""
    with _lock:
        _clear_progress()
        stream = sys.stderr if err else sys.stdout
        stream.write(text + "\n")
        stream.flush()


def badge(text: str, color: str) -> str:
    return f"{color}[{text}]{StyleUI.RESET}"


# Set by events.open(): receives every say() as (level, tag, text).
message_hook = None
_LEVELS = {"RED": "error", "YELLOW": "warning"}


def say(tag: str, color: str, message: str, *, err: bool = False) -> None:
    """``[TAG] message`` - the badge style of the Bash version, which the
    tests and users' log greps rely on."""
    out(f"{badge(tag, color)} {message}", err=err)
    if message_hook is not None:
        message_hook(_LEVELS.get(getattr(color, "name", ""), "info"), tag, _ANSI_RE.sub("", message))


def progress(text: str) -> None:
    """A transient status line, only on a terminal with colours, so log
    files and pipes stay clean."""
    global _progress_open
    if not (sys.stdout.isatty() and StyleUI.RESET):
        return
    width = shutil.get_terminal_size((80, 24)).columns
    with _lock:
        sys.stdout.write("\r" + fit_width(_ANSI_RE.sub("", text), max(10, width - 1)) + "\033[K")
        sys.stdout.flush()
        _progress_open = True


def progress_done() -> None:
    with _lock:
        _clear_progress()


# --- boxes ---------------------------------------------------------------------
# Fixed columns like the Bash version: 62 columns inside the frame, labels
# padded to 21, values to 37. Padding is computed on the visible text in
# terminal columns, so neither a coloured value nor a wide character (CJK,
# emoji) shifts the frame.
BOX_WIDTH = 62


def _pad(text: str, width: int) -> str:
    return pad_width(text, width)


def box(title: str, rows: list, color: str, *, footer: list | None = None) -> None:
    """``rows``/``footer``: (label, value) or (label, value, value_color),
    or a plain string for a centred line."""
    c, r, b = color, StyleUI.RESET, StyleUI.BOLD
    lines = ["", f"{c}╔{'═' * BOX_WIDTH}╗{r}"]
    lines.append(f"{c}║{r}{_center(f'{b}{title}{r}')}{c}║{r}")

    def row(item) -> str:
        if isinstance(item, str):
            return f"{c}║{r}{_center(item)}{c}║{r}"
        label, value, *vc = item
        value = f"{vc[0]}{value}{r}" if vc and vc[0] else value
        return f"{c}║{r}  {_pad(label, 21)} {_pad(value, 37)} {c}║{r}"

    if rows:
        lines.append(f"{c}╠{'═' * BOX_WIDTH}╣{r}")
        lines.extend(row(item) for item in rows)
    if footer:
        lines.append(f"{c}╟{'─' * BOX_WIDTH}╢{r}")
        lines.extend(row(item) for item in footer)
    lines.append(f"{c}╚{'═' * BOX_WIDTH}╝{r}")
    out("\n".join(lines))


def _center(text: str) -> str:
    free = BOX_WIDTH - visible_width(text)
    left = free // 2
    return " " * left + text + " " * (free - left)


def banner(title: str, color: str | None = None) -> None:
    """The three-line heading at the start of a command."""
    c = color or StyleUI.BLUE
    out(f"{c}╔{'═' * BOX_WIDTH}╗{StyleUI.RESET}\n"
        f"{c}║{StyleUI.RESET}{_center(StyleUI.BOLD + title + StyleUI.RESET)}{c}║{StyleUI.RESET}\n"
        f"{c}╚{'═' * BOX_WIDTH}╝{StyleUI.RESET}")


def card(color: str, lines: list[str]) -> None:
    """A per-file result block (┌ │ └), open on the right like the Bash version."""
    c, r = color, StyleUI.RESET
    body = [f"{c}┌{'─' * BOX_WIDTH}┐{r}"]
    body += [f"{c}│{r}  {line}" for line in lines]
    body.append(f"{c}└{'─' * BOX_WIDTH}┘{r}")
    out("\n".join(body))


def section(title: str) -> None:
    out(f"\n{StyleUI.YELLOW}─── {title} ───{StyleUI.RESET}")


# --- formatting ----------------------------------------------------------------
def format_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value, i = float(n), 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{value:.2f} {units[i]}"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def pct_change(old: int, new: int) -> str:
    """Percentage change, '0.0' for an empty original."""
    if old <= 0:
        return "0.0"
    return f"{(new / old - 1) * 100:.1f}"


# --- prompts -------------------------------------------------------------------
YES = ("j", "ja", "y", "yes")      # German answers are tolerated on input
NO = ("n", "nein", "no")


def interactive() -> bool:
    return sys.stdin.isatty()


def caller() -> str | None:
    """The program that started this one and continues in the terminal
    afterwards (MO_CALLER, set by keepbest), or None."""
    return os.environ.get("MO_CALLER", "").strip() or None


def ask(question: str) -> str:
    """input() that treats a closed stdin as an empty answer."""
    with _lock:
        _clear_progress()
    try:
        return input(question).strip()
    except EOFError:
        print()
        return ""


def prompt_bool(question: str, default: bool, *, indent: str = "  ") -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{indent}{question} [{hint}]: ").lower()
    if answer in YES:
        return True
    if answer in NO:
        return False
    return default


def prompt_text(question: str, default: str = "", *, indent: str = "  ") -> str:
    shown = f" [{default}]" if default != "" else ""
    answer = ask(f"{indent}{question}{shown}: ")
    return answer or default


def prompt_setting(settings, name: str, question: str) -> None:
    """Ask for one setting with its current value as the preset. A value that
    does not fit the rule is asked again instead of failing at the end."""
    from .config import parse_value, RULES

    current = getattr(settings, name)
    shown = " ".join(current) if isinstance(current, list) else ("" if current is None else str(current))
    if isinstance(current, bool):
        shown = "true" if current else "false"
    while True:
        answer = prompt_text(question, shown)
        try:
            setattr(settings, name, parse_value(RULES[name], answer))
            return
        except ValueError as exc:
            say("SETTING", StyleUI.RED, f"'{answer}' is not valid here, expected {exc}.")


# --- keeping the window open -----------------------------------------------------
# Started by double-click or from a .desktop file, the terminal closes as soon
# as the program ends and an error would be unreadable. MO_HOLD=1/0 forces the
# behaviour; auto keeps the window open unless the parent is an interactive
# shell (then the user started it by hand and the window stays anyway).
SHELLS = {"bash", "sh", "zsh", "dash", "fish", "ksh", "ksh93", "tcsh", "csh", "elvish", "nu"}


# Programs that start the tool for a shell: the shell is one step up.
WRAPPERS = {"sudo", "doas", "env", "nice", "ionice", "nohup", "time", "chrt", "taskset", "uv", "uvx",
            "poetry", "pipx", "pdm", "hatch", "direnv"}


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def _parent_of(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat[stat.rindex(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _program(argv: list[str]) -> str:
    return argv[0].lstrip("-").rsplit("/", 1)[-1] if argv else ""


def interactive_shell(argv: list[str]) -> bool:
    """A shell someone types into. Options alone do not make it a script:
    'fish --no-config' and 'bash --norc' held the window open after every
    run. A command (-c) or a script file does."""
    if _program(argv) not in SHELLS:
        return False
    for arg in argv[1:]:
        if arg in ("-i", "--interactive"):
            return True
        if arg == "--command" or arg.startswith("--command="):
            return False
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            return False                # -c, -lc
        if not arg.startswith("-"):
            return False                # a script
    return True


def _parent_is_interactive_shell() -> bool:
    pid = os.getppid()
    for _ in range(5):
        argv = _cmdline(pid)
        if _program(argv) not in WRAPPERS:
            return interactive_shell(argv)
        pid = _parent_of(pid)           # sudo, uv run, ...: the shell is further up
    return False


def should_hold(mode: str | None) -> bool:
    mode = (mode or os.environ.get("MO_HOLD", "auto")).lower()
    if mode in ("1", "true", "yes"):
        return True
    if mode in ("0", "false", "no"):
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    return not _parent_is_interactive_shell()


def hold_open(rc: int, mode: str | None) -> None:
    from .config import RC_PARTIAL

    if rc == RC_PARTIAL:
        say("DONE", StyleUI.YELLOW, "Finished, but some files failed (see the summary above).", err=True)
    elif rc != 0:
        say("ABORT", StyleUI.RED, f"Exited with code {rc}.", err=True)
    if not should_hold(mode):
        return
    if rc == 0:
        out()
        say("DONE", StyleUI.GREEN, "Finished without errors.")
    try:
        timeout = int(os.environ.get("MO_HOLD_TIMEOUT", "0") or 0)
    except ValueError:                  # a typo must not end the run with a traceback
        timeout = 0
    try:
        with open("/dev/tty") as tty:
            if timeout > 0:
                import select
                print(f"Window closes in {timeout}s, or press Enter. ", end="", flush=True)
                select.select([tty], [], [], timeout)
            else:
                print("Window stays open. Press Enter to close. ", end="", flush=True)
                tty.readline()
    except OSError:
        pass
    print()
