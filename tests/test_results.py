"""--results FILE: the run's numbers for the program that started it
(keepbest). Written once at the end, also when stopped; stdout and the exit
code stay exactly as they are without it."""

from __future__ import annotations

import json

from media_optimizer import web
from media_optimizer.stages.images import ImageStage
from test_events import FAST_VIDEO, _masked


def _results(path):
    data = json.loads(path.read_text())
    assert data["v"] == 1 and data["program"] == "media-optimizer", data
    return data


def test_full_run_results(folder, tmp_path, run_cli):
    d = folder("r1", "photo.jpg", "garbage.png", "anim.gif", "l.mp4=long.mp4")
    res = tmp_path / "results.json"
    out = run_cli("-y", d, tmp_path / "r1out", "--results", res, env=FAST_VIDEO)
    assert out.rc == 3, out
    data = _results(res)
    assert data["code"] == 3 and data["dry_run"] is False and data["source"] == str(d)
    assert data["target"] == str(tmp_path / "r1out")
    stages = {s["stage"]: s for s in data["stages"]}
    assert list(stages) == ["images", "gifs", "video"]
    assert stages["images"] == {**stages["images"], "title": "Images", "state": "done", "code": 3, "total": 2,
                                "done": 2, "outcomes": {"converted": 1, "failed": 1}}
    assert stages["video"]["outcomes"] == {"converted": 1} and stages["video"]["elapsed"] > 0
    jxl = (tmp_path / "r1out" / "photo.jxl").stat().st_size
    assert stages["images"]["before"] == (d / "photo.jpg").stat().st_size and stages["images"]["after"] == jxl
    assert data["converted"] == 3 and data["failed"] == 1
    assert data["before"] == sum(s["before"] for s in data["stages"]) > data["after"] > 0
    assert not list(tmp_path.glob(".results.json.*")), "no temp file left"


def test_stdout_is_the_same_with_results(folder, tmp_path, run_cli):
    a = folder("r2a", "photo.jpg", "garbage.png", "anim.gif")
    b = folder("r2b", "photo.jpg", "garbage.png", "anim.gif")
    plain = run_cli("-y", a, "--no-delete")
    with_results = run_cli("-y", b, "--no-delete", "--results", tmp_path / "results.json")
    assert plain.rc == with_results.rc == 3
    assert _masked(plain.text, a) == _masked(with_results.text, b)
    assert _results(tmp_path / "results.json")["code"] == 3


def test_results_with_events(folder, tmp_path, run_cli):
    """Both at once: the events file is the same as without the results."""
    d = folder("r3", "photo.jpg")
    ev, res = tmp_path / "events.jsonl", tmp_path / "results.json"
    run_cli("-y", d, "--no-delete", "--events", ev, "--results", res)
    assert ev.read_text().count('"event":"run_end"') == 1
    assert _results(res)["converted"] == 1


def test_dry_run_results(folder, tmp_path, run_cli):
    d = folder("r4", "photo.jpg")
    res = tmp_path / "results.json"
    assert run_cli("-y", "-n", d, "--results", res).rc == 0
    data = _results(res)
    assert data["dry_run"] is True and data["stages"][0]["outcomes"] == {"dry": 1} and data["converted"] == 0


def test_stopped_run_writes_its_results(folder, tmp_path, main, monkeypatch):
    d = folder("r5", "photo.jpg")

    def stop(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(ImageStage, "prepare", stop)
    res = tmp_path / "results.json"
    assert main("-y", d, "--results", res).rc == 130
    data = _results(res)
    assert data["code"] == 130 and data["stages"][0]["state"] == "running"


def test_resumed_run_marks_earlier_stages(folder, tmp_path, run_cli):
    d = folder("r6", "photo.jpg", "anim.gif")
    state = {"status": "INTERRUPTED", "source": str(d), "output": str(tmp_path / "r6out"), "settings": {},
             "done": {"images": True, "gifs": False, "clips": True, "video": True}}
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))
    res = tmp_path / "results.json"
    run_cli("-y", d, tmp_path / "r6out", "--results", res)
    stages = _results(res)["stages"]
    assert [(s["stage"], s["state"]) for s in stages] == [("images", "earlier"), ("gifs", "done")]


def test_no_run_writes_no_results(folder, tmp_path, run_cli):
    res = tmp_path / "results.json"
    out = run_cli("-y", tmp_path / "missing", "--results", res)
    assert out.rc != 0 and not res.exists(), out


def test_unwritable_results_change_nothing(folder, tmp_path, run_cli):
    d = folder("r7", "photo.jpg")
    out = run_cli("images", d, "--no-delete", "--results", tmp_path / "missing" / "results.json")
    assert out.rc == 0 and (d / "photo.jxl").exists(), out
    assert out.text.count("[RESULTS]") == 1


def test_the_page_passes_results_on(tmp_path):
    session = web.Session(web.Settings(), {}, tmp_path, None, results=tmp_path / "r.json")
    cmd = session.command("new", False, tmp_path / "ev.jsonl")
    assert cmd[cmd.index("--results") + 1] == str(tmp_path / "r.json")
    assert "--results" not in web.Session(web.Settings(), {}, tmp_path, None).command("new", False, tmp_path / "e")
