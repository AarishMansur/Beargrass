"""Desktop alerts when screen time is up.

Windows-first, stdlib only. Mechanisms tried in order for ``auto``:

1. ``window``  — a real topmost popup window (tkinter): "Screen time is
   over — go touch grass" plus the actual nearby place and walking route.
2. ``dialog``  — a blocking MessageBox (works even without tkinter/display).
3. ``toast``   — modern Windows 10/11 toast via the WinRT API (PowerShell).
4. ``balloon`` — classic NotifyIcon balloon tip (renders as a toast on Win10+).

Linux falls back to ``notify-send`` and macOS to ``osascript``.
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import sys
from typing import Callable

ALERT_STYLES = ("auto", "window", "toast", "balloon", "dialog", "none")

# PowerShell reads the XML from the environment, so user text never has to be
# escaped for the shell — only for the XML itself.
_TOAST_SCRIPT = r"""
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PowerShell')
    if ($notifier.Setting -ne 'Enabled') { exit 3 }
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($env:TG_TOAST_XML)
    $notifier.Show([Windows.UI.Notifications.ToastNotification]::new($xml))
    exit 0
} catch { exit 1 }
"""

_BALLOON_SCRIPT = r"""
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $ni = New-Object System.Windows.Forms.NotifyIcon
    $ni.Icon = [System.Drawing.SystemIcons]::Information
    $ni.Text = 'Touch Grass Agent'
    $ni.Visible = $true
    $ni.ShowBalloonTip(10000, $env:TG_TITLE, $env:TG_BODY, [System.Windows.Forms.ToolTipIcon]::Info)
    Start-Sleep -Seconds 11
    $ni.Dispose()
    exit 0
} catch { exit 1 }
"""


def format_alert(result: dict) -> tuple[str, str]:
    """Turn a TriggerResponse dict into a (title, body) notification pair."""
    title = result.get("headline") or "Screen time is up."
    place = (result.get("place") or {}).get("name") or "the nearest patch of outdoors"
    route = result.get("route") or {}

    line = place
    distance = route.get("distance_m")
    duration = route.get("duration_min")
    if distance is not None:
        if distance < 1000:
            pretty = f"{round(distance)} m"
        else:
            pretty = f"{distance / 1000:.1f} km"
        pace = f", ~{max(1, round(duration))} min walk" if duration else ""
        line = f"{place} — {pretty}{pace}"

    body = line
    plan = result.get("plan")
    if plan:
        body = f"{line}\n{plan}"
    return title, body


def notify(title: str, body: str, style: str = "auto") -> str:
    """Show a native alert. Returns the mechanism used, or 'none'."""
    if style not in ALERT_STYLES:
        raise ValueError(f"unknown alert style: {style}")
    if style == "none":
        return "none"

    if style != "auto":
        order: tuple[str, ...] = (style,)
    elif sys.platform == "win32":
        order = ("window", "dialog", "toast", "balloon")
    elif sys.platform == "darwin":
        order = ("window", "macos")
    else:
        order = ("window", "linux")

    for name in order:
        mechanism = _MECHANISMS.get(name)
        if mechanism is None:
            continue
        try:
            if mechanism(title, body):
                return name
        except Exception:  # noqa: BLE001 - a broken notifier must never kill the loop
            continue
    return "none"


# --------------------------------------------------------------------------- #
# Windows mechanisms
# --------------------------------------------------------------------------- #
def _run_powershell(script: str, env: dict[str, str], timeout: float = 12.0) -> bool:
    if not shutil.which("powershell"):
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=timeout,
            env=env,
            creationflags=flags,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return completed.returncode == 0


def _toast(title: str, body: str) -> bool:
    if sys.platform != "win32":
        return False
    xml = (
        "<toast activationType='foreground'>"
        "<visual><binding template='ToastGeneric'>"
        f"<text>{html.escape(title)}</text>"
        f"<text>{html.escape(body)}</text>"
        "</binding></visual>"
        "<audio src='ms-winsoundevent:Notification.Default'/>"
        "</toast>"
    )
    env = dict(os.environ, TG_TOAST_XML=xml)
    return _run_powershell(_TOAST_SCRIPT, env)


def _balloon(title: str, body: str) -> bool:
    if sys.platform != "win32":
        return False
    env = dict(os.environ, TG_TITLE=title, TG_BODY=body)
    return _run_powershell(_BALLOON_SCRIPT, env, timeout=15.0)


def _dialog(title: str, body: str) -> bool:
    """Blocking MessageBox — the guaranteed fallback on Windows."""
    if sys.platform != "win32":
        return False
    import ctypes

    MB_OK = 0x00000000
    MB_ICONINFORMATION = 0x00000040
    MB_SETFOREGROUND = 0x00010000
    MB_TOPMOST = 0x00040000
    result = ctypes.windll.user32.MessageBoxW(
        None, body, title, MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
    )
    return result != 0


def _window(title: str, body: str) -> bool:
    """A real popup window that demands attention.

    Layout: "Screen time is over." / headline / place+route / plan / button.
    Blocks until the human dismisses it (or 5 minutes pass).
    """
    try:
        import tkinter as tk
    except ImportError:
        return False

    root = None
    try:
        root = tk.Tk()
        root.title("Touch Grass Agent")
        root.attributes("-topmost", True)
        root.resizable(False, False)

        card = tk.Frame(root, padx=28, pady=20)
        card.pack(fill="both", expand=True)

        tk.Label(
            card, text="Screen time is over.", font=("Segoe UI", 10), fg="#5f6368"
        ).pack(anchor="w")
        tk.Label(
            card, text=title, font=("Segoe UI", 20, "bold")
        ).pack(anchor="w", pady=(2, 10))

        lines = [line for line in body.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            tk.Label(
                card,
                text=line,
                font=("Segoe UI", 13, "bold") if index == 0 else ("Segoe UI", 10),
                wraplength=460,
                justify="left",
            ).pack(anchor="w", pady=(0 if index == 0 else 4, 0))

        tk.Button(
            card,
            text="Alright, I'm going outside",
            command=root.destroy,
            padx=14,
            pady=6,
        ).pack(anchor="e", pady=(18, 0))

        root.update_idletasks()
        width, height = root.winfo_width(), root.winfo_height()
        x = max((root.winfo_screenwidth() - width) // 2, 0)
        y = max((root.winfo_screenheight() - height) // 3, 0)
        root.geometry(f"+{x}+{y}")
        root.after(300_000, root.destroy)  # never trap the loop forever
        root.mainloop()
        return True
    except Exception:  # noqa: BLE001 - no display / headless session
        return False
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# POSIX mechanisms
# --------------------------------------------------------------------------- #
def _macos(title: str, body: str) -> bool:
    if not shutil.which("osascript"):
        return False
    try:
        completed = subprocess.run(
            [
                "osascript",
                "-e", "on run argv",
                "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e", "end run",
                "--", title, body,
            ],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return completed.returncode == 0


def _linux(title: str, body: str) -> bool:
    if not shutil.which("notify-send"):
        return False
    try:
        completed = subprocess.run(
            ["notify-send", "--app-name=TouchGrassAgent", title, body],
            capture_output=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return completed.returncode == 0


_MECHANISMS: dict[str, Callable[[str, str], bool]] = {
    "window": _window,
    "toast": _toast,
    "balloon": _balloon,
    "dialog": _dialog,
    "macos": _macos,
    "linux": _linux,
}
