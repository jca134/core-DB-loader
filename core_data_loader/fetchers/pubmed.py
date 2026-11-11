"""
The PubMed (NCBI E-utilities) client shared by the fetchers.

Free, no API key. NCBI asks automated clients to identify themselves (tool,
plus a contact email when configured -- NCBI_EMAIL, see config.py) and to stay
under 3 requests/second without a key; REQUEST_PAUSE keeps every caller below
that.
"""

import json
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from core_data_loader.config import NCBI_EMAIL, NCBI_TOOL

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
REQUEST_PAUSE = 0.4   # seconds; < 3 req/s, the documented unauthenticated ceiling

# Columns of a parsed efetch record, in the order the fetchers write them.
COLUMNS = ["pmid", "title", "abstract", "mesh_terms", "journal", "year", "authors", "doi"]


def _params(**params) -> dict:
    out = {"db": "pubmed", "tool": NCBI_TOOL, **params}
    if NCBI_EMAIL:
        out["email"] = NCBI_EMAIL
    return out


def _post(endpoint: str, timeout: int, **params) -> bytes:
    request = urllib.request.Request(
        f"{EUTILS}/{endpoint}",
        data=urllib.parse.urlencode(_params(**params)).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def esummary(pmids: list[str]) -> dict:
    """Bibliographic summaries (no abstract, no MeSH) as esummary's JSON."""
    return json.loads(_post("esummary.fcgi", 60, retmode="json", id=",".join(pmids)))


def efetch_xml(pmids: list[str]) -> bytes:
    """Full PubMed records, abstracts and MeSH included, as XML."""
    return _post("efetch.fcgi", 120, retmode="xml", id=",".join(pmids))


def reference_lists(pmids: list[str]) -> dict[str, list[str]]:
    """
    pmid -> the PMIDs it cites (elink pubmed_pubmed_refs). Only papers whose
    full text is in PubMed Central have a reference list; the rest are absent.
    """
    query = urllib.parse.urlencode(
        _params(dbfrom="pubmed", linkname="pubmed_pubmed_refs", retmode="json", id=pmids), doseq=True)
    request = urllib.request.Request(
        f"{EUTILS}/elink.fcgi", data=query.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.load(response)
    out = {}
    for linkset in body.get("linksets", []):
        ids = linkset.get("ids") or []
        dbs = linkset.get("linksetdbs") or []
        if len(ids) == 1 and dbs:
            out[str(ids[0])] = [str(x) for x in dbs[0].get("links", [])]
    return out


def esearch(term: str, retmax: int = 5, retries: int = 3) -> list[str]:
    """PMIDs matching a PubMed query; [] if the search keeps failing."""
    query = urllib.parse.urlencode(_params(term=term, retmode="json", retmax=retmax))
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(f"{EUTILS}/esearch.fcgi?{query}", timeout=60) as r:
                time.sleep(REQUEST_PAUSE)
                return json.load(r)["esearchresult"].get("idlist", [])
        except Exception:
            time.sleep(2 * (attempt + 1))
    return []


def fetch_records(pmids: list[str]) -> dict:
    """pmid -> parsed record, for every record efetch returns and can be parsed."""
    if not pmids:
        return {}
    root = ET.fromstring(efetch_xml(pmids))
    time.sleep(REQUEST_PAUSE)
    out = {}
    for article in root.findall("PubmedArticle"):
        try:
            rec = parse_article(article)
        except AttributeError:
            continue
        out[rec["pmid"]] = rec
    return out


def with_deadline(fn, *args, deadline: float = 90):
    """
    fn(*args), or TimeoutError if it isn't back within `deadline` seconds.
    NCBI occasionally holds a connection open without answering, which no
    socket read timeout catches; the call runs in a daemon thread that is
    abandoned, not joined, when the deadline passes.
    """
    result = {}

    def run():
        try:
            result["value"] = fn(*args)
        except Exception as e:      # handed back to the caller below
            result["error"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        raise TimeoutError(f"no answer within {deadline}s")
    if "error" in result:
        raise result["error"]
    return result["value"]


def parse_grant_list(art: ET.Element) -> list[dict]:
    """The grants a PubMed record acknowledges: GrantID, institute acronym, agency."""
    pmid = art.findtext("MedlineCitation/PMID", "")
    return [
        {"pmid": pmid, "grant_id": (g.findtext("GrantID") or "").strip(),
         "acronym": (g.findtext("Acronym") or "").strip(), "agency": (g.findtext("Agency") or "").strip()}
        for g in art.findall("MedlineCitation/Article/GrantList/Grant")
        if (g.findtext("GrantID") or "").strip()
    ]


def _text(el) -> str:
    """All text inside an element, including nested markup like <i>Ebola</i>."""
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def parse_article(art: ET.Element) -> dict:
    """One efetch <PubmedArticle> as a COLUMNS dict."""
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
