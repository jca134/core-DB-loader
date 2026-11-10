"""
The shared KG search vocabulary (`kg_keywords.csv`) and the matcher that turns
free text -- a grant abstract, a patent title -- into candidate links against
entities already in core.*.

The file is exported from the live DB by hand (see kg_keywords_README.md): one
row per search term, carrying the term, its type, a scope flag, and the core
PKs it came from. Only `scope = 'include'` rows are used for matching; `review`
and `exclude` rows are kept in the file as an auditable record and ignored here.

**Why entity_ids is not trusted for most types.** The file's `entity_ids`
column holds core primary keys as of the day it was exported. Those are
surrogate keys that every ETL rerun regenerates -- a protein_id or antibody_id
from a September export points at a different row today. So this module
resolves by stable natural key at match time instead:

    taxon     entity_ids IS stable -- real NCBI taxon ids, not surrogates
    antibody  resolved by core.antibody.name / alias
    gene      resolved by core.gene.gene_symbol (UCSC's 9-row reference model)
    protein   deliberately not resolved -- see below

`protein` terms are mapped to core.gene rather than core.protein. core.protein
has ~64k accession-keyed rows, so "this patent is about GP" would otherwise
become 300 near-identical links; core.gene is UCSC's single authoritative
model, one row per gene, which is the granularity the claim is actually about.
The gene symbol is stable even though gene_id is not, so resolution happens at
ETL time and etl_evidence must rerun after etl_ucsc.
"""

import os
import re

import pandas as pd
from sqlalchemy import text

from core_data_loader.common.db_utils import engine, blank_to_none

# Exported from the live DB; identical in overview/Context/ and exports/ apart
# from a BOM. Both are git-ignored, so the path is a constant rather than a CLI
# argument -- if it moves, change it here.
KEYWORDS_PATH = "overview/Context/kg_keywords.csv"
FALLBACK_PATH = "exports/kg_keywords.csv"

# Confidence written to core.evidence_link, and from there to core.kg_edge.weight.
# An exact identifier is ground truth; a keyword in a title is a stronger signal
# than one buried in an abstract; the search a patent file came from is weaker
# still, since it is metadata about the query rather than text in the record.
CONFIDENCE = {
    "identifier": 1.00,
    "title": 0.70,
    "abstract": 0.50,
    "query_scope": 0.40,
}

# kg_keywords flags terms whose names are ambiguous without organism context
# ("L", "GP", "NP" are gene symbols but also ordinary words). Those links are
# kept -- dropping them loses real matches -- but halved in confidence and
# marked requires_context so a reviewer can find them.
AMBIGUOUS_PENALTY = 0.5


# Curated additions to the exported vocabulary, hand-seeded here rather than
# edited into kg_keywords.csv -- that file is generated from the live DB, so a
# manual edit would be silently lost on the next export. Same pattern as
# common/taxon_conditions.py: domain knowledge lives in code.
#
# WHY THESE EXIST. kg_keywords carries the formal names ("Ebola virus",
# "Orthoebolavirus zairense") but not the shorthand researchers actually write.
# Measured against the real data: of the 53 grants that ended up with no edge at
# all, 52 mention bare "Ebola" and only 6 use the phrase "Ebola virus". Without
# these terms those grants are isolated nodes -- present in the graph, reachable
# from nothing.
#
# Each maps to the broadest taxon honestly supported. "Ebola" in a grant title
# does not mean Zaire specifically, so it maps to the Ebolavirus genus and
# reaches species and strains through core.taxon's tree, not by guessing.
# `Filoviridae` is in the exported file but scoped `review`; its README states
# the project team may change scope flags, and the family node is the correct
# target for a grant about "filovirus glycoproteins".
#
# requires_context is False for all of these: unlike "L" or "N", none of these
# words is ambiguous in a biomedical corpus.
SUPPLEMENTAL_TERMS = [
    # (search_term, entity_type, entity_id, term_type)
    ("Ebola", "taxon", 186536, "common_name"),        # Ebolavirus (genus)
    ("Marburg", "taxon", 186537, "common_name"),      # Marburgvirus (genus)
    ("filovirus", "taxon", 11266, "common_name"),     # Filoviridae (family)
    ("filoviruses", "taxon", 11266, "common_name"),
    ("filoviral", "taxon", 11266, "common_name"),
    # One token, so "filovirus" never matches inside it; three patent titles
    # ("use as panfilovirus vaccine") are otherwise unlinked.
    ("panfilovirus", "taxon", 11266, "common_name"),
    ("Filoviridae", "taxon", 11266, "db_name"),
]


def load_keywords(path: str = None) -> pd.DataFrame:
    """The `include`-scope rows of kg_keywords.csv, one per search term."""
    if path is None:
        path = KEYWORDS_PATH if os.path.exists(KEYWORDS_PATH) else FALLBACK_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"KG keyword vocabulary not found at {KEYWORDS_PATH} or {FALLBACK_PATH}. "
            "It is exported from the live DB by hand; see kg_keywords_README.md."
        )
    # utf-8-sig: the exports/ copy carries a BOM, which would otherwise end up
    # inside the first column name.
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    df = df[df["scope"] == "include"].copy()
    df["requires_context"] = df["requires_context"].str.lower() == "true"
    return df.reset_index(drop=True)


def _word_boundary_pattern(term: str) -> re.Pattern:
    r"""
    Match `term` as a whole token, case-insensitively.

    A hyphen separates tokens; only letters and digits bind. That is what lets
    "VP40" match "VP40-specific antibodies" and "GP" match "GP-directed", which
    is how grant and patent titles usually write them -- while still refusing
    "xxVP40xx" and matching "1-5D" but not "1-5DX".

    \b alone would not do: it does not fire between a word character and the
    '-' inside a term like "12B5-1-1", so hyphenated antibody names behave
    unpredictably. Explicit lookarounds keep the rule the same at both ends of
    every term, hyphenated or not.
    """
    return re.compile(rf"(?<![0-9A-Za-z]){re.escape(term)}(?![0-9A-Za-z])", re.IGNORECASE)


class KeywordMatcher:
    """
    Resolves kg_keywords terms to live core.* ids once, then matches text
    against them. Build one per ETL run -- the resolution step queries the DB.
    """

    def __init__(self, keywords: pd.DataFrame = None):
        self.keywords = load_keywords() if keywords is None else keywords
        self.terms = self._resolve()

    def _resolve(self) -> list[dict]:
        antibody_by_name = {}
        gene_by_symbol = {}
        taxon_ids = set()
        with engine.begin() as conn:
            for aid, name, alias in conn.execute(
                text("SELECT antibody_id, name, alias FROM core.antibody")
            ):
                for label in [name] + (alias.split(",") if alias else []):
                    label = blank_to_none(label)
                    if label:
                        antibody_by_name.setdefault(label.lower(), aid)
            for gid, symbol in conn.execute(
                text("SELECT gene_id, gene_symbol FROM core.gene")
            ):
                if symbol:
                    gene_by_symbol.setdefault(symbol.lower(), gid)
            taxon_ids = {
                row[0] for row in conn.execute(text("SELECT taxon_id FROM core.taxon"))
            }

        gene_by_canonical = self._gene_by_canonical(gene_by_symbol)

        resolved = []
        for r in self.keywords.itertuples(index=False):
            entity_type, entity_id = self._resolve_one(
                r, antibody_by_name, gene_by_symbol, gene_by_canonical, taxon_ids
            )
            if entity_id is None:
                continue
            resolved.append({
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "search_term": r.search_term,
                "term_type": r.term_type,
                "requires_context": bool(r.requires_context),
                "pattern": _word_boundary_pattern(r.search_term),
            })

        # Supplemental terms resolve the same way the file's own taxon rows do
        # -- by real NCBI taxon id, kept only if that taxon is actually loaded.
        for term, entity_type, entity_id, term_type in SUPPLEMENTAL_TERMS:
            if entity_type != "taxon" or entity_id not in taxon_ids:
                continue
            resolved.append({
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "search_term": term,
                "term_type": term_type,
                "requires_context": False,
                "pattern": _word_boundary_pattern(term),
            })
        return resolved

    def _gene_by_canonical(self, gene_by_symbol: dict) -> dict:
        """
        canonical_name -> gene_id, via the vocabulary's own db_gene_label rows.

        A product label ("envelope glycoprotein", "viral protein 40") is not a
        gene symbol, so it never matches core.gene directly. But kg_keywords
        always pairs such a label with a db_gene_label row under the same
        canonical_name giving the symbol -- "envelope glycoprotein" sits beside
        "GP". Following that pairing is what lets a patent titled "EBOLAVIRUS
        SURFACE GLYCOPROTEIN PEPTIDES" reach the GP gene.

        A canonical_name can carry more than one symbol ("Nucleocapsid protein"
        -> both N and NP); the first that exists in core.gene wins, and a
        canonical whose symbols are all absent is simply left unresolved.
        """
        labels = self.keywords[self.keywords["term_type"] == "db_gene_label"]
        mapping = {}
        for r in labels.itertuples(index=False):
            gene_id = gene_by_symbol.get(r.search_term.lower())
            if gene_id is not None:
                mapping.setdefault(r.canonical_name.lower(), gene_id)
        return mapping

    @staticmethod
    def _resolve_one(r, antibody_by_name, gene_by_symbol, gene_by_canonical, taxon_ids):
        if r.entity_type == "taxon":
            # The one type whose entity_ids are real external ids, so they can
            # be used directly -- but only if the taxon is actually loaded.
            for raw in str(r.entity_ids).split("|"):
                raw = blank_to_none(raw)
                if raw and raw.isdigit() and int(raw) in taxon_ids:
                    return "taxon", int(raw)
            return "taxon", None

        if r.entity_type == "antibody":
            return "antibody", antibody_by_name.get(r.search_term.lower())

        if r.entity_type in ("gene", "protein"):
            # Protein terms resolve against the gene model (see module docstring),
            # by symbol where the term is one, otherwise through the canonical
            # name's db_gene_label sibling.
            return "gene", (gene_by_symbol.get(r.search_term.lower())
                            or gene_by_canonical.get(r.canonical_name.lower()))

        return r.entity_type, None

    def match(self, text_value: str, match_field: str) -> list[dict]:
        """
        Every vocabulary term occurring in `text_value`, as candidate links.
        One dict per (term, entity) hit -- the caller collapses them into one
        evidence_link per entity with a support row each.
        """
        text_value = blank_to_none(text_value)
        if not text_value:
            return []
        hits = []
        for t in self.terms:
            if not t["pattern"].search(text_value):
                continue
            confidence = CONFIDENCE[match_field]
            if t["requires_context"]:
                confidence *= AMBIGUOUS_PENALTY
            hits.append({
                "entity_type": t["entity_type"],
                "entity_id": t["entity_id"],
                "matched_term": t["search_term"],
                "term_type": t["term_type"],
                "requires_context": t["requires_context"],
                "match_field": match_field,
                "match_method": _match_method(t["term_type"]),
                "confidence": round(confidence, 2),
            })
        return hits


def _match_method(term_type: str) -> str:
    """kg_keywords term_type -> core.evidence_link_support.match_method."""
    if term_type == "db_gene_label":
        return "gene_label"
    if term_type and term_type.endswith("alias"):
        return "synonym"
    return "keyword"
