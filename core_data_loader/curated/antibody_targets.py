"""
Curated mapping from LANL's antibody target labels to UCSC's reference gene
model, and the upsert that fills core.antibody_epitope.target_gene_symbol.

This is hand-seeded domain knowledge, not something derived from a raw.* table,
so it lives in code rather than in an etl_*.py transform -- the same shape as
curated/taxon_conditions.py. See etl/antibody_target.py, which just calls
ensure_antibody_targets() once core.antibody_epitope exists.

WHY IT IS NEEDED. LANL reports what an antibody binds as a bare product label
("glycoprotein", "VP40 (matrix)"). core.antibody_epitope has a protein_id
column for exactly this, but it is never populated and cannot be: a product
label does not pick one of the 32004 accession-keyed core.protein rows. So the
question "which antibodies target GP" had no answer in the schema --
protein_id is null on all 149 rows and epitope_id is set on only 126.

UCSC's core.gene is the right granularity: nine authoritative rows, one per
gene, which is the level the LANL claim is actually about. 148 of the 149 rows
resolve; the one that does not is literally labelled "unknown".

WHY A SYMBOL AND NOT A gene_id. The first version of this stored
core.gene.gene_id behind a real foreign key. That made core.antibody_epitope a
dependent of core.gene, so etl.ucsc's `TRUNCATE core.gene CASCADE` emptied all
149 rows -- taking the antibody-to-epitope links with them. A gene symbol is a
stable natural key: etl.ucsc regenerates gene_id on every run but never changes
"GP". Joining `ON g.gene_symbol = ae.target_gene_symbol` costs nothing, and the
column cannot be destroyed by an unrelated ETL.

It also removes an ordering dependency. The symbol comes from the curated map
and the raw LANL row, so this step needs core.antibody_epitope to exist and
nothing else -- it no longer has to run after etl.ucsc.
"""

from sqlalchemy import text

from core_data_loader.common.db_utils import engine

# LANL product label (lowercased, as it appears in hfv_ebola_antibody.protein)
# -> UCSC gene symbol. "unknown" is deliberately absent: it is LANL's own
# placeholder for a target they did not record, not a target.
LANL_PROTEIN_TO_GENE_SYMBOL = {
    "glycoprotein": "GP",
    "nucleoprotein": "NP",
    "vp35": "VP35",
    "vp40 (matrix)": "VP40",
    "vp40": "VP40",
    "vp24": "VP24",
    "vp30": "VP30",
}


def ensure_antibody_targets() -> tuple[int, int]:
    """
    (Re)populate core.antibody_epitope.target_gene_symbol from the raw LANL
    label.

    Returns (resolved, unresolved). Clears the column first so a rerun cannot
    leave a stale value behind. A label with no mapping is left null rather
    than guessed at.
    """
    with engine.begin() as conn:
        conn.execute(text("UPDATE core.antibody_epitope SET target_gene_symbol = NULL"))

        # The join back to the raw row goes through provenance: antibody_epitope
        # has no raw key of its own, but ops.entity_provenance recorded which
        # hfv_ebola_antibody row each core.antibody came from.
        rows = conn.execute(text(
            """
            SELECT ae.antibody_epitope_id, lower(trim(a.protein)) AS label
            FROM core.antibody_epitope ae
            JOIN ops.entity_provenance p
              ON p.core_table = 'antibody' AND p.core_pk = ae.antibody_id::text
             AND p.raw_table = 'hfv_ebola_antibody'
            JOIN raw.hfv_ebola_antibody a ON a."order" = p.raw_pk
            """
        )).fetchall()

        resolved = unresolved = 0
        for antibody_epitope_id, label in rows:
            symbol = LANL_PROTEIN_TO_GENE_SYMBOL.get(label)
            if symbol is None:
                unresolved += 1
                continue
            conn.execute(
                text(
                    "UPDATE core.antibody_epitope SET target_gene_symbol = :symbol "
                    "WHERE antibody_epitope_id = :id"
                ),
                {"symbol": symbol, "id": antibody_epitope_id},
            )
            resolved += 1

    return resolved, unresolved
