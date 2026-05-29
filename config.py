# config.py
# Shared configuration loader for GPS Tracker.
# Handles PyInstaller-safe path resolution and config.json persistence.
#
# PyInstaller bundles everything into a temp folder at runtime.
# sys._MEIPASS points to that temp folder for bundled assets.
# For config.json we want a WRITABLE location next to the .exe,
# not the temp folder — so we use get_app_dir() for all config I/O.

import os
import sys
import json

# ============================================================
#  Default values — used when config.json doesn't exist yet
# ============================================================

DEFAULTS = {
    "gpsbabel_path": r"C:\Program Files\GPSBabel\gpsbabel.exe",
    "port":          8080,
    "vehicle_name":  "My Vehicle"
}

CONFIG_FILENAME = "config.json"


def get_app_dir():
    """
    Returns the directory that should hold config.json.

    - When running as a PyInstaller .exe:
        sys.executable = C:\\Program Files\\GPS Tracker\\GPS Tracker.exe
        → returns  C:\\Program Files\\GPS Tracker\\

    - When running as a plain .py script (development):
        __file__ = C:\\gps_server\\config.py
        → returns  C:\\gps_server\\

    This ensures config.json is always written next to the
    executable/script, not inside PyInstaller's temp folder.
    """
    if getattr(sys, "frozen", False):
        # Running as compiled PyInstaller executable
        return os.path.dirname(sys.executable)
    else:
        # Running as a plain Python script
        return os.path.dirname(os.path.abspath(__file__))


def get_config_path():
    """Returns the full path to config.json."""
    return os.path.join(get_app_dir(), CONFIG_FILENAME)


def load():
    """
    Loads config.json and returns a dict merged with defaults.
    If the file doesn't exist or is corrupt, returns defaults.
    Any missing keys are filled in from defaults automatically.
    """
    config = dict(DEFAULTS)  # Start with a full copy of defaults
    path = get_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        # Merge: saved values override defaults, missing keys stay as default
        config.update(saved)
    except (FileNotFoundError, json.JSONDecodeError):
        pass  # File doesn't exist yet or is corrupt — use defaults
    return config


def save(config: dict):
    """
    Saves the provided config dict to config.json.
    Creates the file if it doesn't exist.
    Raises an exception if the directory is not writable.
    """
    path = get_config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
