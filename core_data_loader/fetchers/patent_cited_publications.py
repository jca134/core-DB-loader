"""
Resolves the literature a patent cites -- free-text "references cited"
strings, with no PMID -- to PubMed records, and writes them to
data/patents/patent_cited_publications.csv for loaders/patents.py to pick up.

    filovirus fetch patent-citations

INPUT. data/patents/patent_external_literature_edges.csv, one row per
(patent, cited reference), as the patents workstream extracted it from the
printed patents. The references are in every citation style a patent office
has ever accepted:

    Filovirus vaccines
    Marzi et al., Science, April 2015, 348(6233):439-442. (Year: 2015) *
    Sanchez, A. et al., " Sequence Analysis of the Ebola Virus Genome: ..., "
    Baker BM, Gagnon SJ, ... Wiley DC. Conversion of a T cell antagonist ...

and some are not papers at all (GenBank records, STN registry entries, CDC
fact sheets, translations of WO patents, office actions). Those stay
unresolved, which is correct.

HOW. Two strategies, each verified against the fetched record before it is
accepted -- a citation that resolves to the wrong paper is worse than one
that stays unresolved, because it becomes an asserted 'cites' edge:

  title     Candidate title segments from the citation are searched as
            all-words-in-title (+ year when known). A hit is accepted only if
            the record's title is a close match (difflib ratio >= 0.85, or the
            segment is contained in it) and its year is within one of the
            citation's.
  locator   For "Author, Journal, Year, Vol(Issue):Page" citations with no
            title: first-author surname + year + volume + first page as
            PubMed field searches. That combination is near-unique; accepted
            only on exactly one hit whose author list contains the surname.

Every resolved row records which strategy and score produced it, so any edge
is auditable. Which resolved papers to keep is decided in etl.evidence, not
here (currently: all of them), so that can change without another network run.

Network, free, no API key. Run once, keep the CSV.
"""

import difflib
import re
import unicodedata

import pandas as pd

from core_data_loader.config import data_path
from core_data_loader.fetchers.pubmed import COLUMNS, esearch, fetch_records

INPUT_FILEPATH = data_path("patents", "patent_external_literature_edges.csv")
OUTPUT_FILEPATH = data_path("patents", "patent_cited_publications.csv")

TITLE_MIN_RATIO = 0.85
MIN_TITLE_WORDS = 4

# Not literature: nothing in PubMed to find, so don't spend requests on them.
NOT_A_PAPER_RE = re.compile(
    r"GenBank|STN Regist|Registry No\.|English Translation|Espacenet|office action|"
    r"Fact Sheet|Information Packet|website|accessed online|WO \d{4}/",
    re.IGNORECASE,
)


# PubMed's stopwords. A field-tagged stopword ("from[ti]") makes the whole
# all-words query return nothing rather than being ignored.
STOPWORDS = {
    "a", "about", "again", "all", "almost", "also", "although", "always", "among", "an",
    "and", "another", "any", "are", "as", "at", "be", "because", "been", "before",
    "being", "between", "both", "but", "by", "can", "could", "did", "do", "does", "done",
    "due", "during", "each", "either", "enough", "especially", "etc", "for", "found",
    "from", "further", "had", "has", "have", "having", "here", "how", "however", "i",
    "if", "in", "into", "is", "it", "its", "itself", "just", "kg", "km", "made", "mainly",
    "make", "may", "mg", "might", "ml", "mm", "most", "mostly", "must", "nearly",
    "neither", "no", "nor", "obtained", "of", "often", "on", "our", "overall", "perhaps",
    "quite", "rather", "really", "regarding", "seem", "seen", "several", "should",
    "show", "showed", "shown", "shows", "significantly", "since", "so", "some", "such",
    "than", "that", "the", "their", "theirs", "them", "then", "there", "therefore",
    "these", "they", "this", "those", "through", "thus", "to", "upon", "use", "used",
    "using", "various", "very", "was", "we", "were", "what", "when", "which", "while",
    "with", "within", "without", "would",
}


def _fold(s: str) -> str:
    """Lowercase ASCII: 'Müller' and 'Muller' must compare equal."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", _fold(s)).split())


def title_segments(citation: str) -> list[str]:
    """
    Plausible title strings inside a citation, longest first. Quoted text is the
    strongest candidate; otherwise the citation is split on sentence and
    field separators and every run of MIN_TITLE_WORDS+ words is a candidate.
    """
    text = citation.replace("“", '"').replace("”", '"')
    text = re.sub(r"\(Year: \d{4}\)|\*", " ", text)
    candidates = [q.strip(" ,.") for q in re.findall(r'"\s*([^"]{15,}?)\s*"', text)]
    # A bare title ("Filovirus vaccines") is its own only candidate.
    if not re.search(r"[.,;:()\"]", text.strip(" .")):
        candidates.append(text.strip(" ."))
    for part in re.split(r"\.\s+|\(|\)|;|,\s+(?=[A-Z][a-z]+\.?\s)|\s\"\s?", text):
        part = part.strip(" ,.\"")
        if len(part.split()) >= MIN_TITLE_WORDS:
            candidates.append(part)
    seen, out = set(), []
    for c in sorted(candidates, key=lambda c: -len(c)):
        if _norm(c) not in seen:
            seen.add(_norm(c))
            out.append(c)
    return out[:4]


def title_score(segment: str, title: str) -> float:
    a, b = _norm(segment), _norm(title)
    if not a or not b:
        return 0.0
    # Containment only when the SHORTER side is a real title: otherwise a
    # record titled "Cold Spring Harbor Laboratory" is "contained" in every
    # citation of a Cold Spring Harbor book.
    if min(len(a.split()), len(b.split())) >= 6 and (a in b or b in a):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


ERRATUM_RE = re.compile(r"\s*(Correction|Erratum|Corrigendum|Retraction|Retracted)\b", re.IGNORECASE)


def _year_ok(cited_year, record_year) -> bool:
    if not cited_year or not record_year:
        return True
    return abs(int(cited_year) - int(record_year)) <= 1


def resolve_by_title(citation: str, year: str):
    for seg in title_segments(citation):
        words = [w for w in re.findall(r"[A-Za-z0-9]+", _fold(seg))
                 if len(w) > 2 and w not in STOPWORDS][:12]
        if len(words) < 2:
            continue
        term = " AND ".join(f"{w}[ti]" for w in words)
        if year:
            term += f" AND {int(year) - 1}:{int(year) + 1}[dp]"
        records = fetch_records(esearch(term))
        # An erratum carries the original's title verbatim ("Correction: ...")
        # and would otherwise win the similarity contest against it.
        records = {k: r for k, r in records.items() if not ERRATUM_RE.match(r["title"])}
        best = max(records.values(), key=lambda r: title_score(seg, r["title"]), default=None)
        if best is None:
            continue
        score = title_score(seg, best["title"])
        if score >= TITLE_MIN_RATIO and _year_ok(year, best["year"]):
            return best, "title", round(score, 3)
    return None, None, None


# volume[(issue)] [:,] [p.] first-page, matched with a lookahead so candidates
# overlap: in "2013, 99, 371-382" both "2013, 99" (rejected: that's the year)
# and "99, 371" are tried.
LOCATOR_RE = re.compile(
    r"(?<!\d)(?=(\d{1,4})\s*(?:\([^)]*\))?\s*[:,]\s*(?:pp?\.?\s*)?([A-Z]?\d{1,6}))"
)
# A page alone ("2001, p. 3267-3271", "E486-E495"), for citations with no volume.
PAGE_RE = re.compile(r"(?:\bpp?\.\s*|,\s*)([A-Z]?\d{2,6})\s*[-–]")


def resolve_by_locator(citation: str, year: str):
    surname = re.match(r"\s*(?:[a-z]\)\s*)?([A-Z][A-Za-z\-À-ſ']+)", citation)
    if not surname or not year:
        return None, None, None
    au = _fold(surname.group(1))
    queries = []
    for m in LOCATOR_RE.finditer(citation):
        volume, page = m.group(1), m.group(2)
        if volume != year:
            queries.append(f"{au}[au] AND {year}[dp] AND {volume}[vi] AND {page}[pg]")
    for m in PAGE_RE.finditer(citation):
        queries.append(f"{au}[au] AND {year}[dp] AND {m.group(1)}[pg]")
    for q in dict.fromkeys(queries):
        ids = esearch(q)
        if len(ids) != 1:
            continue
        rec = fetch_records(ids).get(ids[0])
        if rec and au in _fold(rec["authors"]):
            return rec, "locator", 1.0
    return None, None, None


def main():
    edges = pd.read_csv(INPUT_FILEPATH, dtype=str, keep_default_na=False)
    citations = edges.drop_duplicates("citation_text")
    print(f"{len(citations)} distinct citations across {edges['patent_id'].nunique()} patents")

    resolved = {}
    skipped = 0
    for i, r in enumerate(citations.itertuples(index=False), 1):
        year = r.paper_year.split(".")[0] if r.paper_year else ""
        if NOT_A_PAPER_RE.search(r.citation_text):
            skipped += 1
            continue
        rec, method, score = resolve_by_title(r.citation_text, year)
        if rec is None:
            rec, method, score = resolve_by_locator(r.citation_text, year)
        if rec is not None:
            resolved[r.citation_text] = {**rec, "resolve_method": method, "resolve_score": score}
        if i % 25 == 0:
            print(f"  {i}/{len(citations)} -> {len(resolved)} resolved")

    rows = []
    for e in edges.itertuples(index=False):
        rec = resolved.get(e.citation_text)
        rows.append({
            "patent_id": e.patent_id,
            "external_paper_id": e.external_paper_id,
            "citation_text": e.citation_text,
            "cited_year": e.paper_year.split(".")[0] if e.paper_year else "",
            "resolve_method": rec["resolve_method"] if rec else "",
            "resolve_score": rec["resolve_score"] if rec else "",
            **{c: (rec[c] if rec else "") for c in COLUMNS},
        })
    out = pd.DataFrame(rows)
    out.to_csv(OUTPUT_FILEPATH, index=False)
    n = out["pmid"].ne("").sum()
    print(f"Wrote {len(out)} rows to {OUTPUT_FILEPATH}: {n} resolved to a PMID "
          f"({out.loc[out.pmid.ne(''), 'pmid'].nunique()} distinct papers), "
          f"{skipped} citations skipped as non-literature")


if __name__ == "__main__":
    main()
