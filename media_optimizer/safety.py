"""Everything between "encoded" and "original deleted".

INVARIANTS (each one has cost real data before, see CHANGELOG.md)
  1. Output goes to ``<target>.part.<pid>.<random>.<ext>`` first and is only
     moved into place once it passed verification.
  2. An original is only deleted after its output is verified and in place.
  3. The move never overwrites: ``commit`` links the temp file to the
     target name, which fails if the name exists. (GNU ``mv -n`` returns 0
     when it refuses, which the Bash version had to work around.)
  4. Target names are claimed with a lock directory and re-checked under the
     lock: foo.jpg and foo.png both map to foo.jxl.
  5. One run per working directory: ``RunLock`` takes flock on the directory
     itself (as the former Bash version did); the kernel releases it when the
     process ends.
  6. A failed trash never deletes: the path is queued and asked about once
     at the end of the run.

The lock directory and temp file names are the same as in the former Bash
version (up to 1.2.1), so its leftovers are cleaned up too.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import fnmatch
import os
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

from . import files, ui
from .journal import journal
from .ui import StyleUI as S


# --- per-file outcomes ---------------------------------------------------------
class Outcome(Exception):
    """Raised by a worker to end a file with a recorded result."""


class Failed(Outcome):
    """The file failed; its original is kept."""


class Conflict(Outcome):
    """The target name is taken; the original is kept."""


class Busy(Exception):
    pass


# --- run lock ------------------------------------------------------------------
class RunLock:
    """flock on the working directory, released when the process ends."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.fd: int | None = None

    def acquire(self) -> None:
        self.fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise Busy(f"Another media-optimizer run is working on '{self.folder}'.") from None

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def cleanup_leftovers(folder: Path, extensions: list[str] | None = None) -> int:
    """Remove temp files and lock directories left by a hard abort (SIGKILL,
    power loss). Only call it while holding the RunLock.

    Only names of the exact temp form (files.is_temp_name) count, limited to
    the stage's output extensions (None: any). Trash and NAS system folders
    are not entered: a file there is not ours to delete."""
    wanted = None if extensions is None else {e.lower() for e in extensions}
    removed = 0
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not any(fnmatch.fnmatchcase(d, p) for p in files.SYSTEM_DIRS)]
        for name in names:
            if files.is_temp_name(name) and (wanted is None or name.rsplit(".", 1)[-1].lower() in wanted):
                try:
                    os.unlink(os.path.join(root, name))
                    removed += 1
                except OSError:
                    pass
        for name in list(dirs):
            if name.endswith(".molock"):
                try:
                    os.rmdir(os.path.join(root, name))
                    removed += 1
                    dirs.remove(name)
                except OSError:
                    pass
    if removed:
        ui.say("CLEANUP", S.CYAN, f"{removed} leftover(s) from an aborted run removed.")
    return removed


# --- claiming a target name ------------------------------------------------------
class Claim:
    """Lock directory ``<folder>/.<stem>.molock`` plus a temp file name.

    Use as a context manager; on exit the lock is released and an
    uncommitted temp file removed, whatever happened in between.
    """

    def __init__(self, folder: Path, stem: str, dest: Path):
        self.folder = folder
        self.lock = folder / f".{stem}.molock"
        self.dest = dest
        self.temps: list[Path] = []
        self.held = False

    def __enter__(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise Failed(f"cannot create target folder '{self.folder}'") from None
        try:
            self.lock.mkdir()
        except FileExistsError:
            raise Conflict(f"target name '{self.dest.name}' already claimed") from None
        except OSError:
            raise Failed(f"cannot create a lock in '{self.folder}'") from None
        self.held = True
        # Checked again under the lock: another worker may have finished it
        # since the caller's unlocked check. __exit__ does not run when
        # __enter__ raises, so release the lock here.
        if self.dest.exists() or self.dest.is_symlink():
            self.__exit__(None, None, None)
            raise Conflict(f"'{self.dest.name}' already exists")
        return self

    def temp(self, dest: Path | None = None) -> Path:
        """A unique temp name next to dest (default: the claimed target)."""
        dest = dest or self.dest
        ext = dest.suffix.lstrip(".")
        path = dest.with_name(f"{dest.name}.part.{os.getpid()}.{secrets.token_hex(4)}.{ext}")
        self.temps.append(path)
        return path

    def __exit__(self, *exc):
        for path in self.temps:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if self.held:
            try:
                self.lock.rmdir()
            except OSError:
                pass
        return False


# --- moving without overwriting -----------------------------------------------------
_RENAME_NOREPLACE = 1
_libc = None


def _renameat2(src: Path, dest: Path) -> bool:
    """renameat2(RENAME_NOREPLACE). True when moved, False when the call is
    not available here; FileExistsError when dest exists."""
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    func = getattr(_libc, "renameat2", None)
    if func is None:
        return False
    at_fdcwd = -100
    res = func(at_fdcwd, os.fsencode(src), at_fdcwd, os.fsencode(dest), _RENAME_NOREPLACE)
    if res == 0:
        return True
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(errno.EEXIST, "exists", str(dest))
    if err in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
        return False
    raise OSError(err, os.strerror(err), str(dest))


def move_no_replace(src: Path, dest: Path) -> None:
    """Move src to dest; FileExistsError if dest exists. Never overwrites.

    A hard link is atomic and refuses an existing name. Filesystems without
    hard links (FAT, exFAT, many FUSE mounts) get renameat2 with
    RENAME_NOREPLACE, and when that is unsupported too, a check and rename;
    our own workers are kept apart by the name claim there.
    """
    try:
        os.link(src, dest)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.EMLINK,
                             errno.ENOSYS):
            raise
        if _renameat2(src, dest):
            return
        if dest.exists() or dest.is_symlink():
            raise FileExistsError(errno.EEXIST, "exists", str(dest)) from None
        os.rename(src, dest)
        return
    os.unlink(src)


def copy_times(src: Path, dest: Path) -> None:
    """The output inherits the source's modification time (``touch -r``)."""
    try:
        st = src.stat()
        os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass


def commit(src: Path, tmp: Path, dest: Path) -> None:
    """Move a verified temp file into place. Raises Conflict if the name was
    taken meanwhile, Failed if the move itself failed."""
    copy_times(src, tmp)
    try:
        move_no_replace(tmp, dest)
    except FileExistsError:
        raise Conflict(f"'{dest.name}' already exists") from None
    except OSError as exc:
        raise Failed(f"could not move the output into place ({exc.strerror})") from None


# --- trash ---------------------------------------------------------------------
def _send2trash(path: Path) -> bool:
    try:
        from send2trash import send2trash
    except ImportError:
        return False
    try:
        send2trash(str(path))
        return True
    except Exception:
        return False


def _trash_command(path: Path) -> bool:
    for cmd in (["gio", "trash", str(path)], ["trash-put", "--", str(path)]):
        if shutil.which(cmd[0]) is None:
            continue
        try:
            if subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                return True
        except OSError:
            continue
    return False


def trash_available() -> bool:
    import importlib.util

    if importlib.util.find_spec("send2trash") is not None:
        return True
    return shutil.which("gio") is not None or shutil.which("trash-put") is not None


class Trash:
    """Moves originals to the trash. A file that cannot go there is not
    deleted but queued in ``pending`` (a text file in the working directory)
    and asked about once by ``resolve``."""

    def __init__(self, pending: Path | None, *, force: bool):
        self.pending = pending
        self.force = force
        self.backend = os.environ.get("MO_TRASH", "auto")   # tests: "none" = always fail
        self._lock = threading.Lock()                       # video jobs run in parallel
        # For the journal: the output that replaced a queued original. Only
        # known for originals queued in this run; the list file keeps the
        # Bash format (one path per line).
        self.kept: dict[str, Path] = {}

    def remove(self, path: Path, kept: Path) -> bool:
        """Remove the original of a conversion whose output kept is in place."""
        if not path.exists():
            return True
        before = journal.snapshot(path)
        ok = False
        if self.backend != "none":
            ok = _send2trash(path) or _trash_command(path)
        if ok:
            journal.removed(path, before, how="trash", reason="converted", kept=kept)
            return True
        if self.force:
            # Permanent deletion was already agreed to: no queue, so a rename
            # in place (name_h265 -> name) right after can take the name.
            try:
                path.unlink()
            except OSError:
                pass
            else:
                journal.removed(path, before, how="permanent", reason="converted", kept=kept)
                return True
        if self.pending is not None:
            with self._lock:
                self.kept[str(path)] = kept
                try:
                    with open(self.pending, "a", encoding="utf-8", errors="surrogateescape") as fh:
                        fh.write(f"{path}\n")
                except OSError:
                    pass
        ui.say("TRASH", S.YELLOW, f"failed, original kept for now: {path.name}")
        return False

    def resolve(self) -> list[Path]:
        """At the end of a run: one question for everything that could not
        go to the trash. Returns the originals deleted now."""
        if self.pending is None or not self.pending.is_file():
            return []
        try:
            lines = self.pending.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
        except OSError:
            return []
        # Once each: a file queued by two runs failed its second unlink, and
        # the list stayed.
        files = list(dict.fromkeys(Path(line) for line in lines if line and Path(line).exists()))
        if not files:
            self.pending.unlink(missing_ok=True)
            return []
        ui.card(S.YELLOW, [f"{S.BOLD}TRASH NOT AVAILABLE{S.RESET}",
                           f"{len(files)} original(s) could not be moved to the trash.",
                           "They were deliberately NOT deleted."])
        for f in files[:15]:
            ui.out(f"    {f}")
        if len(files) > 15:
            ui.out(f"    ... and {len(files) - 15} more (see {self.pending})")
        if self.force:
            ui.out("  --force-delete given: deleting permanently.")
        elif not ui.interactive():
            ui.out(f"  {S.CYAN}Cannot prompt (not interactive).{S.RESET} Originals are kept.")
            ui.out(f"  List: {self.pending}")
            return []
        elif not ui.prompt_bool("Delete these file(s) permanently now (no trash)?", False):
            ui.out(f"  Originals are kept. List: {self.pending}")
            return []
        deleted = []
        ok = err = 0
        for f in files:
            before = journal.snapshot(f)
            try:
                f.unlink()
                ok += 1
            except OSError:
                err += 1
            else:
                deleted.append(f)
                journal.removed(f, before, how="permanent", reason="converted", kept=self.kept.get(str(f)))
        line = f"  {S.GREEN}{ok} deleted{S.RESET}"
        if err:
            line += f", {S.RED}{err} failed{S.RESET}"
        ui.out(line)
        if err == 0:
            self.pending.unlink(missing_ok=True)
        return deleted
