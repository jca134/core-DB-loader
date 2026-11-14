#!/usr/bin/env python3
"""Pull NIH grants related to the filovirus KG from the NIH RePORTER API.

Anchors searched:

  study         exact award numbers of the ImmPort studies (--studies CSV)
                -> grant --funds--> study
  condition     disease names (--conditions CSV)
                -> grant --related--> condition
  taxon         include-scope terms from kg_keywords.csv
  gene/protein  include-scope terms from kg_keywords.csv, searched only
                together with a filovirus word because they are ambiguous
                -> grant --related--> taxon / gene / protein

Keyword searches run against project titles and abstracts. Every API hit is
re-checked locally: the term (and, where required, a filovirus word) must
appear as a whole word in the title or abstract. Grants that fail the check
are dropped and counted as rejected in the query log.

Outputs, written to --out-dir:

  grant_evidence.csv      shared evidence format, one row per grant/entity/keyword link
  grants.csv              one row per grant (core project number), for review
  query_log.csv           every search: query text, API hits, grants kept and rejected
  reporter_records.jsonl  raw RePORTER records behind the evidence, for provenance
  summary.txt             human-readable run summary

Usage:

  python fetch_nih_grants.py
  python fetch_nih_grants.py --min-fiscal-year 2010 --out-dir output/test
  python fetch_nih_grants.py --dry-run
"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent

API_URL = "https://api.reporter.nih.gov/v2/projects/search"
PAGE_SIZE = 500
MAX_RECORDS_PER_QUERY = 15_000  # the API refuses offsets past 14,999
REQUEST_DELAY = 1.0  # NIH asks for no more than one request per second
FIRST_FISCAL_YEAR = 1985
MIN_TERM_LENGTH = 2  # single letters such as "L" or "N" cannot be searched as text
# The "terms" field is NIH's thesaurus expansion of each abstract; searching it
# adds false hits (e.g. an Ebola-only grant tagged "Frankfurt-Marburg Syndrome Virus").
SEARCH_FIELDS = "projecttitle,abstracttext"

FIELDS = [
    "ApplId", "SubprojectId", "FiscalYear", "ProjectNum", "CoreProjectNum",
    "ProjectTitle", "AbstractText", "ActivityCode", "AgencyIcAdmin",
    "Organization", "ContactPiName", "PrincipalInvestigators", "AwardAmount",
    "ProjectStartDate", "ProjectEndDate", "AwardNoticeDate", "ProjectDetailUrl",
]

FILOVIRUS_ABBREVIATIONS = ["EBOV", "MARV", "SUDV", "BDBV", "RESTV", "TAFV", "BOMV", "RAVV"]
CONTEXT_QUERY = "(" + " OR ".join(["ebola*", "marburg*", "filovir*"] + FILOVIRUS_ABBREVIATIONS) + ")"
CONTEXT_RE = re.compile(
    r"ebola|marburg|filovir|(?<![A-Za-z0-9])(?:" + "|".join(FILOVIRUS_ABBREVIATIONS) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)

EVIDENCE_COLUMNS = [
    "evidence_id", "source_type", "title", "date", "url", "matched_entity_type",
    "matched_entity_id", "matched_keyword", "match_method", "relation",
]

METHOD_BY_TERM_TYPE = {
    "db_name": "keyword",
    "db_product": "keyword",
    "db_alias": "synonym",
    "common_or_legacy_name": "synonym",
    "abbreviation": "synonym",
    "db_gene_label": "gene_label",  # associated gene label, not a verified protein synonym
}
METHOD_PRIORITY = ["exact_id", "keyword", "synonym", "gene_label"]


def log(message=""):
    print(message, flush=True)


def is_true(value):
    return str(value).strip().lower() in ("true", "1", "yes", "y")


# ---------------------------------------------------------------- inputs

def load_kg_terms(path, scopes):
    """Return search targets for taxon, gene and protein rows of kg_keywords.csv."""
    keywords = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    grant_terms = keywords[
        keywords["entity_type"].isin(["taxon", "gene", "protein"])
    ]
    grant_terms = grant_terms[grant_terms["scope"].isin(scopes)]

    targets = []
    for row in grant_terms.itertuples(index=False):
        targets.append({
            "entity_type": row.entity_type,
            "entity_id": row.entity_ids,
            "canonical_name": row.canonical_name,
            "keyword": row.search_term.strip(),
            "method": METHOD_BY_TERM_TYPE.get(row.term_type, "keyword"),
            "relation": "related",
            "needs_context": is_true(row.requires_context),
        })
    return targets


def load_conditions(path):
    """Return search targets from the conditions CSV (condition_name, search_term, entity_id, requires_context)."""
    if not path.exists():
        log(f"No conditions file at {path}; skipping condition anchors.")
        return []
    conditions = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    return [
        {
            "entity_type": "condition",
            "entity_id": row.entity_id.strip(),
            "canonical_name": row.condition_name.strip(),
            "keyword": row.search_term.strip(),
            "method": "keyword",
            "relation": "related",
            "needs_context": is_true(row.requires_context),
        }
        for row in conditions.itertuples(index=False)
        if row.search_term.strip()
    ]


def core_award_number(award):
    """'1 U19 AI109762-01' -> 'U19AI109762'. Anything unrecognised is passed through cleaned."""
    cleaned = re.sub(r"\s+", "", award.upper())
    match = re.match(r"^\d?([A-Z][A-Z0-9]{2}[A-Z]{2}\d{6})", cleaned)
    return match.group(1) if match else cleaned


def load_study_groups(path):
    """One exact-ID search per award number in the studies CSV (study_id, award_number)."""
    if not path.exists():
        log(f"No studies file at {path}; skipping study anchors.")
        return []
    studies = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    groups = {}
    for row in studies.itertuples(index=False):
        award = row.award_number.strip()
        if not (award and row.study_id.strip()):
            continue
        core = core_award_number(award)
        group = groups.setdefault(core, {
            "label": core,
            "anchor": "study",
            "criteria": {"project_nums": [core]},
            "pattern": None,
            "targets": [],
        })
        group["targets"].append({
            "entity_type": "study",
            "entity_id": row.study_id.strip(),
            "canonical_name": row.study_id.strip(),
            "keyword": award,
            "method": "exact_id",
            "relation": "funds",
            "needs_context": False,
        })
    if not groups:
        log(f"{path} lists no award numbers; skipping study anchors.")
    return list(groups.values())


def term_pattern(term):
    """Whole-word, case-insensitive match; spaces in the term also match hyphens."""
    body = r"[\s\-]+".join(re.escape(word) for word in term.split())
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def build_term_groups(targets):
    """Deduplicate search terms (case-insensitively) while keeping every target they point to."""
    by_term = defaultdict(list)
    for target in targets:
        by_term[target["keyword"].lower()].append(target)

    groups, skipped = [], []
    for terms in by_term.values():
        term = terms[0]["keyword"]
        if len(term) < MIN_TERM_LENGTH:
            skipped.append(term)
            continue
        # Query with filovirus context only when every target needs it; mixed
        # groups query the bare term and enforce context per target below.
        query = '"' + term.replace('"', "") + '"'
        if all(t["needs_context"] for t in terms):
            query += " AND " + CONTEXT_QUERY
        groups.append({
            "label": term,
            "anchor": "/".join(sorted({t["entity_type"] for t in terms})),
            "criteria": {"advanced_text_search": {
                "operator": "advanced",
                "search_field": SEARCH_FIELDS,
                "search_text": query,
            }},
            "pattern": term_pattern(term),
            "targets": terms,
        })
    return groups, skipped


# ---------------------------------------------------------------- RePORTER API

_last_request = 0.0


def post(body):
    global _last_request
    data = json.dumps(body).encode("utf-8")
    error = None
    for attempt in range(5):
        wait = REQUEST_DELAY - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        request = urllib.request.Request(
            API_URL, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"RePORTER returned HTTP {e.code}: {e.read()[:300]!r}") from e
            error = e
        except (urllib.error.URLError, TimeoutError) as e:
            error = e
        backoff = 5 * 2 ** attempt
        log(f"    request failed ({error}); retrying in {backoff}s")
        time.sleep(backoff)
    raise RuntimeError(f"RePORTER request failed after retries: {error}")


def fetch_page(criteria, offset):
    return post({
        "criteria": criteria,
        "include_fields": FIELDS,
        "offset": offset,
        "limit": PAGE_SIZE,
        "sort_field": "appl_id",
        "sort_order": "asc",
    })


def search(criteria, fiscal_years):
    """Return (api_total, records) for criteria, splitting by fiscal year past the offset cap."""
    query = dict(criteria)
    if fiscal_years:
        query["fiscal_years"] = fiscal_years
    page = fetch_page(query, 0)
    total = page["meta"]["total"]

    if total > MAX_RECORDS_PER_QUERY:
        years = fiscal_years or list(range(FIRST_FISCAL_YEAR, dt.date.today().year + 2))
        if len(years) > 1:
            log(f"    {total} records exceed the API cap; splitting by fiscal year")
            records = []
            for year in years:
                records += search(criteria, [year])[1]
            return total, records
        log(f"    WARNING: {total} records in FY{years[0]}; only the first {MAX_RECORDS_PER_QUERY} are retrievable")

    records = list(page["results"])
    for offset in range(PAGE_SIZE, min(total, MAX_RECORDS_PER_QUERY), PAGE_SIZE):
        records += fetch_page(query, offset)["results"]
    return total, records


# ---------------------------------------------------------------- matching

def core_of(record):
    return record.get("core_project_num") or record.get("project_num") or str(record.get("appl_id"))


def record_key(record):
    return (record.get("appl_id"), record.get("subproject_id"))


def target_matches(group, target, text):
    if group["pattern"] is not None and not group["pattern"].search(text):
        return False
    return not target["needs_context"] or bool(CONTEXT_RE.search(text))


def run_group(group, fiscal_years, links, kept_records):
    total, records = search(group["criteria"], fiscal_years)
    all_cores, kept_cores = set(), set()
    for record in records:
        core = core_of(record)
        all_cores.add(core)
        text = f"{record.get('project_title') or ''}\n{record.get('abstract_text') or ''}"
        matched = [t for t in group["targets"] if target_matches(group, t, text)]
        if not matched:
            continue
        kept_cores.add(core)
        kept_records.setdefault(record_key(record), record)
        for target in matched:
            key = (core, target["entity_type"], target["entity_id"], target["keyword"], target["relation"])
            current = links.get(key)
            if current is None or METHOD_PRIORITY.index(target["method"]) < METHOD_PRIORITY.index(current["method"]):
                links[key] = {"core": core, **target}
    return {
        "anchor": group["anchor"],
        "search_term": group["label"],
        "query": json.dumps(group["criteria"]),
        "api_total": total,
        "records_retrieved": len(records),
        "grants_found": len(all_cores),
        "grants_kept": len(kept_cores),
        "grants_rejected": len(all_cores - kept_cores),
        "status": "ok",
    }


# ---------------------------------------------------------------- outputs

def summarize_grants(records, links):
    by_core = defaultdict(list)
    for record in records:
        by_core[core_of(record)].append(record)

    links_by_core = defaultdict(list)
    for link in links:
        links_by_core[link["core"]].append(link)

    grants = {}
    for core, recs in by_core.items():
        # Subproject records of P01/U19 centers carry their own titles and partial
        # amounts; prefer the parent record where one was matched.
        parents = [r for r in recs if not r.get("subproject_id")] or recs
        latest = max(parents, key=lambda r: (r.get("fiscal_year") or 0, r.get("appl_id") or 0))
        starts = sorted(r["project_start_date"][:10] for r in recs if r.get("project_start_date"))
        ends = sorted(r["project_end_date"][:10] for r in recs if r.get("project_end_date"))
        notices = sorted(r["award_notice_date"][:10] for r in recs if r.get("award_notice_date"))
        years = sorted({r["fiscal_year"] for r in recs if r.get("fiscal_year")})
        core_links = links_by_core[core]
        grants[core] = {
            "core_project_num": core,
            "title": latest.get("project_title") or "",
            "date": (starts or notices or [str(years[0]) if years else ""])[0],
            "url": latest.get("project_detail_url") or f"https://reporter.nih.gov/project-details/{latest.get('appl_id')}",
            "activity_code": latest.get("activity_code") or "",
            "admin_ic": (latest.get("agency_ic_admin") or {}).get("abbreviation", ""),
            "contact_pi": latest.get("contact_pi_name") or "",
            "organization": (latest.get("organization") or {}).get("org_name", ""),
            "project_start": starts[0] if starts else "",
            "project_end": ends[-1] if ends else "",
            "fiscal_years_found": f"{years[0]}-{years[-1]}" if years else "",
            "records_found": len(recs),
            "award_amount_sum": sum(r.get("award_amount") or 0 for r in parents),
            "funds_studies": ";".join(sorted({l["entity_id"] for l in core_links if l["relation"] == "funds"})),
            "matched_entity_types": ";".join(sorted({l["entity_type"] for l in core_links})),
            "matched_keywords": ";".join(sorted({l["keyword"] for l in core_links}, key=lambda k: (k.lower(), k))),
            "abstract": latest.get("abstract_text") or "",
        }
    return grants


def evidence_rows(links, grants):
    rows = []
    for link in links:
        grant = grants[link["core"]]
        digest = hashlib.sha1("|".join([
            link["core"], link["entity_type"], link["entity_id"], link["keyword"], link["relation"],
        ]).encode("utf-8")).hexdigest()[:16]
        rows.append({
            "evidence_id": f"grant-{digest}",
            "source_type": "grant",
            "title": grant["title"],
            "date": grant["date"],
            "url": grant["url"],
            "matched_entity_type": link["entity_type"],
            "matched_entity_id": link["entity_id"],
            "matched_keyword": link["keyword"],
            "match_method": link["method"] + ("_with_context" if link["needs_context"] else ""),
            "relation": link["relation"],
        })
    rows.sort(key=lambda r: (r["url"], r["matched_entity_type"], r["matched_keyword"].lower(), r["evidence_id"]))
    return rows


def write_csv(path, rows, columns):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path, args, grants, evidence, query_log, skipped_terms, started):
    by_type = Counter(r["matched_entity_type"] for r in evidence)
    grants_by_type = defaultdict(set)
    for r in evidence:
        grants_by_type[r["matched_entity_type"]].add(r["url"])
    keyword_grants = defaultdict(set)
    for r in evidence:
        keyword_grants[r["matched_keyword"]].add(r["url"])
    unresolved = sorted({r["matched_keyword"] for r in evidence if not r["matched_entity_id"]})

    lines = [
        "NIH RePORTER grant pull",
        "=======================",
        f"Run started:      {started:%Y-%m-%d %H:%M:%S}",
        f"Run finished:     {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Keyword file:     {args.keywords}",
        f"Scopes searched:  {', '.join(args.scopes)}",
        f"Fiscal years:     {args.min_fiscal_year or FIRST_FISCAL_YEAR} onward",
        f"Search fields:    {SEARCH_FIELDS}",
        "",
        f"Queries run:      {len(query_log)}",
        f"Distinct grants:  {len(grants)}",
        f"Evidence links:   {len(evidence)}",
        "",
        "Links and grants by matched entity type:",
    ]
    for entity_type in ["study", "condition", "taxon", "gene", "protein"]:
        lines.append(f"  {entity_type:<10} {by_type.get(entity_type, 0):>6} links  {len(grants_by_type.get(entity_type, ())):>5} grants")
    lines += ["", "Grants per search term (after local verification):"]
    for entry in sorted(query_log, key=lambda e: -e["grants_kept"]):
        lines.append(
            f"  {entry['search_term'][:40]:<40} {entry['anchor']:<16} "
            f"kept {entry['grants_kept']:>5}  rejected {entry['grants_rejected']:>4}  (API hits {entry['api_total']})"
        )
    if skipped_terms:
        lines += ["", "Terms skipped as too short to search: " + ", ".join(sorted(skipped_terms))]
    if unresolved:
        lines += ["", "Matches with no KG entity ID yet (fill entity_id in the inputs file or resolve in ETL):",
                  "  " + ", ".join(unresolved)]
    if not any(e["anchor"] == "study" for e in query_log):
        lines += ["", "No study award numbers were searched; add them to the studies CSV for grant --funds--> study links."]
    lines += [
        "",
        "Notes:",
        "  Keyword matches are candidate 'related' links, not proof that a grant studies the entity.",
        "  match_method '*_with_context' means the term is ambiguous and a filovirus word was also required.",
        "  matched_entity_id keeps the '|'-separated node IDs from kg_keywords.csv; split them during integration.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- main

def parse_args():
    parser = argparse.ArgumentParser(description="Pull NIH RePORTER grants for filovirus KG anchors.")
    parser.add_argument("--keywords", type=Path, default=HERE / "Context" / "Context" / "kg_keywords.csv")
    parser.add_argument("--studies", type=Path, default=HERE / "inputs" / "immport_studies.csv")
    parser.add_argument("--conditions", type=Path, default=HERE / "inputs" / "conditions.csv")
    parser.add_argument("--out-dir", type=Path, default=HERE / "output" / "grants")
    parser.add_argument("--scopes", nargs="+", default=["include"], choices=["include", "review", "exclude"],
                        help="kg_keywords.csv scopes to search (default: include)")
    parser.add_argument("--min-fiscal-year", type=int, help="only grants funded in this fiscal year or later")
    parser.add_argument("--dry-run", action="store_true", help="print the queries without calling the API")
    return parser.parse_args()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = parse_args()
    started = dt.datetime.now()

    study_groups = load_study_groups(args.studies)
    term_groups, skipped = build_term_groups(load_conditions(args.conditions) + load_kg_terms(args.keywords, args.scopes))
    groups = study_groups + term_groups
    log(f"{len(study_groups)} award-number searches, {len(term_groups)} keyword searches "
        f"({len(skipped)} terms too short to search: {', '.join(skipped) or 'none'})")

    if args.dry_run:
        for group in groups:
            log(f"  [{group['anchor']}] {json.dumps(group['criteria'])}  -> {len(group['targets'])} KG targets")
        return

    fiscal_years = (
        list(range(args.min_fiscal_year, dt.date.today().year + 2)) if args.min_fiscal_year else None
    )
    links, kept_records, query_log = {}, {}, []
    for i, group in enumerate(groups, 1):
        log(f"[{i}/{len(groups)}] {group['anchor']}: {group['label']}")
        try:
            entry = run_group(group, fiscal_years, links, kept_records)
        except RuntimeError as e:
            log(f"    FAILED: {e}")
            entry = {"anchor": group["anchor"], "search_term": group["label"],
                     "query": json.dumps(group["criteria"]), "api_total": 0, "records_retrieved": 0,
                     "grants_found": 0, "grants_kept": 0, "grants_rejected": 0, "status": f"error: {e}"}
        else:
            log(f"    {entry['api_total']} API hits, {entry['grants_kept']} grants kept, "
                f"{entry['grants_rejected']} rejected by local check")
        query_log.append(entry)

    grants = summarize_grants(kept_records.values(), links.values())
    evidence = evidence_rows(links.values(), grants)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "grant_evidence.csv", evidence, EVIDENCE_COLUMNS)
    grant_rows = sorted(grants.values(), key=lambda g: g["core_project_num"])
    write_csv(args.out_dir / "grants.csv", grant_rows, list(grant_rows[0]) if grant_rows else ["core_project_num"])
    write_csv(args.out_dir / "query_log.csv", query_log,
              ["anchor", "search_term", "query", "api_total", "records_retrieved",
               "grants_found", "grants_kept", "grants_rejected", "status"])
    with open(args.out_dir / "reporter_records.jsonl", "w", encoding="utf-8") as f:
        for record in kept_records.values():
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    write_summary(args.out_dir / "summary.txt", args, grants, evidence, query_log, skipped, started)

    log(f"\n{len(grants)} grants, {len(evidence)} evidence links written to {args.out_dir}")


if __name__ == "__main__":
    main()
