"""A desktop notification, with an "Open" button where possible.

notify-send (libnotify) when present: with -A it waits for a click and
prints the action's name, so it runs in a thread and calls on_action when
the button was pressed. Otherwise a plain notification through gdbus (the
org.freedesktop.Notifications service GNOME and KDE both provide), without
a button. Otherwise nothing.

No dependencies, and never an exception into the caller: a notification
must not change a run. Small enough to copy into keepbest.

Started with subprocess directly, not proc.run: it is no media tool, a
stop must not wait for it, and it may outlive the run (its button just
does nothing then).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from typing import Callable

APP_NAME = "media-optimizer"


def notify(title: str, body: str, *, icon: str = "", action: str = "",
           on_action: Callable[[], None] | None = None) -> bool:
    """Show a notification; True when one was handed to the desktop. With
    action and on_action, a button that calls on_action when clicked."""
    try:
        if shutil.which("notify-send"):
            cmd = ["notify-send", f"--app-name={APP_NAME}"]
            if icon:
                cmd.append(f"--icon={icon}")
            if action and on_action is not None:
                cmd.append(f"--action=open={action}")
                threading.Thread(target=_wait_for_click, args=(cmd + [title, body], on_action),
                                 name="notify", daemon=True).start()
                return True
            return subprocess.run(cmd + [title, body], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=10).returncode == 0
        if shutil.which("gdbus"):
            cmd = ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                   "--object-path", "/org/freedesktop/Notifications",
                   "--method", "org.freedesktop.Notifications.Notify",
                   APP_NAME, "0", icon, title, body, "[]", "{}", "-1"]
            return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def _wait_for_click(cmd: list[str], on_action: Callable[[], None]) -> None:
    try:
        out = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True).stdout
        if out.strip() == "open":
            on_action()
    except Exception:  # noqa: BLE001 - a notification never ends anything
        pass
