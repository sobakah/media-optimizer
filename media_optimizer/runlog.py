"""The run log: one block per run with settings, one line per file, summaries.

Same format as the former Bash version (up to 0.3.1), so an existing log just
continues. Lines are written under a lock, so parallel workers never interleave (the Bash
version had to keep lines short for O_APPEND atomicity instead).

A dry run promises to write nothing, the log included.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path

from . import ui
from .config import default_log_file
from .events import events
from .ui import StyleUI as S

RULE = "-" * 64


class RunLog:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.run_id: str | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def open(self, *, enabled: bool, log_file: str | None, max_kb: int, dry_run: bool,
             call: str, source: Path, target: Path | None, tag: str) -> None:
        """Start a run block. Inside a run that is already open (a stage of
        the full run) only one line is added."""
        if self.run_id is not None:
            self.line(tag, f"started ({call})")
            return
        if dry_run or not enabled:
            return
        path = Path(log_file).expanduser() if log_file else default_log_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size > max_kb * 1024:
                os.replace(path, path.with_name(path.name + ".1"))
            with open(path, "a", encoding="utf-8"):
                pass
        except OSError:
            ui.say("LOG", S.YELLOW, f"not writable, skipping: {path}", err=True)
            return
        self.path = path
        self.run_id = f"{time.strftime('%y%m%d-%H%M%S')}-{os.getpid()}-{secrets.randbelow(100000)}"
        self._write(["", "=" * 64,
                     f"RUN {self.run_id}  started {time.strftime('%Y-%m-%d %H:%M:%S')}",
                     f"  Call   : {call}",
                     f"  Source : {source}",
                     f"  Target : {target if target else '(in place)'}"])

    def settings(self, values: dict) -> None:
        if not self.enabled:
            return
        lines = ["  Settings:"]
        for name, value in values.items():
            if isinstance(value, list):
                value = " ".join(value)
            lines.append(f"    {name:<24} {'' if value is None else value}")
        lines.append(RULE)
        self._write(lines)

    def line(self, tag: str, message: str) -> None:
        if self.enabled:
            self._write([f"{time.strftime('%H:%M:%S')}  {tag:<6} {message}"])

    def file(self, tag: str, status: str, src: Path, dest: str = "", old: int = 0, new: int = 0) -> None:
        events.file(tag, status, src, dest, old, new)          # with or without the log
        if not self.enabled:
            return
        extra = ""
        if old > 0 and new > 0:
            extra = f" ({ui.format_bytes(old)} -> {ui.format_bytes(new)}, {ui.pct_change(old, new)}%)"
        target = f" -> {dest}" if dest else ""
        self._write([f"{time.strftime('%H:%M:%S')}  {tag:<6} {status:<9} {src}{target}{extra}"])

    def close(self, rc: int) -> None:
        if not self.enabled:
            return
        self._write([RULE, f"RUN {self.run_id}  finished {time.strftime('%Y-%m-%d %H:%M:%S')}  (code {rc})",
                     "=" * 64])
        self.path = None
        self.run_id = None

    def _write(self, lines: list[str]) -> None:
        assert self.path is not None
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8", errors="surrogateescape") as fh:
                    fh.write("\n".join(lines) + "\n")
            except OSError:
                pass


log = RunLog()
