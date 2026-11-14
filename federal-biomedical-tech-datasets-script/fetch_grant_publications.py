#!/usr/bin/env python3
"""Fetch the papers each grant acknowledges, from NIH RePORTER, with their PubMed records.

The same two network steps as core-DB-loader's fetch_grant_publications.py and
fetch_cited_publications.py, run against this project's grants and writing here:

  1. RePORTER publications API: every PMID linked to each grant's core project number.
     These links come from the grantee's own reporting and PubMed grant acknowledgments,
     so they need no author match.
  2. PubMed efetch: title, abstract, MeSH, journal, year and authors for those PMIDs.
     Most are off-topic (centre grants cite thousands of papers); is_filovirus marks the
     ones about filoviruses with core-DB-loader's rule (a filovirus term in the title or
     MeSH, or two in the abstract), so the threshold can change without another fetch.

Both steps cache their output and skip work already done, so a rerun after a failure
only fetches what is missing. Free, no API key.

Outputs, written to output/papers:

  reporter_grant_publications.csv   core_project_num, pmid, appl_id
  cited_papers.csv                  one row per PMID, with is_filovirus

Usage:

  python fetch_grant_publications.py
  python fetch_grant_publications.py --refresh     # re-query RePORTER from scratch
"""

import argparse
import csv
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent

REPORTER_ENDPOINT = "https://api.reporter.nih.gov/v2/publications/search"
EFETCH_ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
TOOL = "federal-biomedical-tech-datasets-script"
CORE_BATCH, PAGE_LIMIT, REPORTER_PAUSE = 50, 500, 0.5
PMID_BATCH, EFETCH_PAUSE, RETRIES = 200, 0.4, 3

LINK_COLUMNS = ["core_project_num", "pmid", "appl_id"]
PAPER_COLUMNS = ["pmid", "title", "abstract", "mesh_terms", "journal", "year", "pub_date",
                 "authors", "doi", "is_filovirus"]

# core-DB-loader etl/etl_evidence.py FILOVIRUS_TITLE_RE / is_filovirus_paper.
FILOVIRUS_RE = re.compile(
    r"(?<![0-9A-Za-z])(filovir\w*|ebola\w*|marburg\w*|sudan virus|bundibugyo|"
    r"ravn virus|EBOV|SUDV|MARV|BDBV|TAFV|RESTV)(?![0-9A-Za-z])",
    re.IGNORECASE,
)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def log(message):
    print(message, flush=True)


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, columns):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def is_filovirus(title, mesh_terms, abstract):
    if FILOVIRUS_RE.search(title) or FILOVIRUS_RE.search(mesh_terms):
        return True
    return len(FILOVIRUS_RE.findall(abstract)) >= 2


def post_json(body):
    request = urllib.request.Request(
        REPORTER_ENDPOINT, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError):
            if attempt == RETRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_reporter_links(cores):
    rows, failed = [], []
    for i in range(0, len(cores), CORE_BATCH):
        batch = cores[i:i + CORE_BATCH]
        offset = 0
        try:
            while True:
                body = post_json({"criteria": {"core_project_nums": batch},
                                  "offset": offset, "limit": PAGE_LIMIT})
                page = body.get("results") or []
                rows += [{"core_project_num": r.get("coreproject"), "pmid": str(r.get("pmid") or ""),
                          "appl_id": str(r.get("applid") or "")} for r in page]
                offset += len(page)
                if not page or offset >= (body.get("meta") or {}).get("total", 0):
                    break
                time.sleep(REPORTER_PAUSE)
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as e:
            failed.append(f"{batch[0]}..{batch[-1]}: {e}")
        log(f"  RePORTER {min(i + CORE_BATCH, len(cores))}/{len(cores)} cores -> {len(rows)} links")
        time.sleep(REPORTER_PAUSE)
    unique = {(r["core_project_num"], r["pmid"], r["appl_id"]): r for r in rows if r["pmid"]}
    return sorted(unique.values(), key=lambda r: (r["core_project_num"], r["pmid"], r["appl_id"])), failed


def text_of(el):
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def parse_article(art):
    cit = art.find("MedlineCitation")
    article = cit.find("Article")
    sections = []
    for at in article.findall("Abstract/AbstractText"):
        body = text_of(at)
        if body:
            sections.append(f"{at.get('Label')}: {body}" if at.get("Label") else body)
    pub = article.find("Journal/JournalIssue/PubDate")
    year, pub_date = "", ""
    if pub is not None:
        year = (pub.findtext("Year") or pub.findtext("MedlineDate") or "")[:4]
        year = year if year.isdigit() else ""
        month = MONTHS.get((pub.findtext("Month") or "")[:3].lower()) or (
            int(pub.findtext("Month")) if (pub.findtext("Month") or "").isdigit() else None)
        day = pub.findtext("Day") or ""
        if year:
            pub_date = year + (f"-{month:02d}" if month else "") + (f"-{int(day):02d}" if month and day.isdigit() else "")
    authors = []
    for a in article.findall("AuthorList/Author"):
        if a.findtext("LastName"):
            authors.append(f"{a.findtext('ForeName') or a.findtext('Initials') or ''} {a.findtext('LastName')}".strip())
        elif a.findtext("CollectiveName"):
            authors.append(a.findtext("CollectiveName"))
    doi = next((aid.text or "" for aid in art.findall("PubmedData/ArticleIdList/ArticleId")
                if aid.get("IdType") == "doi"), "")
    row = {
        "pmid": cit.findtext("PMID", ""), "title": text_of(article.find("ArticleTitle")),
        "abstract": " ".join(sections),
        "mesh_terms": "; ".join(text_of(d) for d in cit.findall("MeshHeadingList/MeshHeading/DescriptorName")),
        "journal": text_of(article.find("Journal/Title")), "year": year, "pub_date": pub_date,
        "authors": "; ".join(authors), "doi": doi,
    }
    row["is_filovirus"] = str(is_filovirus(row["title"], row["mesh_terms"], row["abstract"])).lower()
    return row


def fetch_pubmed(pmids):
    rows, failed = [], 0
    for i in range(0, len(pmids), PMID_BATCH):
        batch = pmids[i:i + PMID_BATCH]
        payload = urllib.parse.urlencode({"db": "pubmed", "retmode": "xml", "id": ",".join(batch),
                                          "tool": TOOL}).encode()
        root = None
        for attempt in range(RETRIES):
            try:
                request = urllib.request.Request(EFETCH_ENDPOINT, data=payload)
                with urllib.request.urlopen(request, timeout=120) as response:
                    root = ET.fromstring(response.read())
                break
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError,
                    ET.ParseError, TimeoutError) as e:
                if attempt == RETRIES - 1:
                    log(f"  PubMed batch {i}: failed after {RETRIES} tries ({e})")
                    failed += len(batch)
                time.sleep(2 * (attempt + 1))
        for article in (root.findall("PubmedArticle") if root is not None else []):
            try:
                rows.append(parse_article(article))
            except (AttributeError, ValueError):
                log(f"  PMID {article.findtext('MedlineCitation/PMID', '?')}: unparseable record, skipped")
        if (i // PMID_BATCH) % 10 == 0 or i + PMID_BATCH >= len(pmids):
            log(f"  PubMed {min(i + PMID_BATCH, len(pmids))}/{len(pmids)} -> {len(rows)} records")
        time.sleep(EFETCH_PAUSE)
    return rows, failed


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--awards", type=Path, default=HERE / "output" / "grants" / "staging" / "stg_grant_award.csv")
    parser.add_argument("--out-dir", type=Path, default=HERE / "output" / "papers")
    parser.add_argument("--refresh", action="store_true", help="re-query RePORTER even if links are cached")
    return parser.parse_args()


def main():
    args = parse_args()
    links_path = args.out_dir / "reporter_grant_publications.csv"
    papers_path = args.out_dir / "cited_papers.csv"

    if links_path.exists() and not args.refresh:
        links = read_csv(links_path)
        log(f"Using cached {links_path.name}: {len(links)} links")
    else:
        cores = sorted({r["core_project_num"] for r in read_csv(args.awards) if r["core_project_num"]})
        log(f"Fetching RePORTER publications for {len(cores)} core project numbers")
        links, failed = fetch_reporter_links(cores)
        write_csv(links_path, links, LINK_COLUMNS)
        for f in failed:
            log(f"  ! failed batch {f}")

    cached = read_csv(papers_path) if papers_path.exists() else []
    have = {r["pmid"] for r in cached}
    todo = sorted({r["pmid"] for r in links} - have, key=int)
    log(f"{len({r['pmid'] for r in links})} distinct PMIDs; {len(have)} cached, fetching {len(todo)}")
    fetched, failed = fetch_pubmed(todo) if todo else ([], 0)
    papers = {r["pmid"]: r for r in cached + fetched}
    rows = sorted(papers.values(), key=lambda r: int(r["pmid"]))
    write_csv(papers_path, rows, PAPER_COLUMNS)
    filo = sum(r["is_filovirus"] == "true" for r in rows)
    log(f"Wrote {len(rows)} papers to {papers_path.name} ({filo} filovirus; "
        f"{sum(bool(r['abstract']) for r in rows)} with abstracts; {failed} in failed batches)")


if __name__ == "__main__":
    main()
