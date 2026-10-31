"""The file journal: what happened to which file, machine-readable.

One JSON object per line (format version 1), appended to journal_file
(default: journal.jsonl in the state folder). picktag reads it so that tags
follow a picture from foo.png to foo.jxl instead of being lost; keepbest
writes the same format. The contract is picktag's JOURNAL_SPEC.md; the
README lists the events and fields.

RULES
  - An event is written only after its action succeeded on disk, never in
    a dry run, and never for discarded results, skips or failures. A file
    whose size and mtime can't be read gets none: readers match by them.
  - Append only, never truncated or rotated: readers remember a byte offset.
  - Each line is one os.write under flock: two runs (or keepbest) may write
    at the same time; the lock also keeps this process's threads apart.
  - ensure_ascii: a file name that is not valid UTF-8 is written with
    surrogate escapes, and os.fsencode(json.loads(line)["src"]) restores
    the exact bytes.
  - A journal problem never changes what the run does: one warning per run,
    then carry on. Independent of the run log in both directions.
  - Paths as cache.canonical makes them: absolute, symlinks in the folder
    part resolved, the file name kept.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from . import cache, ui
from .config import NAME, RC_INTERRUPTED, __version__, default_journal_file
from .ui import StyleUI as S

VERSION = 1

Snapshot = tuple[int, int]          # (st_size, st_mtime_ns)


class Journal:
    def __init__(self) -> None:
        self.path: Path | None = None       # set while a run is open
        self.run_id: str | None = None      # one per process, also across "another directory"
        self.warned = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.path is not None

    # --- the run ---------------------------------------------------------------
    def start(self, journal_file: str | bool | None, *, dry_run: bool, source: Path,
              target: Path | None) -> None:
        """run_start. Inside a run that is already open (verify --fix --run
        starts the video stage) nothing is added."""
        if self.path is not None or dry_run or journal_file is False:
            return
        self.path = Path(journal_file).expanduser() if journal_file else default_journal_file()
        if self.run_id is None:
            self.run_id = f"{time.strftime('%y%m%d-%H%M%S')}-{os.getpid()}-{secrets.randbelow(100000)}"
        self.warned = False
        self._write("run_start", source=_path(source), target=_path(target) if target is not None else None,
                    pid=os.getpid())

    def end(self, code: int) -> None:
        if self.path is None:
            return
        self._write("run_end", code=code, interrupted=code == RC_INTERRUPTED)
        self.path = None

    # --- events --------------------------------------------------------------------
    def snapshot(self, path: Path) -> Snapshot | None:
        """Size and mtime of a file before an action changes or removes it.
        None while no run is open, so callers pay nothing then."""
        return _stat(path) if self.path is not None else None

    def converted(self, stage: str, src: Path, before: Snapshot | None, dest: Path, lossless: bool | None,
                  *, salvaged: bool = False, suspect: bool = False) -> None:
        if self.path is None:
            return
        after = _stat(dest)
        if before is None or after is None:
            return
        extra = {}
        if salvaged:
            extra["salvaged"] = True
        if suspect:
            extra["suspect"] = True
        self._write("converted", stage=stage, src=_path(src), src_size=before[0], src_mtime_ns=before[1],
                    dest=_path(dest), dest_size=after[0], dest_mtime_ns=after[1], lossless=lossless,
                    **extra)

    def renamed(self, src: Path, dest: Path, reason: str) -> None:
        """After the rename. Size and mtime are read from the new name: a
        rename (or link + unlink) keeps the inode, so they are the values
        the file had before."""
        if self.path is None:
            return
        state = _stat(dest)
        if state is not None:
            self._write("renamed", src=_path(src), dest=_path(dest), size=state[0], mtime_ns=state[1], reason=reason)

    def copied(self, src: Path, dest: Path, reason: str) -> None:
        """Size and mtime are the source's: picktag finds the file by them."""
        if self.path is None:
            return
        state = _stat(src)
        if state is not None:
            self._write("copied", src=_path(src), dest=_path(dest), size=state[0], mtime_ns=state[1], reason=reason)

    def removed(self, path: Path, before: Snapshot | None, *, how: str, reason: str,
                kept: Path | None) -> None:
        if self.path is None or before is None:
            return
        self._write("removed", path=_path(path), size=before[0], mtime_ns=before[1], how=how, reason=reason,
                    kept=_path(kept) if kept is not None else None)

    # --- writing ---------------------------------------------------------------------
    def _write(self, event: str, **fields) -> None:
        path = self.path
        if path is None:
            return
        record = {"v": VERSION, "tool": NAME, "tool_version": __version__, "run": self.run_id,
                  "ts": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **fields}
        data = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    view = memoryview(data)
                    while view:                     # one write; the loop only for a short write
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError(0, "nothing written")
                        view = view[written:]
                finally:
                    os.close(fd)                    # releases the lock
            except OSError as exc:
                if not self.warned:
                    self.warned = True
                    ui.say("JOURNAL", S.YELLOW, f"cannot write {path} ({exc.strerror}); the run goes on "
                                                "without it.", err=True)


def _stat(path: Path) -> Snapshot | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _path(path: Path) -> str:
    return cache.canonical(path)


journal = Journal()
