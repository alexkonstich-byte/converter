"""Small OS integrations: open/reveal files, sounds, system notifications."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

_QUIET = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def open_path(path: Path) -> Optional[str]:
    """Open a file or folder with the default app. Returns an error text or None."""
    path = Path(path)
    if not path.exists():
        return "Файл не найден."
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], **_QUIET)
        else:
            subprocess.Popen(["xdg-open", str(path)], **_QUIET)
    except Exception as exc:  # noqa: BLE001
        return f"Не удалось открыть: {exc}"
    return None


def reveal_path(path: Path) -> Optional[str]:
    """Show the file selected in the file manager (or open its folder)."""
    path = Path(path)
    target = path if path.exists() else path.parent
    try:
        if sys.platform == "win32":
            if target.is_file():
                subprocess.Popen(["explorer", f"/select,{target}"])
            else:
                os.startfile(str(target))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)] if target.is_file()
                             else ["open", str(target)], **_QUIET)
        else:
            subprocess.Popen(["xdg-open", str(target.parent if target.is_file() else target)],
                             **_QUIET)
    except Exception as exc:  # noqa: BLE001
        return f"Не удалось открыть папку: {exc}"
    return None


def beep() -> None:
    try:
        if sys.platform == "win32":
            import winsound
            winsound.MessageBeep(winsound.MB_OK)
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"], **_QUIET)
        elif shutil.which("canberra-gtk-play"):
            subprocess.Popen(["canberra-gtk-play", "-i", "complete"], **_QUIET)
    except Exception:
        pass


# PowerShell's own AppUserModelID lets an unpackaged app raise a toast.
_WIN_TOAST = r"""
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$txt = $tpl.GetElementsByTagName('text')
$null = $txt.Item(0).AppendChild($tpl.CreateTextNode($env:CONV_TITLE))
$null = $txt.Item(1).AppendChild($tpl.CreateTextNode($env:CONV_BODY))
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($tpl))
"""


def notify(title: str, body: str) -> None:
    """Native desktop notification. Text goes through env vars — no quoting issues."""
    env = {**os.environ, "CONV_TITLE": title, "CONV_BODY": body}
    try:
        if sys.platform == "win32":
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_TOAST],
                             env=env, creationflags=_NO_WINDOW, **_QUIET)
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e",
                              'display notification (system attribute "CONV_BODY") '
                              'with title (system attribute "CONV_TITLE")'], env=env, **_QUIET)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "Конвертер", title, body], **_QUIET)
    except Exception:
        pass


def is_packaged() -> bool:
    """True inside an app built with `flet build` (custom client, extensions bundled)."""
    return bool(os.environ.get("FLET_APP_STORAGE_DATA")) or bool(os.environ.get("CONVERTER_DND"))
