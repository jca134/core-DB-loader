"""
Shared helpers for the load_*.py raw-ingestion scripts. Every loader mirrors
its source verbatim into raw.<table> (dtype=str, if_exists="replace"); this
factors out that boilerplate so each loader only has to deal with its own
file format.

Also wraps ops.ingest_batch/ops.ingest_file (see sql/03_ops.sql): each
load_*.py run opens one batch per source_code it loads (start_batch),
logs one ops.ingest_file row per file via load_to_raw's batch_id/file_path
args, and closes the batch (complete_batch) in a try/finally so a crashed
run is recorded as 'failed' rather than left 'running' forever.
"""

import pandas as pd
from sqlalchemy import text

from core_data_loader.common.db_utils import engine, clean_columns, hash_file
from core_data_loader.common.sources import ensure_sources


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
    # file_hash is best-effort: file_path may be a synthetic label (e.g. an
    # Excel sheet name) rather than a real path on disk.
    try:
        file_hash = hash_file(file_path)
    except OSError:
        file_hash = None

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
