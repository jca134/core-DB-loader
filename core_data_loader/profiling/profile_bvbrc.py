"""
Profiles every column of every raw.bvbrc_* table: row count, empty-string
rate, distinct-value count, value length range, and a few samples. Writes
the full profile to profile/bvbrc_profile.csv and prints a short summary of
columns worth a second look (fully empty, constant, or candidate keys) to
help with schema design.
"""

import csv
from pathlib import Path

from sqlalchemy import text

from core_data_loader.common.db_utils import engine

SCHEMA = "raw"
TABLE_PREFIX = "bvbrc_"
OUT_PATH = Path("profile/bvbrc_profile.csv")


def get_tables(conn):
    rows = conn.execute(
        text(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = :schema AND table_name LIKE :prefix
            ORDER BY table_name
            """
        ),
        {"schema": SCHEMA, "prefix": f"{TABLE_PREFIX}%"},
    ).fetchall()
    return [r[0] for r in rows]


def get_columns(conn, table):
    rows = conn.execute(
        text(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = :table
            ORDER BY ordinal_position
            """
        ),
        {"schema": SCHEMA, "table": table},
    ).fetchall()
    return [r[0] for r in rows]


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def profile_column(conn, table, column):
    qtable = f"{quote_ident(SCHEMA)}.{quote_ident(table)}"
    qcolumn = quote_ident(column)

    stats = conn.execute(
        text(
            f'''
            SELECT
                count(*) AS row_count,
                count(*) FILTER (WHERE {qcolumn} = '') AS empty_count,
                count(DISTINCT {qcolumn}) AS distinct_count,
                min(length({qcolumn})) AS min_len,
                max(length({qcolumn})) AS max_len
            FROM {qtable}
            '''
        )
    ).mappings().one()

    samples = conn.execute(
        text(
            f'''
            SELECT DISTINCT {qcolumn} FROM {qtable}
            WHERE {qcolumn} <> ''
            LIMIT 5
            '''
        )
    ).scalars().all()

    return stats, samples


def main() -> None:
    profile_rows = []
    flags = []

    with engine.connect() as conn:
        tables = get_tables(conn)
        for table in tables:
            print(f"Profiling {SCHEMA}.{table} ...")
            columns = get_columns(conn, table)
            for column in columns:
                stats, samples = profile_column(conn, table, column)
                row_count = stats["row_count"]
                empty_count = stats["empty_count"]
                distinct_count = stats["distinct_count"]
                non_empty_count = row_count - empty_count
                pct_empty = round(100 * empty_count / row_count, 1) if row_count else 0.0

                profile_rows.append(
                    {
                        "table": table,
                        "column": column,
                        "row_count": row_count,
                        "empty_count": empty_count,
                        "pct_empty": pct_empty,
                        "distinct_count": distinct_count,
                        "min_len": stats["min_len"],
                        "max_len": stats["max_len"],
                        "sample_values": "; ".join(map(str, samples)),
                    }
                )

                if row_count and empty_count == row_count:
                    flags.append(f"  [FULLY EMPTY]   {table}.{column}")
                elif non_empty_count and distinct_count == 1:
                    flags.append(f"  [CONSTANT]      {table}.{column} = {samples[0]!r}")
                elif row_count and distinct_count == row_count and empty_count == 0:
                    flags.append(f"  [CANDIDATE KEY] {table}.{column}")

    OUT_PATH.parent.mkdir(exist_ok=True)
    with OUT_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(profile_rows[0].keys()))
        writer.writeheader()
        writer.writerows(profile_rows)

    print(f"\nWrote {len(profile_rows)} column profiles to {OUT_PATH}")
    print(f"\n{len(flags)} columns flagged for review:")
    for flag in flags:
        print(flag)


if __name__ == "__main__":
    main()
