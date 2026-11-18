import pandas as pd
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

CITATION_FILE = Path(
    "data/processed/new_patent_paper_citations_raw.csv"
)

TEAM_PAPERS_FILE = Path(
    "filovirus_papers.csv"
)

MATCHED_OUTPUT = Path(
    "data/processed/new_patent_citations_matched_to_team.csv"
)

EXTERNAL_NODES_OUTPUT = Path(
    "data/processed/new_external_paper_nodes.csv"
)

EXTERNAL_EDGES_OUTPUT = Path(
    "data/processed/new_external_patent_paper_edges.csv"
)


def normalize_text(text):
    if pd.isna(text):
        return ""

    text = str(text)

    text = unicodedata.normalize(
        "NFKD",
        text
    ).encode(
        "ascii",
        "ignore"
    ).decode()

    text = text.lower()

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text
    )

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


def extract_possible_title(citation):
    """
    This is intentionally conservative.

    We keep the full citation text, but also create
    a cleaned version for approximate matching.
    """

    citation = str(citation).strip()

    # Remove leading/trailing quotes
    citation = citation.strip('"').strip("'")

    return citation


citations = pd.read_csv(CITATION_FILE)
papers = pd.read_csv(TEAM_PAPERS_FILE)

print("Patent-literature citations loaded:", len(citations))
print("Team papers loaded:", len(papers))
print()


# --------------------------------------------------
# Normalize team paper titles
# --------------------------------------------------

papers["normalized_title"] = (
    papers["title"]
    .apply(normalize_text)
)


# --------------------------------------------------
# Process every patent citation
# --------------------------------------------------

matched_rows = []
external_rows = []

for i, citation in citations.iterrows():

    citation_text = extract_possible_title(
        citation["citation_text"]
    )

    normalized_citation = normalize_text(
        citation_text
    )

    best_score = 0
    best_paper = None

    # ----------------------------------------------
    # Look for team-paper title inside citation
    # ----------------------------------------------

    for _, paper in papers.iterrows():

        paper_title = paper["normalized_title"]

        if not paper_title:
            continue

        # Strongest case:
        # full paper title appears inside citation text
        if (
            len(paper_title) >= 20
            and paper_title in normalized_citation
        ):
            best_score = 1.0
            best_paper = paper
            break

        # Otherwise compare normalized strings
        score = SequenceMatcher(
            None,
            normalized_citation,
            paper_title
        ).ratio()

        if score > best_score:
            best_score = score
            best_paper = paper


    # ----------------------------------------------
    # Consider >= 0.90 a likely team-paper match
    # ----------------------------------------------

    if best_paper is not None and best_score >= 0.90:

        matched_rows.append({
            "patent_id":
                citation["patent_id"],

            "patent_title":
                citation["patent_title"],

            "citation_text":
                citation_text,

            "pmid":
                best_paper.get(
                    "pmid",
                    ""
                ),

            "pmcid":
                best_paper.get(
                    "pmcid",
                    ""
                ),

            "doi":
                best_paper.get(
                    "doi",
                    ""
                ),

            "team_paper_title":
                best_paper["title"],

            "team_paper_year":
                best_paper.get(
                    "year",
                    ""
                ),

            "relation":
                "cites",

            "match_score":
                round(best_score, 3),

            "match_type":
                (
                    "title contained in citation"
                    if best_score == 1.0
                    else "fuzzy"
                )
        })

    else:

        external_rows.append({
            "patent_id":
                citation["patent_id"],

            "patent_title":
                citation["patent_title"],

            "citation_text":
                citation_text,

            "paper_year":
                citation.get(
                    "paper_year",
                    ""
                ),

            "relation":
                "cites"
        })


# --------------------------------------------------
# Save team-paper matches
# --------------------------------------------------

matched = pd.DataFrame(
    matched_rows
)

if len(matched) > 0:

    matched = matched.drop_duplicates(
        subset=[
            "patent_id",
            "pmid",
            "citation_text"
        ]
    )

matched.to_csv(
    MATCHED_OUTPUT,
    index=False
)


# --------------------------------------------------
# Build standalone external literature nodes
# --------------------------------------------------

external = pd.DataFrame(
    external_rows
)

if len(external) > 0:

    # One node per unique citation text
    nodes = (
        external[
            [
                "citation_text",
                "paper_year"
            ]
        ]
        .drop_duplicates(
            subset=["citation_text"]
        )
        .reset_index(drop=True)
    )

    nodes["external_paper_id"] = [
        f"EXT_NEW_{i+1:04d}"
        for i in range(len(nodes))
    ]

    nodes = nodes[
        [
            "external_paper_id",
            "citation_text",
            "paper_year"
        ]
    ]

    edges = external.merge(
        nodes,
        on=[
            "citation_text",
            "paper_year"
        ],
        how="left"
    )

    edges = edges[
        [
            "patent_id",
            "patent_title",
            "external_paper_id",
            "citation_text",
            "paper_year",
            "relation"
        ]
    ]

else:

    nodes = pd.DataFrame(
        columns=[
            "external_paper_id",
            "citation_text",
            "paper_year"
        ]
    )

    edges = pd.DataFrame(
        columns=[
            "patent_id",
            "patent_title",
            "external_paper_id",
            "citation_text",
            "paper_year",
            "relation"
        ]
    )


nodes.to_csv(
    EXTERNAL_NODES_OUTPUT,
    index=False
)

edges.to_csv(
    EXTERNAL_EDGES_OUTPUT,
    index=False
)


# --------------------------------------------------
# Summary
# --------------------------------------------------

print("CITATION PROCESSING COMPLETE")
print("----------------------------")

print(
    "Total citation edges:",
    len(citations)
)

print(
    "Matched to existing team papers:",
    len(matched)
)

print(
    "External citation edges:",
    len(edges)
)

print(
    "Unique external literature nodes:",
    len(nodes)
)

print()
print(
    "Saved team matches to:",
    MATCHED_OUTPUT
)

print(
    "Saved external nodes to:",
    EXTERNAL_NODES_OUTPUT
)

print(
    "Saved external edges to:",
    EXTERNAL_EDGES_OUTPUT
)