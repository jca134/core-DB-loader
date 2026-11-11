"""
Copies the core.* schema into Neo4j as a property graph, so it can be queried
in Cypher -- the query language SerenQA's knowledge-retrieval task generates
(the paper's own benchmark KG, CKG, is a Neo4j database).

    filovirus export

Postgres stays the source of truth. Every run wipes the Neo4j database and
rebuilds it from core.*, so rerun this after every build; nothing is ever edited
in Neo4j directly.

    NEO4J_URI       default bolt://localhost:7687
    NEO4J_USER      default neo4j
    NEO4J_PASSWORD  default filovirus (the local dev password; change it
                    anywhere the database is reachable by anyone else)

SHAPE. One node per row of an entity table (NODES), labelled in CamelCase and
keyed on the table's own primary-key column, so `MATCH (t:Taxon {taxon_id:
186538})` means the same thing in both databases. Every node also gets a
`name` for display. Foreign keys and join tables become relationships
(EDGES); core.evidence_link becomes one relationship per row, typed by its
relation (RELATED, CITES, CLAIMS, DESCRIBES) and carrying its confidence and
the terms that produced it.

Left in Postgres: raw nucleotide / amino-acid / alignment strings (tens of MB
nobody reads in a graph browser), and the lineage tables (xref values are
copied onto their nodes as properties instead; provenance is not).

NOTHING IS DROPPED SILENTLY. Each relationship query MATCHes both endpoints;
an endpoint that doesn't exist would make Neo4j skip the row without error.
So every node and relationship count is compared with the Postgres row count
it came from, and the run fails on any difference.
"""

import sys
import time
from decimal import Decimal

from neo4j import GraphDatabase
from sqlalchemy import text

from core_data_loader.common.db_utils import engine
from core_data_loader.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

BATCH = 5000

# Columns never copied: bulk sequence strings, and source_id (replaced by a
# readable `source` property).
SKIP_COLUMNS = {"sequence_text", "aligned_sequence", "source_id"}

# label, table, primary key, SQL expression for the display `name`
NODES = [
    ("Taxon", "taxon", "taxon_id", "taxon_name"),
    ("Isolate", "isolate", "isolate_id", "primary_strain_name"),
    ("Sequence", "sequence", "sequence_id", "concat_ws(' ', molecule_type, segment, sequence_type)"),
    ("Feature", "feature", "feature_id", "coalesce(product, gene, feature_type)"),
    ("Protein", "protein", "protein_id", "coalesce(product, gene)"),
    ("Gene", "gene", "gene_id", "gene_symbol"),
    # gene_family has its own `name` column (the full name); the display name
    # must keep it, since SELECT *, <expr> AS name lets the second `name` win.
    ("GeneFamily", "gene_family", "gene_family_id", "symbol || ': ' || name"),
    ("GeneDomain", "gene_domain", "gene_domain_id", "pfam_name"),
    ("Structure", "structure", "structure_id", "pdb_id"),
    ("Epitope", "epitope", "epitope_id", "epitope_sequence"),
    ("EpitopeAssay", "epitope_assay", "epitope_assay_id", "concat_ws(' ', assay_type, assay_outcome)"),
    ("Antibody", "antibody", "antibody_id", "name"),
    ("AssayType", "assay_type", "assay_type_id", "name"),
    ("Symptom", "symptom", "symptom_id", "name"),
    ("Condition", "condition", "condition_id", "coalesce(condition_preferred, condition_reported)"),
    ("Study", "study", "study_accession", "brief_title"),
    ("StudyArm", "study_arm", "arm_accession", "name"),
    ("Subject", "subject", "subject_accession", "subject_accession"),
    ("ImmuneExposure", "immune_exposure", "exposure_accession",
     "coalesce(exposure_material_preferred, exposure_process_preferred, exposure_accession)"),
    ("Biosample", "biosample", "biosample_accession", "coalesce(name, biosample_accession)"),
    ("Treatment", "treatment", "treatment_accession", "coalesce(name, treatment_accession)"),
    ("Experiment", "experiment", "experiment_accession", "coalesce(name, experiment_accession)"),
    ("ExperimentSample", "experiment_sample", "expsample_accession", "coalesce(name, expsample_accession)"),
    ("Protocol", "protocol", "protocol_accession", "coalesce(name, protocol_accession)"),
    ("Alignment", "alignment", "alignment_id", "name"),
    ("Publication", "publication", "publication_id", "coalesce(title, 'PMID ' || pubmed_id)"),
    ("Grant", "grant_award", "grant_award_id", "project_title"),
    ("GrantFunding", "grant_funding", "grant_funding_id", "project_num"),
    ("Patent", "patent", "patent_id", "title"),
    ("Countermeasure", "countermeasure", "countermeasure_id", "name"),
    ("Organization", "organization", "organization_id", "name"),
]
LABEL_KEY = {label: key for label, _, key, _ in NODES}

# Relationships: (from label, type, to label, SQL returning s, d [, props...]).
# Every column after s and d becomes a relationship property.
EDGES = [
    ("Taxon", "CHILD_OF", "Taxon",
     "SELECT taxon_id s, parent_id d FROM core.taxon WHERE parent_id IS NOT NULL AND parent_id <> taxon_id"),
    ("Isolate", "OF_TAXON", "Taxon", "SELECT isolate_id s, taxon_id d FROM core.isolate WHERE taxon_id IS NOT NULL"),
    ("Sequence", "FROM_ISOLATE", "Isolate", "SELECT sequence_id s, isolate_id d FROM core.sequence WHERE isolate_id IS NOT NULL"),
    ("Feature", "ON_SEQUENCE", "Sequence", "SELECT feature_id s, sequence_id d FROM core.feature WHERE sequence_id IS NOT NULL"),
    ("Gene", "ON_SEQUENCE", "Sequence", "SELECT gene_id s, sequence_id d FROM core.gene WHERE sequence_id IS NOT NULL"),
    ("Protein", "ENCODED_BY", "Feature", "SELECT protein_id s, feature_id d FROM core.protein WHERE feature_id IS NOT NULL"),
    # Gene families: the virus-neutral bridge between proteins of every
    # filovirus and the Ebola reference genes (curated/gene_families.py).
    ("Gene", "IN_FAMILY", "GeneFamily",
     "SELECT gene_id s, gene_family_id d FROM core.gene WHERE gene_family_id IS NOT NULL"),
    ("Protein", "IN_FAMILY", "GeneFamily",
     "SELECT protein_id s, gene_family_id d, gene_family_basis AS basis FROM core.protein "
     "WHERE gene_family_id IS NOT NULL"),
    ("GeneDomain", "DOMAIN_OF", "Gene", "SELECT gene_domain_id s, gene_id d FROM core.gene_domain WHERE gene_id IS NOT NULL"),
    ("Structure", "OF_PROTEIN", "Protein", "SELECT structure_id s, protein_id d FROM core.structure WHERE protein_id IS NOT NULL"),
    ("Structure", "OF_TAXON", "Taxon", "SELECT structure_id s, taxon_id d FROM core.structure WHERE taxon_id IS NOT NULL"),
    ("Epitope", "ON_PROTEIN", "Protein", "SELECT epitope_id s, protein_id d FROM core.epitope WHERE protein_id IS NOT NULL"),
    ("Epitope", "OF_TAXON", "Taxon", "SELECT epitope_id s, taxon_id d FROM core.epitope WHERE taxon_id IS NOT NULL"),
    ("EpitopeAssay", "TESTS", "Epitope", "SELECT epitope_assay_id s, epitope_id d FROM core.epitope_assay WHERE epitope_id IS NOT NULL"),
    ("EpitopeAssay", "HAS_ASSAY_TYPE", "AssayType", "SELECT epitope_assay_id s, assay_type_id d FROM core.epitope_assay_assay_type"),
    ("AssayType", "SUBTYPE_OF", "AssayType",
     "SELECT assay_type_id s, parent_assay_type_id d FROM core.assay_type WHERE parent_assay_type_id IS NOT NULL"),
    ("Symptom", "SUBTYPE_OF", "Symptom",
     "SELECT symptom_id s, parent_symptom_id d FROM core.symptom WHERE parent_symptom_id IS NOT NULL"),
    ("Isolate", "HAS_SYMPTOM", "Symptom", "SELECT isolate_id s, symptom_id d FROM core.isolate_symptom"),
    # The inherited view, not taxon_condition itself: a strain then reaches its
    # disease in one hop, as it does in SQL. mapped_taxon_id says which
    # species-level mapping it came from.
    ("Taxon", "CAUSES", "Condition",
     "SELECT taxon_id s, condition_id d, mapped_taxon_id FROM core.taxon_condition_inherited"),
    ("Antibody", "BINDS", "Epitope",
     "SELECT antibody_id s, epitope_id d, binding_comment, target_gene_symbol FROM core.antibody_epitope "
     "WHERE epitope_id IS NOT NULL"),
    # antibody_epitope only carries the gene symbol; sql/checks.sql guarantees
    # every symbol exists in core.gene.
    ("Antibody", "TARGETS", "Gene",
     "SELECT DISTINCT ae.antibody_id s, g.gene_id d FROM core.antibody_epitope ae "
     "JOIN core.gene g ON g.gene_symbol = ae.target_gene_symbol"),
    ("Study", "STUDIES", "Condition", "SELECT study_accession s, condition_id d FROM core.study_condition"),
    ("Study", "HAS_PUBLICATION", "Publication", "SELECT study_accession s, publication_id d FROM core.study_publication"),
    ("StudyArm", "ARM_OF", "Study", "SELECT arm_accession s, study_accession d FROM core.study_arm WHERE study_accession IS NOT NULL"),
    ("Subject", "IN_ARM", "StudyArm",
     "SELECT subject_accession s, arm_accession d, age_event, min_subject_age, max_subject_age, age_unit "
     "FROM core.study_arm_subject"),
    ("ImmuneExposure", "OF_SUBJECT", "Subject",
     "SELECT exposure_accession s, subject_accession d FROM core.immune_exposure WHERE subject_accession IS NOT NULL"),
    ("ImmuneExposure", "IN_ARM", "StudyArm",
     "SELECT exposure_accession s, arm_accession d FROM core.immune_exposure WHERE arm_accession IS NOT NULL"),
    ("Biosample", "FROM_SUBJECT", "Subject",
     "SELECT biosample_accession s, subject_accession d FROM core.biosample WHERE subject_accession IS NOT NULL"),
    ("Biosample", "IN_STUDY", "Study",
     "SELECT biosample_accession s, study_accession d FROM core.biosample WHERE study_accession IS NOT NULL"),
    ("Biosample", "RECEIVED", "Treatment", "SELECT biosample_accession s, treatment_accession d FROM core.biosample_treatment"),
    ("Experiment", "IN_STUDY", "Study",
     "SELECT experiment_accession s, study_accession d FROM core.experiment WHERE study_accession IS NOT NULL"),
    ("Experiment", "USES_PROTOCOL", "Protocol", "SELECT experiment_accession s, protocol_accession d FROM core.experiment_protocol"),
    ("Experiment", "HAS_ASSAY_TYPE", "AssayType", "SELECT experiment_accession s, assay_type_id d FROM core.experiment_assay_type"),
    ("ExperimentSample", "IN_EXPERIMENT", "Experiment",
     "SELECT expsample_accession s, experiment_accession d FROM core.experiment_sample WHERE experiment_accession IS NOT NULL"),
    ("ExperimentSample", "FROM_BIOSAMPLE", "Biosample",
     "SELECT expsample_accession s, biosample_accession d FROM core.experiment_sample WHERE biosample_accession IS NOT NULL"),
    ("Sequence", "MEMBER_OF", "Alignment",
     "SELECT sequence_id s, alignment_id d, strain_label, row_order FROM core.alignment_member"),
    ("GrantFunding", "FUNDING_OF", "Grant", "SELECT grant_funding_id s, grant_award_id d FROM core.grant_funding"),
    # Countermeasures (curated/countermeasures.py).
    ("Countermeasure", "TARGETS", "Taxon", "SELECT countermeasure_id s, taxon_id d FROM core.countermeasure_taxon"),
    ("Countermeasure", "ACTS_ON", "GeneFamily",
     "SELECT countermeasure_id s, gene_family_id d FROM core.countermeasure_gene_family"),
    ("Countermeasure", "CONTAINS", "Antibody",
     "SELECT countermeasure_id s, antibody_id d FROM core.countermeasure_antibody"),
    ("Countermeasure", "TESTED_IN", "Study",
     "SELECT countermeasure_id s, study_accession d FROM core.countermeasure_study"),
    # Organizations (curated/organizations.py).
    ("Grant", "AWARDED_TO", "Organization",
     "SELECT grant_award_id s, organization_id d FROM core.grant_award_organization"),
    ("Patent", "ASSIGNED_TO", "Organization",
     "SELECT patent_id s, organization_id d, role FROM core.patent_organization"),
]

# core.evidence_link's entity_type / evidence_type values -> node labels.
EVIDENCE_LABELS = {
    "grant_award": "Grant", "patent": "Patent", "publication": "Publication",
    "taxon": "Taxon", "gene": "Gene", "antibody": "Antibody", "epitope": "Epitope",
    "structure": "Structure", "feature": "Feature", "isolate": "Isolate",
    "condition": "Condition", "symptom": "Symptom", "assay_type": "AssayType",
    "study": "Study", "countermeasure": "Countermeasure",
}

# One row per evidence link, with its support rows rolled up: the distinct
# terms, fields and methods that produced it.
EVIDENCE_SQL = """
    SELECT el.evidence_link_id, el.evidence_type, el.evidence_id, el.entity_type, el.entity_id,
           el.relation, el.confidence, src.source_code AS source,
           array_remove(array_agg(DISTINCT s.matched_term), NULL) AS matched_terms,
           array_remove(array_agg(DISTINCT s.match_field), NULL)  AS match_fields,
           array_remove(array_agg(DISTINCT s.match_method), NULL) AS match_methods,
           count(s.*) AS support_count
    FROM core.evidence_link el
    LEFT JOIN core.evidence_link_support s USING (evidence_link_id)
    LEFT JOIN core.source src ON src.source_id = el.source_id
    GROUP BY el.evidence_link_id, src.source_code
"""


def _value(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def _props(row: dict) -> dict:
    """Neo4j stores no nulls, and its lists must be homogeneous and non-null."""
    out = {}
    for k, v in row.items():
        if v is None or k in SKIP_COLUMNS:
            continue
        if isinstance(v, list):
            v = [_value(x) for x in v if x is not None]
        out[k] = _value(v)
    return out


def _xref_properties(conn) -> dict:
    """(table, entity_id) -> {'xref_<type>': [values]}: external ids as node properties."""
    out = {}
    for entity_type, entity_id, xref_type, values in conn.execute(text(
        "SELECT entity_type, entity_id, xref_type, array_agg(DISTINCT xref_value ORDER BY xref_value) "
        "FROM core.xref GROUP BY 1, 2, 3"
    )):
        out.setdefault((entity_type, entity_id), {})[f"xref_{xref_type}"] = values
    return out


def _batches(rows, size=BATCH):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _write(session, cypher: str, rows: list) -> int:
    created = 0
    for chunk in _batches(rows):
        created += session.run(cypher, rows=chunk).single()[0]
    return created


def reset(session):
    """Empty the graph and (re)create one uniqueness constraint per label."""
    session.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
    for label, key in LABEL_KEY.items():
        session.run(f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE").consume()
    session.run("CALL db.awaitIndexes(300)").consume()


def export_nodes(conn, session, mismatches):
    sources = dict(conn.execute(text("SELECT source_id, source_code FROM core.source")).fetchall())
    xrefs = _xref_properties(conn)
    for label, table, key, name_expr in NODES:
        result = conn.execute(text(f"SELECT *, {name_expr} AS name FROM core.{table}")).mappings()
        rows = []
        for r in result:
            props = _props(dict(r))
            if r.get("source_id") is not None:
                props["source"] = sources[r["source_id"]]
            props.update(xrefs.get((table, r[key]), {}) if isinstance(r[key], int) else {})
            rows.append(props)
        created = _write(session, f"UNWIND $rows AS p CREATE (n:{label}) SET n = p RETURN count(n)", rows)
        print(f"  {label:<17} {created:>7}")
        if created != len(rows):
            mismatches.append(f"{label}: {created} nodes for {len(rows)} core.{table} rows")


def export_edges(conn, session, mismatches):
    for src, rel, dst, sql in EDGES:
        rows = []
        for r in conn.execute(text(sql)).mappings():
            r = dict(r)
            rows.append({"s": r.pop("s"), "d": r.pop("d"), "p": _props(r)})
        cypher = (f"UNWIND $rows AS r MATCH (a:{src} {{{LABEL_KEY[src]}: r.s}}) "
                  f"MATCH (b:{dst} {{{LABEL_KEY[dst]}: r.d}}) "
                  f"CREATE (a)-[x:{rel}]->(b) SET x = r.p RETURN count(x)")
        created = _write(session, cypher, rows)
        print(f"  ({src})-[:{rel}]->({dst}) {created}")
        if created != len(rows):
            mismatches.append(f"{src}-{rel}->{dst}: {created} of {len(rows)} rows")


def export_evidence(conn, session, mismatches):
    groups = {}
    for r in conn.execute(text(EVIDENCE_SQL)).mappings():
        r = dict(r)
        src, dst = EVIDENCE_LABELS.get(r["evidence_type"]), EVIDENCE_LABELS.get(r["entity_type"])
        if src is None or dst is None:
            mismatches.append(f"evidence_link {r['evidence_link_id']}: no label for "
                              f"{r['evidence_type']} -> {r['entity_type']}")
            continue
        # entity_id is text in Postgres (it is polymorphic); match the target's key type.
        entity_id = r["entity_id"] if dst == "Study" else int(r["entity_id"])
        props = _props({k: r[k] for k in ("evidence_link_id", "confidence", "source", "matched_terms",
                                          "match_fields", "match_methods", "support_count")})
        groups.setdefault((src, r["relation"].upper(), dst), []).append(
            {"s": r["evidence_id"], "d": entity_id, "p": props})

    for (src, rel, dst), rows in sorted(groups.items()):
        cypher = (f"UNWIND $rows AS r MATCH (a:{src} {{{LABEL_KEY[src]}: r.s}}) "
                  f"MATCH (b:{dst} {{{LABEL_KEY[dst]}: r.d}}) "
                  f"CREATE (a)-[x:{rel}]->(b) SET x = r.p RETURN count(x)")
        created = _write(session, cypher, rows)
        print(f"  ({src})-[:{rel}]->({dst}) {created}  [evidence]")
        if created != len(rows):
            mismatches.append(f"evidence {src}-{rel}->{dst}: {created} of {len(rows)} links")


def export_document_dates(conn, session):
    """
    One comparable `date` per Grant, Patent and Publication (core.v_document_date:
    when it became public), with `date_precision` and `date_basis` saying how
    exact it is and where it came from.
    """
    labels = {"grant_award": "Grant", "patent": "Patent", "publication": "Publication"}
    rows = {}
    for doc_type, doc_id, date, precision, basis in conn.execute(text(
            "SELECT document_type, document_id, date, date_precision, date_basis FROM core.v_document_date")):
        rows.setdefault(doc_type, []).append({"id": doc_id, "date": date, "precision": precision, "basis": basis})
    for doc_type, label in labels.items():
        key = LABEL_KEY[label]
        set_ = _write(session, f"UNWIND $rows AS r MATCH (n:{label} {{{key}: r.id}}) "
                               "SET n.date = r.date, n.date_precision = r.precision, n.date_basis = r.basis "
                               "RETURN count(n)", rows.get(doc_type, []))
        print(f"  {label}.date set on {set_} nodes")


def main():
    started = time.time()
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    driver.verify_connectivity()
    mismatches = []
    with driver.session() as session, engine.connect() as conn:
        print(f"Rebuilding Neo4j at {NEO4J_URI} from core.*")
        reset(session)
        print("nodes:")
        export_nodes(conn, session, mismatches)
        print("relationships:")
        export_edges(conn, session, mismatches)
        export_evidence(conn, session, mismatches)
        export_document_dates(conn, session)
        nodes = session.run("MATCH (n) RETURN count(n)").single()[0]
        rels = session.run("MATCH ()-[r]->() RETURN count(r)").single()[0]
    driver.close()

    print(f"Neo4j: {nodes} nodes, {rels} relationships ({time.time() - started:.0f}s)")
    if mismatches:
        print("FAILED -- rows that did not make it into the graph:")
        for m in mismatches:
            print(f"  ! {m}")
        sys.exit(1)
    print("Every node and relationship matches its Postgres row count.")


if __name__ == "__main__":
    main()
