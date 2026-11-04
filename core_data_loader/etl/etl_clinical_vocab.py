"""
Seeds core.symptom / core.assay_type from common/clinical_vocab.py and links
them to the rows whose text names them:

  isolate        -> symptom     raw.hfv_ebola_annotation_web.patient_symptoms
  experiment     -> assay_type  core.experiment.measurement_technique (ImmPort)
  epitope_assay  -> assay_type  core.epitope_assay.assay_type (IEDB category)
                                + core.epitope_assay.assay_method (LANL CTL)

Run after etl_bvbrc_hfv and etl_immport: they truncate core.isolate,
core.epitope_assay and core.experiment, and the CASCADE empties these link
tables. run_all.py encodes the order; sql/checks.sql flags an emptied table.

Also writes exports/kg_clinical_terms.csv in kg_keywords.csv's layout --
symptoms, assay types, and disease terms (CONDITION_TERMS) -- so grant/paper
keyword matching can use the same terms. entity_ids there are the
HPO / OBI ids (stable), not this DB's surrogate ids.
"""

import csv
import os

from sqlalchemy import text

from core_data_loader.common.accessions import base_accession
from core_data_loader.common.clinical_vocab import (
    ASSAY_TYPES, CONDITION_TERMS, IEDB_CATEGORY, SYMPTOMS,
    ensure_vocab, match_assay_name, parse_ctl_methods, parse_symptoms,
)
from core_data_loader.common.db_utils import engine
from core_data_loader.common.etl_common import raw_table_exists, read_raw

ANNOTATION_TABLE = "hfv_ebola_annotation_web"
EXPORT_PATH = "exports/kg_clinical_terms.csv"


def link_isolate_symptoms(conn, symptom_ids) -> None:
    conn.execute(text("DELETE FROM core.isolate_symptom"))
    if not raw_table_exists(ANNOTATION_TABLE):
        print(f"isolate -> symptom: raw.{ANNOTATION_TABLE} absent, skipped")
        return

    # accession (either form) -> isolate, through the sequence it belongs to.
    isolate_by_accession = dict(conn.execute(text(
        """
        SELECT x.xref_value, s.isolate_id
        FROM core.xref x JOIN core.sequence s ON s.sequence_id = x.entity_id
        WHERE x.entity_type = 'sequence' AND x.xref_type = 'genbank_accession'
          AND s.isolate_id IS NOT NULL
        """
    )).fetchall())

    raw = read_raw(ANNOTATION_TABLE).fillna("")
    pairs, unmatched, no_isolate, with_text = set(), {}, 0, 0
    for r in raw.itertuples(index=False):
        names, misses = parse_symptoms(r.patient_symptoms)
        for m in misses:
            unmatched[m] = unmatched.get(m, 0) + 1
        if not names and not misses:
            continue
        with_text += 1
        acc = (r.accession or "").strip()
        isolate_id = isolate_by_accession.get(acc) or isolate_by_accession.get(base_accession(acc))
        if isolate_id is None:
            no_isolate += 1
            continue
        pairs.update((isolate_id, symptom_ids[n]) for n in names)

    for isolate_id, symptom_id in sorted(pairs):
        conn.execute(
            text("INSERT INTO core.isolate_symptom (isolate_id, symptom_id) VALUES (:i, :s)"),
            {"i": isolate_id, "s": symptom_id},
        )
    print(f"core.isolate_symptom: {len(pairs)} rows from {with_text} annotated isolates"
          + (f" ({no_isolate} accessions not in core)" if no_isolate else ""))
    for fragment, n in sorted(unmatched.items()):
        print(f"  ! unmatched symptom text {fragment!r} x{n} -- add a synonym to clinical_vocab.py")


def link_experiment_assays(conn, assay_ids) -> None:
    conn.execute(text("DELETE FROM core.experiment_assay_type"))
    rows = conn.execute(text(
        "SELECT experiment_accession, measurement_technique FROM core.experiment"
    )).fetchall()
    linked = 0
    for accession, technique in rows:
        name = match_assay_name(technique)
        if name is None:
            if technique:
                print(f"  ! unmatched measurement_technique {technique!r} ({accession})")
            continue
        conn.execute(
            text("INSERT INTO core.experiment_assay_type VALUES (:e, :a) ON CONFLICT DO NOTHING"),
            {"e": accession, "a": assay_ids[name]},
        )
        linked += 1
    print(f"core.experiment_assay_type: {linked} of {len(rows)} experiments linked")


def link_epitope_assays(conn, assay_ids) -> None:
    conn.execute(text("DELETE FROM core.epitope_assay_assay_type"))
    rows = conn.execute(text(
        """
        SELECT epitope_assay_id, assay_type, assay_method FROM core.epitope_assay
        """
    )).fetchall()
    pairs, unparsed = set(), {}
    for ea_id, category, method_text in rows:
        if category in IEDB_CATEGORY:
            pairs.add((ea_id, assay_ids[IEDB_CATEGORY[category]]))
        methods = parse_ctl_methods(method_text)
        pairs.update((ea_id, assay_ids[m]) for m in methods)
        if method_text and not methods:
            unparsed[method_text] = unparsed.get(method_text, 0) + 1

    if pairs:  # an empty executemany raises rather than inserting nothing
        conn.execute(
            text("INSERT INTO core.epitope_assay_assay_type VALUES (:e, :a)"),
            [{"e": e, "a": a} for e, a in sorted(pairs)],
        )
    methods = sum(1 for _e, a in pairs if a not in {assay_ids[v] for v in IEDB_CATEGORY.values()})
    print(f"core.epitope_assay_assay_type: {len(pairs)} rows "
          f"({len(pairs) - methods} category, {methods} method) over {len(rows)} assays")
    for text_value, n in sorted(unparsed.items()):
        # Not an assay method: a protocol step or a result description.
        print(f"  (CTL assay text names no known method: {text_value!r} x{n})")


def write_export(path=EXPORT_PATH) -> int:
    """kg_keywords.csv's columns, one row per search term."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = []
    for entity_type, vocab, id_label in (("symptom", SYMPTOMS, "HPO"), ("assay_type", ASSAY_TYPES, "OBI")):
        for name, ext_id, parent, synonyms in vocab:
            terms = dict.fromkeys([name.lower(), *synonyms])  # ordered, deduped
            for term in terms:
                short = len(term) <= 4
                rows.append({
                    "entity_type": entity_type,
                    "canonical_name": name,
                    "search_term": term,
                    "term_type": "db_name" if term == name.lower() else "synonym",
                    "scope": "include",
                    # 'ics', 'pcr', 'icc' collide with unrelated text
                    "requires_context": "TRUE" if short else "FALSE",
                    "entity_ids": ext_id or "",
                    "source": f"core.{entity_type} (common/clinical_vocab.py)",
                    "notes": (f"no {id_label} term at this granularity" if not ext_id else "")
                             + (f"; kind of {parent}" if parent else ""),
                })
    for name, entity_ids, note, terms in CONDITION_TERMS:
        for term, scope, requires_context in terms:
            rows.append({
                "entity_type": "condition",
                "canonical_name": name,
                "search_term": term,
                "term_type": "db_name" if term == name.lower() else "synonym",
                "scope": scope,
                "requires_context": "TRUE" if requires_context else "FALSE",
                "entity_ids": entity_ids,
                "source": "Disease Ontology / MONDO (common/clinical_vocab.py)",
                "notes": note,
            })
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main():
    with engine.begin() as conn:
        symptom_ids, assay_ids = ensure_vocab(conn)
        print(f"core.symptom: {len(symptom_ids)} terms, core.assay_type: {len(assay_ids)} terms")
        link_isolate_symptoms(conn, symptom_ids)
        link_experiment_assays(conn, assay_ids)
        link_epitope_assays(conn, assay_ids)
    print(f"{EXPORT_PATH}: {write_export()} search terms")


if __name__ == "__main__":
    main()
