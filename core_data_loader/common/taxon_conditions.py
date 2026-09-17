"""
Curated taxon -> disease-condition mappings (e.g. Zaire ebolavirus 186538 ->
Ebola hemorrhagic fever DOID:4325). This is hand-seeded domain knowledge, not
something derived from any raw.* source, so it lives in code rather than in
an etl_*.py transform -- see etl_taxon_condition.py, which just calls
ensure_taxon_conditions() after core.taxon and core.condition are populated.

taxon_id values are real NCBI ids already present in core.taxon (BV-BRC's
Filoviridae taxonomy pull; verified against data/bvbrc/taxonomy.csv and the
taxon_ids actually attached to genomes in data/bvbrc/genome.csv). ontology_id
values are Disease Ontology ids, matched against core.condition.ontology_id,
which etl_immport.py populates from ImmPort's own lk_disease vocabulary.

Reston ebolavirus (taxon 186539) is deliberately left out: DOID:4325 defines
Ebola hemorrhagic fever as caused by Zaire, Sudan, Tai Forest, or Bundibugyo
ebolavirus only -- Reston isn't associated with human disease.
"""

from sqlalchemy import text

from core_data_loader.common.db_utils import engine

# (taxon_id, taxon_name, disease_ontology_id, disease_name)
TAXON_CONDITIONS = [
    (186538, "Zaire ebolavirus", "DOID:4325", "Ebola hemorrhagic fever"),
    (186540, "Sudan ebolavirus", "DOID:4325", "Ebola hemorrhagic fever"),
    (565995, "Bundibugyo virus", "DOID:4325", "Ebola hemorrhagic fever"),
    (186541, "Tai Forest ebolavirus", "DOID:4325", "Ebola hemorrhagic fever"),
    (3052505, "Orthomarburgvirus marburgense", "DOID:4327", "Marburg hemorrhagic fever"),
]


def ensure_taxon_conditions() -> int:
    """
    (Re)populate core.taxon_condition from TAXON_CONDITIONS. A mapping is
    inserted only where both sides already exist -- taxon_id in core.taxon,
    ontology_id matching a core.condition row -- so a disease with no
    matching ImmPort study loaded yet (e.g. Marburg) is silently skipped
    rather than failing. Safe to rerun any time either table changes.
    """
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM core.taxon_condition"))
        inserted = 0
        for taxon_id, _taxon_name, ontology_id, _disease_name in TAXON_CONDITIONS:
            result = conn.execute(
                text(
                    """
                    INSERT INTO core.taxon_condition (taxon_id, condition_id)
                    SELECT t.taxon_id, c.condition_id
                    FROM core.taxon t
                    JOIN core.condition c ON c.ontology_id = :ontology_id
                    WHERE t.taxon_id = :taxon_id
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"taxon_id": taxon_id, "ontology_id": ontology_id},
            )
            inserted += result.rowcount
    return inserted
