"""The file journal picktag reads: events, fields, order, and the writing
rules (append only, one line per write, no journal in a dry run or when off,
never a reason for a run to behave differently).

Every test's journal is also checked line by line in conftest
(check_journal); these tests check what the lines say.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import PYDIR, check_journal, wait_running
from media_optimizer import cache, media, safety
from media_optimizer import journal as journal_mod
from media_optimizer.stages.images import ImageStage

FAST = ["--encoder", "cpu", "--min-size", "0", "--no-probe", "--preset", "ultrafast"]


def _journal(tmp_path: Path, path: Path | None = None) -> list[dict]:
    path = path or tmp_path / "state" / "journal.jsonl"
    return check_journal(path) if path.exists() else []


def _c(path) -> str:
    return cache.canonical(path)


def _only(records: list[dict], event: str, **match) -> dict:
    found = [r for r in records if r["event"] == event and all(r.get(k) == v for k, v in match.items())]
    assert len(found) == 1, (event, match, records)
    return found[0]


def _snap(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


# --- run frame and conversions ---------------------------------------------------------
def test_image_conversion_and_removal(folder, main, tmp_path):
    d = folder("j1", "photo.jpg", "other.png")
    before = {n: _snap(d / n) for n in ("photo.jpg", "other.png")}
    out = main("images", d, "--force-delete")
    assert out.rc == 0, out
    recs = _journal(tmp_path)
    assert recs[0]["event"] == "run_start" and recs[-1]["event"] == "run_end", recs
    assert recs[0]["source"] == _c(d) and recs[0]["target"] is None and recs[0]["pid"] == os.getpid()
    assert recs[-1]["code"] == 0 and recs[-1]["interrupted"] is False
    assert len({r["run"] for r in recs}) == 1
    for name, dest_name in (("photo.jpg", "photo.jxl"), ("other.png", "other.jxl")):
        conv = _only(recs, "converted", src=_c(d / name))
        assert conv["stage"] == "img" and conv["lossless"] is True, conv
        assert (conv["src_size"], conv["src_mtime_ns"]) == before[name], "taken before the conversion"
        assert conv["dest"] == _c(d / dest_name)
        assert (conv["dest_size"], conv["dest_mtime_ns"]) == _snap(d / dest_name)
        removed = _only(recs, "removed", path=_c(d / name))
        assert removed == {**removed, "how": "permanent", "reason": "converted", "kept": conv["dest"]}
        assert (removed["size"], removed["mtime_ns"]) == before[name]
        assert recs.index(removed) > recs.index(conv)


def _png16(path: Path) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "gradients=s=64x48:c0=0x102030:c1=0xf0e0d0:seed=3,format=rgb48be", "-frames:v", "1",
                    "-pix_fmt", "rgb48be", str(path)], check=True)


def test_lossless_follows_the_encode(folder, main, tmp_path):
    """JPEG -> WebP is lossy, PNG -> lossless WebP keeps the pixels, except
    for a 16-bit PNG: WebP stores 8 bits (measured: different pixels)."""
    d = folder("j2", "photo.jpg", "other.png")
    _png16(d / "deep.png")
    out = main("images", d, "--target", "webp", "--no-delete")
    assert out.rc == 0, out
    recs = _journal(tmp_path)
    lossless = {Path(r["src"]).name: r["lossless"] for r in recs if r["event"] == "converted"}
    assert lossless == {"photo.jpg": False, "other.png": True, "deep.png": False}, recs

    d2 = folder("j2b", "other.png")
    _png16(d2 / "deep.png")
    out = main("images", d2, "--png-mode", "lossy", "--no-delete")
    assert out.rc == 0, out
    recs = [r for r in _journal(tmp_path) if r["event"] == "converted" and r["src"].startswith(_c(d2))]
    assert [r["lossless"] for r in recs] == [False, False], recs

    d3 = folder("j2c")
    _png16(d3 / "deep.png")
    out = main("images", d3, "--no-delete")
    assert _only(_journal(tmp_path), "converted", src=_c(d3 / "deep.png"))["lossless"] is True, \
        "JXL -d 0 keeps 16 bits"


def test_jxl_fallback_to_webp_is_lossless(folder, run_cli, stub, tmp_path):
    d = folder("j3", "other.png")
    path = stub("cjxl", 'case "$1" in *.png) exit 1 ;; esac\nPATH="${PATH#*:}" exec cjxl "$@"\n')
    out = run_cli("images", d, "--no-delete", path=path)
    assert "[FALLBACK]" in out, out
    conv = _only(_journal(tmp_path), "converted")
    assert conv["dest"] == _c(d / "other.webp") and conv["lossless"] is True, conv


# --- renames and copies ------------------------------------------------------------------
def test_extension_corrections_in_place(folder, main, tmp_path):
    """A kept fake.jpg (a PNG) is renamed after its conversion, so the
    converted event names fake.jpg and the renamed event follows it."""
    d = folder("j4", "fake.jpg", "isgif.png")
    sizes = {n: (d / n).stat().st_size for n in ("fake.jpg", "isgif.png")}
    out = main("images", d, "--no-delete")
    assert out.rc == 0, out
    recs = _journal(tmp_path)
    conv = _only(recs, "converted", src=_c(d / "fake.jpg"))
    assert conv["dest"] == _c(d / "fake.jxl") and conv["lossless"] is True
    ren = _only(recs, "renamed", src=_c(d / "fake.jpg"))
    assert ren["dest"] == _c(d / "fake.png") and ren["reason"] == "extension" and ren["size"] == sizes["fake.jpg"]
    assert recs.index(ren) > recs.index(conv)
    take = _only(recs, "renamed", src=_c(d / "isgif.png"))
    assert take["dest"] == _c(d / "isgif.gif") and take["reason"] == "take_over"
    assert take["size"] == sizes["isgif.png"] and "removed" not in [r["event"] for r in recs]


def test_take_over_copies_in_target_mode(folder, main, tmp_path):
    d = folder("j5", "isgif.png")
    o = tmp_path / "j5out"
    out = main("images", d, o)
    assert out.rc == 0, out
    recs = _journal(tmp_path)
    copied = _only(recs, "copied")
    assert copied["src"] == _c(d / "isgif.png") and copied["dest"] == _c(o / "isgif.gif"), copied
    assert (copied["size"], copied["mtime_ns"]) == _snap(d / "isgif.png") and copied["reason"] == "take_over"
    assert not [r for r in recs if r["event"] in ("renamed", "removed")]
    assert recs[0]["target"] == _c(o)


def test_a_file_that_cannot_be_read_gets_no_event(tmp_path):
    """Sizes and times are integers in the contract, and picktag matches by
    them: an event without them (a stat that failed) can't be used anyway."""
    log = tmp_path / "journal.jsonl"
    there, gone = tmp_path / "there.png", tmp_path / "gone.png"
    there.write_bytes(b"x")
    j = journal_mod.Journal()
    j.start(str(log), dry_run=False, source=tmp_path, target=None)
    j.converted("img", there, None, there, True)
    j.converted("img", there, (1, 2), gone, True)
    j.renamed(there, gone, "extension")
    j.copied(gone, there, "take_over")
    j.removed(there, None, how="trash", reason="converted", kept=there)
    j.end(0)
    assert [r["event"] for r in check_journal(log)] == ["run_start", "run_end"]


def test_preflight_rename_comes_before_the_conversion(folder, run_cli, tmp_path):
    d = folder("j6", "fake.jpg")
    out = run_cli("-y", "--preflight", "--no-delete", d)
    assert out.rc == 0, out
    recs = _journal(tmp_path)
    ren = _only(recs, "renamed")
    assert (ren["src"], ren["dest"], ren["reason"]) == (_c(d / "fake.jpg"), _c(d / "fake.png"), "extension")
    conv = _only(recs, "converted", src=_c(d / "fake.png"))
    assert recs.index(conv) > recs.index(ren)


# --- GIFs, clips, video ------------------------------------------------------------------
def test_gif_events(folder, main, tmp_path):
    d = folder("j7", "anim.gif")
    out = main("gifs", d, "--force-delete")
    assert out.rc == 0, out
    recs = _journal(tmp_path)
    conv = _only(recs, "converted")
    assert conv["stage"] == "gif" and conv["lossless"] is True and conv["dest"] == _c(d / "anim.webp")
    assert _only(recs, "removed")["kept"] == conv["dest"]

    d2 = folder("j7b", "anim.gif")
    out = main("gifs", d2, "--target", "avif", "--keep-larger", "--no-delete")
    assert out.rc == 0, out
    conv = _only(_journal(tmp_path), "converted", src=_c(d2 / "anim.gif"))
    assert conv["lossless"] is False and conv["dest"] == _c(d2 / "anim.avif")


def test_clip_events(folder, main, tmp_path, monkeypatch):
    d = folder("j8", "c.mp4=clip.mp4")
    out = main("clips", d, "--force-delete")
    assert out.rc == 0, out
    recs = _journal(tmp_path)
    conv = _only(recs, "converted")
    assert conv["stage"] == "clips" and conv["lossless"] is False and "suspect" not in conv, conv
    assert _only(recs, "removed")["kept"] == _c(d / "c.avif")

    d2 = folder("j8b", "c.mp4=clip.mp4")
    monkeypatch.setattr(media, "verify_visual", lambda *a, **k: media.Visual(False, 9.0, "1.00s=9 dB"))
    out = main("clips", d2, "--force-delete")
    recs = [r for r in _journal(tmp_path) if _c(d2) in (r.get("src") or r.get("path") or "")]
    assert [r["event"] for r in recs] == ["converted"], "the original is kept"
    assert recs[0]["suspect"] is True


def test_video_in_place_sequence(folder, main, tmp_path):
    """converted names name_h265.mp4, the original is removed with that as
    its successor, then renamed moves it to name.mp4."""
    d = folder("j9", "v.mp4=long.mp4")
    before = _snap(d / "v.mp4")
    out = main("video", d, *FAST, "--force-delete")
    assert out.rc == 0 and (d / "v.mp4").exists() and not (d / "v_h265.mp4").exists(), out
    recs = [r for r in _journal(tmp_path) if r["event"] not in ("run_start", "run_end")]
    assert [r["event"] for r in recs] == ["converted", "removed", "renamed"], recs
    conv, removed, ren = recs
    assert conv["stage"] == "video" and conv["lossless"] is False and "salvaged" not in conv
    assert conv["src"] == _c(d / "v.mp4") and conv["dest"] == _c(d / "v_h265.mp4")
    assert (conv["src_size"], conv["src_mtime_ns"]) == before
    assert removed["path"] == _c(d / "v.mp4") and removed["kept"] == _c(d / "v_h265.mp4")
    assert (removed["size"], removed["mtime_ns"]) == before
    assert (ren["src"], ren["dest"], ren["reason"]) == (_c(d / "v_h265.mp4"), _c(d / "v.mp4"), "rename_inplace")
    assert (ren["size"], ren["mtime_ns"]) == (conv["dest_size"], conv["dest_mtime_ns"]) == _snap(d / "v.mp4")


def test_suspicious_video_is_marked_and_kept(folder, main, tmp_path, monkeypatch):
    d = folder("j10", "v.mp4=long.mp4")
    monkeypatch.setattr(media, "verify_visual", lambda *a, **k: media.Visual(False, 10.0, "x"))
    out = main("video", d, *FAST, "--force-delete")
    assert "[PICTURE?]" in out, out
    recs = [r for r in _journal(tmp_path) if r["event"] not in ("run_start", "run_end")]
    assert [r["event"] for r in recs] == ["converted"], recs
    assert recs[0]["suspect"] is True and recs[0]["dest"] == _c(d / "v_h265.mp4")


def test_salvaged_video_is_marked_and_kept(folder, run_cli, stub, tmp_path):
    """The encoder reports damage in the source: the output is kept as
    salvaged, the original is never deleted."""
    d = folder("j11", "v.mp4=long.mp4")
    path = stub("ffmpeg", 'case "$*" in *libx265*) echo "corrupt input packet" >&2 ;; esac\n'
                          'PATH="${PATH#*:}" exec ffmpeg "$@"\n')
    out = run_cli("video", d, *FAST, "--force-delete", path=path)
    assert out.rc == 0 and (d / "v.mp4").exists(), out
    recs = [r for r in _journal(tmp_path) if r["event"] not in ("run_start", "run_end")]
    assert [r["event"] for r in recs] == ["converted"], recs
    assert recs[0]["salvaged"] is True and recs[0]["stage"] == "video"


def test_verify_fix_journals_the_deleted_output(folder, tmp_path, media, run_cli):
    d = folder("j12", "a.mp4=long.mp4")
    o = tmp_path / "j12out"
    run_cli("video", d, o, *FAST)
    (o / "a.mp4").write_bytes((media / "long.mp4").read_bytes()[:20000])
    broken = _snap(o / "a.mp4")
    starts = len([r for r in _journal(tmp_path) if r["event"] == "run_start"])
    out = run_cli("verify", d, o, "--no-visual")
    assert out.rc == 1, out
    assert len([r for r in _journal(tmp_path) if r["event"] == "run_start"]) == starts, \
        "a plain verify changes nothing and writes nothing"
    out = run_cli("verify", d, o, "--no-visual", "--fix")
    assert out.rc == 0 and not (o / "a.mp4").exists(), out
    recs = _journal(tmp_path)
    removed = _only(recs, "removed")
    assert removed == {**removed, "path": _c(o / "a.mp4"), "how": "permanent", "reason": "broken_output",
                       "kept": None}
    assert (removed["size"], removed["mtime_ns"]) == broken
    assert recs[-1]["event"] == "run_end" and recs[-1]["code"] == 0


# --- removal paths -------------------------------------------------------------------------
def test_trash_is_journaled_as_trash(folder, main, tmp_path, monkeypatch):
    bin_ = tmp_path / "trashbin"
    bin_.mkdir()

    def fake_trash(path):
        shutil.move(str(path), str(bin_ / Path(path).name))
        return True

    monkeypatch.setenv("MO_TRASH", "auto")
    monkeypatch.setattr(safety, "_send2trash", fake_trash)
    d = folder("j13", "photo.jpg")
    out = main("images", d, "--delete")
    assert out.rc == 0 and (bin_ / "photo.jpg").exists(), out
    removed = _only(_journal(tmp_path), "removed")
    assert removed["how"] == "trash" and removed["kept"] == _c(d / "photo.jxl")


def test_pending_list_removal_is_journaled(folder, main, tmp_path, monkeypatch):
    """The trash failed, so the original was queued; --force-delete deletes
    it at the end of the run. Only then is it journaled."""
    d = folder("j14", "photo.jpg")
    out = main("images", d, "--delete", "--force-delete")
    assert out.rc == 0 and not (d / "photo.jpg").exists(), out
    recs = _journal(tmp_path)
    removed = _only(recs, "removed")
    assert removed["how"] == "permanent" and removed["kept"] == _c(d / "photo.jxl")
    assert recs.index(removed) > recs.index(_only(recs, "converted"))


def test_pending_entry_of_an_earlier_run_has_no_known_successor(folder, main, tmp_path, monkeypatch):
    """The list file keeps the Bash format (paths only), so an original
    queued by an earlier run is journaled with kept null. Its converted
    event from that run still links it to the output."""
    d = folder("j15", "photo.jpg")
    out = main("images", d, "--delete")
    assert "Originals are kept" in out and (d / "photo.jpg").exists(), out
    out = main("images", d, "--delete", "--force-delete")
    assert not (d / "photo.jpg").exists(), out
    recs = _journal(tmp_path)
    assert _only(recs, "converted")["dest"] == _c(d / "photo.jxl")
    assert _only(recs, "removed")["kept"] is None


# --- writing rules ---------------------------------------------------------------------------
def test_dry_run_writes_no_journal(folder, main, run_cli, tmp_path):
    d = folder("j16", "photo.jpg", "anim.gif")
    main("images", d, "-n")
    run_cli("-y", "-n", d)
    run_cli("-y", "-n", d, tmp_path / "j16out")
    assert not (tmp_path / "state" / "journal.jsonl").exists()


def test_journal_off_writes_nothing(folder, main, tmp_path, monkeypatch):
    d = folder("j17", "photo.jpg", "other.png", "anim.gif")
    out = main("images", d, "--no-journal", "--no-delete")
    assert out.rc == 0 and (d / "photo.jxl").exists(), out
    monkeypatch.setenv("MO_JOURNAL_FILE", "false")
    out = main("gifs", d, "--no-delete")
    assert out.rc == 0 and (d / "anim.webp").exists(), out
    monkeypatch.delenv("MO_JOURNAL_FILE")
    config = tmp_path / "off.json"
    config.write_text('{"journal_file": false}')
    (d / "other.jxl").unlink()
    out = main("images", d, "-c", config, "--no-delete")
    assert out.rc == 0 and (d / "other.jxl").exists(), out
    assert not (tmp_path / "state" / "journal.jsonl").exists()


def test_journal_file_option(folder, main, tmp_path):
    d = folder("j18", "photo.jpg")
    own = tmp_path / "elsewhere" / "j.jsonl"
    out = main("images", d, "--journal", own, "--no-delete")
    assert out.rc == 0, out
    assert [r["event"] for r in _journal(tmp_path, own)] == ["run_start", "converted", "run_end"]
    assert not (tmp_path / "state" / "journal.jsonl").exists()
    out = main("images", d, "--journal", "false", "--no-delete")    # the same rule as the setting
    assert out.rc == 0 and len(_journal(tmp_path, own)) == 3, out


def test_journal_and_log_are_independent(folder, main, tmp_path, monkeypatch):
    d = folder("j19", "photo.jpg")
    log = tmp_path / "run.log"
    monkeypatch.setenv("MO_LOG", "true")
    out = main("images", d, "--log", log, "--no-journal", "--no-delete")
    assert out.rc == 0 and log.read_text().count("RUN ") == 2, out
    assert not (tmp_path / "state" / "journal.jsonl").exists(), "the log does not switch the journal on"
    monkeypatch.setenv("MO_LOG", "false")
    (d / "photo.jxl").unlink()
    out = main("images", d, "--log", log, "--no-delete")
    assert out.rc == 0 and log.read_text().count("RUN ") == 2, "no log this time"
    assert _only(_journal(tmp_path), "converted")["src"] == _c(d / "photo.jpg"), \
        "a disabled log does not switch the journal off"


def test_non_utf8_name_round_trips(tmp_path, media, run_cli):
    # A real process in UTF-8 mode (the default from Python 3.15): before,
    # printing such a name to a strict stdout fails the file after its
    # commit, a problem of its own (not the journal's).
    d = tmp_path / "j20"
    d.mkdir()
    name = os.fsdecode(b"caf\xe9.jpg")
    shutil.copy(media / "photo.jpg", d / name)
    out = run_cli("images", d, "--force-delete", env={"PYTHONUTF8": "1"})
    assert out.rc == 0, out
    raw = (tmp_path / "state" / "journal.jsonl").read_text("ascii")
    assert "\\udce9" in raw
    conv = _only(_journal(tmp_path), "converted")
    assert os.fsencode(conv["src"]) == os.fsencode(_c(d / name))
    assert os.fsencode(conv["src"]).endswith(b"caf\xe9.jpg")
    assert os.path.exists(os.fsencode(conv["dest"]))


_WRITER = """
import sys, threading
from pathlib import Path
from media_optimizer.journal import journal
path, tag = sys.argv[1], sys.argv[2]
journal.start(path, dry_run=False, source=Path("/src"), target=None)
long = "x" * 20000          # longer than any write buffer and PIPE_BUF
def work(t):
    for i in range(50):
        journal.removed(Path(f"/w/{tag}-{t}-{i}-{long}"), (i, i), how="trash", reason="converted", kept=None)
threads = [threading.Thread(target=work, args=(t,)) for t in range(4)]
[t.start() for t in threads]
[t.join() for t in threads]
journal.end(0)
"""


def test_concurrent_writers_produce_whole_lines(tmp_path):
    """Four processes with four threads each append to one journal. (The
    lock matters where O_APPEND is not atomic, e.g. NFS; locally a split
    write would show up here as torn lines.)"""
    path = tmp_path / "shared.jsonl"
    env = {**os.environ, "PYTHONPATH": str(PYDIR)}
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, str(path), str(n)], env=env) for n in range(4)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    recs = check_journal(path)
    assert len(recs) == 4 * (4 * 50 + 2)
    assert len({r["path"] for r in recs if r["event"] == "removed"}) == 4 * 4 * 50
    assert len({r["run"] for r in recs}) == 4


def test_unwritable_journal_warns_once_and_changes_nothing(folder, main, tmp_path):
    d = folder("j21", "photo.jpg", "other.png")
    blocked = tmp_path / "blocked"
    blocked.mkdir()                                  # a folder where the file should be
    out = main("images", d, "--journal", blocked, "--force-delete")
    assert out.rc == 0 and (d / "photo.jxl").exists() and (d / "other.jxl").exists(), out
    assert not (d / "photo.jpg").exists() and not (d / "other.png").exists()
    assert out.text.count("[JOURNAL]") == 1, out


@pytest.mark.parametrize("step", ["handle", "prepare"])
def test_interruption_ends_the_run_in_the_journal(folder, main, tmp_path, monkeypatch, step):
    """handle: the stage catches it and returns 130. prepare: outside the
    stage's own handling, only main's finally ends the run."""
    d = folder("j22", "photo.jpg")

    def stop(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(ImageStage, step, stop)
    out = main("images", d)
    assert out.rc == 130, out
    end = _journal(tmp_path)[-1]
    assert end["event"] == "run_end" and end["code"] == 130 and end["interrupted"] is True


@pytest.mark.slow
def test_sigterm_ends_the_run_in_the_journal(folder, tmp_path, start_cli):
    d = folder("j23", "s.mp4=slow.mp4")
    p = start_cli("video", d, tmp_path / "j23out", "--encoder", "cpu", "--min-size", "0", "--no-probe",
                  "--preset", "slow", out=tmp_path / "term.out")
    assert wait_running("ffmpeg", str(d))
    time.sleep(1)
    p.terminate()
    assert p.wait(timeout=30) == 130, (tmp_path / "term.out").read_text()
    recs = _journal(tmp_path)
    assert [r["event"] for r in recs] == ["run_start", "run_end"], recs
    assert recs[-1]["code"] == 130 and recs[-1]["interrupted"] is True


def test_resume_uses_the_journal_setting_of_this_call(folder, tmp_path, run_cli):
    """The journal belongs to this invocation like the log: --no-journal
    given now applies, not the saved setting of the interrupted run."""
    a = folder("j25", "photo.jpg")
    old = tmp_path / "old.jsonl"
    state = {"status": "INTERRUPTED", "source": str(a), "output": str(tmp_path / "j25out"),
             "settings": {"journal_file": str(old)},
             "done": {"images": False, "gifs": True, "clips": True, "video": True}}
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))
    out = run_cli("-y", "--no-journal", a, tmp_path / "j25out")
    assert "resuming" in out and (tmp_path / "j25out" / "photo.jxl").exists(), out
    assert not old.exists() and not (tmp_path / "state" / "journal.jsonl").exists()
