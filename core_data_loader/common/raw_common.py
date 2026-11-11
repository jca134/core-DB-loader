"""
Shared helpers for the loaders (core_data_loader/loaders/). Every loader
mirrors its source files verbatim into raw.<table> -- all columns as text,
each table replaced on every run -- so each loader only has to deal with its
own file format.

Each run is recorded in ops.ingest_batch / ops.ingest_file (sql/03_ops.sql):

    with ingest_batch("ncbi", notes="sequences.csv") as batch:
        batch.load(read_csv(path), "ncbi_sequences", path)

opens one batch for the source, logs one ops.ingest_file row (path, md5, row
count) per table loaded, and marks the batch 'succeeded' -- or 'failed' if the
block raises, rather than leaving it 'running' forever.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from core_data_loader.common.db_utils import clean_columns, engine, hash_file
from core_data_loader.common.sources import ensure_sources
from core_data_loader.config import PROJECT_ROOT


def read_csv(path, **kwargs) -> pd.DataFrame:
    """A source file as all-text columns, with blanks kept as '' (raw.* is verbatim)."""
    return pd.read_csv(path, dtype=str, keep_default_na=False, **kwargs)


@dataclass
class Batch:
    batch_id: int
    source_id: int

    def load(self, df: pd.DataFrame, table: str, file_path, clean_col_names: bool = True) -> int:
        return load_to_raw(df, table, clean_col_names=clean_col_names,
                           batch_id=self.batch_id, source_id=self.source_id, file_path=file_path)

    def load_optional(self, path: Path, table: str, hint: str = "", **read_kwargs) -> bool:
        """Load a companion file if it exists; say so and carry on if it doesn't."""
        if not Path(path).exists():
            print(f"  (no {_display(path)}; skipped{': ' + hint if hint else ''})")
            return False
        self.load(read_csv(path, **read_kwargs), table, path)
        return True


@contextmanager
def ingest_batch(source_code: str, notes: str | None = None):
    ensure_raw_schema()
    batch_id, source_id = start_batch(source_code, notes)
    try:
        yield Batch(batch_id, source_id)
    except BaseException:
        complete_batch(batch_id, status="failed")
        raise
    complete_batch(batch_id, status="succeeded")


def _display(path) -> str:
    """Paths are logged relative to the project, so ops.ingest_file is portable."""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(p)


def ensure_raw_schema():
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS raw"))


def start_batch(source_code: str, notes: str = None) -> tuple[int, int]:
    """Opens an ops.ingest_batch row and returns (batch_id, source_id)."""
    source_id = ensure_sources()[source_code]
    with engine.begin() as conn:
        batch_id = conn.execute(
            text(
                """
                INSERT INTO ops.ingest_batch (source_id, notes)
                VALUES (:source_id, :notes)
                RETURNING batch_id
                """
            ),
            {"source_id": source_id, "notes": notes},
        ).scalar()
    return batch_id, source_id


def complete_batch(batch_id: int, status: str = "succeeded"):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE ops.ingest_batch
                SET status = :status, completed_at = now()
                WHERE batch_id = :batch_id
                """
            ),
            {"status": status, "batch_id": batch_id},
        )


def load_to_raw(df: pd.DataFrame, table_name: str, clean_col_names: bool = True,
                 batch_id: int = None, source_id: int = None, file_path: str = None) -> int:
    if clean_col_names:
        df = df.copy()
        df.columns = clean_columns(df.columns)
    df.to_sql(name=table_name, con=engine, schema="raw", if_exists="replace", index=False)
    print(f"Loaded raw.{table_name} ({len(df)} rows)")

    if batch_id is not None:
        _log_ingest_file(batch_id, source_id, file_path or table_name, len(df))

    return len(df)


def _log_ingest_file(batch_id: int, source_id: int, file_path: str, row_count: int):
    # file_hash is best-effort: file_path may be a directory (the patent
    # exports are loaded as one table) rather than a single file.
    try:
        file_hash = hash_file(file_path)
    except OSError:
        file_hash = None
    file_path = _display(file_path)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO ops.ingest_file (batch_id, source_id, file_path, file_hash, row_count)
                VALUES (:batch_id, :source_id, :file_path, :file_hash, :row_count)
                """
            ),
            {
                "batch_id": batch_id,
                "source_id": source_id,
                "file_path": file_path,
                "file_hash": file_hash,
                "row_count": row_count,
            },
        )
