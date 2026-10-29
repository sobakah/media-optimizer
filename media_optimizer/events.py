"""Structured progress for programs that show a run: ``--events FILE``.

One JSON object per line, written while the run goes on; the browser page
reads it to draw a card per stage. stdout is not touched: the events come
from the same calls that print (log.file for every file outcome, ui.say for
every message), never from parsing the printed text.

Events (``v`` 1, ``event``, ``ts`` on every line):
  run_start    command, source, target, dry_run, pid
  plan         stages: [{stage, total, done}] (again when a stage is added)
  stage_start  stage, total (null when not counted beforehand)
  job_start    stage, idx, total, src, encoder, reason        (video)
  encode       stage, idx, src, encoder, percent, speed, eta  (video, <= 1/s per job)
  file         stage, status, src, dest, before, after        (what the log line says)
  done         stage, src, outcome, reason                    (one per finished file)
  message      level (error, warning, info), tag, text
  stage_end    stage, code, counters
  run_end      code

Like the journal: one write per line, thread safe, a write problem warns
once and changes nothing else. Unlike the journal it is written in a dry
run too: the page shows what a dry run would do. A listener (--results,
results.py) gets every event too, with or without a file.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from . import ui
from .ui import StyleUI as S

VERSION = 1

# The log tags of the stages, by the stage names the orchestrator uses.
STAGE_OF_TAG = {"img": "images", "gif": "gifs", "clips": "clips", "h265": "video", "verify": "verify"}


class Events:
    def __init__(self) -> None:
        self.fd: int | None = None
        self.path: Path | None = None
        self.running = False            # between run_start and run_end
        self.warned = False
        self.listener = None            # called with every event (results.py)
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.fd is not None or self.listener is not None

    def open(self, path: Path) -> None:
        try:
            self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        except OSError as exc:
            ui.say("EVENTS", S.YELLOW, f"cannot write {path} ({exc.strerror}); the run goes on without it.",
                   err=True)
            return
        self.path = path
        self.warned = False
        ui.message_hook = self.message

    def close(self) -> None:
        ui.message_hook = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = self.path = None
        self.running = False

    # --- the run -----------------------------------------------------------------
    def run_start(self, command: str, source: Path, target: Path | None, dry_run: bool) -> None:
        if not self.active or self.running:
            return
        self.running = True
        self.emit("run_start", command=command, source=str(source),
                  target=str(target) if target is not None else None, dry_run=dry_run, pid=os.getpid())

    def run_end(self, code: int) -> None:
        if not self.active or not self.running:
            return
        self.running = False
        self.emit("run_end", code=code)

    def stage_end(self, stage_name: str, code: int, stage) -> None:
        """With the stage's own counters (Counters or VideoCounters)."""
        counters = getattr(stage, "counters", None) or getattr(stage, "c", None)
        if dataclasses.is_dataclass(counters):
            counters = dataclasses.asdict(counters)
        self.emit("stage_end", stage=stage_name, code=code, counters=counters)

    # --- from the output calls -----------------------------------------------------
    def message(self, level: str, tag: str, text: str) -> None:
        self.emit("message", level=level, tag=tag, text=text)

    def file(self, tag: str, status: str, src: Path, dest: str, before: int, after: int) -> None:
        self.emit("file", stage=STAGE_OF_TAG.get(tag, tag), status=status, src=str(src), dest=dest or None,
                  before=before or None, after=after or None)

    def done(self, tag: str, src: Path, outcome: str | None, reason: str | None = None) -> None:
        self.emit("done", stage=STAGE_OF_TAG.get(tag, tag), src=str(src), outcome=outcome, reason=reason)

    # --- writing -------------------------------------------------------------------
    def emit(self, event: str, **fields) -> None:
        fd, listener = self.fd, self.listener
        if fd is None and listener is None:
            return
        record = {"v": VERSION, "event": event,
                  "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), **fields}
        if listener is not None:
            with self._lock:
                listener(record)
        if fd is None:
            return
        data = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        with self._lock:
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError(0, "nothing written")
                    view = view[written:]
            except OSError as exc:
                if not self.warned:
                    self.warned = True
                    # Not through say(): its message would come back here.
                    ui.out(f"{ui.badge('EVENTS', S.YELLOW)} cannot write {self.path} ({exc.strerror}); "
                           "the run goes on without it.", err=True)


events = Events()
