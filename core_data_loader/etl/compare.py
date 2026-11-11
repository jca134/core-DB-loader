"""
Compares the contents of two builds of the database, table by table.

    filovirus compare filovirus_test          # this database vs filovirus_test
    filovirus compare postgresql+psycopg2://user@host/db

WHY. Row counts have hidden real bugs here before -- a column silently going
NULL, links pointing at the wrong row. Before an ETL change is merged, build a
fresh copy with it and compare the copy with the current database: every
difference should be one the change meant to make.

HOW. Every core.* table and ops.entity_provenance is read in full on both
sides and compared as a multiset of rows. Ids that legitimately differ between
two correct builds are replaced by natural keys first:

  source_id        -> source_code   (a fresh database numbers sources 1..n;
                                     an old one has gaps from years of upserts)
  publication_id   -> pubmed_id     (papers get ids in upsert order)
  organization_id  -> org_key       (numbered in name order: one change renumbers the rest)
  a polymorphic reference to a publication -- evidence_link, xref and
  provenance rows whose entity type is 'publication' -- the same way
  evidence_link_id -> the link itself (evidence/entity/relation), in support
                      and lineage rows -- link ids renumber whenever a link is added

and per-row serial ids that only number the rows of a rebuilt table
(xref_id, provenance_id, the link ids, loaded_at) are dropped. Every other id
is compared as is: the ETL truncates with RESTART IDENTITY and inserts in a
fixed order, so a correct rebuild reproduces them.
"""

import re
from collections import Counter

from sqlalchemy import create_engine, text

from core_data_loader.config import DB_URL

# Columns that only number the rows of a table rewritten wholesale on every run.
ROW_IDS = {
    "xref": {"xref_id"},
    "evidence_link": {"evidence_link_id"},
    "evidence_link_support": {"evidence_link_support_id"},
    "entity_provenance": {"provenance_id", "loaded_at", "ingest_file_id"},
}

# (table, type column, id column): a polymorphic reference that may point at a
# publication, whose id must be translated to its PMID.
POLYMORPHIC = {
    "evidence_link": [("evidence_type", "evidence_id"), ("entity_type", "entity_id")],
    "xref": [("entity_type", "entity_id")],
    "entity_provenance": [("core_table", "core_pk")],
}

SAMPLE = 3


def resolve_url(target: str) -> str:
    """A full SQLAlchemy URL, or a bare database name on the same server as DB_URL."""
    if "://" in target:
        return target
    return re.sub(r"/[^/?]+(\?|$)", f"/{target}\\1", DB_URL, count=1)


class Snapshot:
    """One database's tables as Counters of normalised rows."""

    def __init__(self, url: str):
        self.engine = create_engine(url)
        with self.engine.connect() as conn:
            self.sources = dict(conn.execute(text("SELECT source_id, source_code FROM core.source")).fetchall())
            self.pmids = {str(k): v for k, v in conn.execute(
                text("SELECT publication_id, pubmed_id FROM core.publication")).fetchall()}
            # Organizations are numbered in name order, so one added or removed
            # renumbers the rest: compare them by their normalized key.
            self.orgs = {}
            if conn.execute(text("SELECT to_regclass('core.organization')")).scalar():
                self.orgs = dict(conn.execute(text("SELECT organization_id, org_key FROM core.organization")).fetchall())
            self.tables = [r[0] for r in conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'core' AND table_type = 'BASE TABLE' ORDER BY 1"))]
            self.links = {}
            for row in conn.execute(text(
                    "SELECT evidence_link_id, evidence_type, evidence_id, entity_type, entity_id, relation "
                    "FROM core.evidence_link")):
                lid, et, ei, nt, ni, rel = row
                self.links[lid] = (et, self._ref(et, ei), nt, self._ref(nt, ni), rel)

    def _ref(self, kind, value):
        if kind == "publication":
            return ("pmid", self.pmids.get(str(value)))
        if kind == "evidence_link" and hasattr(self, "links"):
            # Lineage rows for links: link ids renumber whenever a link is added.
            return self.links.get(int(value), value)
        return value

    def rows(self, schema: str, table: str) -> Counter:
        with self.engine.connect() as conn:
            result = conn.execute(text(f'SELECT * FROM {schema}."{table}"'))
            columns = list(result.keys())
            drop = ROW_IDS.get(table, set())
            out = Counter()
            for raw in result:
                row = dict(zip(columns, raw))
                for type_col, id_col in POLYMORPHIC.get(table, []):
                    row[id_col] = self._ref(row[type_col], row[id_col])
                if "source_id" in row:
                    row["source_id"] = self.sources.get(row["source_id"], row["source_id"])
                if "publication_id" in row:
                    row["publication_id"] = ("pmid", self.pmids.get(str(row["publication_id"])))
                if "organization_id" in row:
                    row["organization_id"] = ("org", self.orgs.get(row["organization_id"]))
                if table == "evidence_link_support":
                    row["evidence_link_id"] = self.links.get(row["evidence_link_id"])
                out[tuple(sorted((k, repr(v)) for k, v in row.items() if k not in drop))] += 1
            return out


def compare(target: str) -> int:
    """Print a per-table comparison of this database with `target`. Returns an exit code."""
    other_url = resolve_url(target)
    print(f"Comparing {DB_URL.rsplit('/', 1)[-1]} (this) with {other_url.rsplit('/', 1)[-1]} (other)\n")
    this, other = Snapshot(DB_URL), Snapshot(other_url)

    tables = [("core", t) for t in sorted(set(this.tables) | set(other.tables))]
    tables.append(("ops", "entity_provenance"))
    width = max(len(f"{s}.{t}") for s, t in tables)
    differing = 0
    for schema, table in tables:
        name = f"{schema}.{table}"
        if table not in this.tables + ["entity_provenance"] or table not in other.tables + ["entity_provenance"]:
            print(f"  DIFF  {name:<{width}}  table exists on one side only")
            differing += 1
            continue
        a, b = this.rows(schema, table), other.rows(schema, table)
        if a == b:
            print(f"  same  {name:<{width}}  {sum(a.values()):>8,} rows")
            continue
        differing += 1
        only_a, only_b = a - b, b - a
        print(f"  DIFF  {name:<{width}}  {sum(a.values()):>8,} vs {sum(b.values()):,} rows; "
              f"{sum(only_a.values()):,} only in this, {sum(only_b.values()):,} only in other")
        for label, rows in (("this", only_a), ("other", only_b)):
            for row in list(rows)[:SAMPLE]:
                shown = {k: (v if len(v) <= 80 else v[:77] + "...") for k, v in row}
                print(f"        only in {label}: {shown}")

    print(f"\n{'No differences.' if not differing else f'{differing} table(s) differ.'}")
    return 1 if differing else 0
