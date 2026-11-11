"""
Fetches, for every paper in the graph, the grants it acknowledges and the date
it went online, from its PubMed record, and writes them to
data/papers/paper_grants.csv and data/papers/paper_dates.csv for
loaders/papers.py to pick up.

    filovirus fetch paper-details

DATES. A paper's year (PubDate) is its print issue's, and journals publish
online first -- sometimes years first (NEJM's ChAd3 Ebola vaccine paper: online
2014, issue 2017). A paper is public from the earlier of the two, which is what
core.v_document_date uses. About 1 in 15 papers here went online in an earlier
year than their issue's.

GRANTS. RePORTER lists the papers each grant reported, but grantees don't always
report every paper, so grant -> paper citations miss papers that credit a
grant RePORTER doesn't list. A PubMed record's GrantList comes from the paper
itself ("Supported by NIAID R01 AI123456"); the build matches each grant
number exactly against the grants in the graph and adds what RePORTER missed.

WHICH PAPERS. The ones the build keeps in core.publication: the papers bundle,
the papers patents cite, and the grant-cited papers that pass the same
filovirus rule the build uses (etl/evidence.py is_filovirus_paper). If that
rule changes, rerun this.

Network, free, no key. Run after the other PubMed fetchers (it reads their
CSVs); run once and keep the CSV.
"""

import csv
import http.client
import time
import urllib.error
import xml.etree.ElementTree as ET

from core_data_loader.config import data_path
from core_data_loader.etl.evidence import is_filovirus_paper
from core_data_loader.fetchers.pubmed import (
    REQUEST_PAUSE,
    efetch_xml,
    parse_electronic_date,
    parse_grant_list,
    with_deadline,
)

BUNDLE = data_path("papers", "papers_iedb_rcsb_update", "output_v2", "unique_papers_v2.csv")
GRANT_CITED = data_path("grants", "cited_publications.csv")
PATENT_CITED = data_path("patents", "patent_cited_publications.csv")
OUTPUT_FILEPATH = data_path("papers", "paper_grants.csv")
DATES_FILEPATH = data_path("papers", "paper_dates.csv")
BATCH_SIZE = 100
RETRIES = 3
COLUMNS = ["pmid", "grant_id", "acronym", "agency"]
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError,
                  ET.ParseError, ValueError)


def _rows(path):
    if not path.exists():
        print(f"  (no {path.name}; its papers are not included)")
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def graph_pmids() -> list[str]:
    pmids = {r["pmid"] for r in _rows(BUNDLE)}
    pmids |= {r["pmid"] for r in _rows(PATENT_CITED) if r.get("pmid")}
    pmids |= {r["pmid"] for r in _rows(GRANT_CITED)
              if is_filovirus_paper(r.get("title", ""), r.get("mesh_terms", ""), r.get("abstract", ""))}
    return sorted((p.strip() for p in pmids if p and p.strip().isdigit()), key=int)


def main():
    pmids = graph_pmids()
    print(f"Fetching grant lists and online dates for {len(pmids)} papers", flush=True)
    rows, dates, failed = [], [], []
    for i in range(0, len(pmids), BATCH_SIZE):
        batch = pmids[i:i + BATCH_SIZE]
        for attempt in range(RETRIES):
            try:
                root = ET.fromstring(with_deadline(efetch_xml, batch, deadline=120))
                break
            except NETWORK_ERRORS as e:
                if attempt == RETRIES - 1:
                    failed.append((batch[0], batch[-1], str(e)))
                    root = None
                time.sleep(2 * (attempt + 1))
        if root is not None:
            for article in root.findall("PubmedArticle"):
                rows.extend(parse_grant_list(article))
                epub = parse_electronic_date(article)
                if epub:
                    dates.append({"pmid": article.findtext("MedlineCitation/PMID", ""), "epub_date": epub})
        print(f"  {min(i + BATCH_SIZE, len(pmids))}/{len(pmids)} -> {len(rows)} acknowledged grants", flush=True)
        time.sleep(REQUEST_PAUSE)

    with open(OUTPUT_FILEPATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    with open(DATES_FILEPATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["pmid", "epub_date"])
        writer.writeheader()
        writer.writerows(dates)
    print(f"Wrote {len(dates)} online dates to {DATES_FILEPATH}", flush=True)
    print(f"Wrote {len(rows)} paper -> grant acknowledgements for "
          f"{len({r['pmid'] for r in rows})} papers to {OUTPUT_FILEPATH}", flush=True)
    for first, last, error in failed:
        print(f"  ! batch {first}..{last} failed: {error}")


if __name__ == "__main__":
    main()
