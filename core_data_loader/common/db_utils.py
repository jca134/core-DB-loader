import getpass
import hashlib
import os
import re
from datetime import datetime

from sqlalchemy import create_engine

# Matches the README's documented default of
# postgresql+psycopg2://$(whoami)@localhost:5432/filovirus -- resolved from
# the current OS user rather than hardcoded, so this works for whoever's
# running it, not just the machine it was written on.
DB_URL = os.environ.get(
    "FILOVIRUS_DB_URL",
    f"postgresql+psycopg2://{getpass.getuser()}@localhost:5432/filovirus",
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
    except (ValueError, OverflowError):
        # OverflowError covers the values float() accepts but int() can't
        # represent ("inf", "1e400") -- without it one bad cell aborts a run.
        return None


def safe_float(v):
    s = blank_to_none(v)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y",
)


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


# A year, optionally followed by a month: "2014", "2014-08". Collection dates
# are this imprecise for roughly a fifth of the BV-BRC/NCBI records.
_PARTIAL_DATE_RE = re.compile(r"(\d{4})(?:-\d{2})?")


def safe_year(v):
    """
    Year of a full or partial date, for sources that often only report the
    year. safe_date() rejects "2014" (there is no honest DATE for it), so
    without this those records would lose their collection year entirely.
    Ranges like "2014-2015" stay None -- no single year is correct.
    """
    d = safe_date(v)
    if d is not None:
        return d.year
    s = blank_to_none(v)
    m = _PARTIAL_DATE_RE.fullmatch(s) if s else None
    return int(m.group(1)) if m else None


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
