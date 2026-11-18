import pandas as pd
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

PAPER_FILE = Path("filovirus_papers.csv")
LINK_FILE = Path("data/processed/patent_paper_links.csv")

MATCH_OUTPUT = Path("data/processed/patent_paper_matches.csv")
MISSING_OUTPUT = Path("data/processed/patent_paper_not_in_dataset.csv")


def normalize_title(text):
    text = str(text)

    text = unicodedata.normalize(
        "NFKD", text
    ).encode(
        "ascii", "ignore"
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


papers = pd.read_csv(PAPER_FILE)
links = pd.read_csv(LINK_FILE)

# Normalize titles so punctuation/capitalization do not matter
papers["normalized_title"] = papers["title"].apply(normalize_title)
links["normalized_title"] = links["paper_title"].apply(normalize_title)

results = []

for _, link in links.iterrows():

    citation_title = link["normalized_title"]

    # First try exact normalized title match
    exact = papers[
        papers["normalized_title"] == citation_title
    ]

    if not exact.empty:

        paper = exact.iloc[0]

        results.append({
            "patent_id": link["patent_id"],
            "patent_title": link["patent_title"],
            "paper_title": link["paper_title"],
            "paper_year": link["paper_year"],
            "relation": link["relation"],
            "pmid": paper["pmid"],
            "doi": paper["doi"],
            "dataset_title": paper["title"],
            "match_type": "exact",
            "match_score": 1.0
        })

        continue

    # If exact match fails, look for very similar title
    best_score = 0
    best_paper = None

    for _, paper in papers.iterrows():

        score = SequenceMatcher(
            None,
            citation_title,
            paper["normalized_title"]
        ).ratio()

        if score > best_score:
            best_score = score
            best_paper = paper

    # Conservative fuzzy threshold
    if best_score >= 0.90:

        results.append({
            "patent_id": link["patent_id"],
            "patent_title": link["patent_title"],
            "paper_title": link["paper_title"],
            "paper_year": link["paper_year"],
            "relation": link["relation"],
            "pmid": best_paper["pmid"],
            "doi": best_paper["doi"],
            "dataset_title": best_paper["title"],
            "match_type": "fuzzy",
            "match_score": round(best_score, 3)
        })


matches = pd.DataFrame(results)

# Save confirmed matches
matches.to_csv(
    MATCH_OUTPUT,
    index=False
)

# Find citations that did not match the existing paper dataset
matched_pairs = set(
    zip(
        matches["patent_id"],
        matches["paper_title"]
    )
)

missing = links[
    ~links.apply(
        lambda row: (
            row["patent_id"],
            row["paper_title"]
        ) in matched_pairs,
        axis=1
    )
].copy()

missing = missing.drop(
    columns=["normalized_title"],
    errors="ignore"
)

missing.to_csv(
    MISSING_OUTPUT,
    index=False
)

print("Patent-paper comparison complete.")
print()
print("Total patent-paper citations:", len(links))
print("Matches already in paper dataset:", len(matches))
print("Cited papers not currently in dataset:", len(missing))

print("\nMATCHED PAPERS:")
print(
    matches[
        [
            "patent_id",
            "paper_title",
            "pmid",
            "match_type"
        ]
    ].to_string(index=False)
)

print(f"\nSaved matches to: {MATCH_OUTPUT}")
print(f"Saved missing papers to: {MISSING_OUTPUT}")