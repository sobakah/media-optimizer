"""Running the external tools so that a stop reaches them at once.

Every tool runs in its own session (``start_new_session``), so the process
group id is its pid and ``killpg`` reaches it and anything it started. The
terminal's Ctrl+C goes only to this program; it forwards SIGINT itself. That
replaces the Bash version's job control (background jobs, SIGSTOP, walking
the tree with pgrep) and its quirks:

- A script started in the background of a non-interactive shell inherits
  SIGINT as ignored. Bash cannot undo that; Python installs its own handler
  (see cli.install_signal_handlers), and a handled signal is reset to the
  default in every child at exec, so ffmpeg and cjxl receive SIGINT again.
- ffmpeg finishes the file (flush, trailer) after SIGINT, which took about a
  second with x265 slow. ``stop_all`` therefore waits for every signalled
  group, up to MO_STOP_TIMEOUT seconds (default 10), then sends SIGKILL.

A worker whose tool was stopped gets ``Stopped`` instead of a failure, so an
interrupted file is neither counted as failed nor touched: the run is
resumable and the file is tried again next time.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable


class Stopped(Exception):
    """The run is being stopped. The current file is left as it was."""


class ToolMissing(Exception):
    """A required program is not (or no longer) there. Every following file
    would fail the same way, so the batch stops (exit 255)."""

    def __init__(self, tool: str):
        super().__init__(f"{tool} not found")
        self.tool = tool


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


stopping = threading.Event()
_running: set[subprocess.Popen] = set()
_lock = threading.Lock()


def which(tool: str) -> str | None:
    return shutil.which(tool)


def missing(*tools: str) -> list[str]:
    return [t for t in tools if which(t) is None]


def stop_timeout() -> float:
    try:
        return float(os.environ.get("MO_STOP_TIMEOUT", "10"))
    except ValueError:
        return 10.0


def run(cmd: list[str], *, capture: bool = True, on_line: Callable[[str], None] | None = None,
        merge_stderr: bool = False, timeout: float | None = None) -> Result:
    """Run a tool to completion. stdout/stderr are captured as text unless
    capture is False. With on_line, stdout is streamed line by line (used for
    ffmpeg's -progress) while stderr is still collected. After ``timeout``
    seconds the tool is stopped like on Ctrl+C (SIGINT, then SIGKILL) and the
    result is a failure with returncode 124."""
    if stopping.is_set():
        raise Stopped()
    pipe = subprocess.PIPE if capture or on_line else subprocess.DEVNULL
    err = subprocess.STDOUT if merge_stderr else (subprocess.PIPE if capture or on_line else subprocess.DEVNULL)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=pipe, stderr=err,
                                start_new_session=True, text=True, errors="replace")
    except FileNotFoundError:
        raise ToolMissing(cmd[0]) from None
    except PermissionError:
        raise ToolMissing(cmd[0]) from None
    with _lock:
        _running.add(proc)
        # stop_all sets the flag before it takes its list under this lock:
        # a tool started in between is not on that list, but sees the flag.
        late = stopping.is_set()
    try:
        if late:
            _terminate([proc])
        if on_line is not None:
            stderr_chunks: list[str] = []
            reader = None
            if proc.stderr is not None:
                reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()),
                                          daemon=True)
                reader.start()
            timed_out = threading.Event()
            timer = None
            if timeout is not None:
                def expire() -> None:
                    timed_out.set()
                    _terminate([proc])
                timer = threading.Timer(timeout, expire)
                timer.daemon = True
                timer.start()
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    on_line(line.rstrip("\n"))
                proc.wait()
            finally:
                if timer is not None:
                    timer.cancel()
            if reader:
                reader.join()
            stdout, stderr = "", "".join(stderr_chunks)
            if timed_out.is_set() and not stopping.is_set():
                return Result(124, stdout, stderr + f"\n{cmd[0]}: timed out after {timeout:g}s")
        else:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate([proc])
                stdout, stderr = proc.communicate()
                return Result(124, stdout or "", (stderr or "") + f"\n{cmd[0]}: timed out after {timeout:g}s")
    except BaseException:
        # KeyboardInterrupt (Ctrl+C, SIGTERM) while the main thread waited.
        _terminate([proc])
        raise
    finally:
        with _lock:
            _running.discard(proc)
    if stopping.is_set() and proc.returncode != 0:
        raise Stopped()
    return Result(proc.returncode, stdout or "", stderr or "")


def _signal(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            _signal(p, signal.SIGINT)
    deadline = time.monotonic() + stop_timeout()
    while time.monotonic() < deadline:
        if all(p.poll() is not None for p in procs):
            return
        time.sleep(0.05)
    for p in procs:
        if p.poll() is None:
            _signal(p, signal.SIGKILL)
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def stop_all() -> None:
    """Stop every running tool and keep new ones from starting."""
    stopping.set()
    with _lock:
        procs = list(_running)
    _terminate(procs)


def reset() -> None:
    """For a new run in the same process (tests, 'process another directory')."""
    stopping.clear()


@contextmanager
def shielded():
    """Ignore further Ctrl+C/SIGTERM while counters are saved after a stop,
    so a second Ctrl+C does not leave a half-written state file."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    for s in saved:
        signal.signal(s, signal.SIG_IGN)
    try:
        yield
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)
