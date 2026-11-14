import pandas as pd
import re
from pathlib import Path

PATENT_FILE = Path(
    "data/processed/patent_evidence_with_abstracts.csv"
)

KEYWORD_FILE = Path(
    "kg_keywords.csv"
)

DETAILED_OUTPUT = Path(
    "data/processed/patent_entity_links_title_abstract_detailed.csv"
)

SUMMARY_OUTPUT = Path(
    "data/processed/patent_entity_links_title_abstract_summary.csv"
)


def normalize(text):
    if pd.isna(text):
        return ""

    text = str(text).lower()

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


# -----------------------------
# Load data
# -----------------------------

patents = pd.read_csv(PATENT_FILE)
keywords = pd.read_csv(KEYWORD_FILE)

print("Patents loaded:", len(patents))
print("Keyword rows loaded:", len(keywords))


# -----------------------------
# Keep only included entities
# -----------------------------

keywords = keywords[
    keywords["scope"]
    .astype(str)
    .str.lower()
    == "include"
].copy()

keywords = keywords[
    keywords["entity_type"]
    .isin([
        "taxon",
        "protein"
    ])
].copy()


# -----------------------------
# Combine title + abstract
# -----------------------------

patents["combined_text"] = (
    patents["title"]
    .fillna("")
    .astype(str)
    + " "
    + patents["abstract"]
    .fillna("")
    .astype(str)
)

patents["normalized_text"] = (
    patents["combined_text"]
    .apply(normalize)
)

keywords["normalized_term"] = (
    keywords["search_term"]
    .apply(normalize)
)


# -----------------------------
# Match entities
# -----------------------------

results = []

for _, patent in patents.iterrows():

    text = patent["normalized_text"]

    if not text:
        continue

    normalized_title = normalize(
        patent["title"]
    )

    for _, entity in keywords.iterrows():

        term = entity["normalized_term"]

        if not term:
            continue

        # Avoid extremely short terms
        if len(term) < 3:
            continue

        pattern = (
            r"\b"
            + re.escape(term)
            + r"\b"
        )

        if re.search(pattern, text):

            # Determine where match occurred
            if re.search(
                pattern,
                normalized_title
            ):
                match_location = "title"
            else:
                match_location = "abstract"

            results.append({

                "evidence_id":
                    patent["evidence_id"],

                "patent_id":
                    patent["patent_id"],

                "patent_title":
                    patent["title"],

                "entity_type":
                    entity["entity_type"],

                "entity_id":
                    entity["entity_ids"],

                "canonical_name":
                    entity["canonical_name"],

                "matched_keyword":
                    entity["search_term"],

                "match_location":
                    match_location,

                "relation":
                    "related",

                "match_method":
                    "exact keyword in patent title or abstract"
            })


links = pd.DataFrame(results)


# -----------------------------
# Detailed output
# -----------------------------

if len(links) > 0:

    links = links.drop_duplicates(
        subset=[
            "patent_id",
            "entity_type",
            "entity_id",
            "matched_keyword"
        ]
    )

links.to_csv(
    DETAILED_OUTPUT,
    index=False
)


# -----------------------------
# Human-readable summary
# -----------------------------

if len(links) > 0:

    summary = (
        links.groupby(
            [
                "evidence_id",
                "patent_id",
                "patent_title",
                "entity_type",
                "canonical_name",
                "matched_keyword",
                "relation",
                "match_method"
            ],
            as_index=False
        )
        .agg({
            "entity_id":
                lambda x:
                "|".join(
                    sorted(
                        set(
                            str(v)
                            for v in x
                        )
                    )
                ),

            "match_location":
                lambda x:
                "|".join(
                    sorted(
                        set(
                            str(v)
                            for v in x
                        )
                    )
                )
        })
    )

else:

    summary = pd.DataFrame()


summary.to_csv(
    SUMMARY_OUTPUT,
    index=False
)


# -----------------------------
# Summary
# -----------------------------

print()
print("TITLE + ABSTRACT ENTITY LINKING COMPLETE")
print("----------------------------------------")

print(
    "Detailed KG links:",
    len(links)
)

print(
    "Unique human-readable links:",
    len(summary)
)

print(
    "Patents with at least one entity link:",
    summary["patent_id"].nunique()
    if len(summary) > 0
    else 0
)

if len(summary) > 0:

    print(
        "Links found from title:",
        summary[
            summary["match_location"]
            .str.contains("title", na=False)
        ].shape[0]
    )

    print(
        "Links involving abstract matches:",
        summary[
            summary["match_location"]
            .str.contains("abstract", na=False)
        ].shape[0]
    )

print()
print("Saved detailed file to:")
print(DETAILED_OUTPUT)

print()
print("Saved summary file to:")
print(SUMMARY_OUTPUT)