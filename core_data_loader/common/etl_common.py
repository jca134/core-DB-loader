"""Shared helpers for the etl_*.py raw-to-core transform scripts."""

import pandas as pd
from sqlalchemy import text

from core_data_loader.common.db_utils import (
    blank_to_none,
    clean_columns,
    engine,
    safe_date,
    safe_float,
    safe_int,
    safe_year,
)
from core_data_loader.common.sources import SOURCES, ensure_sources

# Re-exported for etl_*.py, which imports its db_utils/sources helpers
# through this module rather than reaching past the etl_common -> raw_common
# layering to get them directly.
__all__ = [
    "engine", "blank_to_none", "safe_int", "safe_float", "safe_date", "safe_year", "clean_columns",
    "SOURCES", "ensure_sources",
    "truncate", "read_raw", "raw_table_exists", "write_core", "blank_strings_to_null",
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


def blank_strings_to_null(df: pd.DataFrame) -> pd.DataFrame:
    """
    Replace '' with None in every text column.

    The loaders read their sources with `keep_default_na=False`, so a blank CSV
    cell arrives as an empty string rather than NaN. Most build_*() functions
    pass values through blank_to_none on the way out, but not all of them did,
    and the ones that didn't were invisible: the row count looked right and the
    column looked populated.

    It had spread to 17 columns across 8 tables -- 66,206 of core.feature's
    product values, and 4,903 of core.sequence's 4,907 sequence_type values,
    were '' rather than NULL. That breaks the ordinary ways of asking whether a
    value is there: `WHERE product IS NULL` missed every one of them, and
    `coalesce(product, gene)` returned '' instead of falling through.

    Doing it here rather than at each call site means a new column cannot
    reintroduce it. '' and NULL both mean "the source had nothing here" in
    every column of this schema -- there is no column where an empty string is
    a meaningful value distinct from absence.
    """
    # Both dtypes, deliberately. pandas 3 gives a column of Python strings the
    # `str` dtype, not `object`, so a plain `dtype == object` test skips every
    # text column built from a list of strings and this function silently does
    # nothing. `object` is still checked because a mixed or date-bearing column
    # lands there; `.eq("")` is False throughout such a column, so including it
    # costs nothing.
    text_cols = [c for c in df.columns
                 if pd.api.types.is_string_dtype(df[c]) or df[c].dtype == object]
    if not text_cols:
        return df
    df = df.copy()
    for c in text_cols:
        # mask rather than .replace(): only exact empty strings, leaving
        # whitespace-only values alone for the caller to decide about.
        df[c] = df[c].mask(df[c].eq(""), None)
    return df


def write_core(df: pd.DataFrame, table: str, note: str = "") -> pd.DataFrame:
    """Appends df to core.<table> and logs the row count -- the standard
    last step of every build_*() in an etl_*.py script."""
    df = blank_strings_to_null(df)
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
    call per entity — for the etl/immport.py style of building a core table
    as a single vectorized DataFrame rather than an itertuples loop. df must
    carry raw_table_col/source_id_col (see etl.immport.concat_tables), since
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


def write_provenance(provenance_rows: list, core_tables: list, shared_tables: dict = None):
    """
    Replace the lineage rows for `core_tables`, which this ETL owns outright.

    `shared_tables` is for a core table two ETLs both write to -- currently only
    core.publication, which etl.immport populates from its studies and
    etl.evidence upserts papers onto. Deleting by core_table alone would make
    each script wipe the other's lineage, and not deleting at all would
    accumulate a fresh copy on every rerun. So it maps a table name to the
    source_ids this ETL is responsible for, and only those rows are replaced:
    {"publication": [pubmed_source_id]}.
    """
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ops.entity_provenance WHERE core_table = ANY(:tables)"),
            {"tables": core_tables},
        )
        for table, source_ids in (shared_tables or {}).items():
            conn.execute(
                text(
                    "DELETE FROM ops.entity_provenance "
                    "WHERE core_table = :table AND source_id = ANY(:source_ids)"
                ),
                {"table": table, "source_ids": list(source_ids)},
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
