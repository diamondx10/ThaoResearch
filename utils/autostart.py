"""Windows autostart via registry (HKCU\\...\\Run)."""
from __future__ import annotations

import logging
import platform
import shutil
import sys
from pathlib import Path
from typing import Tuple

logger = logging.getLogger("thaoresearch.utils.autostart")

APP_NAME = "ThaoResearch"

REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_windows() -> bool:
    return platform.system() == "Windows"


def _get_executable_path() -> str:
    """Get the command to launch ThaoResearch at startup."""
    exe = sys.executable
    script = (Path(__file__).resolve().parent.parent / "main.py").resolve()
    return f'"{exe}" "{script}" --daemon'


def get_autostart_status() -> Tuple[bool, str]:
    """Return (enabled, current_command_or_reason)."""
    if not is_windows():
        return (False, "autostart is only supported on Windows")

    try:
        import winreg  # type: ignore
    except ImportError:
        return (False, "pywin32 is not installed")

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ) as key:
            try:
                value, _ = winreg.QueryValueEx(key, APP_NAME)
                return (True, str(value))
            except FileNotFoundError:
                return (False, "not registered")
    except OSError as e:
        return (False, f"registry error: {e}")


def install_autostart() -> Tuple[bool, str]:
    """Register ThaoResearch to run at Windows startup. Returns (ok, message)."""
    if not is_windows():
        return (False, "autostart is only supported on Windows")

    try:
        import winreg  # type: ignore
    except ImportError:
        return (False, "pywin32 is not installed")

    cmd = _get_executable_path()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        logger.info("Autostart registered: %s", cmd)
        return (True, f"Registered: {cmd}")
    except OSError as e:
        logger.error("Failed to register autostart: %s", e)
        return (False, f"Failed to write registry: {e}")


def uninstall_autostart() -> Tuple[bool, str]:
    """Remove ThaoResearch from Windows startup. Returns (ok, message)."""
    if not is_windows():
        return (False, "autostart is only supported on Windows")

    try:
        import winreg  # type: ignore
    except ImportError:
        return (False, "pywin32 is not installed")

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            try:
                winreg.DeleteValue(key, APP_NAME)
                logger.info("Autostart unregistered")
                return (True, "Autostart removed")
            except FileNotFoundError:
                return (True, "Autostart was not registered (nothing to remove)")
    except OSError as e:
        logger.error("Failed to remove autostart: %s", e)
        return (False, f"Failed to update registry: {e}")
