"""
Fetches PubMed records -- title, abstract, MeSH headings, journal, year,
authors, DOI -- for every paper a grant cites (data/grants/grant_publications.csv)
that is not already one of the papers workstream's 300, and writes them to
data/grants/cited_publications.csv for load_grants.py to pick up.

    uv run python -m core_data_loader.loaders.fetch_cited_publications

WHY. RePORTER lists ~20k papers for the grants in the KG, but only the ~95 that
were also reached from an entity have a core.publication row, so every other
grant -> paper citation was dropped. Most of the 20k are off-topic: big centre
grants (P51 primate centres, P30 cancer centres) cite thousands of unrelated
papers. This script fetches all of them anyway; etl_evidence decides which are
filovirus papers (is_filovirus_paper), so the threshold can change without
another network run.

The abstract is fetched because the title alone misses papers whose title
doesn't name the virus, and because it is the text a similarity graph needs.

Network, free, no API key. Like the other fetch_* scripts: run once, keep the
CSV. Uses efetch (XML) rather than esummary, which carries no abstract or MeSH.
"""

import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import pandas as pd

from core_data_loader.loaders.fetch_publication_metadata import (
    EMAIL, PAPERS_FILEPATH, TOOL,
)

GRANT_PUBS_FILEPATH = "data/grants/grant_publications.csv"
OUTPUT_FILEPATH = "data/grants/cited_publications.csv"

ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
BATCH_SIZE = 200      # efetch XML for 200 records is a few MB; comfortable
REQUEST_PAUSE = 0.4   # < 3 req/s, the documented unauthenticated ceiling
RETRIES = 3

COLUMNS = ["pmid", "title", "abstract", "mesh_terms", "journal", "year", "authors", "doi"]


def fetch_batch(pmids: list[str]) -> bytes:
    payload = urllib.parse.urlencode({
        "db": "pubmed",
        "retmode": "xml",
        "id": ",".join(pmids),
        "tool": TOOL,
        "email": EMAIL,
    }).encode()
    request = urllib.request.Request(
        ENDPOINT, data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _text(el) -> str:
    """All text inside an element, including nested markup like <i>Ebola</i>."""
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def parse_article(art: ET.Element) -> dict:
    cit = art.find("MedlineCitation")
    article = cit.find("Article")

    # Structured abstracts come as several labelled sections; keep the labels so
    # "BACKGROUND: ... METHODS: ..." reads as it does on PubMed.
    sections = []
    for at in article.findall("Abstract/AbstractText"):
        label = at.get("Label")
        body = _text(at)
        if body:
            sections.append(f"{label}: {body}" if label else body)

    # Year: PubDate/Year when present, else the leading year of MedlineDate
    # ("2014 Jun-Jul"). Only the year is reliable across both forms.
    pub_date = article.find("Journal/JournalIssue/PubDate")
    year = ""
    if pub_date is not None:
        year = (pub_date.findtext("Year") or pub_date.findtext("MedlineDate") or "")[:4]
        if not year.isdigit():
            year = ""

    authors = []
    for a in article.findall("AuthorList/Author"):
        last = a.findtext("LastName")
        if last:
            initials = a.findtext("Initials") or ""
            authors.append(f"{last} {initials}".strip())
        elif a.findtext("CollectiveName"):
            authors.append(a.findtext("CollectiveName"))

    doi = next(
        (aid.text or "" for aid in art.findall("PubmedData/ArticleIdList/ArticleId")
         if aid.get("IdType") == "doi"),
        "",
    )

    return {
        "pmid": cit.findtext("PMID", ""),
        "title": _text(article.find("ArticleTitle")).rstrip("."),
        "abstract": " ".join(sections),
        "mesh_terms": "; ".join(
            _text(d) for d in cit.findall("MeshHeadingList/MeshHeading/DescriptorName")
        ),
        "journal": _text(article.find("Journal/Title")),
        "year": year,
        "authors": "; ".join(authors),
        "doi": doi,
    }


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
                root = ET.fromstring(fetch_batch(batch))
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
