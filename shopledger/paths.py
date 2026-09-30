"""OS-appropriate per-user data locations for the app.

Windows:  %APPDATA%/ShopLedger/
macOS:    ~/Library/Application Support/ShopLedger/
Linux:    $XDG_DATA_HOME/ShopLedger/  (fallback ~/.local/share/ShopLedger/)

SHOPLEDGER_HOME overrides all of the above — the demo and tests point it at a
temp directory so they never touch a real install.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME


def data_dir() -> Path:
    """Return the per-user data directory, creating it if needed."""
    override = os.environ.get("SHOPLEDGER_HOME")
    if override:
        root = Path(override)
    elif sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        root = Path(base) / APP_NAME
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        root = Path(base) / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    """Absolute path to the SQLite database file."""
    return data_dir() / "shop.db"


def backups_dir() -> Path:
    """Directory that holds timestamped DB backups."""
    d = data_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d
