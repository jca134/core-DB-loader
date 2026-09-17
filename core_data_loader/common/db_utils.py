"""
Shared, source-agnostic helpers: the DB connection and defensive parsers for
the all-TEXT columns everything in raw.* gets loaded with.

Layering: db_utils (this file, no schema knowledge) -> sources (the
core.source catalog) -> raw_common (load_*.py helpers) -> etl_common
(etl_*.py helpers).
"""

import hashlib
import os
import re
from datetime import datetime

from sqlalchemy import create_engine

DB_URL = os.environ.get(
    "FILOVIRUS_DB_URL",
    "postgresql+psycopg2://jadenaguilon@localhost:5432/filovirus",
)

engine = create_engine(DB_URL)


def blank_to_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def safe_int(v):
    s = blank_to_none(v)
    if s is None:
        return None
    try:
        return int(float(s))  # float() first so values like "3.0" still parse
    except ValueError:
        return None


def safe_float(v):
    s = blank_to_none(v)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%m/%d/%Y")


def safe_date(v):
    s = blank_to_none(v)
    if s is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def hash_file(path: str) -> str:
    # Streamed so a large source download doesn't get read into memory twice
    # (once here, once by pandas).
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
    return md5.hexdigest()


def clean_columns(columns):
    # Handles messy Excel headers: mixed case, punctuation, and duplicate
    # "Unnamed: N" columns pandas produces for blank header cells.
    cleaned = []
    for c in columns:
        c = str(c).strip().lower()
        c = re.sub(r"[^0-9a-z]+", "_", c).strip("_")
        cleaned.append(c or "col")
    seen = {}
    deduped = []
    for c in cleaned:
        seen[c] = seen.get(c, 0) + 1
        deduped.append(c if seen[c] == 1 else f"{c}_{seen[c]}")
    return deduped
