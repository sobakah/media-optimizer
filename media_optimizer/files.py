"""Finding source files and telling what they really are.

``find_files`` is the one scanner every stage and the orchestrator's counts
use, so they always see the same files:

- Trash and NAS system folders are always skipped (.Trash, .Trash-<uid>,
  $RECYCLE.BIN, @eaDir, #recycle, #snapshot, System Volume Information,
  lost+found). Converting a trashed file would resurrect it elsewhere.
- AppleDouble files (``._name``) are always skipped; they only look like
  images.
- Other hidden files and folders are skipped unless include_hidden is set.
- Only regular files count, symlinks are not followed (like ``find -type f``).
- An unreadable folder is reported and skipped; it never fails the batch.

``source_files`` adds the folder choice of the settings (``subfolders``,
``exclude_folders``); every scan of the source goes through it.

The real type comes from the first bytes of the file, never from the
extension: cjxl and cwebp detect the format by content, so a PNG named .jpg
would otherwise get the JPEG settings, which for cjxl means lossy.
"""

from __future__ import annotations

import fnmatch
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

SYSTEM_DIRS = (".Trash", ".Trash-*", "$RECYCLE.BIN", "@eaDir", "#recycle", "#snapshot",
               "System Volume Information", "lost+found")


def _skip_dir(name: str, include_hidden: bool) -> bool:
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in SYSTEM_DIRS):
        return True
    return name.startswith(".") and not include_hidden


def _walk(root: Path, include_hidden: bool, maxdepth: int | None, report,
          skip: frozenset[str] = frozenset()) -> Iterator[os.DirEntry]:
    """Regular files below root, depth-first, entries sorted by name so runs
    are reproducible. The root itself is never filtered, so a hidden source
    folder works. The files of folders in *skip* (relative, "/"-separated,
    "." = root) are left out; their subfolders are still walked."""
    stack: list[tuple[Path, int]] = [(root, 1)]
    while stack:
        folder, depth = stack.pop()
        own_files = not skip or relative_folder(folder, root) not in skip
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            if report:
                print(f"cannot read folder '{folder}': {exc.strerror}", file=sys.stderr)
            continue
        subdirs = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if (maxdepth is None or depth < maxdepth) and not _skip_dir(entry.name, include_hidden):
                        subdirs.append(Path(entry.path))
                elif own_files and entry.is_file(follow_symlinks=False):
                    yield entry
            except OSError:
                continue
        stack.extend((d, depth + 1) for d in reversed(subdirs))


def relative_folder(folder: Path, root: Path) -> str:
    """A folder relative to root in "/" form; "." for root itself."""
    return Path(os.path.relpath(folder, root)).as_posix()


def normalize_folder(text: str) -> str:
    """An exclude_folders entry in the form relative_folder returns:
    "./a//b/" -> "a/b", "" or "/" -> "."."""
    parts = [p for p in text.replace(os.sep, "/").split("/") if p not in ("", ".")]
    return "/".join(parts) or "."


def find_files(root: Path, match: Callable[[str], bool], *, include_hidden: bool = False,
               maxdepth: int | None = None, report: bool = True,
               skip_folders: frozenset[str] = frozenset()) -> Iterator[Path]:
    for entry in _walk(root, include_hidden, maxdepth, report, skip_folders):
        name = entry.name
        if name.startswith("._") or (name.startswith(".") and not include_hidden):
            continue
        if match(name):
            yield Path(entry.path)


def find_state_files(root: Path, name: str, *, include_hidden: bool = False) -> list[Path]:
    """State files called ``name`` in subfolders of root (not root itself).
    Hidden by nature, so they are matched by name; the folder rules are the
    same as for find_files, so a cache in the trash is never picked up."""
    out = []
    for entry in _walk(root, include_hidden, None, report=False):
        path = Path(entry.path)
        if entry.name == name and path.parent != root:
            out.append(path)
    return out


# Our temp files: the target name, ".part.<pid>.<random>." and the target's
# extension again (safety.Claim.temp; the Bash version used a decimal random
# part). Matched exactly: "*.part.*" also took "Movie.part.1.mp4" or
# "archive.part.01.rar", and the cleanup deleted them.
_TEMP_NAME = re.compile(r".+\.([^./]+)\.part\.\d+\.[0-9A-Za-z]+\.\1")


def is_temp_name(name: str) -> bool:
    return _TEMP_NAME.fullmatch(name) is not None


def extension_matcher(extensions, *, exclude_parts: bool = False) -> Callable[[str], bool]:
    """Case-insensitive match on the extension. exclude_parts leaves out our
    own temp files (``x.mp4.part.<pid>.<random>.mp4``)."""
    wanted = {e.lower().lstrip(".") for e in extensions}

    def match(name: str) -> bool:
        if exclude_parts and is_temp_name(name):
            return False
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return ext in wanted

    return match


def source_files(root: Path, match: Callable[[str], bool], settings, *,
                 report: bool = True) -> Iterator[Path]:
    """find_files with the folder choice of the settings: subfolders
    "exclude" scans only root, exclude_folders leaves those folders' own
    files out. "ask" that was not answered (no terminal, a resumed run of an
    older version) counts as include."""
    maxdepth = 1 if settings.subfolders == "exclude" else None
    skip = frozenset(normalize_folder(f) for f in settings.exclude_folders)
    return find_files(root, match, include_hidden=settings.include_hidden, maxdepth=maxdepth,
                      report=report, skip_folders=skip)


def count(root: Path, match, settings) -> int:
    return sum(1 for _ in source_files(root, match, settings, report=False))


# Never offered as the source: started from a launcher, the working directory
# is the home folder, and Enter at every question would convert all of it.
# Same rule as keepbest.
SYSTEM_FOLDERS = {"/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/opt", "/proc", "/run",
                  "/sbin", "/srv", "/sys", "/usr", "/var"}


def protected_folder(path: Path) -> bool:
    """The home folder, a folder above it, the root folder or a system folder.
    Compared with symlinks resolved (Fedora Atomic: /home -> /var/home)."""
    path = path.resolve()
    homes = {Path.home(), Path.home().resolve()}
    if path in homes or any(path in h.parents for h in homes):
        return True
    return path == Path(path.anchor) or path.as_posix() in SYSTEM_FOLDERS


def suggested_source() -> str:
    """The working directory as the preset for the source question, "" when
    it is protected or gone. As the shell shows it ($PWD), so a folder
    reached through a symlink keeps that path."""
    try:
        cwd = os.getcwd()
    except OSError:
        return ""
    pwd = os.environ.get("PWD", "")
    if pwd and os.path.isabs(pwd) and os.path.realpath(pwd) == os.path.realpath(cwd):
        cwd = pwd
    return "" if protected_folder(Path(cwd)) else cwd


# --- names -----------------------------------------------------------------------
def stem(name: str) -> str:
    """Name without the last extension; '.png' stays '.png' instead of ''."""
    s = name.rsplit(".", 1)[0] if "." in name else name
    return s or name


def extension(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    return "jpg" if ext == "jpeg" else ext


def unique_name(folder: Path, base: str, ext: str) -> Path:
    """First free ``base[_n].ext`` in folder."""
    candidate = folder / f"{base}.{ext}"
    n = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = folder / f"{base}_{n}.{ext}"
        n += 1
    return candidate


def target_folder(src: Path, source_dir: Path, output_dir: Path | None) -> Path:
    """Where the output of src goes: mirrored below output_dir, or next to
    the source in place."""
    if output_dir is None:
        return src.parent
    return output_dir / src.parent.relative_to(source_dir)


# --- real file type ------------------------------------------------------------------
_JXL_CONTAINER = b"\x00\x00\x00\x0cJXL \r\n\x87\n"
# ISO base media brands that are not plain video.
_NOT_MP4_BRANDS = {b"avif", b"avis", b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1",
                   b"qt  ", b"crx "}


def real_type(path: Path) -> str:
    """'jpg', 'png', 'webp', 'jxl', 'gif', 'mp4', or '' when unknown."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return ""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"\xff\x0a") or head.startswith(_JXL_CONTAINER):
        return "jxl"
    if head[4:8] == b"ftyp" and head[8:12] not in _NOT_MP4_BRANDS:
        return "mp4"
    return ""


def gif_width(path: Path) -> int | None:
    """The logical screen width from the GIF header, without decoding."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    if head[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    return int.from_bytes(head[6:8], "little")


def describe(path: Path) -> str:
    """A short description of unknown content for messages."""
    kind = real_type(path)
    if kind:
        return kind.upper()
    try:
        size = path.stat().st_size
    except OSError:
        return "unreadable"
    return "empty file" if size == 0 else "unknown content"


@dataclass
class JpegInfo:
    components: int        # 1 grey, 3 YCbCr/RGB, 4 CMYK/YCCK
    precision: int         # bits per sample: 8, or 12
    arithmetic: bool       # arithmetic-coded instead of Huffman
    progressive: bool


# SOF markers: C0-C3, C5-C7, C9-CB, CD-CF. C9-CB and CD-CF are arithmetic.
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_info(path: Path) -> JpegInfo | None:
    """Reads the frame header of a JPEG. None when there is none, e.g. a file
    that ends before it. Replaces ``file``'s "components 4" for CMYK."""
    try:
        with open(path, "rb") as fh:
            if fh.read(2) != b"\xff\xd8":
                return None
            while True:
                byte = fh.read(1)
                if byte != b"\xff":             # every marker starts with 0xFF
                    return None
                while byte == b"\xff":          # fill bytes before the marker code
                    byte = fh.read(1)
                if not byte:
                    return None
                marker = byte[0]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    continue                    # markers without a length
                if marker == 0xD9:
                    return None
                raw = fh.read(2)
                if len(raw) < 2:
                    return None
                length = struct.unpack(">H", raw)[0]
                if marker in _SOF:
                    data = fh.read(6)
                    if len(data) < 6:
                        return None
                    return JpegInfo(components=data[5], precision=data[0],
                                    arithmetic=marker >= 0xC9,
                                    progressive=marker in (0xC2, 0xC6, 0xCA, 0xCE))
                fh.seek(length - 2, os.SEEK_CUR)
    except OSError:
        return None
