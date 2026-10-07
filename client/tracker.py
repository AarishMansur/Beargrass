"""Cross-platform active/idle time tracking for the local trigger client.

Stdlib only: no daemons, no admin rights, no telemetry. The client samples
the OS idle counter, accumulates *active* (non-idle) seconds, and reports
when the screen-time threshold is crossed.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Optional

IDLE_CUTOFF_SECONDS = 60.0  # >60s without input counts as "away from screen"


def get_idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard/mouse input, or None if unsupported."""
    system = platform.system()
    if system == "Windows":
        return _windows_idle_seconds()
    if system == "Linux":
        return _linux_idle_seconds()
    if system == "Darwin":
        return _macos_idle_seconds()
    return None


def _windows_idle_seconds() -> Optional[float]:
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("dwTime", wintypes.UINT),
        ]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    # GetTickCount is the same 32-bit epoch GetLastInputInfo reports against.
    tick_ms = ctypes.windll.kernel32.GetTickCount()
    idle_ms = (tick_ms - info.dwTime) & 0xFFFFFFFF
    return idle_ms / 1000.0


def _linux_idle_seconds() -> Optional[float]:
    """X11 idle time via the tiny `xprintidle` utility (millisecond stdout)."""
    if shutil.which("xprintidle") is None:
        return None
    try:
        output = subprocess.check_output(["xprintidle"], timeout=3).decode().strip()
        return int(output) / 1000.0
    except (subprocess.SubprocessError, ValueError):
        return None


def _macos_idle_seconds() -> Optional[float]:
    """IOKit HID idle time (nanoseconds) without any third-party package."""
    try:
        output = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem"], timeout=5
        ).decode(errors="replace")
    except (subprocess.SubprocessError, OSError):
        return None
    for line in output.splitlines():
        if "HIDIdleTime" in line:
            try:
                nanos = int(line.split("=")[-1].strip())
            except ValueError:
                return None
            return nanos / 1_000_000_000.0
    return None


class ActiveTimeTracker:
    """Accumulates non-idle seconds and edge-triggers on a threshold.

    `tick()` returns True exactly once per breach so the caller can send a
    single alert instead of spamming the backend; call `acknowledge()` after
    a successful trigger to start the next window.
    """

    def __init__(
        self,
        threshold_minutes: int = 120,
        idle_cutoff_seconds: float = IDLE_CUTOFF_SECONDS,
    ) -> None:
        if threshold_minutes <= 0:
            raise ValueError("threshold_minutes must be positive")
        self.threshold_minutes = threshold_minutes
        self.idle_cutoff_seconds = idle_cutoff_seconds
        self.active_seconds = 0.0
        self._breached = False

    @property
    def threshold_seconds(self) -> float:
        return self.threshold_minutes * 60.0

    @property
    def active_minutes(self) -> int:
        return int(self.active_seconds // 60)

    @property
    def progress(self) -> float:
        """0.0 -> 1.0 progress toward the threshold."""
        return min(self.active_seconds / self.threshold_seconds, 1.0)

    @property
    def breached(self) -> bool:
        return self.active_seconds >= self.threshold_seconds

    def tick(self, idle_seconds: Optional[float], elapsed_seconds: float) -> bool:
        """Fold one sampling period into the tracker.

        Returns True only on the transition into the breached state.
        """
        if elapsed_seconds < 0:
            raise ValueError("elapsed_seconds cannot be negative")

        if idle_seconds is None:
            # Unknown idle state (headless/unsupported): treat every elapsed
            # second as active so the agent still nudges the user outdoors.
            is_active = True
        else:
            is_active = idle_seconds < self.idle_cutoff_seconds

        if is_active:
            self.active_seconds += elapsed_seconds

        if self.breached and not self._breached:
            self._breached = True
            return True
        return False

    def acknowledge(self) -> None:
        """Reset the window after an alert has been delivered."""
        self.active_seconds = 0.0
        self._breached = False
