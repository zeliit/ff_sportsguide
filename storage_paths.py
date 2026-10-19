"""Persistent paths used by FF SportsGuide."""
import os
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent

if os.name != "nt" and Path("/data").is_dir():
    DB_DIR = Path("/data/db")
    RUNTIME_DIR = Path("/data/db/ff_sportsguide")
else:
    DB_DIR = PLUGIN_ROOT / "data"
    RUNTIME_DIR = PLUGIN_ROOT / "data"

LOCK_DIR = RUNTIME_DIR / "locks"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
LOCK_DIR.mkdir(parents=True, exist_ok=True)

# Backward-compatible alias for any external helper that imported DATA_DIR.
DATA_DIR = RUNTIME_DIR
