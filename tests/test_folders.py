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
def test_tree_has_no_gaps():
    counts = {".": (1, 0), "x/y/z": (2, 0), "w": (0, 1)}
    assert folders.subfolder_entries(counts) == ["w", "x", "x/y", "x/y/z"]


# --- the question ---------------------------------------------------------------------
def test_without_a_terminal_subfolders_are_included(folder, run_cli):
    d = _tree(folder, "q1")
    out = run_cli("images", d, "--no-delete")
    assert "SUBFOLDERS" not in out
    assert _converted(d) == {"photo.jxl", "a/photo.jxl", "a/b/photo.jxl", "c d/photo.jxl"}, out


def test_exclude_processes_only_the_source_itself(folder, tmp_path, run_cli):
    d = _tree(folder, "q3")
    o = tmp_path / "q3out"
    assert run_cli("images", d, o, "--subfolders", "exclude").rc == 0
    assert _converted(o) == {"photo.jxl"}


def test_a_stage_takes_the_subfolders_along_without_asking(folder, tmp_path, run_cli):
    d = _tree(folder, "q3b")
    o = tmp_path / "q3bout"
    assert run_cli("images", d, o).rc == 0
    assert "photo.jxl" in _converted(o) and len(_converted(o)) > 1


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


def test_a_folder_with_only_gifs_is_offered(folder):
    """GIFs count as images in the subfolder question and the page's tree."""
    d = folder("f9", "sub/anim.gif=anim.gif")
    counts = folders.media_counts(d, config.Settings())
    assert counts == {"sub": (1, 0)} and folders.subfolder_entries(counts) == ["sub"]
