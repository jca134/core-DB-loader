"""
Seeds core.countermeasure from core_data_loader/curated/countermeasures.py and
links each one to its target taxa, gene family, component antibodies and the
studies that evaluated it.

Run after etl/bvbrc_hfv.py (rebuilds core.taxon and core.antibody), etl/immport.py
(core.study) and etl/gene_family.py; their truncates cascade these links away.
etl/evidence.py, which runs later, adds the grant/patent/paper -> countermeasure
keyword links.
"""

from sqlalchemy import text

from core_data_loader.common.db_utils import engine
from core_data_loader.curated.countermeasures import COUNTERMEASURES


def main():
    missing = []
    counts = {"taxa": 0, "gene families": 0, "antibodies": 0, "studies": 0}
    with engine.begin() as conn:
        taxa = {r[0] for r in conn.execute(text("SELECT taxon_id FROM core.taxon"))}
        families = dict(conn.execute(text("SELECT symbol, gene_family_id FROM core.gene_family")).fetchall())
        antibodies = dict(conn.execute(text("SELECT name, antibody_id FROM core.antibody")).fetchall())
        studies = {r[0] for r in conn.execute(text("SELECT study_accession FROM core.study"))}

        conn.execute(text("DELETE FROM core.countermeasure WHERE countermeasure_id <> ALL(:ids)"),
                     {"ids": [c.countermeasure_id for c in COUNTERMEASURES]})
        for c in COUNTERMEASURES:
            conn.execute(text(
                """
                INSERT INTO core.countermeasure
                    (countermeasure_id, name, kind, status, developer, regulatory, names)
                VALUES (:id, :name, :kind, :status, :developer, :regulatory, :names)
                ON CONFLICT (countermeasure_id) DO UPDATE SET
                    name = EXCLUDED.name, kind = EXCLUDED.kind, status = EXCLUDED.status,
                    developer = EXCLUDED.developer, regulatory = EXCLUDED.regulatory, names = EXCLUDED.names
                """),
                {"id": c.countermeasure_id, "name": c.name, "kind": c.kind, "status": c.status,
                 "developer": c.developer, "regulatory": c.regulatory or None, "names": list(c.names)})
            for table in ("taxon", "gene_family", "antibody", "study"):
                conn.execute(text(f"DELETE FROM core.countermeasure_{table} WHERE countermeasure_id = :id"),
                             {"id": c.countermeasure_id})

            for taxon_id in c.target_taxa:
                if taxon_id in taxa:
                    _link(conn, "taxon", "taxon_id", c.countermeasure_id, taxon_id)
                    counts["taxa"] += 1
                else:
                    missing.append(f"{c.name}: taxon {taxon_id}")
            if c.target_gene_family:
                if c.target_gene_family in families:
                    _link(conn, "gene_family", "gene_family_id", c.countermeasure_id, families[c.target_gene_family])
                    counts["gene families"] += 1
                else:
                    missing.append(f"{c.name}: gene family {c.target_gene_family}")
            for name in c.antibodies:
                if name in antibodies:
                    _link(conn, "antibody", "antibody_id", c.countermeasure_id, antibodies[name])
                    counts["antibodies"] += 1
                else:
                    missing.append(f"{c.name}: antibody {name}")
            for accession in c.studies:
                if accession in studies:
                    _link(conn, "study", "study_accession", c.countermeasure_id, accession)
                    counts["studies"] += 1
                else:
                    missing.append(f"{c.name}: study {accession}")

    print(f"core.countermeasure: {len(COUNTERMEASURES)} rows; linked to "
          + ", ".join(f"{v} {k}" for k, v in counts.items()))
    for m in missing:
        print(f"  ! not in core, link skipped: {m}")


def _link(conn, table, column, countermeasure_id, value):
    conn.execute(text(f"INSERT INTO core.countermeasure_{table} (countermeasure_id, {column}) VALUES (:c, :v) "
                      "ON CONFLICT DO NOTHING"), {"c": countermeasure_id, "v": value})


if __name__ == "__main__":
    main()
