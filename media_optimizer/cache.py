"""Per-stage caches: files that need no work on the next run.

Each stage keeps one text file in its working directory:

    .img_cache.txt  .gif_cache.txt  .clip_cache.txt  .video_conversion_cache.txt

Only outcomes that leave no output to find next time are recorded
(discarded as larger, unsupported, not selected, probe without gain) plus,
for video, every handled file, because its checks (ffprobe, probe encodes)
are expensive. Failures are never cached: they are tried again.

FORMAT (v2)
    # media-optimizer cache v2
    <size>\t<mtime_ns>\t<fingerprint>\t<outcome>\t<path>

An entry only counts when size and modification time still match, so a file
replaced under the same name is handled again, and when the fingerprint
matches: a short hash of the settings the result depends on (changing the
PNG quality retries the PNGs that were discarded as larger). Later lines win;
the file is compacted when it is loaded.

The video cache of version 1.x (and of the Bash version) holds bare paths.
It is converted to v2 only after asking (``find_legacy``, ``convert``);
converted entries get the fingerprint "*" (matches any settings), because
the settings of that run are unknown. Declined, the old file stays untouched
and is used as before: matched by path only, new entries appended as paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from . import files, ui
from .ui import StyleUI as S

HEADER = "# media-optimizer cache v2"
ANY = "*"
VIDEO_CACHE = ".video_conversion_cache.txt"


def fingerprint(settings, names: tuple[str, ...]) -> str:
    """Short hash of the settings a stage's results depend on."""
    data = json.dumps({n: getattr(settings, n) for n in names}, sort_keys=True)
    return hashlib.sha1(data.encode()).hexdigest()[:10]


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except OSError:
        return []


def is_legacy(path: Path) -> bool:
    """A cache file with content but without the v2 header."""
    for line in _read_lines(path):
        if line.strip():
            return line.strip() != HEADER
    return False


def find_legacy(work_dir: Path, include_hidden: bool) -> list[Path]:
    """Old-format video caches in the working directory and its subfolders."""
    found = [work_dir / VIDEO_CACHE] if (work_dir / VIDEO_CACHE).is_file() else []
    found += files.find_state_files(work_dir, VIDEO_CACHE, include_hidden=include_hidden)
    return [p for p in found if is_legacy(p)]


def _stat(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def convert(path: Path) -> tuple[int, int]:
    """Rewrite an old cache in v2 format, keeping a copy as <name>.v1.bak.
    Entries for files that exist now get their current size and time.
    Returns (kept, dropped)."""
    kept: dict[str, str] = {}
    dropped = 0
    for line in _read_lines(path):
        if not line or line.startswith("#"):
            continue
        st = _stat(line)
        if st is None:
            dropped += 1
            continue
        kept[line] = f"{st[0]}\t{st[1]}\t{ANY}\tdone\t{line}"
    shutil.copy2(path, path.with_name(path.name + ".v1.bak"))
    _write(path, [HEADER, *kept.values()])
    return len(kept), dropped


def _write(path: Path, lines: list[str]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", errors="surrogateescape")
    os.replace(tmp, path)


def prune(path: Path, remove: set[str]) -> int:
    """Remove the entries for the given paths (verify --fix), in either
    format, keeping a .bak. Returns the number of removed entries."""
    lines = _read_lines(path)
    keep, removed = [], 0
    for line in lines:
        entry_path = line.split("\t", 4)[4] if line.count("\t") >= 4 and not line.startswith("#") else line
        if entry_path in remove:
            removed += 1
        else:
            keep.append(line)
    shutil.copy2(path, path.with_name(path.name + ".bak"))
    _write(path, keep)
    return removed


def ask_convert(paths: list[Path], *, interactive: bool, assume_yes: bool) -> bool:
    """The question about old caches. Without a terminal, or with -y, the
    default answer applies: convert."""
    entries = sum(1 for p in paths for line in _read_lines(p) if line and not line.startswith("#"))
    ui.say("CACHE", S.YELLOW, f"{len(paths)} video cache file(s) in the old format ({entries} entries, "
                              "paths only):")
    for p in paths[:5]:
        ui.out(f"          {p}")
    if len(paths) > 5:
        ui.out(f"          ... and {len(paths) - 5} more")
    ui.out("          The new format also stores size, modification time and the settings a result\n"
           "          depends on, so a replaced file or changed settings are noticed. The old\n"
           "          files are kept as .v1.bak; the Bash version cannot read the new format.")
    if assume_yes or not interactive:
        ui.out(f"          {S.CYAN}Converting (default; {'-y given' if assume_yes else 'no terminal to ask'})."
               f"{S.RESET}")
        return True
    return ui.prompt_bool("Convert them to the new format now?", True, indent="  ")


@dataclass
class Entry:
    size: int
    mtime: int
    fingerprint: str
    outcome: str


class StageCache:
    def __init__(self, path: Path, *, enabled: bool, dry_run: bool, fingerprint: str):
        self.path = path
        self.enabled = enabled
        self.dry_run = dry_run
        self.fp = fingerprint
        self.entries: dict[str, Entry] = {}
        self.legacy: set[str] | None = None      # old format, used as before
        self.hits = 0
        self._lock = threading.Lock()

    # --- loading -------------------------------------------------------------------
    def load(self) -> None:
        if not self.enabled or not self.path.is_file():
            return
        if is_legacy(self.path):
            # Not converted (declined, or a dry run): matched by path only.
            self.legacy = {line for line in _read_lines(self.path)
                           if line and not line.startswith("#") and os.path.exists(line)}
            return
        stale = 0
        count = 0
        for line in _read_lines(self.path):
            if not line or line.startswith("#"):
                continue
            count += 1
            parsed = self._parse(line)
            if parsed is None:
                stale += 1
                continue
            path, entry = parsed
            if not os.path.exists(path):
                stale += 1
                continue
            self.entries[path] = entry
        superseded = count - stale - len(self.entries)
        if (stale or superseded) and not self.dry_run:
            self._compact()
            if stale:
                ui.say("CACHE", S.CYAN, f"{self.path.name}: {stale} stale entries removed.")

    @staticmethod
    def _parse(line: str) -> tuple[str, Entry] | None:
        parts = line.split("\t", 4)
        if len(parts) != 5:
            return None
        try:
            return parts[4], Entry(int(parts[0]), int(parts[1]), parts[2], parts[3])
        except ValueError:
            return None

    def _compact(self) -> None:
        try:
            _write(self.path, [HEADER] + [f"{e.size}\t{e.mtime}\t{e.fingerprint}\t{e.outcome}\t{p}"
                                          for p, e in sorted(self.entries.items())])
        except OSError:
            pass

    def merge_subfolders(self, work_dir: Path, source: Path, include_hidden: bool) -> None:
        """Caches left in subfolders by earlier runs on a subfolder are taken
        over. A cache in <work>/<sub> describes sources in <source>/<sub>;
        anything else came from a run with a different source or target and
        is left out. The subfolder caches are only read."""
        if not self.enabled:
            return
        taken = caches = 0
        for sub in files.find_state_files(work_dir, self.path.name, include_hidden=include_hidden):
            prefix = str(source / sub.parent.relative_to(work_dir)) + os.sep
            legacy = is_legacy(sub)
            n = 0
            for line in _read_lines(sub):
                if not line or line.startswith("#"):
                    continue
                if legacy:
                    st = _stat(line)
                    if st is None:
                        continue
                    path, entry = line, Entry(st[0], st[1], ANY, "done")
                else:
                    parsed = self._parse(line)
                    if parsed is None:
                        continue
                    path, entry = parsed
                if not path.startswith(prefix) or path in self.entries or not os.path.exists(path):
                    continue
                self._store(path, entry)
                n += 1
            if n:
                taken += n
                caches += 1
        if taken:
            ui.say("CACHE", S.CYAN, f"{taken} entries taken over from {caches} {self.path.name} file(s) "
                                    "in subfolders.")

    # --- use -------------------------------------------------------------------------
    def lookup(self, path: Path) -> str | None:
        """The cached outcome, or None when the file needs handling."""
        if not self.enabled:
            return None
        key = str(path)
        if self.legacy is not None:
            if key in self.legacy:
                with self._lock:
                    self.hits += 1
                return "done"
            return None
        entry = self.entries.get(key)
        if entry is None:
            return None
        st = _stat(key)
        if st is None or st != (entry.size, entry.mtime):
            return None
        if entry.fingerprint not in (ANY, self.fp):
            return None
        with self._lock:
            self.hits += 1
        return entry.outcome

    def add(self, path: Path | str, outcome: str) -> None:
        if not self.enabled or self.dry_run:
            return
        key = str(path)
        if self.legacy is not None:
            with self._lock:
                if key in self.legacy:
                    return
                self.legacy.add(key)
                self._append(key)
            return
        st = _stat(key)
        if st is None:
            return
        self._store(key, Entry(st[0], st[1], self.fp, outcome))

    def _store(self, key: str, entry: Entry) -> None:
        with self._lock:
            if self.entries.get(key) == entry or self.dry_run:
                self.entries[key] = entry
                return
            self.entries[key] = entry
            if not self.path.exists() or self.path.stat().st_size == 0:
                self._append(HEADER)
            self._append(f"{entry.size}\t{entry.mtime}\t{entry.fingerprint}\t{entry.outcome}\t{key}")

    def _append(self, line: str) -> None:
        try:
            with open(self.path, "a", encoding="utf-8", errors="surrogateescape") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
