#!/usr/bin/env python3
"""Turn the RePORTER pull into staging CSVs for schema/load_grants.sql.

Reads output/grants/reporter_records.jsonl (written by fetch_nih_grants.py) and
re-runs the same keyword matching per record and per field, so every link
records which fiscal year matched and whether it was the title or the abstract.

Outputs, written to --out-dir (default output/grants/staging):

  stg_grant_award.csv     one row per grant (award_key)
  stg_grant_funding.csv   one row per RePORTER record (appl_id)
  stg_grant_evidence.csv  one row per (record, field, term, entity) match

Then load them with:

  psql -d <db> -v staging_dir=output/grants/staging -f schema/load_grants.sql

Usage:

  python build_grants_staging.py
  python build_grants_staging.py --records output/test/reporter_records.jsonl --out-dir output/test/staging
"""

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from fetch_nih_grants import (
    CONTEXT_RE, HERE, build_term_groups, load_conditions, load_kg_terms, log,
)

# Standard NIH core numbers: activity code + IC + 6-digit serial, e.g. R01AI123456, ZIAAI005079.
STANDARD_CORE_RE = re.compile(r"^[A-Z0-9]{3}[A-Z]{2}\d{6}$")
NO_TASK_ORDER = {"", "0", "9999"}

AWARD_COLUMNS = [
    "award_key", "core_project_num", "project_title", "activity_code", "admin_ic_code",
    "admin_ic_name", "org_name", "org_city", "org_state", "org_country", "contact_pi_name",
    "project_start_date", "project_end_date", "relevance_tier",
]
FUNDING_COLUMNS = [
    "appl_id", "award_key", "fiscal_year", "project_num", "subproject_id", "project_title",
    "abstract_text", "award_amount", "award_notice_date", "project_detail_url",
]
EVIDENCE_COLUMNS = [
    "appl_id", "award_key", "entity_type", "entity_id", "matched_term", "term_type",
    "match_field", "match_method", "requires_context",
]


def clean_text(value):
    """Strip and collapse runs of spaces/tabs; keep line breaks in abstracts."""
    if not value:
        return ""
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in str(value).splitlines())
    return "\n".join(line for line in lines if line)


def clean_title(value):
    return re.sub(r"\s+", " ", value or "").strip()


def date_only(value):
    return (value or "")[:10]


def award_key(record):
    """Grouping key for grant_award.

    RePORTER truncates contract core numbers (e.g. '26120150000'), merging different contracts
    and task orders. For those, rebuild the key from project_num: contract number plus task order,
    e.g. '261201500003I-0-26100013-1' -> '261201500003I-26100013'.
    """
    core = (record.get("core_project_num") or "").strip()
    if STANDARD_CORE_RE.match(core):
        return core
    parts = (record.get("project_num") or core or str(record["appl_id"])).split("-")
    task = parts[2] if len(parts) > 2 else ""
    return parts[0] if task in NO_TASK_ORDER else f"{parts[0]}-{task}"


def relevance_tier(records):
    """strong: filovirus word in a title; moderate: 2+ mentions in one abstract; weak: otherwise."""
    if any(CONTEXT_RE.search(r.get("project_title") or "") for r in records):
        return "strong"
    mentions = max(len(CONTEXT_RE.findall(r.get("abstract_text") or "")) for r in records)
    return "moderate" if mentions >= 2 else "weak"


def load_term_types(path):
    """search_term (lowercased) -> set of term_type, for the evidence rows."""
    term_types = defaultdict(set)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            term_types[row["search_term"].strip().lower()].add(row["term_type"])
    return term_types


def match_record(record, groups, term_types, unresolved):
    title = record.get("project_title") or ""
    abstract = record.get("abstract_text") or ""
    has_context = bool(CONTEXT_RE.search(f"{title}\n{abstract}"))
    for group in groups:
        fields = [name for name, text in (("title", title), ("abstract", abstract))
                  if group["pattern"].search(text)]
        if not fields:
            continue
        for target in group["targets"]:
            if target["needs_context"] and not has_context:
                continue
            if not target["entity_id"]:
                unresolved[f"{target['entity_type']}: {target['keyword']}"] += 1
                continue
            for entity_id in target["entity_id"].split("|"):
                for field in fields:
                    yield {
                        "appl_id": record["appl_id"],
                        "entity_type": target["entity_type"],
                        "entity_id": entity_id.strip(),
                        "matched_term": target["keyword"],
                        "term_type": ";".join(sorted(term_types.get(target["keyword"].lower(), ()))),
                        "match_field": field,
                        "match_method": target["method"],
                        "requires_context": str(target["needs_context"]).lower(),
                    }


def build_awards(by_key):
    awards = []
    for key, recs in sorted(by_key.items()):
        parents = [r for r in recs if not r.get("subproject_id")] or recs
        earliest = min(parents, key=lambda r: (r.get("fiscal_year") or 0, r["appl_id"]))
        latest = max(parents, key=lambda r: (r.get("fiscal_year") or 0, r["appl_id"]))
        org = latest.get("organization") or {}
        ic = latest.get("agency_ic_admin") or {}
        starts = sorted(date_only(r.get("project_start_date")) for r in recs if r.get("project_start_date"))
        ends = sorted(date_only(r.get("project_end_date")) for r in recs if r.get("project_end_date"))
        awards.append({
            "award_key": key,
            "core_project_num": latest.get("core_project_num") or key,
            "project_title": clean_title(earliest.get("project_title")),
            "activity_code": latest.get("activity_code") or "",
            "admin_ic_code": ic.get("code") or "",
            "admin_ic_name": ic.get("abbreviation") or "",
            "org_name": org.get("org_name") or "",
            "org_city": org.get("org_city") or org.get("city") or "",
            "org_state": org.get("org_state") or "",
            "org_country": org.get("org_country") or org.get("country") or "",
            "contact_pi_name": clean_title(latest.get("contact_pi_name")),
            "project_start_date": starts[0] if starts else "",
            "project_end_date": ends[-1] if ends else "",
            "relevance_tier": relevance_tier(recs),
        })
    return awards


def build_funding(records, keys):
    return [{
        "appl_id": r["appl_id"],
        "award_key": keys[r["appl_id"]],
        "fiscal_year": r.get("fiscal_year") or "",
        "project_num": r.get("project_num") or "",
        "subproject_id": r.get("subproject_id") or "",
        "project_title": clean_title(r.get("project_title")),
        "abstract_text": clean_text(r.get("abstract_text")),
        "award_amount": "" if r.get("award_amount") is None else r["award_amount"],
        "award_notice_date": date_only(r.get("award_notice_date")),
        "project_detail_url": r.get("project_detail_url") or "",
    } for r in sorted(records, key=lambda r: r["appl_id"])]


def write_csv(path, rows, columns):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def parse_args():
    parser = argparse.ArgumentParser(description="Build staging CSVs for the grant tables.")
    parser.add_argument("--records", type=Path, default=HERE / "output" / "grants" / "reporter_records.jsonl")
    parser.add_argument("--keywords", type=Path, default=HERE / "Context" / "Context" / "kg_keywords.csv")
    parser.add_argument("--conditions", type=Path, default=HERE / "inputs" / "conditions.csv")
    parser.add_argument("--out-dir", type=Path, default=HERE / "output" / "grants" / "staging")
    parser.add_argument("--scopes", nargs="+", default=["include"], choices=["include", "review", "exclude"],
                        help="kg_keywords.csv scopes to match; use the same scopes as the fetch (default: include)")
    # Protein labels such as 'VP35' map to thousands of isolate-level protein records, so a
    # grant that names a protein is linked to its gene node instead (the gene terms cover the
    # same nine filovirus genes). Add 'protein' to link every protein record as well.
    parser.add_argument("--link-types", nargs="+", default=["taxon", "gene", "condition"],
                        choices=["taxon", "gene", "protein", "condition"],
                        help="entity types to link grants to (default: taxon gene condition)")
    return parser.parse_args()


def main():
    args = parse_args()
    with open(args.records, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]

    keys = {r["appl_id"]: award_key(r) for r in records}
    by_key = defaultdict(list)
    for r in records:
        by_key[keys[r["appl_id"]]].append(r)

    targets = load_conditions(args.conditions) + load_kg_terms(args.keywords, args.scopes)
    groups, _ = build_term_groups([t for t in targets if t["entity_type"] in args.link_types])
    term_types = load_term_types(args.keywords)
    unresolved = Counter()
    evidence, seen = [], set()
    for r in records:
        for row in match_record(r, groups, term_types, unresolved):
            dedupe = (row["appl_id"], row["entity_type"], row["entity_id"],
                      row["matched_term"].lower(), row["match_field"])
            if dedupe in seen:  # 'VP40' and 'vp40' are the same match
                continue
            seen.add(dedupe)
            evidence.append({**row, "award_key": keys[r["appl_id"]]})

    awards = build_awards(by_key)
    funding = build_funding(records, keys)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "stg_grant_award.csv", awards, AWARD_COLUMNS)
    write_csv(args.out_dir / "stg_grant_funding.csv", funding, FUNDING_COLUMNS)
    write_csv(args.out_dir / "stg_grant_evidence.csv", evidence, EVIDENCE_COLUMNS)

    pairs = {(e["award_key"], e["entity_type"], e["entity_id"]) for e in evidence}
    matched_awards = {e["award_key"] for e in evidence}
    split_cores = sum(1 for core in {r.get("core_project_num") for r in records}
                      if len({keys[r["appl_id"]] for r in records if r.get("core_project_num") == core}) > 1)
    log(f"{len(awards)} grants ({split_cores} truncated contract cores split into separate grants)")
    log(f"{len(funding)} funding records")
    log(f"{len(evidence)} evidence rows -> {len(pairs)} grant-entity links "
        f"({', '.join(f'{t} {n}' for t, n in sorted(Counter(p[1] for p in pairs).items()))})")
    log(f"Relevance tiers: {dict(Counter(a['relevance_tier'] for a in awards))}")
    if len(matched_awards) < len(awards):
        log(f"{len(awards) - len(matched_awards)} grants have no link with a KG entity ID")
    if unresolved:
        log(f"Matches skipped because the term has no KG entity ID yet: {sum(unresolved.values())} "
            f"({', '.join(sorted(unresolved))})")
    log(f"Staging CSVs written to {args.out_dir}")


if __name__ == "__main__":
    main()
