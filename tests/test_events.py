"""--events FILE: the structured progress the browser page reads. stdout
must stay exactly as it is with or without it."""

from __future__ import annotations

import json
import re
from pathlib import Path

from media_optimizer.stages.images import ImageStage

FAST_VIDEO = {"MO_MIN_SIZE_MB": "0", "MO_ENCODER_MODE": "cpu", "MO_ENABLE_PROBE": "false",
              "MO_CPU_PRESET": "ultrafast"}
KNOWN = {"run_start", "plan", "stage_start", "job_start", "encode", "file", "done", "message", "stage_end",
         "run_end"}


def _events(path: Path) -> list[dict]:
    data = path.read_bytes()
    assert not data or data.endswith(b"\n")
    out = []
    for line in data.splitlines():
        line.decode("ascii")
        rec = json.loads(line)
        assert rec["v"] == 1 and rec["event"] in KNOWN and rec["ts"][23] in "+-Z", rec
        out.append(rec)
    return out


def _of(recs: list[dict], event: str, **match) -> list[dict]:
    return [r for r in recs if r["event"] == event and all(r.get(k) == v for k, v in match.items())]


def test_full_run_events(folder, tmp_path, run_cli):
    d = folder("e1", "photo.jpg", "garbage.png", "anim.gif", "l.mp4=long.mp4")
    ev = tmp_path / "events.jsonl"
    out = run_cli("-y", d, tmp_path / "e1out", "--events", ev, env=FAST_VIDEO)
    assert out.rc == 3, out
    recs = _events(ev)
    assert recs[0]["event"] == "run_start" and recs[-1] == {**recs[-1], "event": "run_end", "code": 3}
    assert recs[0]["source"] == str(d) and recs[0]["dry_run"] is False
    plan = _of(recs, "plan")[0]["stages"]
    assert plan == [{"stage": "images", "total": 2, "done": False}, {"stage": "gifs", "total": 1, "done": False},
                    {"stage": "video", "total": 1, "done": False}]
    for stage in plan:
        start = _of(recs, "stage_start", stage=stage["stage"])
        assert start == [{**start[0], "total": stage["total"]}]
        assert len(_of(recs, "done", stage=stage["stage"])) == stage["total"], "one done per file"
    assert [r["code"] for r in _of(recs, "stage_end")] == [3, 0, 0]
    assert _of(recs, "stage_end", stage="images")[0]["counters"]["failed"] == 1
    ok = _of(recs, "file", status="OK", src=str(d / "photo.jpg"))[0]
    assert ok["stage"] == "images" and ok["dest"] == "photo.jxl" and ok["before"] > 0 and ok["after"] > 0
    assert _of(recs, "done", src=str(d / "garbage.png"))[0]["outcome"] == "failed"
    assert _of(recs, "done", src=str(d / "l.mp4"))[0]["outcome"] == "converted"
    job = _of(recs, "job_start", src=str(d / "l.mp4"))[0]
    assert job["idx"] == 1 and job["total"] == 1 and job["encoder"] == "cpu"
    encode = _of(recs, "encode", src=str(d / "l.mp4"))
    assert encode and all(0 <= e["percent"] <= 100 for e in encode), "live progress of the encode"
    assert recs.index(job) < recs.index(encode[0])


def test_message_levels_without_colours(folder, tmp_path, run_cli):
    """The tests run with NO_COLOR: every colour code is empty, yet red is
    still an error and yellow a warning."""
    d = folder("e2", "garbage.png", "cmyk.jpg")
    ev = tmp_path / "events.jsonl"
    run_cli("images", d, "--no-delete", "--events", ev)
    recs = _events(ev)
    assert _of(recs, "message", tag="ERROR")[0]["level"] == "error"
    assert _of(recs, "message", tag="UNSUPPORTED")[0]["level"] == "warning"
    assert all("\x1b" not in r["text"] for r in _of(recs, "message"))


def _masked(text: str, d: Path) -> str:
    text = text.replace(str(d), "D")
    return re.sub(r"(\d+h )?(\d+m )?\d+(\.\d+)?s\b", "T", text)       # runtimes


def test_stdout_is_the_same_with_events(folder, tmp_path, run_cli):
    a = folder("e3a", "photo.jpg", "fake.jpg", "garbage.png", "anim.gif")
    b = folder("e3b", "photo.jpg", "fake.jpg", "garbage.png", "anim.gif")
    plain = run_cli("-y", a, "--no-delete")
    with_events = run_cli("-y", b, "--no-delete", "--events", tmp_path / "events.jsonl")
    assert plain.rc == with_events.rc == 3
    assert _masked(plain.text, a) == _masked(with_events.text, b)
    assert (tmp_path / "events.jsonl").stat().st_size > 0


def test_skip_reasons(folder, tmp_path, run_cli):
    d = folder("e4", "photo.jpg", "l.mp4=long.mp4")
    ev = tmp_path / "events.jsonl"
    run_cli("images", d, "--no-delete")
    run_cli("images", d, "--no-delete", "--events", ev)
    assert _of(_events(ev), "done")[0] == {**_of(_events(ev), "done")[0], "outcome": "skipped"}
    run_cli("video", d, "--no-delete", "--events", ev, env={**FAST_VIDEO, "MO_MIN_SIZE_MB": "1000"})
    assert _of(_events(ev), "done")[0]["reason"] == "small"
    run_cli("video", d, "--no-delete", "--events", ev, env={**FAST_VIDEO, "MO_MIN_SIZE_MB": "1000"})
    assert _of(_events(ev), "done")[0]["outcome"] == "cached", "remembered from the run before"
    recs = _events(ev)
    assert recs[0]["command"] == "video" and _of(recs, "stage_start")[0] == {**_of(recs, "stage_start")[0],
                                                                              "stage": "video", "total": None}


def test_dry_run_writes_events(folder, tmp_path, run_cli):
    d = folder("e5", "photo.jpg")
    ev = tmp_path / "events.jsonl"
    out = run_cli("-y", "-n", d, "--events", ev)
    assert out.rc == 0 and not (d / "photo.jxl").exists(), out
    recs = _events(ev)
    assert _of(recs, "run_start")[0]["dry_run"] is True and _of(recs, "done")[0]["outcome"] == "dry"


def test_resumed_run_marks_finished_stages(folder, tmp_path, run_cli):
    d = folder("e6", "photo.jpg", "anim.gif")
    state = {"status": "INTERRUPTED", "source": str(d), "output": str(tmp_path / "e6out"), "settings": {},
             "done": {"images": True, "gifs": False, "clips": True, "video": True}}
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))
    ev = tmp_path / "events.jsonl"
    out = run_cli("-y", d, tmp_path / "e6out", "--events", ev)
    assert "resuming" in out, out
    recs = _events(ev)
    assert _of(recs, "plan")[0]["stages"] == [{"stage": "images", "total": 1, "done": True},
                                              {"stage": "gifs", "total": 1, "done": False}]
    assert [r["stage"] for r in _of(recs, "stage_start")] == ["gifs"]


def test_unwritable_events_file_changes_nothing(folder, tmp_path, run_cli):
    d = folder("e7", "photo.jpg")
    out = run_cli("images", d, "--no-delete", "--events", tmp_path / "missing" / "events.jsonl")
    assert out.rc == 0 and (d / "photo.jxl").exists(), out
    assert out.text.count("[EVENTS]") == 1


def test_interrupted_run_ends_in_the_events(folder, tmp_path, main, monkeypatch):
    d = folder("e8", "photo.jpg")

    def stop(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(ImageStage, "prepare", stop)
    ev = tmp_path / "events.jsonl"
    out = main("images", d, "--events", ev)
    assert out.rc == 130, out
    assert _events(ev)[-1] == {**_events(ev)[-1], "event": "run_end", "code": 130}
