"""
Fetches PubMed records -- title, abstract, MeSH headings, journal, year,
authors, DOI -- for every paper a grant cites (data/grants/grant_publications.csv)
that is not already one of the papers workstream's 300, and writes them to
data/grants/cited_publications.csv for loaders/grants.py to pick up.

    filovirus fetch cited-publications

WHY. RePORTER lists ~20k papers for the grants in the KG, but only the ~95 that
were also reached from an entity have a core.publication row, so every other
grant -> paper citation was dropped. Most of the 20k are off-topic: big centre
grants (P51 primate centres, P30 cancer centres) cite thousands of unrelated
papers. This script fetches all of them anyway; etl.evidence decides which are
filovirus papers (is_filovirus_paper), so the threshold can change without
another network run.

The abstract is fetched because the title alone misses papers whose title
doesn't name the virus, and because it is the text a similarity graph needs.

Network, free, no API key. Like the other fetchers: run once, keep the CSV. Uses efetch (XML) rather than esummary, which carries no abstract or MeSH.
"""

import http.client
import time
import urllib.error
import xml.etree.ElementTree as ET

import pandas as pd

from core_data_loader.config import data_path
from core_data_loader.fetchers.publication_metadata import PAPERS_FILEPATH
from core_data_loader.fetchers.pubmed import (
    COLUMNS,
    REQUEST_PAUSE,
    efetch_xml,
    parse_article,
)

GRANT_PUBS_FILEPATH = data_path("grants", "grant_publications.csv")
OUTPUT_FILEPATH = data_path("grants", "cited_publications.csv")

BATCH_SIZE = 200      # efetch XML for 200 records is a few MB; comfortable
RETRIES = 3


def main():
    cited = pd.read_csv(GRANT_PUBS_FILEPATH, dtype=str, keep_default_na=False)
    papers = pd.read_csv(PAPERS_FILEPATH, dtype=str, keep_default_na=False)
    pmids = sorted(set(cited["pmid"]) - set(papers["pmid"]) - {""})
    print(f"{len(pmids)} cited PMIDs not among the {papers['pmid'].nunique()} KG papers")

    rows, failed = [], []
    for i in range(0, len(pmids), BATCH_SIZE):
        batch = pmids[i:i + BATCH_SIZE]
        for attempt in range(RETRIES):
            try:
                root = ET.fromstring(efetch_xml(batch))
                break
            # http.client errors (RemoteDisconnected, IncompleteRead) are not
            # URLErrors, and a long run will eventually hit one.
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError,
                    ET.ParseError, TimeoutError) as e:
                if attempt == RETRIES - 1:
                    print(f"  batch {i}: failed after {RETRIES} tries ({e})")
                    failed.extend(batch)
                time.sleep(2 * (attempt + 1))
        else:
            root = None
        if root is not None:
            # One malformed record (no <Article>, say) must not cost the batch.
            for article in root.findall("PubmedArticle"):
                try:
                    rows.append(parse_article(article))
                except AttributeError:
                    pmid = article.findtext("MedlineCitation/PMID", "?")
                    print(f"  PMID {pmid}: unparseable record, skipped")
        if (i // BATCH_SIZE) % 20 == 0:
            print(f"  {min(i + BATCH_SIZE, len(pmids))}/{len(pmids)} -> {len(rows)} records")
        time.sleep(REQUEST_PAUSE)

    out = pd.DataFrame(rows, columns=COLUMNS).drop_duplicates("pmid")
    out.to_csv(OUTPUT_FILEPATH, index=False)
    missing = len(pmids) - len(out)
    print(f"Wrote {len(out)} records to {OUTPUT_FILEPATH} "
          f"({out['abstract'].ne('').sum()} with abstracts; "
          f"{missing} not returned, {len(failed)} in failed batches)")


if __name__ == "__main__":
    main()
