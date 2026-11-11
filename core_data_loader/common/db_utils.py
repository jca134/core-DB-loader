"""The database engine and the value-parsing helpers every stage shares."""

import hashlib
import html
import re
from datetime import datetime

from sqlalchemy import create_engine

from core_data_loader.config import DB_URL

engine = create_engine(DB_URL)


def blank_to_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MOJIBAKE_RE = re.compile(r"Ã.|â€|Â")


def _fix_mojibake(s: str) -> str:
    """
    Undo UTF-8 that was decoded as Latin-1/cp1252, possibly more than once:
    'KÃ¼ttner' -> 'Küttner'. Each round is kept only if it decodes cleanly and
    leaves fewer mojibake markers, so correct text is never touched.
    """
    for _ in range(3):
        if not _MOJIBAKE_RE.search(s):
            break
        for codec in ("cp1252", "latin-1"):
            try:
                fixed = s.encode(codec).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if len(_MOJIBAKE_RE.findall(fixed)) < len(_MOJIBAKE_RE.findall(s)):
                s = fixed
                break
        else:
            break
    return s


def clean_text(v):
    """
    Free text from a source, as it should be stored: HTML entities decoded
    (RePORTER abstracts carry '&#946;' for 'β'), control characters removed,
    double-decoded UTF-8 repaired, blank -> None. Use it for titles, abstracts
    and citation strings -- anything a reader or the keyword matcher sees.
    """
    s = blank_to_none(v)
    if s is None:
        return None
    if "&" in s:
        s = html.unescape(s)
    # \x13 / \x14 are an en / em dash (U+2013 / U+2014) that lost its high
    # byte on the way: "residues 524 \x13 539". Restore those; any other
    # control character becomes a space.
    s = _CONTROL_CHARS_RE.sub(" ", s.replace("\x13", "\u2013").replace("\x14", "\u2014"))
    s = _fix_mojibake(s)
    return blank_to_none(s)


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
    # The unzoned "...T00:00:00" variant is what NIH RePORTER returns. Without
    # it every grant date parsed as None and was written to the database as
    # NULL -- silently, since safe_date's contract is to return None rather
    # than raise on anything it cannot read. 1,811 project start dates, 1,839
    # end dates and 1,890 award notice dates were lost that way.
    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
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
