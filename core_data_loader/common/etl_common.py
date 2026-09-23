"""Shared helpers for the etl_*.py raw-to-core transform scripts."""

import pandas as pd
from sqlalchemy import text

from core_data_loader.common.db_utils import engine, blank_to_none, safe_int, safe_float, safe_date, safe_year, clean_columns
from core_data_loader.common.sources import SOURCES, ensure_sources

# Re-exported for etl_*.py, which imports its db_utils/sources helpers
# through this module rather than reaching past the etl_common -> raw_common
# layering to get them directly.
__all__ = [
    "engine", "blank_to_none", "safe_int", "safe_float", "safe_date", "safe_year", "clean_columns",
    "SOURCES", "ensure_sources",
    "truncate", "read_raw", "raw_table_exists", "write_core",
    "add_xref", "write_xref",
    "add_provenance", "write_provenance",
]


# Walks foreign keys outward from `tables` to every table TRUNCATE ... CASCADE
# would also empty. Recursive because CASCADE is transitive: a table two FK
# hops away goes too.
_CASCADE_TARGETS_SQL = """
WITH RECURSIVE listed AS (
    SELECT c.oid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname || '.' || c.relname = ANY(:tables)
), dependents AS (
    SELECT con.conrelid AS oid
    FROM pg_constraint con
    WHERE con.contype = 'f' AND con.confrelid IN (SELECT oid FROM listed)
  UNION
    SELECT con.conrelid
    FROM pg_constraint con
    JOIN dependents d ON con.confrelid = d.oid
    WHERE con.contype = 'f'
)
SELECT DISTINCT n.nspname || '.' || c.relname
FROM dependents d
JOIN pg_class c ON c.oid = d.oid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname || '.' || c.relname <> ALL(:tables)
"""


def truncate(*tables: str):
    """
    TRUNCATE ... RESTART IDENTITY CASCADE over `tables`.

    CASCADE also empties any table with a foreign key into one of these, so
    the argument list alone understates what is destroyed -- e.g. truncating
    core.condition takes core.taxon_condition with it, which is seeded by a
    separate ETL and won't come back on its own. Anything cascaded that the
    caller didn't list is reported here rather than silently emptied, so a
    partial rerun can't leave a hole nobody notices.
    """
    names = ", ".join(tables)
    with engine.begin() as conn:
        collateral = [
            (name, conn.execute(text(f"SELECT count(*) FROM {name}")).scalar())
            for (name,) in conn.execute(text(_CASCADE_TARGETS_SQL), {"tables": list(tables)})
        ]
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))

    for name, row_count in sorted(collateral):
        if row_count:
            print(f"  ! cascaded into {name}: {row_count} rows emptied -- rerun the ETL that seeds it")


def read_raw(table: str) -> pd.DataFrame:
    return pd.read_sql_table(table, engine, schema="raw")


def write_core(df: pd.DataFrame, table: str, note: str = "") -> pd.DataFrame:
    """Appends df to core.<table> and logs the row count -- the standard
    last step of every build_*() in an etl_*.py script."""
    df.to_sql(table, engine, schema="core", if_exists="append", index=False)
    print(f"core.{table}: {len(df)} rows{note}")
    return df


def raw_table_exists(table: str) -> bool:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'raw' AND table_name = :t"
            ),
            {"t": table},
        ).fetchone()
    return row is not None


# core.xref is one polymorphic table for every entity type's external ids
# (see sql/02_core.sql). These two helpers are the only things an ETL script
# should need to touch it directly.

def add_xref(xref_rows: list, entity_type: str, entity_id, source_id: int, xref_type: str, value):
    value = blank_to_none(value)
    if entity_id is None or value is None:
        return
    xref_rows.append({
        "entity_type": entity_type,
        "entity_id": entity_id,
        "source_id": source_id,
        "xref_type": xref_type,
        "xref_value": value,
    })


def write_xref(xref_rows: list, entity_types: list):
    # Scoped delete, not a blanket truncate, so ETL scripts that own
    # different entity_types can re-run independently without wiping each
    # other's xref rows.
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM core.xref WHERE entity_type = ANY(:types)"),
            {"types": entity_types},
        )
    if not xref_rows:
        print("core.xref: 0 rows")
        return
    # Dedup per-entity, not per-value — two different entities can
    # legitimately share the same external id.
    df = pd.DataFrame(xref_rows).drop_duplicates(
        subset=["entity_type", "entity_id", "source_id", "xref_type", "xref_value"]
    )
    df.to_sql("xref", engine, schema="core", if_exists="append", index=False)
    print(f"core.xref: {len(df)} rows ({', '.join(entity_types)})")


# ops.entity_provenance is row-level lineage from a core row back to the raw
# row(s) it was derived from (see sql/03_ops.sql). Scoped the same way as
# core.xref above: one accumulator list per script, flushed once at the end.

def add_provenance(provenance_rows: list, core_table: str, core_pk, source_id, raw_table: str, raw_pk=None):
    if core_pk is None:
        return
    provenance_rows.append({
        "core_table": core_table,
        "core_pk": str(core_pk),
        "source_id": source_id,
        "raw_table": raw_table,
        "raw_pk": str(raw_pk) if blank_to_none(raw_pk) is not None else None,
    })


def add_provenance_bulk(provenance_rows: list, df: pd.DataFrame, core_table: str, core_pk_col: str,
                         raw_pk_col: str = None, raw_table_col: str = "_raw_table",
                         source_id_col: str = "_source_id"):
    """
    Same as add_provenance, but derives one row per row of df instead of one
    call per entity — for the etl_immport.py style of building a core table
    as a single vectorized DataFrame rather than an itertuples loop. df must
    carry raw_table_col/source_id_col (see etl_immport.concat_tables), since
    a core DataFrame assembled across studies needs a different raw_table
    per row, not one value for the whole call.
    """
    if df.empty:
        return
    raw_pk_series = df[raw_pk_col] if raw_pk_col else df[core_pk_col]
    for core_pk, source_id, raw_table, raw_pk in zip(
        df[core_pk_col], df[source_id_col], df[raw_table_col], raw_pk_series
    ):
        add_provenance(provenance_rows, core_table, core_pk, source_id, raw_table, raw_pk)


def write_provenance(provenance_rows: list, core_tables: list):
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ops.entity_provenance WHERE core_table = ANY(:tables)"),
            {"tables": core_tables},
        )
    if not provenance_rows:
        print("ops.entity_provenance: 0 rows")
        return
    # Dedup like write_xref: an identical lineage tuple carries no extra
    # information. Needed because not every raw table has a unique row key --
    # hfv_ebola_ctl only has iedb_id, so two of its rows that collapse into one
    # epitope *and* share an iedb_id are indistinguishable here.
    df = pd.DataFrame(provenance_rows).drop_duplicates(
        subset=["core_table", "core_pk", "source_id", "raw_table", "raw_pk"]
    )
    df.to_sql("entity_provenance", engine, schema="ops", if_exists="append", index=False)
    print(f"ops.entity_provenance: {len(df)} rows ({', '.join(core_tables)})")
