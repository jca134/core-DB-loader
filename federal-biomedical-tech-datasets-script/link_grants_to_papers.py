#!/usr/bin/env python3
"""Link grants to papers their PIs published after the award started.

For every grant (award_key, same grouping as build_grants_staging.py) and each of its PIs:

  1. Author match   the PI appears in the paper's author list (same last name, compatible
                    first name / initials).
  2. Timing         the paper was published after the award start date.
  3. Relevance      the paper is about the grant's topic, or acknowledges the grant in PubMed.
                    Topic closeness is the cosine similarity of text embeddings (grant title +
                    abstract vs. paper title + abstract), ranked against every paper in the set:
                    similarity_percentile 0.9 means the paper is closer to this grant than 90%
                    of all papers are. Every paper here is about filoviruses, so raw cosine
                    values sit in a narrow band and only the rank separates topics.

Every pair that passes 1 and 2 is written, with is_relevant marking the ones that also pass 3,
so the threshold can be changed in SQL without re-running.

Inputs:

  output/grants/reporter_records.jsonl    RePORTER records (full PI lists, abstracts)
  output/papers/filovirus_papers.csv      one row per paper
  output/papers/paper_authors.csv         one row per paper author
  output/papers/paper_grants_from_pubmed.csv  grants each paper acknowledges in PubMed

Paper abstracts are not in the paper files, so they are fetched once from PubMed and cached in
output/papers/paper_abstracts.csv.

Outputs, written to --out-dir (default output/links):

  grant_paper_link.csv        one row per (grant, PI, paper) published after the award start
  grant_pi_paper_summary.csv  one row per (grant, PI): papers before vs. after the award

Load with:

  psql -d <db> -v links_dir=output/links -f schema/load_grant_paper_links.sql

Usage:

  python link_grants_to_papers.py
  python link_grants_to_papers.py --threshold 0.9 --model pritamdeka/S-PubMedBert-MS-MARCO
"""

import argparse
import csv
import datetime as dt
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from build_grants_staging import award_key, clean_text, clean_title, date_only, relevance_tier, write_csv
from fetch_nih_grants import HERE, log

EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EFETCH_BATCH = 200
# Against papers that acknowledge the grant, bge-small ranked them slightly better than
# S-PubMedBert-MS-MARCO, all-MiniLM-L6-v2 and TF-IDF (AUC 0.73 vs. 0.70-0.72).
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
# Keeps 78% of acknowledged pairs on text alone. Spot checks: below ~0.4 pairs are off-topic,
# 0.6-0.75 are borderline (same virus, different question), above 0.9 are clearly on-topic.
DEFAULT_THRESHOLD = 0.75

# NIH grant number inside free text, e.g. 'R21 AI180801', '1R01 AI123535', 'U19AI135995'.
# Captures the IC + serial ('AI180801'), which identifies the grant regardless of activity code.
NIH_GRANT_RE = re.compile(r"(?<![A-Z0-9])(?:\d?[A-Z]\d{2}\s*-?\s*)?([A-Z]{2})\s*-?\s*(\d{6})(?![0-9])")
STANDARD_CORE_RE = re.compile(r"^[A-Z0-9]{3}([A-Z]{2}\d{6})$")

# Words too common in organization names to show that an affiliation is the grantee's.
ORG_STOPWORDS = {
    "of", "the", "and", "at", "for", "in", "inc", "llc", "corp", "corporation", "co", "ltd",
    "university", "univ", "institute", "institutes", "inst", "college", "school", "center",
    "centre", "ctr", "hospital", "department", "dept", "medical", "medicine", "health",
    "sciences", "science", "research", "national", "foundation", "state", "system", "systems",
    "laboratory", "laboratories", "division", "faculty", "campus",
}

LINK_COLUMNS = [
    "award_key", "core_project_num", "grant_title", "activity_code", "admin_ic_name",
    "grant_org_name", "grant_relevance_tier", "award_start_date", "award_end_date",
    "award_date_source", "pi_profile_id", "pi_name", "pi_is_contact",
    "pmid", "doi", "pmcid", "paper_title", "journal", "pub_date", "pub_date_precision",
    "paper_year", "n_authors", "cited_by_count", "pubmed_url", "author_name", "author_position",
    "is_last_author", "author_affiliation", "name_match", "affiliation_match", "match_confidence",
    "days_after_award_start", "published_during_award", "grant_acknowledged", "has_paper_abstract",
    "similarity", "similarity_percentile", "is_best_grant_match", "is_relevant", "link_basis",
]
SUMMARY_COLUMNS = [
    "award_key", "core_project_num", "pi_profile_id", "pi_name", "award_start_date",
    "award_end_date", "papers_before_award", "relevant_papers_before_award",
    "papers_after_award", "relevant_papers_after_award",
    "acknowledged_papers", "first_relevant_year", "last_relevant_year",
]


# ---------------------------------------------------------------- names

def ascii_lower(value):
    return unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()


def name_key(value):
    """'Mühlberger' and 'Muhlberger', 'Ollmann-Saphire' and 'Ollmann Saphire' compare equal."""
    return re.sub(r"[^a-z]", "", ascii_lower(value))


def name_tokens(value):
    return [t for t in re.split(r"[^a-z]+", ascii_lower(value)) if t]


def name_match(pi, author):
    """Return 'forename', 'initials' or None.

    RePORTER gives full first/middle names; PubMed gives fore_name ('Erica Ollmann' or 'E O')
    and initials ('EO'). Last names must agree exactly. First names must agree, or one must be
    an initial or prefix of the other ('Chris'/'Christopher'). A middle initial on both sides
    must agree, which separates e.g. 'J E Crowe' from 'J R Crowe'.
    """
    if not pi["last_key"] or pi["last_key"] != name_key(author["last_name"]):
        return None
    pi_first = name_tokens(pi["first_name"])
    au_first = name_tokens(author["fore_name"])
    au_initials = re.sub(r"[^a-z]", "", (author["initials"] or "").lower())
    if not pi_first or not (au_first or au_initials):
        return None
    if au_first and len(au_first[0]) > 1 and len(pi_first[0]) > 1:
        a, b = au_first[0], pi_first[0]
        if not (a.startswith(b) or b.startswith(a)):
            return None
        method = "forename"
    else:
        if (au_initials or au_first[0])[0] != pi_first[0][0]:
            return None
        method = "initials"
    pi_middle = (name_tokens(pi["middle_name"]) or pi_first[1:])[:1]
    if pi_middle and len(au_initials) > 1 and au_initials[1] != pi_middle[0][0]:
        return None
    return method


def match_confidence(method, affiliation_ok, acknowledged):
    """high: the paper cites the grant or the author's affiliation is the grantee org;
    medium: full first name agrees but nothing else confirms it; low: initials only.
    Common names ('Chris Williams', 'Biao He') can match a different person at medium/low."""
    if acknowledged or affiliation_ok:
        return "high"
    return "medium" if method == "forename" else "low"


def org_tokens(value):
    return {t for t in name_tokens(value) if len(t) > 1 and t not in ORG_STOPWORDS}


def affiliation_match(org_token_sets, affiliation):
    """True if at least half of an org name's distinctive words appear in the affiliation."""
    if not affiliation:
        return None
    words = set(name_tokens(affiliation))
    return any(tokens and len(tokens & words) * 2 >= len(tokens) for tokens in org_token_sets)


# ---------------------------------------------------------------- grants

def fiscal_year_start(fiscal_year):
    return f"{int(fiscal_year) - 1}-10-01" if fiscal_year else ""


def load_grants(path):
    """One dict per award_key with its PIs, dates and text for embedding."""
    with open(path, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    by_key = defaultdict(list)
    for r in records:
        by_key[award_key(r)].append(r)

    grants = []
    for key, recs in sorted(by_key.items()):
        parents = [r for r in recs if not r.get("subproject_id")] or recs
        earliest = min(parents, key=lambda r: (r.get("fiscal_year") or 0, r["appl_id"]))
        latest = max(parents, key=lambda r: (r.get("fiscal_year") or 0, r["appl_id"]))
        starts = sorted(date_only(r.get("project_start_date")) for r in recs if r.get("project_start_date"))
        ends = sorted(date_only(r.get("project_end_date")) for r in recs if r.get("project_end_date"))
        notices = sorted(date_only(r.get("award_notice_date")) for r in recs if r.get("award_notice_date"))
        if starts:
            start, source = starts[0], "project_start_date"
        elif notices:
            start, source = notices[0], "award_notice_date"
        else:
            start, source = fiscal_year_start(earliest.get("fiscal_year")), "fiscal_year_start"

        pis = {}
        for r in sorted(recs, key=lambda r: (r.get("fiscal_year") or 0, r["appl_id"])):
            for p in r.get("principal_investigators") or []:
                # Keyed by name: one person can have two RePORTER profile IDs (e.g. Gary J Nabel).
                pid = (name_key(p.get("last_name")), name_key(p.get("first_name")))
                entry = pis.setdefault(pid, {
                    "profile_id": p.get("profile_id") or "",
                    "first_name": (p.get("first_name") or "").strip(),
                    "middle_name": (p.get("middle_name") or "").strip(),
                    "last_name": (p.get("last_name") or "").strip(),
                    "last_key": name_key(p.get("last_name")),
                    "is_contact": False,
                })
                entry["is_contact"] |= bool(p.get("is_contact_pi"))

        # Abstract wording changes little between years; the longest one carries the most topic text.
        abstract = max((clean_text(r.get("abstract_text")) for r in recs), key=len, default="")
        orgs = {(r.get("organization") or {}).get("org_name") or "" for r in recs} - {""}
        grants.append({
            "award_key": key,
            "core_project_num": latest.get("core_project_num") or key,
            "title": clean_title(earliest.get("project_title")),
            "abstract": abstract,
            "activity_code": latest.get("activity_code") or "",
            "admin_ic_name": (latest.get("agency_ic_admin") or {}).get("abbreviation") or "",
            "org_name": (latest.get("organization") or {}).get("org_name") or "",
            "org_token_sets": [org_tokens(o) for o in orgs],
            "relevance_tier": relevance_tier(recs),
            "start": start,
            "end": ends[-1] if ends else "",
            "start_source": source,
            "pis": list(pis.values()),
        })
    return grants


def grant_serial(core_project_num):
    """'R01AI123456' -> 'AI123456'; contract numbers return None."""
    m = STANDARD_CORE_RE.match(core_project_num or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- papers

def paper_date(paper):
    """('2013-01-01', 'month') for '2013-01'. Partial dates take the earliest possible day, so a
    paper only counts as after the award when even its earliest possible date is after it."""
    raw = (paper["pub_date"] or paper["year"] or "").strip()
    parts = raw.split("-")
    precision = {1: "year", 2: "month", 3: "day"}.get(len(parts), "")
    return "-".join((parts + ["01", "01"])[:3]), precision


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def fetch_abstracts(pmids):
    """pmid -> abstract text from PubMed efetch (structured abstracts are joined by section)."""
    abstracts = {}
    for i in range(0, len(pmids), EFETCH_BATCH):
        batch = pmids[i:i + EFETCH_BATCH]
        body = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(batch), "retmode": "xml"}).encode()
        with urllib.request.urlopen(urllib.request.Request(EFETCH_URL, data=body), timeout=120) as resp:
            root = ET.fromstring(resp.read())
        for article in root.iter("PubmedArticle"):
            pmid = article.findtext("MedlineCitation/PMID")
            parts = []
            for node in article.findall("MedlineCitation/Article/Abstract/AbstractText"):
                text = clean_title("".join(node.itertext()))
                label = node.get("Label")
                parts.append(f"{label}: {text}" if label and text else text)
            abstracts[pmid] = "\n".join(p for p in parts if p)
        time.sleep(0.4)  # NCBI allows 3 requests per second without an API key
    return abstracts


def load_abstracts(cache, pmids, refresh):
    cached = {} if refresh or not cache.exists() else {r["pmid"]: r["abstract"] for r in read_csv(cache)}
    missing = [p for p in pmids if p not in cached]
    if missing:
        log(f"Fetching {len(missing)} paper abstracts from PubMed")
        fetched = fetch_abstracts(missing)
        cached.update({p: fetched.get(p, "") for p in missing})
        write_csv(cache, [{"pmid": p, "abstract": a} for p, a in sorted(cached.items())], ["pmid", "abstract"])
    return cached


def acknowledged_serials(grant_rows):
    """pmid -> set of NIH IC+serial numbers the paper acknowledges."""
    serials = defaultdict(set)
    for row in grant_rows:
        for ic, num in NIH_GRANT_RE.findall((row["grant_id"] or "").upper()):
            serials[row["pmid"]].add(ic + num)
    return serials


# ---------------------------------------------------------------- embeddings

def embed(model, texts):
    return model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)


def doc_text(title, abstract):
    return f"{title}\n{abstract}".strip()


# ---------------------------------------------------------------- main

def parse_args():
    parser = argparse.ArgumentParser(description="Link grants to papers published by their PIs after the award.")
    parser.add_argument("--records", type=Path, default=HERE / "output" / "grants" / "reporter_records.jsonl")
    parser.add_argument("--papers-dir", type=Path, default=HERE / "output" / "papers")
    parser.add_argument("--out-dir", type=Path, default=HERE / "output" / "links")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"sentence-transformers model (default: {DEFAULT_MODEL})")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"minimum similarity_percentile for is_relevant (default: {DEFAULT_THRESHOLD})")
    parser.add_argument("--refresh-abstracts", action="store_true", help="re-fetch paper abstracts from PubMed")
    return parser.parse_args()


def main():
    args = parse_args()
    grants = load_grants(args.records)
    papers = {p["pmid"]: p for p in read_csv(args.papers_dir / "filovirus_papers.csv")}
    authors_by_last = defaultdict(list)
    for a in read_csv(args.papers_dir / "paper_authors.csv"):
        if a["pmid"] in papers:
            authors_by_last[name_key(a["last_name"])].append(a)
    acknowledged = acknowledged_serials(read_csv(args.papers_dir / "paper_grants_from_pubmed.csv"))
    abstracts = load_abstracts(args.papers_dir / "paper_abstracts.csv", sorted(papers), args.refresh_abstracts)

    # ---- candidate (grant, PI, paper) pairs from author names
    candidates = {}
    for g in grants:
        serial = grant_serial(g["core_project_num"])
        for pi in g["pis"]:
            for author in authors_by_last.get(pi["last_key"], ()):
                method = name_match(pi, author)
                if not method:
                    continue
                key = (g["award_key"], pi["last_key"], pi["first_name"].lower(), author["pmid"])
                if key in candidates:  # same person listed twice on one paper
                    continue
                paper = papers[author["pmid"]]
                candidates[key] = {
                    "grant": g, "pi": pi, "author": author, "paper": paper, "name_match": method,
                    "acknowledged": bool(serial and serial in acknowledged.get(author["pmid"], ())),
                }

    # ---- relevance: cosine similarity between grant and paper text, ranked over all papers
    from sentence_transformers import SentenceTransformer  # slow import; only needed past this point

    log(f"Embedding with {args.model}")
    model = SentenceTransformer(args.model)
    grant_keys = sorted({c["grant"]["award_key"] for c in candidates.values()})
    grant_by_key = {g["award_key"]: g for g in grants}
    pmids = sorted(papers)
    paper_index = {p: i for i, p in enumerate(pmids)}
    grant_vecs = embed(model, [doc_text(grant_by_key[k]["title"], grant_by_key[k]["abstract"]) for k in grant_keys])
    paper_vecs = embed(model, [doc_text(papers[p]["title"], abstracts.get(p, "")) for p in pmids])
    sims = dict(zip(grant_keys, grant_vecs @ paper_vecs.T))  # award_key -> similarity to every paper

    links, summary = [], defaultdict(lambda: {"before": set(), "relevant_before": set(), "after": set(),
                                         "relevant": set(), "ack": set()})
    for (key, *pid, pmid), c in sorted(candidates.items()):
        g, pi, author, paper = c["grant"], c["pi"], c["author"], c["paper"]
        pub_date, precision = paper_date(paper)
        s = summary[(key, *pid)]
        s["pi"], s["grant"] = pi, g
        row = sims[key]
        similarity = float(row[paper_index[pmid]])
        percentile = float((row < similarity).mean())
        if not g["start"] or pub_date <= g["start"]:
            # On-topic papers before the award are the baseline for the after-award count.
            s["before"].add(pmid)
            if percentile >= args.threshold:
                s["relevant_before"].add(pmid)
            continue
        relevant = c["acknowledged"] or percentile >= args.threshold
        basis = [b for b, ok in (("acknowledged", c["acknowledged"]),
                                 ("similarity", percentile >= args.threshold)) if ok]
        days = (dt.date.fromisoformat(pub_date) - dt.date.fromisoformat(g["start"])).days
        s["after"].add(pmid)
        if relevant:
            s["relevant"].add((pmid, paper["year"]))
        if c["acknowledged"]:
            s["ack"].add(pmid)
        aff = affiliation_match(g["org_token_sets"], author["affiliation"])
        links.append({
            "award_key": key,
            "core_project_num": g["core_project_num"],
            "grant_title": g["title"],
            "activity_code": g["activity_code"],
            "admin_ic_name": g["admin_ic_name"],
            "grant_org_name": g["org_name"],
            "grant_relevance_tier": g["relevance_tier"],
            "award_start_date": g["start"],
            "award_end_date": g["end"],
            "award_date_source": g["start_source"],
            "pi_profile_id": pi["profile_id"],
            "pi_name": " ".join(p for p in (pi["first_name"], pi["middle_name"], pi["last_name"]) if p),
            "pi_is_contact": str(pi["is_contact"]).lower(),
            "pmid": pmid,
            "doi": paper["doi"],
            "pmcid": paper["pmcid"],
            "paper_title": paper["title"],
            "journal": paper["journal"],
            "pub_date": pub_date,
            "pub_date_precision": precision,
            "paper_year": paper["year"],
            "n_authors": paper["n_authors"],
            "cited_by_count": paper["cited_by_count"],
            "pubmed_url": paper["pubmed_url"],
            "author_name": author["full_name"],
            "author_position": author["author_position"],
            "is_last_author": str(author["is_last_author"] == "yes").lower(),
            "author_affiliation": author["affiliation"],
            "name_match": c["name_match"],
            "affiliation_match": "" if aff is None else str(aff).lower(),
            "match_confidence": match_confidence(c["name_match"], aff, c["acknowledged"]),
            "days_after_award_start": days,
            "published_during_award": str(bool(g["end"]) and pub_date <= g["end"]).lower(),
            "grant_acknowledged": str(c["acknowledged"]).lower(),
            "has_paper_abstract": str(bool(abstracts.get(pmid))).lower(),
            "similarity": f"{similarity:.4f}",
            "similarity_percentile": f"{percentile:.4f}",
            "is_relevant": str(relevant).lower(),
            "link_basis": "+".join(basis),
        })

    # A PI with many grants gets the same paper linked to each of them. Flag the grant whose text
    # is closest to the paper, so each paper can be attributed to one grant per PI.
    best = {}
    for l in links:
        k = (l["pi_name"], l["pmid"])
        if k not in best or float(l["similarity"]) > float(best[k]["similarity"]):
            best[k] = l
    for l in links:
        l["is_best_grant_match"] = str(best[(l["pi_name"], l["pmid"])] is l).lower()

    summary_rows = []
    for (key, *_), s in sorted(summary.items()):
        years = sorted(int(y) for _, y in s["relevant"])
        summary_rows.append({
            "award_key": key,
            "core_project_num": s["grant"]["core_project_num"],
            "pi_profile_id": s["pi"]["profile_id"],
            "pi_name": " ".join(p for p in (s["pi"]["first_name"], s["pi"]["last_name"]) if p),
            "award_start_date": s["grant"]["start"],
            "award_end_date": s["grant"]["end"],
            "papers_before_award": len(s["before"]),
            "relevant_papers_before_award": len(s["relevant_before"]),
            "papers_after_award": len(s["after"]),
            "relevant_papers_after_award": len(s["relevant"]),
            "acknowledged_papers": len(s["ack"]),
            "first_relevant_year": years[0] if years else "",
            "last_relevant_year": years[-1] if years else "",
        })

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "grant_paper_link.csv", links, LINK_COLUMNS)
    write_csv(args.out_dir / "grant_pi_paper_summary.csv", summary_rows, SUMMARY_COLUMNS)

    # ---- run report, including how well the threshold recovers acknowledged pairs
    ack_pcts = [float(l["similarity_percentile"]) for l in links if l["grant_acknowledged"] == "true"]
    other_pcts = [float(l["similarity_percentile"]) for l in links if l["grant_acknowledged"] == "false"]
    relevant = [l for l in links if l["is_relevant"] == "true"]
    log(f"{len(grants)} grants, {sum(len(g['pis']) for g in grants)} grant-PI pairs, {len(papers)} papers "
        f"({sum(1 for p in papers if abstracts.get(p))} with abstracts)")
    log(f"{len(candidates)} grant-PI-paper author matches; {len(links)} published after the award start")
    log(f"{len(relevant)} relevant links ({len({l['award_key'] for l in relevant})} grants, "
        f"{len({l['pmid'] for l in relevant})} papers); "
        f"{sum(1 for l in links if l['grant_acknowledged'] == 'true')} acknowledge the grant in PubMed")
    if ack_pcts and other_pcts:
        log(f"Threshold {args.threshold}: passes {sum(x >= args.threshold for x in ack_pcts) / len(ack_pcts):.0%} "
            f"of acknowledged pairs and {sum(x >= args.threshold for x in other_pcts) / len(other_pcts):.0%} "
            f"of the rest on similarity alone")
    conf = Counter(l["match_confidence"] for l in links)
    log(f"Author match confidence: high {conf['high']}, medium {conf['medium']}, low {conf['low']} "
        f"(medium/low can be a different person with the same name)")
    log(f"CSVs written to {args.out_dir}")


if __name__ == "__main__":
    main()
