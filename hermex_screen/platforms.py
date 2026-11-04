"""Which hosts this plugin serves, and what it can say about their lock screens."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

WINDOWS_OPT_IN_ENV = "HERMEX_SCREEN_WINDOWS"


def windows_opted_in() -> bool:
    return os.environ.get(WINDOWS_OPT_IN_ENV, "").strip() == "1"


def refusal(platform: Optional[str] = None) -> Optional[str]:
    """Why this plugin won't serve this host, in words for the person holding the phone."""
    platform = platform or sys.platform
    if platform == "darwin":
        return None
    if platform == "win32":
        return None if windows_opted_in() else (
            f"Windows isn't supported yet. A tester can set {WINDOWS_OPT_IN_ENV}=1 for the dashboard to opt in.")
    return "This host has stock Bot Screen, so it doesn't use this plugin."


def locked() -> Optional[bool]:
    """Whether the console session's screen is locked. None where this can't tell."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["/usr/sbin/ioreg", "-n", "Root", "-d1"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        return "CGSSessionScreenIsLocked" in out
    if sys.platform == "win32":
        # The input desktop can't be opened from a session while the secure desktop is up.
        import ctypes
        desktop = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)
        if desktop:
            ctypes.windll.user32.CloseDesktop(desktop)
        return not desktop
    return None


def lock_accepts_input(platform: Optional[str] = None) -> bool:
    """Windows doesn't allow remote input on its lock screen. A Mac's is for the owner's phone test."""
    return (platform or sys.platform) == "darwin"
