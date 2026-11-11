"""
Fetches title, journal and publication date for every paper in the papers
workstream from PubMed, and writes them to
data/papers/publication_metadata.csv for loaders/papers.py to pick up.

Why this is needed: the papers bundle carries identifiers and links but no
bibliographic metadata, so core.publication rows had no title and no year. Without
it a paper shows up as "PMID 28854372" with no date: 297 of the 300 were
undated, which left the largest evidence class out of any query that filters
or orders by date.

Only 126 of the 300 papers have any citation text in the local files, so this
is a network fetch rather than a parse. Like fetchers/grant_publications.py it is
kept separate from the loader: run it once, keep the CSV, and the ETL is
offline from then on.

    filovirus fetch publication-metadata

PubMed's esummary endpoint is free and needs no API key. NCBI asks that tools
identify themselves and stay at or below 3 requests/second without a key; both
are honoured below.
"""

import csv
import time
import urllib.error

import pandas as pd

from core_data_loader.config import data_path
from core_data_loader.fetchers.pubmed import REQUEST_PAUSE, esummary

PAPERS_FILEPATH = data_path("papers", "papers_iedb_rcsb_update", "output_v2", "unique_papers_v2.csv")
OUTPUT_FILEPATH = data_path("papers", "publication_metadata.csv")

BATCH_SIZE = 200      # esummary accepts a few hundred ids per POST comfortably


def parse_record(rec: dict) -> dict:
    # pubdate is free text: "2014 Jun 12", "2016 Jan", "2021". Only the year is
    # reliably present, so only the year is kept -- never an invented day.
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
            body = esummary(batch)
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
