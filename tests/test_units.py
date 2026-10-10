"""Pure-Python building blocks: settings, file detection, scanning, safe moves."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from media_optimizer import config, files, safety
from media_optimizer.config import Settings, SettingsError, load_settings, parse_value


# --- settings ----------------------------------------------------------------------
def test_defaults_are_valid():
    assert Settings().errors() == []


@pytest.mark.parametrize("name,text,expected", [
    ("jxl_effort", "10", 10),
    ("cpu_crf", "20.5", 20.5),
    ("delete_original", "true", True),
    ("delete_original", "", None),
    ("video_extensions", "mp4 mkv", ["mp4", "mkv"]),
    ("gpu_bf", "", None),
])
def test_parse_value_accepts(name, text, expected):
    assert parse_value(config.RULES[name], text) == expected


@pytest.mark.parametrize("name,text", [
    ("jxl_effort", "11"), ("jxl_effort", "abc"), ("max_workers", "0"),
    ("delete_original", "yes"), ("delete_original", "1"), ("img_copy_metadata", "false"),
    ("png_quality", "9.5"),
])
def test_parse_value_rejects(name, text):
    with pytest.raises(ValueError):
        parse_value(config.RULES[name], text)


def test_config_file_and_environment(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"jxl_effort": 9, "_comment": "x", "unknown": 1, "cpu_crf": 20}))
    monkeypatch.setenv("MO_CONFIG", str(path))
    monkeypatch.setenv("MO_PNG_MODE", "lossy")
    s, source, warnings = load_settings()
    assert (s.jxl_effort, s.png_mode, s.cpu_crf) == (9, "lossy", 20.0)
    assert source == str(path)
    assert any("unknown" in w for w in warnings)


def test_environment_beats_config(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"jxl_effort": 9}))
    monkeypatch.setenv("MO_CONFIG", str(path))
    monkeypatch.setenv("MO_JXL_EFFORT", "3")
    assert load_settings()[0].jxl_effort == 3


@pytest.mark.parametrize("content", ['{"delete_original": "yes"}', '{"max_workers": 0}', "not json", "[]"])
def test_invalid_config_stops(tmp_path, monkeypatch, content):
    path = tmp_path / "c.json"
    path.write_text(content)
    monkeypatch.setenv("MO_CONFIG", str(path))
    with pytest.raises(SettingsError):
        load_settings()


def test_invalid_environment_stops(monkeypatch):
    monkeypatch.setenv("MO_DELETE_ORIGINAL", "yes")
    with pytest.raises(SettingsError) as exc:
        load_settings()
    assert "MO_DELETE_ORIGINAL" in str(exc.value)


def test_example_config_is_in_sync():
    """config.example.json is generated from the defaults; regenerate with
    python3 -c 'import json; from media_optimizer.config import example_config;
    print(json.dumps(example_config(), indent=2))' > config.example.json"""
    path = Path(__file__).resolve().parent.parent / "config.example.json"
    assert json.loads(path.read_text()) == json.loads(json.dumps(config.example_config()))


# --- real file type ----------------------------------------------------------------
def test_real_type(media):
    assert files.real_type(media / "photo.jpg") == "jpg"
    assert files.real_type(media / "fake.jpg") == "png"
    assert files.real_type(media / "isgif.png") == "gif"
    assert files.real_type(media / "garbage.png") == ""
    assert files.real_type(media / "other.webp") == ""          # text named .webp
    assert files.real_type(media / "clip.mp4") == "mp4"


def test_jpeg_info(media):
    assert files.jpeg_info(media / "photo.jpg").components == 3
    assert files.jpeg_info(media / "cmyk.jpg").components == 4
    assert files.jpeg_info(media / "fake.jpg") is None
    if (media / "arith.jpg").exists():
        assert files.jpeg_info(media / "arith.jpg").arithmetic


def test_jpeg_info_truncated_header(tmp_path, media):
    cut = tmp_path / "cut.jpg"
    cut.write_bytes((media / "photo.jpg").read_bytes()[:40])
    assert files.jpeg_info(cut) is None


def test_stem_and_unique_name(tmp_path):
    assert files.stem("a.b.jpg") == "a.b"
    assert files.stem(".png") == ".png"
    (tmp_path / "x.png").touch()
    (tmp_path / "x_1.png").touch()
    assert files.unique_name(tmp_path, "x", "png").name == "x_2.png"


# --- scanning ------------------------------------------------------------------------
def test_find_files_rules(tmp_path):
    for rel in ["a.jpg", ".hid/b.jpg", ".Trash-1000/c.jpg", "@eaDir/d.jpg", "._e.jpg", ".f.jpg",
                "sub/g.JPG", "$RECYCLE.BIN/h.jpg", "#snapshot/i.jpg", "j.txt"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    (tmp_path / "link.jpg").symlink_to(tmp_path / "a.jpg")
    match = files.extension_matcher(["jpg"])
    found = sorted(str(p.relative_to(tmp_path)) for p in files.find_files(tmp_path, match))
    assert found == ["a.jpg", "sub/g.JPG"]
    found = sorted(str(p.relative_to(tmp_path)) for p in files.find_files(tmp_path, match, include_hidden=True))
    assert found == [".f.jpg", ".hid/b.jpg", "a.jpg", "sub/g.JPG"]


def test_find_files_unreadable_folder(tmp_path, capsys):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 folder")
    (tmp_path / "ok.jpg").touch()
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "x.jpg").touch()
    locked.chmod(0)
    try:
        found = list(files.find_files(tmp_path, files.extension_matcher(["jpg"])))
    finally:
        locked.chmod(0o755)
    assert [p.name for p in found] == ["ok.jpg"]
    assert "cannot read folder" in capsys.readouterr().err


def test_find_state_files_skips_root_and_trash(tmp_path):
    for rel in [".video_conversion_cache.txt", "a/.video_conversion_cache.txt",
                ".Trash-1000/.video_conversion_cache.txt"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    found = files.find_state_files(tmp_path, ".video_conversion_cache.txt")
    assert [str(p.relative_to(tmp_path)) for p in found] == ["a/.video_conversion_cache.txt"]


# --- moving without overwriting --------------------------------------------------------------
def test_move_no_replace(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("new")
    b.write_text("old")
    with pytest.raises(FileExistsError):
        safety.move_no_replace(a, b)
    assert b.read_text() == "old" and a.exists()
    b.unlink()
    safety.move_no_replace(a, b)
    assert b.read_text() == "new" and not a.exists()


def test_move_no_replace_without_hard_links(tmp_path, monkeypatch):
    """FAT, exFAT and many FUSE mounts have no hard links: renameat2 or a
    checked rename take over, and an existing name is still refused."""
    def no_link(src, dst):
        raise OSError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "link", no_link)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("new")
    b.write_text("old")
    with pytest.raises(FileExistsError):
        safety.move_no_replace(a, b)
    assert b.read_text() == "old"
    b.unlink()
    safety.move_no_replace(a, b)
    assert b.read_text() == "new" and not a.exists()


def test_claim_releases_lock_on_conflict(tmp_path):
    dest = tmp_path / "x.jxl"
    dest.write_text("taken")
    with pytest.raises(safety.Conflict):
        with safety.Claim(tmp_path, "x", dest):
            pass
    assert not (tmp_path / ".x.molock").exists()


def test_claim_removes_uncommitted_temp(tmp_path):
    with safety.Claim(tmp_path, "x", tmp_path / "x.jxl") as claim:
        tmp = claim.temp()
        tmp.write_text("half")
    assert not tmp.exists() and not (tmp_path / ".x.molock").exists()


def test_run_lock_is_exclusive(tmp_path):
    with safety.RunLock(tmp_path):
        with pytest.raises(safety.Busy):
            safety.RunLock(tmp_path).acquire()
    with safety.RunLock(tmp_path):
        pass


def test_renamed_settings_are_named(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"enable_avif_stage": True}))
    monkeypatch.setenv("MO_CONFIG", str(path))
    monkeypatch.setenv("MO_ANIM_FORMAT", "webp")
    with pytest.raises(SettingsError) as exc:
        load_settings()
    assert "renamed to 'clip_stage'" in str(exc.value) and "MO_CLIP_FORMAT" in str(exc.value)
    assert Settings.from_dict({"enable_avif_stage": True, "anim_format": "webp"}).clip_format == "webp"


def test_old_command_name_is_explained(run_cli, tmp_path):
    out = run_cli("avif", tmp_path, cwd=tmp_path)
    assert out.rc == 2 and "renamed to 'clips'" in out.text


def test_progress_line_is_cut_by_display_width():
    """CJK and emoji take two columns; cut by characters, the progress line
    wrapped and every update left a line behind."""
    from media_optimizer import ui
    assert ui.fit_width("abc", 2) == "ab"
    assert ui.fit_width("20歳を掘って", 6) == "20歳を"
    assert ui.fit_width("a🔥b", 3) == "a🔥"
    assert ui.fit_width("éx", 2) == "éx"        # combining accent takes no column
