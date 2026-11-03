"""
Fetches title, journal and publication date for every paper in the papers
workstream from PubMed, and writes them to
data/papers/publication_metadata.csv for load_papers.py to pick up.

Why this is needed: the papers bundle carries identifiers and links but no
bibliographic metadata, so core.publication rows had no title and no year. Two
things depend on that metadata:

  labels  core.kg_node.label falls back to "PMID 28854372" without a title, so
          every paper in the graph is unreadable without a second lookup.
  dates   core.kg_node.node_date is NULL without a year. 297 of the 300
          publications were undated, which left the largest evidence class out
          of any query that filters or orders by date.

Only 126 of the 300 papers have any citation text in the local files, so this
is a network fetch rather than a parse. Like fetch_grant_publications.py it is
kept separate from the loader: run it once, keep the CSV, and the ETL is
offline from then on.

    uv run python -m core_data_loader.loaders.fetch_publication_metadata

PubMed's esummary endpoint is free and needs no API key. NCBI asks that tools
identify themselves and stay at or below 3 requests/second without a key; both
are honoured below.
"""

import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

PAPERS_FILEPATH = "data/papers/papers_iedb_rcsb_update/output_v2/unique_papers_v2.csv"
OUTPUT_FILEPATH = "data/papers/publication_metadata.csv"

ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
# NCBI asks that automated clients identify themselves.
TOOL = "core-DB-loader"
EMAIL = "jadaguilon@gmail.com"

BATCH_SIZE = 200      # esummary accepts a few hundred ids per POST comfortably
REQUEST_PAUSE = 0.4   # < 3 req/s, the documented unauthenticated ceiling


def fetch_batch(pmids: list[str]) -> dict:
    payload = urllib.parse.urlencode({
        "db": "pubmed",
        "retmode": "json",
        "id": ",".join(pmids),
        "tool": TOOL,
        "email": EMAIL,
    }).encode()
    request = urllib.request.Request(
        ENDPOINT, data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def parse_record(rec: dict) -> dict:
    # pubdate is free text: "2014 Jun 12", "2016 Jan", "2021". Only the year is
    # reliably present, and that is all node_date needs -- core.kg_node builds a
    # January 1st date from it rather than inventing a day.
    pubdate = (rec.get("pubdate") or "").strip()
    year = pubdate.split(" ")[0] if pubdate[:4].isdigit() else ""
    authors = "; ".join(
        a.get("name", "") for a in (rec.get("authors") or []) if a.get("name")
    )
    doi = next(
        (aid.get("value") for aid in (rec.get("articleids") or [])
         if aid.get("idtype") == "doi"),
        "",
    )
    return {
        "pmid": rec.get("uid", ""),
        "title": (rec.get("title") or "").strip().rstrip("."),
        "journal": (rec.get("fulljournalname") or rec.get("source") or "").strip(),
        "year": year,
        "authors": authors,
        "doi": doi,
    }


def main():
    papers = pd.read_csv(PAPERS_FILEPATH, dtype=str, keep_default_na=False)
    pmids = sorted({p for p in papers["pmid"] if p.strip().isdigit()})
    print(f"Fetching PubMed metadata for {len(pmids)} papers")

    rows = []
    failed = []
    for i in range(0, len(pmids), BATCH_SIZE):
        batch = pmids[i:i + BATCH_SIZE]
        try:
            body = fetch_batch(batch)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            failed.append((batch[0], batch[-1], str(e)))
            continue
        result = body.get("result") or {}
        for uid in result.get("uids", []):
            rec = result.get(uid)
            if isinstance(rec, dict):
                rows.append(parse_record(rec))
        print(f"  {min(i + BATCH_SIZE, len(pmids))}/{len(pmids)} -> {len(rows)} records")
        time.sleep(REQUEST_PAUSE)

    with open(OUTPUT_FILEPATH, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["pmid", "title", "journal", "year", "authors", "doi"]
        )
        writer.writeheader()
        writer.writerows(rows)

    dated = sum(1 for r in rows if r["year"])
    titled = sum(1 for r in rows if r["title"])
    print(f"Wrote {len(rows)} rows ({titled} with a title, {dated} with a year) "
          f"-> {OUTPUT_FILEPATH}")
    missing = len(pmids) - len(rows)
    if missing:
        print(f"  ! {missing} PMIDs returned no record")
    for first, last, error in failed:
        print(f"  ! batch {first}..{last} failed: {error}")


if __name__ == "__main__":
    main()
