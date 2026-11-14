"""Running in a toolbox or distrobox container (Fedora Silverblue, Bluefin and
other image-based systems).

The container shares the home folder and the session bus with the host, but
the programs installed in it aren't on the host. So what the host starts (the
app-menu entry, the D-Bus service) enters the container first (``on_host``),
and what this program opens in other apps (the browser, a Flatpak, the file
manager) leaves it (``host_command``, ``open_url``).

The same file is in cdport, keepbest, lyrembed and media-optimizer (copied
unchanged); picktag's ``core/desktop.py`` recognises containers the same way.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

# What toolbox and podman put into a container (module constants for the tests).
TOOLBOX_ENV = Path("/run/.toolboxenv")
CONTAINER_ENV = Path("/run/.containerenv")
HOST_ROOT = Path("/run/host")          # the host's / inside toolbox and distrobox
OSTREE_BOOTED = Path("/run/ostree-booted")


def container() -> tuple[str, str] | None:
    """("toolbox" or "distrobox", the container's name) when running in one."""
    name = None
    try:
        for line in CONTAINER_ENV.read_text().splitlines():
            if line.startswith("name="):
                name = line[5:].strip().strip('"')
    except OSError:
        pass
    if TOOLBOX_ENV.exists():
        return ("toolbox", name) if name else None
    box = os.environ.get("CONTAINER_ID")      # distrobox's: the container's name
    return ("distrobox", box) if box else None


def image_based_host() -> bool:
    """Running directly on an image-based system, where dnf can't install."""
    return OSTREE_BOOTED.exists() and container() is None


def _host_program(name: str) -> str:
    """The host's path of *name*, as seen under /run/host, else the bare name.
    A D-Bus service needs an absolute path."""
    for folder in (Path("/usr/bin"), Path("/usr/local/bin"), Path.home() / ".local" / "bin"):
        if (HOST_ROOT / folder.relative_to("/") / name).exists():
            return str(folder / name)
    return name


def on_host(cmd: list[str]) -> list[str]:
    """*cmd* as the host starts it: inside the container this program runs in,
    if it runs in one."""
    found = container()
    if found is None:
        return cmd
    kind, name = found
    if kind == "toolbox":
        return [_host_program("toolbox"), "run", "--container", name, *cmd]
    return [_host_program("distrobox-enter"), "-n", name, "--", *cmd]


def host_command(cmd: list[str]) -> list[str]:
    """*cmd* run on the host from inside the container, if this program runs
    in one: the browser, the file manager and Flatpak are the host's."""
    found = container()
    if found is None:
        return cmd
    if found[0] == "toolbox" or shutil.which("flatpak-spawn"):
        return ["flatpak-spawn", "--host", *cmd]
    return [shutil.which("host-spawn") or "distrobox-host-exec", *cmd]


def flatpak_on_host(cmd: list[str]) -> list[str]:
    """A ``flatpak`` command runs on the host from a container (Flatpak isn't
    in it); any other command stays as it is."""
    if container() is not None and cmd and Path(cmd[0]).name == "flatpak":
        return host_command(cmd)
    return cmd


def open_url(url: str) -> bool:
    """Open *url* in the browser, the host's from a container; False when
    nothing could be started. A toolbox has no xdg-open, and Python's
    webbrowser then asks gio, which finds no browser but reports success."""
    if container() is None:
        return webbrowser.open(url)
    try:
        subprocess.Popen(host_command(["xdg-open", url]), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True
