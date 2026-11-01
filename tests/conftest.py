"""Shared fixtures for the media-optimizer tests.

Every test runs isolated: its own HOME, config, state folder and an empty
config file, so a personal config never changes a result. The trash is
switched off (MO_TRASH=none): deletions go through --force-delete, nothing
ever reaches a real trash.

Two ways to run the program:
  run_cli(...)   a real process (python -m media_optimizer), for signals,
                 terminals and anything that needs isolation
  main(...)      in-process, so a test can monkeypatch one step and force a
                 failure exactly there (the Bash suite used stub commands
                 for that; mv, stat and touch are no longer commands here)

Stand-in programs for the external tools still work like in the Bash suite:
``stub(tool, script)`` puts a script first in PATH, which does its damage and
then runs the real tool via PATH="${PATH#*:}".
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

PYDIR = Path(__file__).resolve().parent.parent
TOOLS = ("cjxl", "djxl", "cwebp", "gif2webp", "ffmpeg", "ffprobe", "exiftool")
MISSING = [t for t in TOOLS if shutil.which(t) is None]
if MISSING:
    pytest.exit(f"Missing tools for the tests: {' '.join(MISSING)}", returncode=2)

from PIL import Image, PngImagePlugin  # noqa: E402  (after the tool check)

# The user's real journal, from the environment before any test changes it.
REAL_JOURNAL = Path(os.environ.get("MO_STATE_DIR") or Path(
    os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "media-optimizer") / "journal.jsonl"

# Fields each journal event must have (JOURNAL_SPEC.md, version 1).
JOURNAL_FIELDS = {
    "run_start": {"source": str, "target": (str, type(None)), "pid": int},
    "run_end": {"code": int, "interrupted": bool},
    "converted": {"stage": str, "src": str, "src_size": int, "src_mtime_ns": int, "dest": str,
                  "dest_size": int, "dest_mtime_ns": int, "lossless": (bool, type(None))},
    "renamed": {"src": str, "dest": str, "size": int, "mtime_ns": int, "reason": str},
    "copied": {"src": str, "dest": str, "size": int, "mtime_ns": int, "reason": str},
    "removed": {"path": str, "size": int, "mtime_ns": int, "how": str, "reason": str,
                "kept": (str, type(None))},
}


def check_journal(path: Path) -> list[dict]:
    """Every line a complete, ASCII-only JSON object with the common fields
    and the fields of its event. Returns the records."""
    data = path.read_bytes()
    assert not data or data.endswith(b"\n"), "last journal line incomplete"
    records = []
    for line in data.splitlines():
        line.decode("ascii")                                    # ensure_ascii=True
        rec = json.loads(line)
        assert rec["v"] == 1 and rec["tool"] == "media-optimizer", rec
        assert isinstance(rec["tool_version"], str) and isinstance(rec["run"], str), rec
        assert isinstance(rec["ts"], str) and rec["ts"][19] in "+-Z", rec            # offset included
        for name, kind in JOURNAL_FIELDS[rec["event"]].items():
            assert isinstance(rec[name], kind) and not (kind is int and isinstance(rec[name], bool)), \
                (name, rec)
        for name in ("source", "target", "src", "dest", "path", "kept"):
            assert rec.get(name) is None or rec[name].startswith("/"), (name, rec)
        records.append(rec)
    return records


# --- isolation -----------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "test-config.json"
    config.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("MO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MO_CONFIG", str(config))
    monkeypatch.setenv("MO_TRASH", "none")
    monkeypatch.setenv("MO_HOLD", "0")
    monkeypatch.setenv("MO_LOG", "false")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("MO_") and name not in ("MO_STATE_DIR", "MO_CONFIG", "MO_TRASH", "MO_HOLD", "MO_LOG"):
            monkeypatch.delenv(name)
    # The update check asks a repository that isn't there, never GitHub (tests/test_updates.py makes one).
    monkeypatch.setenv("MO_UPDATE_REPO", str(tmp_path / "no-releases"))
    from media_optimizer import proc
    from media_optimizer.events import events
    from media_optimizer.journal import journal
    # In-process runs: nothing left open by another test, and no stop of an
    # interrupted one still in force for tests that run tools directly.
    journal.path = journal.run_id = None
    events.close()
    proc.reset()
    project_log = PYDIR / "media-optimizer.log"
    before = project_log.stat().st_mtime_ns if project_log.exists() else None
    real_journal = REAL_JOURNAL.stat().st_size if REAL_JOURNAL.exists() else None
    yield tmp_path
    journal.path = journal.run_id = None
    events.close()
    after = project_log.stat().st_mtime_ns if project_log.exists() else None
    assert after == before, "a test wrote the real run log in the project folder"
    assert (REAL_JOURNAL.stat().st_size if REAL_JOURNAL.exists() else None) == real_journal, \
        "a test wrote the real journal"
    if (tmp_path / "state" / "journal.jsonl").exists():
        check_journal(tmp_path / "state" / "journal.jsonl")


def releases(folder: Path, *tags: str) -> str:
    """A git repository with these release tags, for the update check (MO_UPDATE_REPO)."""
    git = ["git", "-C", str(folder), "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "release"], check=True)
    for tag in tags:
        subprocess.run([*git, "tag", tag], check=True)
    return f"file://{folder}"


class Out:
    def __init__(self, rc: int, text: str):
        self.rc = rc
        self.text = text

    def __contains__(self, needle: str) -> bool:
        return needle in self.text

    def __repr__(self) -> str:
        return f"<rc={self.rc}>\n{self.text}"


def _env(extra: dict | None, path: str | None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PYDIR)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("DISPLAY", None)                   # no test opens a browser
    env.pop("WAYLAND_DISPLAY", None)
    if path is not None:
        env["PATH"] = path
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def without_page(args) -> list[str]:
    """A full run opens the page unless it has -y, so the fixtures add that."""
    from media_optimizer import cli

    argv = [str(a) for a in args]
    if {"--web", "-y", "--yes", "-h", "--help", "--version", "--install-desktop-entry",
        "--remove-desktop-entry"} & set(argv) or argv[:1] and argv[0] != "run" and argv[0] in cli.COMMANDS:
        return argv
    return [*argv, "-y"]


@pytest.fixture
def run_cli():
    """run_cli(*args, env={...}, path=..., timeout=...) -> Out (stdout+stderr)."""
    def run(*args, env: dict | None = None, path: str | None = None, timeout: float = 300,
            cwd: Path | None = None) -> Out:
        proc = subprocess.run([sys.executable, "-m", "media_optimizer", *without_page(args)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace", env=_env(env, path), timeout=timeout, cwd=cwd)
        return Out(proc.returncode, proc.stdout)
    return run


@pytest.fixture
def start_cli():
    """A running process, for signal tests. Returns the Popen."""
    procs = []

    def start(*args, env: dict | None = None, out: Path) -> subprocess.Popen:
        fh = open(out, "w")
        p = subprocess.Popen([sys.executable, "-m", "media_optimizer", *map(str, args)],
                             stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                             env=_env(env, None))
        procs.append((p, fh))
        return p

    yield start
    for p, fh in procs:
        if p.poll() is None:
            p.kill()
            p.wait()
        fh.close()


@pytest.fixture
def main(capsys):
    """In-process run: main(*args) -> Out. Restores the signal handlers that
    the program installs."""
    from media_optimizer import cli

    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}

    def run(*args) -> Out:
        capsys.readouterr()
        try:
            rc = cli.main(without_page(args))
        finally:
            for s, h in saved.items():
                signal.signal(s, h)
        captured = capsys.readouterr()
        return Out(rc, captured.out + captured.err)

    return run


# --- stand-in programs -----------------------------------------------------------
@pytest.fixture
def stub(tmp_path, monkeypatch):
    """stub(tool, bash_script): a script first in PATH. Returns the PATH."""
    bindir = tmp_path / "stubs"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    def make(tool: str, script: str) -> str:
        path = bindir / tool
        path.write_text("#!/bin/bash\n" + textwrap.dedent(script))
        path.chmod(0o755)
        return os.environ["PATH"]
    return make


@pytest.fixture
def vanishing_path(tmp_path):
    """A PATH with every program of the current PATH, except ``tool``, which
    runs once (and fails) and is gone afterwards: the encoder vanishes
    mid-run."""
    def make(tool: str) -> str:
        bindir = tmp_path / f"no-{tool}"
        bindir.mkdir()
        for d in os.environ["PATH"].split(":"):
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                target = bindir / name
                src = os.path.join(d, name)
                if not target.exists() and os.access(src, os.X_OK) and name != tool:
                    try:
                        target.symlink_to(src)
                    except OSError:
                        pass
        stub = bindir / tool
        stub.write_text('#!/bin/bash\nrm -f "$0"; exit 1\n')
        stub.chmod(0o755)
        return str(bindir)
    return make


# --- test media (generated once per session) ------------------------------------------
def _sh(*cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _photo(path: Path, size=(96, 64), seed=0, quality=90, mode="RGB", fmt=None):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7 + seed * 31) % 256, (y * 5 + x * 3 + seed * 17) % 256, (x * y + seed) % 256)
    if mode != "RGB":
        img = img.convert(mode)
    kwargs = {"quality": quality} if (fmt or path.suffix.lower()) in ("JPEG", ".jpg", ".jpeg") else {}
    img.save(path, format=fmt, **kwargs)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """Directory of source files. Copy them, never change them in place."""
    d = tmp_path_factory.mktemp("media")
    _photo(d / "photo.jpg")
    _photo(d / "other.png", (64, 64), seed=3)
    meta = PngImagePlugin.PngInfo()
    meta.add_text("Artist", "Max")
    meta.add_text("Copyright", "CC")
    img = Image.new("RGB", (64, 64), (10, 120, 200))
    img.save(d / "meta.png", pnginfo=meta)
    _sh("exiftool", "-q", "-overwrite_original", "-EXIF:GPSLatitude=48.1", "-EXIF:GPSLatitudeRef=N",
        "-EXIF:DateTimeOriginal=2020:01:02 03:04:05", str(d / "meta.png"))
    _photo(d / "fake.jpg", (64, 64), seed=5, fmt="PNG")              # PNG content, .jpg name
    Image.new("RGB", (16, 16), (0, 0, 255)).save(d / "isgif.png", format="GIF")   # GIF named .png
    (d / "garbage.png").write_bytes(os.urandom(5000))
    (d / "other.webp").write_text("unrelated")                         # must not block other.jxl
    _photo(d / "exif.jpg", (320, 240), seed=7, quality=88)
    _sh("exiftool", "-q", "-overwrite_original", "-Artist=Max", "-Copyright=CC", "-GPSLatitude=48.1",
        "-GPSLatitudeRef=N", "-XMP:Title=Hello", str(d / "exif.jpg"))
    _photo(d / "cmyk.jpg", (120, 80), seed=9, mode="CMYK", fmt="JPEG")
    _photo(d / "big.jpg", (1200, 900), seed=11, quality=88)
    (d / "truncated.jpg").write_bytes((d / "big.jpg").read_bytes()[:30000])
    if shutil.which("magick"):
        _sh("magick", "-size", "64x48", "plasma:fractal", "-define", "jpeg:arithmetic-coding=true",
            str(d / "arith.jpg"))
    frames = [Image.new("RGB", (160, 120), c) for c in ((200, 0, 0), (0, 200, 0), (0, 0, 200), (200, 200, 0))]
    for i, f in enumerate(frames):
        for x in range(0, 160, 8):
            for y in range(i * 10, 120, 16):
                f.putpixel((x, y), (255, 255, 255))
    frames[0].save(d / "anim.gif", save_all=True, append_images=frames[1:], duration=100, loop=0)

    def video(name, *args):
        _sh("ffmpeg", "-v", "error", "-y", *args, str(d / name))
    # At x264's default rate its AVIF is larger than it, and those are discarded.
    video("clip.mp4", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "3",
          "-c:v", "libx264", "-b:v", "2M", "-pix_fmt", "yuv420p")
    video("withaudio.mp4", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-f", "lavfi",
          "-i", "sine=f=440", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    video("long.mp4", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-t", "8",
          "-c:v", "libx264", "-b:v", "3M", "-pix_fmt", "yuv420p")
    video("slow.mp4", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-t", "40",
          "-c:v", "libx264", "-b:v", "4M", "-pix_fmt", "yuv420p")
    # A portrait phone video: landscape frames plus a 90° rotation flag.
    video("landscape.mp4", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "3",
          "-c:v", "libx264", "-b:v", "2M", "-pix_fmt", "yuv420p")
    video("portrait.mp4", "-display_rotation", "90", "-i", str(d / "landscape.mp4"), "-c", "copy")
    (d / "landscape.mp4").unlink()
    return d


@pytest.fixture
def folder(tmp_path, media):
    """folder(name, *files) -> a fresh directory with copies of media files.
    'a.jpg=photo.jpg' copies photo.jpg under another name."""
    def make(name: str, *names: str) -> Path:
        d = tmp_path / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        for spec in names:
            target, _, source = spec.partition("=")
            dest = d / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(media / (source or target), dest)
        return d
    return make


# --- helpers for assertions -------------------------------------------------------------
def snapshot(d: Path) -> list:
    """Names, sizes, mtimes and contents of everything below d."""
    out = []
    for p in sorted(d.rglob("*")):
        st = p.lstat()
        entry = [str(p.relative_to(d)), st.st_mtime_ns]
        if p.is_file():
            entry += [st.st_size, p.read_bytes()]
        out.append(entry)
    return out


def leftovers(d: Path) -> list[str]:
    return [str(p) for p in d.rglob("*") if ".part." in p.name or p.name.endswith(".molock")]


def running(tool: str, marker: str) -> bool:
    """Is a process of ``tool`` still running with ``marker`` in its arguments?"""
    res = subprocess.run(["pgrep", "-af", f"^{tool} "], stdout=subprocess.PIPE, text=True)
    return any(marker in line for line in res.stdout.splitlines())


def wait_running(tool: str, marker: str, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if running(tool, marker):
            return True
        time.sleep(0.2)
    return False
