"""Filesystem locations for the distributable GGE Equipment Manager app.

Runtime/user data is deliberately kept outside the application directory so a
shared copy of the program never contains another user's remembered accounts or
commander snapshots.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "GGE Equipment Manager"
APP_VERSION = "0.7.0"


def program_dir() -> Path:
    """Directory containing the source files or the frozen executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Directory containing bundled read-only resources (PyInstaller aware)."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle)
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    return resource_dir() / name


def app_data_dir() -> Path:
    """Per-Windows-user persistent data directory.

    Nothing in this directory is bundled into or copied with the app itself.
    """
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / APP_NAME
    return Path.home() / ".gge_equipment_manager"


APP_DATA_DIR = app_data_dir()
ACCOUNTS_PATH = APP_DATA_DIR / "accounts.json"  # usernames only, never passwords
LOADOUT_DIR = APP_DATA_DIR / "commander_loadouts"


def ensure_app_data_dirs() -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOADOUT_DIR.mkdir(parents=True, exist_ok=True)


def migrate_legacy_loadouts() -> int:
    """Copy older per-program-folder snapshots into AppData once.

    Returns the number of newly copied snapshot files. The legacy folder is not
    deleted automatically because it contains no password material and may be
    useful as a backup.
    """
    legacy_dir = program_dir() / "commander_loadouts"
    if not legacy_dir.exists() or legacy_dir.resolve() == LOADOUT_DIR.resolve():
        return 0

    ensure_app_data_dirs()
    copied = 0
    for src in legacy_dir.glob("player_*.json"):
        dst = LOADOUT_DIR / src.name
        if dst.exists():
            continue
        try:
            shutil.copy2(src, dst)
            copied += 1
        except OSError:
            pass
    return copied
