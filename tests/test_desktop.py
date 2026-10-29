"""The app-menu entry (--install-desktop-entry), as keepbest's and lyrembed's:
it starts the media-optimizer that wrote it, on the browser page."""

from __future__ import annotations

import shutil
import subprocess
import sys

from conftest import PYDIR
from media_optimizer import desktop


def test_the_entry_opens_the_page(tmp_path, monkeypatch):
    launcher = tmp_path / "check out" / "run.py"
    launcher.parent.mkdir()
    launcher.write_text("")
    monkeypatch.setattr(sys, "argv", [str(launcher)])
    monkeypatch.setattr(desktop, "_refresh", lambda folder: None)
    entry, icon, command = desktop.install()
    assert command == [sys.executable, str(launcher)]
    assert entry == tmp_path / "home" / ".local" / "share" / "applications" / "media-optimizer.desktop"
    text = entry.read_text()
    assert f'Exec={sys.executable} "{launcher}" run --web %f' in text and f"Icon={icon}" in text
    assert "Terminal=false" in text and "MimeType=inode/directory;" in text
    assert icon.read_bytes() == desktop.ICON_SOURCE.read_bytes()
    if shutil.which("desktop-file-validate"):
        checked = subprocess.run(["desktop-file-validate", str(entry)], capture_output=True, text=True)
        assert checked.returncode == 0 and not checked.stdout.strip(), checked.stdout
    assert desktop.remove() == [entry, icon] and not entry.exists() and desktop.remove() == []


def test_exec_arguments_are_quoted_and_escaped():
    assert desktop.exec_argument("/usr/bin/media-optimizer") == "/usr/bin/media-optimizer"
    assert desktop.exec_argument("/home/a b/run.py") == '"/home/a b/run.py"'
    assert desktop.exec_argument("/p/100%") == "/p/100%%"
    assert desktop.exec_argument("/x/$y") == '"/x/\\\\$y"'


def test_the_command_line_installs_and_removes_it(run_cli, tmp_path):
    """Without a command: not taken for a full run of a folder."""
    entry = tmp_path / "home" / ".local" / "share" / "applications" / "media-optimizer.desktop"
    result = run_cli("--install-desktop-entry", cwd=tmp_path)
    assert result.rc == 0 and entry.is_file(), result.text
    assert f"{PYDIR / 'run.py'} run --web %f" in entry.read_text()
    assert "run --web" in result.text
    result = run_cli("--remove-desktop-entry", cwd=tmp_path)
    assert result.rc == 0 and not entry.exists(), result.text
    result = run_cli("--remove-desktop-entry", cwd=tmp_path)
    assert result.rc == 0 and "There was no app-menu entry." in result.text


def test_install_and_remove_exclude_each_other(run_cli, tmp_path):
    assert run_cli("--install-desktop-entry", "--remove-desktop-entry", cwd=tmp_path).rc == 2
