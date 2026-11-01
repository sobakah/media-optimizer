"""Pure-Python building blocks: settings, file detection, scanning, safe moves."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from media_optimizer import config, files, safety, ui
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


def test_only_our_temp_files_are_leftovers(tmp_path):
    """'*.part.*' deleted any file with ".part." in its name before a video
    run, permanently and in the trash too."""
    keep = ["Movie.part.1.mp4", "archive.part.01.rar", "a.part.2.3.mkv", "x.mp4.part.1.2.mkv",
            ".Trash-1000/files/y.mp4.part.4711.ab12cd34.mp4", "#snapshot/z.mp4.part.1.2.mp4"]
    gone = ["x.mp4.part.4711.12345.mp4", "sub/y.jxl.part.99.ab12cd34.jxl"]
    for name in keep + gone:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")
    assert safety.cleanup_leftovers(tmp_path, ["jxl"]) == 1, "only the stage's own extensions"
    assert safety.cleanup_leftovers(tmp_path) == 1
    assert [n for n in keep if not (tmp_path / n).exists()] == []
    assert [n for n in gone if (tmp_path / n).exists()] == []
    match = files.extension_matcher(["mp4"], exclude_parts=True)
    assert match("Movie.part.1.mp4") and not match("x.mp4.part.4711.12345.mp4")
    with safety.Claim(tmp_path, "c", tmp_path / "c.mkv") as claim:
        assert files.is_temp_name(claim.temp().name)


def test_tool_started_during_a_stop_is_stopped(monkeypatch):
    """stop_all took its list of running tools while one was being started:
    that one was never signalled and the stop waited for it to finish."""
    import subprocess
    import time
    from media_optimizer import proc
    real = subprocess.Popen

    def popen(*args, **kwargs):
        child = real(*args, **kwargs)
        proc.stop_all()                   # lands after the start, before the registration
        return child
    monkeypatch.setattr(proc.subprocess, "Popen", popen)
    t0 = time.monotonic()
    try:
        with pytest.raises(proc.Stopped):
            proc.run(["sleep", "8"])
    finally:
        proc.reset()
    assert time.monotonic() - t0 < 4


def test_timeout_applies_with_streamed_output():
    import time
    from media_optimizer import proc
    t0 = time.monotonic()
    res = proc.run(["bash", "-c", "for i in $(seq 80); do echo x; sleep 0.1; done"],
                   on_line=lambda line: None, timeout=1)
    assert res.returncode == 124 and time.monotonic() - t0 < 4, res


def test_box_frame_with_wide_characters(capsys):
    from media_optimizer import ui
    ui.box("RESULT", [("File:", "日本の動画.mp4"), ("Emoji:", "🎬 clip"), ("Plain:", "x")], "")
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len({ui.visible_width(line) for line in lines}) == 1, lines


def test_options_before_the_command(folder, main, tmp_path):
    """'-c FILE images DIR' ran a full run of a folder called "images"."""
    d = folder("u1", "photo.jpg")
    out = main("-c", tmp_path / "test-config.json", "images", d, "--no-delete")
    assert out.rc == 0 and "IMAGE SUMMARY" in out and (d / "photo.jxl").exists(), out


def test_stage_option_before_the_command(folder, main):
    """'--gpu-workers 2 video DIR' was a full run of a folder called "video"
    into DIR: only the general options were known to take a value."""
    d = folder("u1b", "photo.jpg")
    out = main("--webp-quality", "80", "images", d, "--no-delete")
    assert out.rc == 0 and "IMAGE SUMMARY" in out and (d / "photo.jxl").exists(), out


def test_a_folder_named_like_a_command_gets_a_hint(folder, main, monkeypatch):
    """'-n images' means the images stage, so it stops without a source. A
    folder called images is what was meant, and nothing here says how to ask for it."""
    d = folder("hint", "images/photo.jpg=photo.jpg")
    monkeypatch.chdir(d)
    out = main("-n", "images")
    assert out.rc == 1 and "media-optimizer run images" in out.text, out.text
    assert "media-optimizer run" not in main("-n", "gifs").text


def test_no_option_is_a_flag_in_one_command_and_takes_a_value_in_another():
    """_command_first cannot know the command yet: the meaning must be the same everywhere."""
    import argparse
    from media_optimizer import cli
    sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    flags = {o for p in sub.choices.values() for a in p._actions if a.nargs == 0 for o in a.option_strings}
    assert not flags & cli.value_options(cli.build_parser())


def test_protected_folders_are_not_suggested(tmp_path, monkeypatch):
    home = tmp_path / "home" / "me"
    (home / "Pictures").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for path in (home, home.parent, tmp_path, Path("/"), Path("/usr")):
        assert files.protected_folder(path), path
    assert not files.protected_folder(home / "Pictures")
    monkeypatch.chdir(home)
    monkeypatch.setenv("PWD", str(home))
    assert files.suggested_source() == ""
    # Reached through a symlink: offered as the shell shows it.
    link = tmp_path / "link"
    link.symlink_to(home / "Pictures")
    monkeypatch.chdir(link)
    monkeypatch.setenv("PWD", str(link))
    assert files.suggested_source() == str(link)
    monkeypatch.setenv("PWD", "/somewhere/else")
    assert files.suggested_source() == str(home / "Pictures")


def test_a_path_queued_twice_is_deleted_once(tmp_path):
    """Queued by two runs: the second unlink failed, the run reported a
    failure and the list stayed."""
    from media_optimizer import safety
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"x")
    pending = tmp_path / ".img_pending_deletes.txt"
    pending.write_text(f"{f}\n{f}\n")
    assert safety.Trash(pending, force=True).resolve() == [f]
    assert not f.exists() and not pending.exists()


def test_input_with_two_more_folders_is_an_error(folder, run_cli, tmp_path):
    """'-i A B C': C was ignored without a word."""
    d = folder("u9", "photo.jpg")
    out = run_cli("images", "-i", d, tmp_path / "b", tmp_path / "c")
    assert out.rc == 2 and "which one is the target" in out, out
    assert not (tmp_path / "b").exists()


def test_jpeg_info_stops_at_image_data(tmp_path):
    """Image data before any frame header was scanned for markers."""
    f = tmp_path / "odd.jpg"
    sof = b"\xff\xc0\x00\x11\x08\x00\x10\x00\x10\x03" + bytes(9)
    f.write_bytes(b"\xff\xd8" + b"\xff\xda\x00\x02" + sof)
    assert files.jpeg_info(f) is None


@pytest.mark.parametrize("head, kind", [(b"\x00\x00\x00\x08ftyp", ""), (b"\x00\x00\x00\x18ftypMA1B" + bytes(12), ""),
                                        (b"\x00\x00\x00\x18ftypisom" + bytes(12), "mp4")])
def test_real_type_of_short_and_avif_headers(tmp_path, head, kind):
    """A file that ends after 'ftyp', or an AVIF with a profile brand, was an MP4."""
    f = tmp_path / "x.bin"
    f.write_bytes(head)
    assert files.real_type(f) == kind


def test_a_symlink_pointing_elsewhere_is_seen_at_the_next_load(tmp_path):
    """The resolved folders were remembered for the life of the process (the
    page's server, 'Process another directory?')."""
    from media_optimizer import cache
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "a")
    assert cache.canonical(link / "x.mp4") == str(tmp_path / "a" / "x.mp4")
    link.unlink()
    link.symlink_to(tmp_path / "b")
    cache.StageCache(tmp_path / ".img_cache.txt", enabled=True, dry_run=True, fingerprint="f").load()
    assert cache.canonical(link / "x.mp4") == str(tmp_path / "b" / "x.mp4")
