"""
Curated taxon -> disease-condition mappings (e.g. Zaire ebolavirus 186538 ->
Ebola hemorrhagic fever DOID:4325). This is hand-seeded domain knowledge, not
something derived from any raw.* source, so it lives in code rather than in
an etl_*.py transform -- see etl_taxon_condition.py, which just calls
ensure_taxon_conditions() after core.taxon and core.condition are populated.

taxon_id values are real NCBI ids at species rank, present in core.taxon
(BV-BRC's Filoviridae taxonomy pull). Data rows mostly attach to taxa below
the species -- e.g. Zaire ebolavirus 186538 and Ebola virus 1570291 both sit
under Orthoebolavirus zairense 3052462 -- so mappings are made at the species
and reach those rows through the core.taxon_condition_inherited view. BV-BRC
mixes taxonomy vintages: Marburg appears under both the renamed species
(3052505) and the legacy one (11269, which has its own genus branch), so both
are listed. ontology_id
values are Disease Ontology ids, matched against core.condition.ontology_id,
which etl_immport.py populates from ImmPort's own lk_disease vocabulary.

Reston (Orthoebolavirus restonense 3052459) is deliberately left out: DOID:4325 defines
Ebola hemorrhagic fever as caused by Zaire, Sudan, Tai Forest, or Bundibugyo
ebolavirus only -- Reston isn't associated with human disease.
"""

from sqlalchemy import text

from core_data_loader.common.db_utils import engine

# (taxon_id, taxon_name, disease_ontology_id, disease_name)
TAXON_CONDITIONS = [
    (3052462, "Orthoebolavirus zairense", "DOID:4325", "Ebola hemorrhagic fever"),
    (3052460, "Orthoebolavirus sudanense", "DOID:4325", "Ebola hemorrhagic fever"),
    (3052458, "Orthoebolavirus bundibugyoense", "DOID:4325", "Ebola hemorrhagic fever"),
    (3052461, "Orthoebolavirus taiense", "DOID:4325", "Ebola hemorrhagic fever"),
    (3052505, "Orthomarburgvirus marburgense", "DOID:4327", "Marburg hemorrhagic fever"),
    (11269, "Marburg marburgvirus", "DOID:4327", "Marburg hemorrhagic fever"),
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
