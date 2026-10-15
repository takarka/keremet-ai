"""Поиск adb: PATH, затем пакет WinGet (Google.PlatformTools), чтобы работать из любой оболочки."""

from __future__ import annotations

import glob
import os
import shutil


def find_adb() -> str:
    found = shutil.which("adb")
    if found:
        return found
    base = os.environ.get("LOCALAPPDATA", "")
    pattern = os.path.join(base, "Microsoft", "WinGet", "Packages", "Google.PlatformTools*", "platform-tools", "adb.exe")
    matches = glob.glob(pattern)
    return matches[0] if matches else "adb"


def is_network_serial(serial: str | None) -> bool:
    """Серийник вида ip:port — устройство подключено по Wi-Fi."""
    return bool(serial) and ":" in serial


def ensure_connected(serial: str | None, timeout: float = 15.0) -> bool:
    """Для Wi-Fi устройства выполнить `adb connect` (повторный вызов безопасен). Для USB ничего не делает."""
    if not is_network_serial(serial):
        return True
    import subprocess

    try:
        out = subprocess.run([find_adb(), "connect", serial], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "connected" in out.lower() and "cannot" not in out.lower() and "failed" not in out.lower()
