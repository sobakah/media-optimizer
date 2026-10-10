"""Which folders a run covers: the subfolder question, the picker, and the
settings subfolders / exclude_folders."""

from __future__ import annotations

from media_optimizer import config, files, folders


def _converted(d) -> set[str]:
    return {p.relative_to(d).as_posix() for p in d.rglob("*.jxl")}


def _tree(folder, name):
    """photo.jpg in the source, in a/, a/b/ and c d/ (a name with a space)."""
    return folder(name, "photo.jpg", "a/photo.jpg=photo.jpg", "a/b/photo.jpg=photo.jpg", "c d/photo.jpg=photo.jpg")


# --- the scanner ------------------------------------------------------------------
def test_excluded_folder_keeps_its_subfolders(folder):
    d = _tree(folder, "f1")
    s = config.Settings(exclude_folders=["a", "./c d/"])
    found = {p.relative_to(d).as_posix() for p in files.source_files(d, lambda n: n.endswith(".jpg"), s)}
    assert found == {"photo.jpg", "a/b/photo.jpg"}


def test_main_folder_and_exclude_mode(folder):
    d = _tree(folder, "f2")
    jpg = lambda n: n.endswith(".jpg")                     # noqa: E731
    s = config.Settings(exclude_folders=["."])
    assert "photo.jpg" not in {p.relative_to(d).as_posix() for p in files.source_files(d, jpg, s)}
    s = config.Settings(subfolders="exclude")
    assert [p.name for p in files.source_files(d, jpg, s)] == ["photo.jpg"]


# --- switching in the picker (as in keepbest) ---------------------------------------
def test_switching_asks_about_subfolders_only_when_they_change():
    entries = ["a", "a/b", "a/b/c", "d"]
    excluded: set[str] = set()
    assert folders.subfolders_along(excluded, entries, [1]) == (["a"], 2)
    assert folders.subfolders_along(excluded, entries, [4]) == ([], 0)
    assert folders.subfolders_along(excluded, entries, [1, 2, 3]) == ([], 0), "listed themselves"
    folders.switch_folders(excluded, entries, [1], with_subfolders=False)
    assert excluded == {"a"}
    folders.switch_folders(excluded, entries, [1, 4])                 # 1 on again, 4 off
    assert excluded == {"d"}
    assert folders.parse_numbers("2-3, 1", 4) == [2, 3, 1] and folders.parse_numbers("5", 4) is None


def test_tree_has_no_gaps():
    counts = {".": (1, 0), "x/y/z": (2, 0), "w": (0, 1)}
    assert folders.subfolder_entries(counts) == ["w", "x", "x/y", "x/y/z"]


# --- the question ---------------------------------------------------------------------
def test_without_a_terminal_subfolders_are_included(folder, run_cli):
    d = _tree(folder, "q1")
    out = run_cli("images", d, "--no-delete")
    assert "SUBFOLDERS" not in out
    assert _converted(d) == {"photo.jxl", "a/photo.jxl", "a/b/photo.jxl", "c d/photo.jxl"}, out


def test_enter_includes(folder, tmp_path, pty_run):
    d = _tree(folder, "q2")
    o = tmp_path / "q2out"
    out = pty_run([""], "images", d, o)
    assert "4 images in them" not in out and "3 images in them, 1 image directly in the source" in out, out
    assert _converted(o) == {"photo.jxl", "a/photo.jxl", "a/b/photo.jxl", "c d/photo.jxl"}, out


def test_exclude_processes_only_the_source_itself(folder, tmp_path, pty_run):
    d = _tree(folder, "q3")
    o = tmp_path / "q3out"
    out = pty_run(["e"], "images", d, o)
    assert "Only the files directly in the source" in out, out
    assert _converted(o) == {"photo.jxl"}


def test_custom_opens_the_picker(folder, tmp_path, pty_run):
    d = _tree(folder, "q4")
    o = tmp_path / "q4out"
    # entries: 1 a, 2 a/b, 3 c d. Switch a off but not a/b, then c d off.
    out = pty_run(["c", "1", "n", "3", ""], "images", d, o)
    assert "CHOOSE FOLDERS (3 subfolders)" in out and "Also switch the 1 subfolder below a/?" in out, out
    assert "main folder + 1 of 3 subfolders · 2 images" in out, out
    assert _converted(o) == {"photo.jxl", "a/b/photo.jxl"}, out


def test_picker_main_folder_key(folder, tmp_path, pty_run):
    d = _tree(folder, "q5")
    o = tmp_path / "q5out"
    out = pty_run(["c", "m", ""], "images", d, o)
    assert "Main folder - excluded" in out, out
    assert _converted(o) == {"a/photo.jxl", "a/b/photo.jxl", "c d/photo.jxl"}


def test_no_question_without_subfolders_or_with_a_setting(folder, tmp_path, pty_run):
    d = folder("q6", "photo.jpg")
    out = pty_run([], "images", d, tmp_path / "q6out")
    assert "SUBFOLDERS" not in out and (tmp_path / "q6out" / "photo.jxl").exists(), out
    d = _tree(folder, "q7")
    out = pty_run([], "images", d, tmp_path / "q7out", "--subfolders", "exclude")
    assert "SUBFOLDERS" not in out and _converted(tmp_path / "q7out") == {"photo.jxl"}, out


def test_full_run_asks_before_the_first_stage_and_keeps_the_choice(folder, tmp_path, pty_run):
    d = _tree(folder, "q8")
    o = tmp_path / "q8out"
    out = pty_run(["e"], d, o, env={"MO_HOLD": "0"})
    assert out.text.index("SUBFOLDERS") < out.text.index("Starting image conversion"), out
    assert "(1 files)" in out, "the counts follow the choice"
    assert _converted(o) == {"photo.jxl"}


def test_option_and_environment(folder, run_cli):
    d = _tree(folder, "q9")
    run_cli("images", d, "--no-delete", "--exclude-folder", "c d", "--exclude-folder", "a/b")
    assert _converted(d) == {"photo.jxl", "a/photo.jxl"}
    d = _tree(folder, "q10")
    run_cli("images", d, "--no-delete", env={"MO_EXCLUDE_FOLDERS": "a:c d"})
    assert _converted(d) == {"photo.jxl", "a/b/photo.jxl"}


def test_environment_takes_a_json_list(folder, run_cli):
    """keepbest passes its folder choice as JSON: names may contain ':'."""
    d = folder("q11", "photo.jpg", "a:b/photo.jpg=photo.jpg", "c/photo.jpg=photo.jpg")
    run_cli("images", d, "--no-delete", env={"MO_EXCLUDE_FOLDERS": '["a:b"]', "MO_SUBFOLDERS": "include"})
    assert _converted(d) == {"photo.jxl", "c/photo.jxl"}
    d = folder("q12", "photo.jpg", "c/photo.jpg=photo.jpg")
    run_cli("images", d, "--no-delete", env={"MO_SUBFOLDERS": "exclude"})
    assert _converted(d) == {"photo.jxl"}
