"""
Runs every core ETL in the one order that produces a correct database, then
verifies the result with sql/checks.sql.

    uv run python -m core_data_loader.etl.run_all

WHY THIS EXISTS. The order is not a convention, it is a correctness
requirement, and getting it wrong does not raise anything -- it leaves a
database that looks fine and is quietly wrong:

  * etl_bvbrc_hfv truncates core.sequence, which CASCADEs into core.gene and
    core.gene_domain, so etl_ucsc has to follow it.
  * etl_bvbrc_hfv also rebuilds core.antibody_epitope, so etl_antibody_target
    has to follow it.
  * etl_immport truncates core.condition, which CASCADEs into
    core.taxon_condition, so etl_taxon_condition has to follow it.
  * etl_evidence resolves links to live primary keys for gene, antibody,
    epitope, structure, isolate, feature and condition -- every one of them a
    surrogate that an earlier script regenerates -- so it has to run last.

Those dependencies were written down in three places and still got missed by
hand, including once in a way that silently emptied a table. Encoding the order
here is the fix: one command, no order to remember.

Each step runs in-process rather than as a subprocess, so they share one
database engine and a failure surfaces as a real traceback rather than an exit
code. A step that fails stops the run -- continuing would build on a database
that is already wrong.
"""

import argparse
import importlib
import re
import sys
import time
from pathlib import Path

from sqlalchemy import text

from core_data_loader.common.db_utils import engine

# (module, why it sits here). Order is load-bearing; see the module docstring.
STEPS = [
    ("core_data_loader.etl.etl_bvbrc_hfv",
     "genomic core: taxa, isolates, sequences, features, proteins, structures, epitopes, antibodies"),
    ("core_data_loader.etl.etl_immport",
     "clinical core: studies, subjects, biosamples, conditions"),
    ("core_data_loader.etl.etl_ucsc",
     "reference gene model — after etl_bvbrc_hfv, which cascades core.gene away"),
    ("core_data_loader.etl.etl_taxon_condition",
     "curated taxon→disease — after etl_immport, which cascades core.taxon_condition away"),
    ("core_data_loader.etl.etl_antibody_target",
     "curated antibody→gene — after etl_bvbrc_hfv, which rebuilds core.antibody_epitope"),
    ("core_data_loader.etl.etl_evidence",
     "grants, patents, papers — last, because it resolves links to every other table's keys"),
]

CHECKS_SQL = Path(__file__).resolve().parents[2] / "sql" / "checks.sql"

# Most core tables are BIGSERIAL, but the ETLs assign their primary keys from
# in-process counters and INSERT them explicitly, so nextval() is never called
# and the sequence stays at 1 while the table fills. core.feature ended up with
# 135,892 rows behind a sequence sitting at 1.
#
# Nothing in the pipeline notices, because the pipeline never uses the default.
# Anything else does: one ordinary `INSERT INTO core.structure (pdb_id) VALUES
# (...)` -- a manual correction, a later ETL, an application -- takes
# structure_id 1, which already exists, and fails on the primary key. It keeps
# failing until the sequence catches up past every row.
#
# Resynchronising costs nothing and is idempotent, so it runs at the end of
# every pipeline rather than being left as a thing to remember.
SERIAL_COLUMNS_SQL = """
    SELECT s.relname AS seq, t.relname AS tbl, a.attname AS col
    FROM pg_class s
    JOIN pg_namespace sn ON sn.oid = s.relnamespace AND sn.nspname = 'core'
    JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
    JOIN pg_class t ON t.oid = d.refobjid
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
    WHERE s.relkind = 'S'
    ORDER BY t.relname
"""


def resync_serial_sequences(verbose: bool = False) -> list[tuple[str, int, int]]:
    """
    Advance every core BIGSERIAL sequence past the ids already in its table.

    Returns one (table.column, sequence_was_at, max_id) per sequence that was
    behind. Idempotent, and cheap enough to run after every pipeline.

    The loop is here rather than in a DO block so the list of what was actually
    corrected can be reported -- a DO block can only raise notices.
    """
    corrected = []
    with engine.begin() as conn:
        targets = conn.execute(text(SERIAL_COLUMNS_SQL)).fetchall()
        for seq, tbl, col in targets:
            # Identifiers come from the catalog, not from user input, and are
            # quoted on the way back out.
            max_id = conn.execute(
                text(f'SELECT coalesce(max("{col}"), 0) FROM core."{tbl}"')
            ).scalar()
            last_value = conn.execute(
                text("SELECT last_value FROM pg_sequences "
                     "WHERE schemaname = 'core' AND sequencename = :s"),
                {"s": seq},
            ).scalar()
            if max_id and last_value is not None and last_value < max_id:
                conn.execute(text(f"SELECT setval('core.\"{seq}\"', :v)"), {"v": max_id})
                corrected.append((f"{tbl}.{col}", last_value, max_id))
    if verbose:
        for name, was, now in corrected:
            print(f"  {name}: sequence was at {was:,}, advanced to {now:,}")
    return corrected


def run_checks() -> int:
    """
    Execute sql/checks.sql and print its report. Returns the number of failing
    checks so the caller can set an exit code.

    The file is read and run through SQLAlchemy rather than shelled out to
    psql, so this works wherever the ETL works. psql's backslash meta-commands
    (\echo, \pset) are stripped -- they are formatting for interactive use and
    mean nothing to the server.
    """
    if not CHECKS_SQL.exists():
        print(f"  ! {CHECKS_SQL} not found, skipping verification")
        return 0

    sql = "\n".join(
        line for line in CHECKS_SQL.read_text().splitlines()
        if not line.lstrip().startswith("\\")
    ).strip().rstrip(";")

    with engine.begin() as conn:
        rows = conn.execute(text(sql)).fetchall()

    width = max((len(r.check_name) for r in rows), default=10)
    for r in rows:
        print(f"  {'ok  ' if r.failures == 0 else 'FAIL'}  {r.check_name:<{width}}  {r.failures}")
    return sum(1 for r in rows if r.failures != 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--skip-checks", action="store_true",
                        help="run the ETL steps without verifying the result afterwards")
    args = parser.parse_args()

    started = time.perf_counter()
    for i, (module_name, why) in enumerate(STEPS, 1):
        short = module_name.rsplit(".", 1)[-1]
        print(f"\n[{i}/{len(STEPS)}] {short} — {why}")
        print("-" * 72)
        importlib.import_module(module_name).main()

    elapsed = time.perf_counter() - started
    print(f"\nAll {len(STEPS)} ETL steps finished in {elapsed:.0f}s.")

    corrected = resync_serial_sequences(verbose=True)
    if corrected:
        print(f"Resynchronised {len(corrected)} BIGSERIAL sequence(s) that were behind "
              "their table -- an ordinary INSERT would have collided on the primary key.")

    if args.skip_checks:
        print("Verification skipped (--skip-checks).")
        return 0

    print("\nVerifying (sql/checks.sql)")
    print("-" * 72)
    failures = run_checks()
    if failures:
        print(f"\n{failures} check(s) FAILED — the database is not correct. "
              "Each check's `what_it_means` says what kind of defect it is.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
