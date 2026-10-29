"""The results of a run for the program that started it: ``--results FILE``.

keepbest starts media-optimizer after its own run and shows these numbers
next to its own. The file is written once, when the run ends (also when it
was stopped or failed), from the same events as ``--events``: one JSON
object, replaced atomically, never half written. A run that never started
(a bad option, the page closed without a run) writes nothing.

Format (``v`` 1):
  program, version, code, dry_run, source, target
  stages: [{stage, title, state, code, total, done, outcomes, before, after, elapsed}]
      state: done, running (stopped in it), waiting (not reached), earlier
      (finished by the interrupted run this one resumed: no numbers)
      outcomes: {converted, skipped, cached, failed, discarded, conflict, dry}: files
      before, after: bytes of the converted files, before and after
  converted, failed, before, after: the sums over the stages

Changing the meaning of a field needs ``"v": 2``: keepbest reads it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import ui
from .config import NAME, __version__
from .events import events
from .ui import StyleUI as S

VERSION = 1

STAGE_TITLES = {"images": "Images", "gifs": "GIFs", "clips": "Short clips", "video": "Videos",
                "verify": "Final check"}


class Tally:
    """The events of a run folded into per-stage counts and sizes. The
    page's progress (web.Progress) builds on it, so both count alike."""

    def __init__(self) -> None:
        self.stages: dict[str, dict] = {}
        self.order: list[str] = []
        self.current: str | None = None
        self.dry_run = False
        self.source: str | None = None
        self.target: str | None = None

    def _stage(self, name: str) -> dict:
        if name not in self.stages:
            self.stages[name] = {"stage": name, "title": STAGE_TITLES.get(name, name), "total": None, "done": 0,
                                 "outcomes": {}, "before": 0, "after": 0, "state": "waiting", "code": None,
                                 "elapsed": None}
            self.order.append(name)
        return self.stages[name]

    def apply(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "run_start":
            self.dry_run = bool(ev.get("dry_run"))
            self.source, self.target = ev.get("source"), ev.get("target")
        elif kind == "plan":
            for entry in ev.get("stages", []):
                st = self._stage(entry["stage"])
                st["total"] = entry.get("total")
                if entry.get("done"):
                    st["state"] = "earlier"           # finished by the interrupted run
        elif kind == "stage_start":
            st = self._stage(ev["stage"])
            st["state"] = "running"
            if ev.get("total") is not None:
                st["total"] = ev["total"]
            self.current = ev["stage"]
        elif kind == "stage_end":
            st = self._stage(ev["stage"])
            st["state"] = "done"
            st["code"] = ev.get("code")
            st["elapsed"] = (ev.get("counters") or {}).get("elapsed")
        elif kind == "done":
            st = self._stage(ev["stage"])
            st["done"] += 1
            outcome = ev.get("outcome") or "finished"
            st["outcomes"][outcome] = st["outcomes"].get(outcome, 0) + 1
        elif kind == "file":
            st = self._stage(ev["stage"])
            if ev.get("status") in ("OK", "SALVAGED") and ev.get("before") and ev.get("after"):
                st["before"] += ev["before"]
                st["after"] += ev["after"]

    def totals(self) -> dict:
        stages = self.stages.values()
        return {"converted": sum(st["outcomes"].get("converted", 0) for st in stages),
                "failed": sum(st["outcomes"].get("failed", 0) for st in stages),
                "before": sum(st["before"] for st in stages),
                "after": sum(st["after"] for st in stages)}


class Results(Tally):
    def __init__(self) -> None:
        super().__init__()
        self.path: Path | None = None

    def open(self, path: Path) -> None:
        self.path = path
        events.listener = self.apply

    def close(self) -> None:
        if events.listener == self.apply:
            events.listener = None
        self.path = None

    def apply(self, ev: dict) -> None:
        if ev.get("event") == "run_start":
            Tally.__init__(self)                      # a new run in the same program ("another folder")
        super().apply(ev)
        if ev.get("event") == "run_end":
            self.write(ev.get("code"))

    def write(self, code: int | None) -> None:
        if self.path is None:
            return
        record = {"v": VERSION, "program": NAME, "version": __version__, "code": code, "dry_run": self.dry_run,
                  "source": self.source, "target": self.target,
                  "stages": [self.stages[name] for name in self.order], **self.totals()}
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=True)
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            # Not through say(): it runs inside an event, and its message would be one.
            ui.out(f"{ui.badge('RESULTS', S.YELLOW)} cannot write {self.path} ({exc.strerror}).", err=True)


results = Results()
