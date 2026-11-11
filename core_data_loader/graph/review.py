"""
Human review of keyword-based evidence links, to calibrate their confidence.

The query page's "Review links" tab shows one link at a time -- the grant or
patent, the entity, and the text that produced the link -- and records accept
or reject in ops.link_review (keyed by what the link connects, so a review
survives rebuilds; see common/link_keys.py). `filovirus review-stats` then
reports precision per confidence level: what a 0.25 link is actually worth.

Only links from a grant or patent to a keyword-matched entity are offered:
exact-identifier links (confidence 1.0) need no review.
"""

import re

from sqlalchemy import text

from core_data_loader.common.db_utils import engine
from core_data_loader.common.link_keys import REVIEWABLE_DOCUMENTS, REVIEWABLE_ENTITIES, link_key, natural_ids

ENTITY_NAME_SQL = {
    "taxon": "SELECT taxon_name FROM core.taxon WHERE taxon_id::text = :id",
    "gene": "SELECT gene_symbol FROM core.gene WHERE gene_id::text = :id",
    "antibody": "SELECT name FROM core.antibody WHERE antibody_id::text = :id",
    "condition": "SELECT coalesce(condition_preferred, condition_reported) FROM core.condition "
                 "WHERE condition_id::text = :id",
    "symptom": "SELECT name FROM core.symptom WHERE symptom_id::text = :id",
    "assay_type": "SELECT name FROM core.assay_type WHERE assay_type_id::text = :id",
    "countermeasure": "SELECT name FROM core.countermeasure WHERE countermeasure_id::text = :id",
}
SNIPPET_CHARS = 160


def _snippet(body: str, term: str) -> str:
    m = re.search(rf"(?<![0-9A-Za-z]){re.escape(term)}(?![0-9A-Za-z])", body or "", re.IGNORECASE)
    if not m:
        return ""
    start, end = max(0, m.start() - SNIPPET_CHARS), min(len(body), m.end() + SNIPPET_CHARS)
    return ("…" if start else "") + body[start:m.start()] + "[[" + m.group(0) + "]]" + body[m.end():end] + \
        ("…" if end < len(body) else "")


def next_link(tier: float | None = None) -> dict | None:
    """A random unreviewed link (optionally at one confidence level), with its evidence."""
    with engine.connect() as conn:
        ids = natural_ids(conn)
        reviewed = {r[0] for r in conn.execute(text("SELECT link_key FROM ops.link_review"))}
        rows = conn.execute(text(
            "SELECT evidence_link_id, evidence_type, evidence_id, entity_type, entity_id, relation, confidence "
            "FROM core.evidence_link WHERE evidence_type = ANY(:docs) AND entity_type = ANY(:ents) "
            "AND confidence < 1 AND (CAST(:tier AS numeric) IS NULL OR confidence = :tier) ORDER BY random()"),
            {"docs": list(REVIEWABLE_DOCUMENTS), "ents": list(REVIEWABLE_ENTITIES), "tier": tier}).fetchall()
        for link_id, et, ei, nt, ni, rel, conf in rows:
            key = link_key(ids, et, ei, nt, ni, rel)
            if key and key not in reviewed:
                return _describe(conn, link_id, et, ei, nt, ni, rel, conf, key)
    return None


def _describe(conn, link_id, et, ei, nt, ni, rel, conf, key) -> dict:
    if et == "grant_award":
        doc = conn.execute(text("SELECT project_title, org_name FROM core.grant_award WHERE grant_award_id = :i"),
                           {"i": ei}).fetchone()
        doc_title, doc_sub = doc[0], doc[1]
    else:
        doc = conn.execute(text("SELECT title, document_id FROM core.patent WHERE patent_id = :i"), {"i": ei}).fetchone()
        doc_title, doc_sub = doc[0], doc[1]
    entity = conn.execute(text(ENTITY_NAME_SQL[nt]), {"id": str(ni)}).scalar()

    evidence = []
    for term, field, method, funding_id in conn.execute(text(
            "SELECT matched_term, match_field, match_method, grant_funding_id FROM core.evidence_link_support "
            "WHERE evidence_link_id = :i ORDER BY match_field"), {"i": link_id}):
        body = ""
        if funding_id is not None:
            body = conn.execute(text(
                f"SELECT {'project_title' if field == 'title' else 'abstract_text'} FROM core.grant_funding "
                "WHERE grant_funding_id = :f"), {"f": funding_id}).scalar() or ""
        elif et == "patent":
            body = conn.execute(text(
                f"SELECT {'title' if field == 'title' else 'abstract'} FROM core.patent WHERE patent_id = :i"),
                {"i": ei}).scalar() or ""
        item = {"term": term, "field": field, "method": method, "snippet": _snippet(body, term or "")}
        if item in evidence:        # the same text in another fiscal year's record
            continue
        evidence.append(item)
        if len(evidence) >= 4:
            break
    return {
        "link_key": key, "confidence": float(conf), "relation": rel,
        "document": {"type": et, "title": doc_title, "detail": doc_sub},
        "entity": {"type": nt, "name": entity},
        "evidence": evidence,
    }


def record(link_key_: str, decision: str, confidence: float, note: str | None = None) -> None:
    if decision not in ("accepted", "rejected"):
        raise ValueError("decision must be 'accepted' or 'rejected'")
    with engine.begin() as conn:
        conn.execute(text(
            """
            INSERT INTO ops.link_review (link_key, decision, confidence_at_review, note)
            VALUES (:k, :d, :c, :n)
            ON CONFLICT (link_key) DO UPDATE
                SET decision = EXCLUDED.decision, confidence_at_review = EXCLUDED.confidence_at_review,
                    note = EXCLUDED.note, reviewed_at = now()
            """), {"k": link_key_, "d": decision, "c": confidence, "n": note})


def stats() -> list[dict]:
    """Per confidence level: links available, reviewed, accepted, precision."""
    with engine.connect() as conn:
        available = dict(conn.execute(text(
            "SELECT confidence, count(*) FROM core.evidence_link WHERE evidence_type = ANY(:docs) "
            "AND entity_type = ANY(:ents) AND confidence < 1 GROUP BY 1"),
            {"docs": list(REVIEWABLE_DOCUMENTS), "ents": list(REVIEWABLE_ENTITIES)}).fetchall())
        reviewed = conn.execute(text(
            "SELECT confidence_at_review, count(*), count(*) FILTER (WHERE decision = 'accepted') "
            "FROM ops.link_review GROUP BY 1")).fetchall()
    done = {float(c): (n, a) for c, n, a in reviewed}
    return [{"confidence": float(c), "links": n, "reviewed": done.get(float(c), (0, 0))[0],
             "accepted": done.get(float(c), (0, 0))[1],
             "precision": (done[float(c)][1] / done[float(c)][0]) if done.get(float(c), (0, 0))[0] else None}
            for c, n in sorted(available.items(), key=lambda kv: -kv[0])]


def print_stats() -> int:
    rows = stats()
    print(f"{'confidence':>10}  {'links':>6}  {'reviewed':>8}  {'accepted':>8}  {'precision':>9}")
    for r in rows:
        p = f"{r['precision']:.0%}" if r["precision"] is not None else "—"
        print(f"{r['confidence']:>10.2f}  {r['links']:>6,}  {r['reviewed']:>8}  {r['accepted']:>8}  {p:>9}")
    print("\nReview links on the query page (filovirus serve -> Review links). ~30 per level gives a usable "
          "precision estimate.")
    return 0
