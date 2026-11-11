"""
Natural keys for evidence links, so a human review survives rebuilds.

core.evidence_link is rebuilt on every build and its ids change, so a review
(ops.link_review) can't point at an id. It is keyed instead by what the link
connects, in identifiers that stay put:

    grant_award:R01AI123456|gene:GP|related
    patent:US 20180291088 A1|taxon:186538|related

Only the link kinds worth reviewing are covered -- the keyword links from a
grant or patent (the ones with confidence below 1). Exact-identifier links
need no review.
"""

from sqlalchemy import text

# entity_type -> SQL giving (surrogate id as text, natural id) for every row.
NATURAL_ID_SQL = {
    "grant_award": "SELECT grant_award_id::text, award_key FROM core.grant_award",
    "patent": "SELECT patent_id::text, document_id FROM core.patent",
    "publication": "SELECT publication_id::text, 'PMID ' || pubmed_id FROM core.publication",
    "taxon": "SELECT taxon_id::text, taxon_id::text FROM core.taxon",
    "gene": "SELECT gene_id::text, gene_symbol FROM core.gene",
    "antibody": "SELECT antibody_id::text, name FROM core.antibody",
    "condition": "SELECT condition_id::text, coalesce(ontology_id, condition_reported) FROM core.condition",
    "symptom": "SELECT symptom_id::text, hpo_id FROM core.symptom",
    "assay_type": "SELECT assay_type_id::text, coalesce(obi_id, name) FROM core.assay_type",
    "countermeasure": "SELECT countermeasure_id::text, countermeasure_id::text FROM core.countermeasure",
}
REVIEWABLE_DOCUMENTS = ("grant_award", "patent")
REVIEWABLE_ENTITIES = ("taxon", "gene", "antibody", "condition", "symptom", "assay_type", "countermeasure")


def natural_ids(conn) -> dict[str, dict[str, str]]:
    """entity_type -> {surrogate id as text: natural id}."""
    return {t: dict(conn.execute(text(sql)).fetchall()) for t, sql in NATURAL_ID_SQL.items()}


def link_key(ids: dict, evidence_type: str, evidence_id, entity_type: str, entity_id, relation: str) -> str | None:
    doc = ids.get(evidence_type, {}).get(str(evidence_id))
    ent = ids.get(entity_type, {}).get(str(entity_id))
    if doc is None or ent is None:
        return None
    return f"{evidence_type}:{doc}|{entity_type}:{ent}|{relation}"


def apply_reviews(conn) -> int:
    """
    Copy every saved decision onto the rebuilt link's support rows. Returns
    how many links received a decision. Called at the end of etl/evidence.py.
    """
    reviews = dict(conn.execute(text("SELECT link_key, decision FROM ops.link_review")).fetchall())
    if not reviews:
        return 0
    ids = natural_ids(conn)
    applied = 0
    for link_id, et, ei, nt, ni, rel in conn.execute(text(
            "SELECT evidence_link_id, evidence_type, evidence_id, entity_type, entity_id, relation "
            "FROM core.evidence_link WHERE evidence_type = ANY(:docs) AND entity_type = ANY(:ents)"),
            {"docs": list(REVIEWABLE_DOCUMENTS), "ents": list(REVIEWABLE_ENTITIES)}).fetchall():
        decision = reviews.get(link_key(ids, et, ei, nt, ni, rel))
        if decision:
            conn.execute(text("UPDATE core.evidence_link_support SET review_status = :d WHERE evidence_link_id = :id"),
                         {"d": decision, "id": link_id})
            applied += 1
    return applied
