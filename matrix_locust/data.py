"""Paths for the dataset consumed by Locust."""

from __future__ import annotations

import os
from pathlib import Path


DATA_DIR = Path(os.environ.get("MATRIX_DATA_DIR", "data/homeserver"))
USERS_CSV = DATA_DIR / "users.csv"
TOKENS_CSV = DATA_DIR / "tokens.csv"
ROOMS_JSON = DATA_DIR / "rooms.json"
