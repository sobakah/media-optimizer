"""In a toolbox or distrobox (Fedora Silverblue, Bluefin): what the host starts
enters the container, what media-optimizer opens in other apps leaves it."""

import sys

import pytest

from media_optimizer import desktop, gpu


def test_in_a_toolbox_the_menu_entry_enters_it(toolbox, tmp_path, monkeypatch):
    """The desktop reads the entry on the host, where the container's Python isn't."""
    monkeypatch.setattr(desktop, "_refresh", lambda folder: None)
    shim = tmp_path / "bin" / "media-optimizer"
    shim.parent.mkdir()
    shim.write_text("")
    monkeypatch.setattr(sys, "argv", [str(shim)])
    entry, _, command = desktop.install()
    assert command == ["/usr/bin/toolbox", "run", "--container", "my box", str(shim)]
    assert f'Exec=/usr/bin/toolbox run --container "my box" {shim} run --web %f\n' in entry.read_text()


def test_in_a_toolbox_the_page_opens_in_the_hosts_browser(toolbox, monkeypatch):
    """A toolbox has no xdg-open: Python's webbrowser asks gio there, which
    finds no browser and still reports success."""
    started = []
    monkeypatch.setattr(toolbox.subprocess, "Popen", lambda argv, **kw: started.append(argv))
    monkeypatch.setattr(toolbox.webbrowser, "open", lambda url: pytest.fail("webbrowser in a container"))
    assert toolbox.open_url("http://127.0.0.1:1/") is True
    assert started == [["flatpak-spawn", "--host", "xdg-open", "http://127.0.0.1:1/"]]


def test_on_an_image_based_system_the_driver_hint_layers_it(tmp_path, monkeypatch):
    """Silverblue's dnf can't install: the freeworld driver is layered with rpm-ostree."""
    from media_optimizer import container
    monkeypatch.setattr(gpu, "_distro", lambda: "fedora")
    reason = "No usable encoding profile found"
    assert any("sudo dnf install mesa-va-drivers-freeworld" in line for line in gpu.driver_hint(reason))
    (tmp_path / "ostree-booted").touch()
    monkeypatch.setattr(container, "OSTREE_BOOTED", tmp_path / "ostree-booted")
    hint = gpu.driver_hint(reason)
    assert any("rpm-ostree install mesa-va-drivers-freeworld" in line for line in hint)
    assert not any("dnf" in line for line in hint)
