"""A newer media-optimizer: found by the release tags on GitHub, installed by pipx.

The same approach as picktag and keepbest. The repository is private, so the
check asks git (``git ls-remote``), which uses the same access as ``pipx
install`` (``gh auth setup-git``); it never prompts for a password. At every
start, in a thread, so the start doesn't wait; the newest version found is
remembered in the state folder (``known``), so a full run in the terminal can
offer it before it starts without asking GitHub first. Release tags have two
or three parts (``1.0``, ``0.8.3``). A pipx install is updated by pipx: ``pipx
upgrade`` for an install from the repository, ``pipx install --force`` with
the new tag for one pinned to a release (it stays pinned; injected packages
are injected again). Any other install gets the command to run, not an offer.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import NAME, PROJECT_URL, __version__, state_dir

REPO = PROJECT_URL
# Another repository to ask (a local one in the tests, so they never reach GitHub).
REPO_ENV = "MO_UPDATE_REPO"
STATE_FILE = "update_check.json"
_VERSION = r"(\d+)\.(\d+)(?:\.(\d+))?"
_TAG = re.compile(rf"refs/tags/{_VERSION}$")


class UpdateError(Exception):
    pass


def parse(version: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(_VERSION, version.strip())
    return tuple(int(n or 0) for n in m.groups()) if m else None


def newer(version: str | None, than: str = __version__) -> bool:
    a, b = parse(version or ""), parse(than)
    return a is not None and b is not None and a > b


def repo() -> str:
    return os.environ.get(REPO_ENV) or REPO


def _git_env() -> dict[str, str]:
    """No password prompt, neither in a terminal nor in a window."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_ASKPASS", "SSH_ASKPASS")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def latest(url: str | None = None, timeout: float = 30) -> str:
    """The newest release tag of *url* (``repo()``), as it is written (``1.1``)."""
    git = shutil.which("git")
    if git is None:
        raise UpdateError("git isn't installed")
    try:
        r = subprocess.run([git, "-c", "core.askPass=", "ls-remote", "--tags", "--refs", url or repo()],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                           env=_git_env(), start_new_session=True)
    except subprocess.TimeoutExpired:
        raise UpdateError("GitHub didn't answer") from None
    except OSError as exc:
        raise UpdateError(f"git didn't start: {exc}") from None
    if r.returncode != 0:
        last = (r.stderr.strip().splitlines() or [f"git ended with code {r.returncode}"])[-1]
        raise UpdateError(last.removeprefix("fatal: "))
    tags = [line.rsplit("/", 1)[1] for line in r.stdout.splitlines() if _TAG.search(line)]
    if not tags:
        raise UpdateError("no releases found")
    return max(tags, key=parse)


def _state_path() -> Path:
    return state_dir() / STATE_FILE


def _load_state() -> dict:
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def known() -> str | None:
    """The newer release the last check found, without asking GitHub: for a
    start that mustn't wait (a full run in the terminal offers it before it starts)."""
    state = _load_state()
    found = state.get("latest") if isinstance(state.get("latest"), str) else None
    return found if newer(found) else None


def available() -> str | None:
    """A newer release than this media-optimizer, or None. GitHub is asked at
    every start (callers run this in a thread, so the start doesn't wait); when
    it can't be reached, the one found last time counts."""
    try:
        found = latest()
    except UpdateError:
        return known()
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked": time.time(), "latest": found}), encoding="utf-8")
    except OSError:
        pass
    return found if newer(found) else None


@dataclass
class Install:
    """How this media-optimizer was installed and how to update it: *commands*
    are run by the program (a pipx install), *command* is shown either way."""
    kind: str                           # "pipx", "pinned", "checkout" or "other"
    command: str
    commands: list[list[str]] | None = None


def pipx_metadata(prefix: str | Path | None = None) -> dict | None:
    try:
        meta = json.loads((Path(prefix or sys.prefix) / "pipx_metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def how_installed(version: str, prefix: str | Path | None = None,
                  package_dir: Path = Path(__file__).resolve().parent) -> Install:
    """The way to *version* for this install (*prefix*: its venv, ``sys.prefix``)."""
    meta = pipx_metadata(prefix)
    if meta is not None:
        main = meta.get("main_package") or {}
        name = main.get("package") or NAME
        url = main.get("package_or_url") or ""
        pipx = shutil.which("pipx") or "pipx"
        if main.get("pinned"):          # held back with `pipx pin`: that stays the owner's call
            return Install("pinned", f"pipx unpin {name}\npipx upgrade {name}")
        # A release named in the URL ("...media-optimizer@1.0"): the new one, still pinned.
        pinned = re.fullmatch(r"(.*git\+\S+?)@[^/@\s]+", url)
        if pinned:
            commands = [[pipx, "install", "--force", *meta.get("venv_args", []),
                         *(["--pip-args", shlex.join(main["pip_args"])] if main.get("pip_args") else []),
                         f"{pinned.group(1)}@{version}"]]
            injected = [(info or {}).get("package_or_url") or package
                        for package, info in sorted((meta.get("injected_packages") or {}).items())]
            if injected:                # --force starts a new venv: what was injected goes in again
                commands.append([pipx, "inject", name, *injected])
        else:
            commands = [[pipx, "upgrade", name]]
        return Install("pipx", "\n".join(shlex.join(["pipx", *c[1:]]) for c in commands), commands)
    top = package_dir.parent
    if (top / ".git").exists():
        return Install("checkout", f"git -C {shlex.quote(str(top))} pull")
    return Install("other", f"pipx install --force git+{REPO}")


def install(commands: list[list[str]], log: Path | None, timeout: float = 900, show: bool = False) -> tuple[int, str]:
    """Run the update (pipx clones and builds: about a minute); return the exit
    code of the first command that failed (else 0) and the end of the output,
    which is also kept in *log*. With *show* the output goes to the terminal
    as it comes instead."""
    out, code = [], 0
    for argv in commands:
        out.append(f"$ {shlex.join(argv)}")
        try:
            r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=not show, text=True,
                               timeout=timeout, env=_git_env(), start_new_session=not show)  # Ctrl+C reaches it
        except subprocess.TimeoutExpired:
            code = 124
            out.append(f"stopped after {timeout:.0f} seconds")
        except OSError as exc:
            code = 127
            out.append(str(exc))
        else:
            code = r.returncode
            if not show:
                out.append((r.stdout + r.stderr).strip())
        if code != 0:
            break
    text = "\n".join(line for line in out if line)
    if log is not None:
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(text + "\n", encoding="utf-8")
        except OSError:
            pass
    return code, "\n".join(text.splitlines()[-6:])


def restart_argv() -> list[str]:
    """The command the program was started with, to start it again after an update."""
    first = Path(sys.argv[0])
    if first.name == NAME and os.access(first, os.X_OK):
        return [str(first), *sys.argv[1:]]
    if first.name == "__main__.py":     # python -m media_optimizer
        return [sys.executable, "-m", "media_optimizer", *sys.argv[1:]]
    return [sys.executable, *sys.argv]


def restart(argv: list[str]) -> None:
    """Start *argv* in this process's place (it keeps the process id, so a
    program waiting for it still gets its exit code)."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(argv[0], argv)
