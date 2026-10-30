"""The questionnaire table and the settings schema the browser page draws
its form from, and handing every setting to a run through MO_* variables."""

from __future__ import annotations

import json
from pathlib import Path

from media_optimizer import questions
from media_optimizer.config import (GROUP_STARTS, RULES, Settings, check_value, format_value, groups,
                                    load_settings, parse_value, to_env)


def _other(rule: tuple, value):
    """A valid value different from value."""
    kind = rule[0]
    if kind == "opt":
        return _other(rule[1], value) if value is None else None
    if kind == "bool":
        return not value
    if kind == "int":
        return rule[1] if value != rule[1] else rule[2]
    if kind == "num":
        return float(rule[2]) if value != rule[2] else float(rule[1])
    if kind == "choice":
        return next(c for c in rule[1:] if c != value)
    if kind == "words":
        return ["a", "b"]
    if kind == "paths":
        return ["Old: stuff", "2019/raw", "with space"]
    if kind == "file_or_off":
        return False if value is None else None
    return "x y" if value != "x y" else ""


def test_every_setting_survives_the_environment(monkeypatch):
    """The page hands every setting to its run as MO_*; each must come back
    exactly, and win over a config file and inherited variables."""
    want = Settings()
    for name, rule in RULES.items():
        setattr(want, name, _other(rule, getattr(Settings(), name)))
    monkeypatch.setenv("MO_SUBFOLDERS", "exclude")          # inherited, as from keepbest
    for key, value in to_env(want).items():
        monkeypatch.setenv(key, value)
    got, _, _ = load_settings()
    assert {n: getattr(got, n) for n in RULES} == {n: getattr(want, n) for n in RULES}


def test_format_value_reads_back():
    wrong = [(name, value) for name, rule in RULES.items()
             for value in (getattr(Settings(), name), _other(rule, getattr(Settings(), name)))
             if parse_value(rule, format_value(rule, value)) != value]
    assert not wrong


def test_groups_cover_every_setting_once():
    names = [n for _, members in groups() for n in members]
    assert names == list(RULES)
    assert set(GROUP_STARTS) <= set(RULES)


def test_questionnaire_refers_to_real_settings_and_values():
    seen = set()
    for section in questions.FULL_RUN:
        for q in section.questions:
            assert q.setting in RULES and q.setting not in seen, q
            seen.add(q.setting)
            assert q.special in ("", "delete", "rescan")
            if q.when:
                name, *allowed = q.when
                assert allowed, q
                for value in allowed:
                    assert (name == "target" and value is True) or check_value(RULES[name], value), q
    assert {q.setting for s in questions.FULL_RUN for q in s.questions if q.special} == \
        {"delete_original", "use_cache"}


def test_conditions():
    q = {q.setting: q for s in questions.FULL_RUN for q in s.questions}
    s = Settings()
    assert not questions.shown(q["png_quality"], s, None)
    s.png_mode = "lossy"
    assert questions.shown(q["png_quality"], s, None)
    assert questions.shown(q["gpu_qp"], s, None) and not questions.shown(q["bitrate_threshold_kbps"],
                                                                          Settings(encoder_mode="cpu"), None)
    assert not questions.shown(q["enable_verify_output"], s, None)
    assert questions.shown(q["enable_verify_output"], s, Path("/target"))


def test_the_page_folds_the_image_and_video_questions():
    sections = questions.schema(Settings())["questionnaire"]
    assert [bool(sec["hint"]) for sec in sections] == [False, True, True]


def test_schema_for_the_page(tmp_path, monkeypatch):
    config = tmp_path / "c.json"
    config.write_text('{"jxl_effort": 9}')
    monkeypatch.setenv("MO_CONFIG", str(config))
    monkeypatch.setenv("MO_GPU_QP", "30")
    origins: dict[str, str] = {}
    s, _, _ = load_settings(origins=origins)
    data = json.loads(json.dumps(questions.schema(s, origins)))      # JSON all the way
    fields = {f["name"]: f for g in data["groups"] for f in g["fields"]}
    assert len(fields) == len(RULES) == sum(len(g["fields"]) for g in data["groups"])
    assert fields["jxl_effort"] == {**fields["jxl_effort"], "value": 9, "default": 7, "origin": "config",
                                    "kind": "int", "min": 1, "max": 10}
    assert fields["gpu_qp"]["origin"] == "env" and fields["gpu_qp"]["value"] == 30
    assert fields["img_target"]["choices"] == ["jxl", "webp"] and fields["img_target"]["origin"] == "default"
    assert fields["max_workers"]["nullable"] is True and fields["max_workers"]["kind"] == "int"
    assert fields["exclude_folders"]["kind"] == "paths"
    first = data["questionnaire"][0]["questions"]
    assert first[1] == {"setting": "delete_original", "text": questions.DELETE_TEXT, "when": [], "special": "delete"}
    png = next(q for sec in data["questionnaire"] for q in sec["questions"] if q["setting"] == "png_quality")
    assert png["when"] == ["png_mode", "lossy"]
