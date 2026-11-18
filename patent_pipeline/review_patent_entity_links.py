import pandas as pd
from pathlib import Path

INPUT = Path(
    "data/processed/patent_entity_links_title_abstract_summary.csv"
)

OUTPUT = Path(
    "data/processed/patent_entity_links_review.csv"
)

df = pd.read_csv(INPUT)

# Default everything to medium confidence
df["review_status"] = "review"
df["review_reason"] = ""

# -----------------------------------
# High-confidence taxon matches
# -----------------------------------

taxon_mask = (
    df["entity_type"]
    .astype(str)
    .str.lower()
    == "taxon"
)

df.loc[
    taxon_mask,
    "review_status"
] = "likely_valid"

df.loc[
    taxon_mask,
    "review_reason"
] = "explicit virus/taxon keyword match"


# -----------------------------------
# Generic protein terms need review
# -----------------------------------

generic_terms = [
    "glycoprotein",
    "nucleoprotein",
    "protein"
]

generic_mask = (
    df["matched_keyword"]
    .astype(str)
    .str.lower()
    .isin(generic_terms)
)

df.loc[
    generic_mask,
    "review_status"
] = "needs_review"

df.loc[
    generic_mask,
    "review_reason"
] = "generic protein term may refer to multiple biological entities"


# -----------------------------------
# Title matches are stronger
# than abstract-only matches
# -----------------------------------

title_mask = (
    df["match_location"]
    .astype(str)
    .str.contains(
        "title",
        case=False,
        na=False
    )
)

df.loc[
    title_mask & ~generic_mask,
    "review_status"
] = "likely_valid"

df.loc[
    title_mask & ~generic_mask,
    "review_reason"
] = "entity appears directly in patent title"


# -----------------------------------
# Abstract-only protein matches
# should be manually checked
# -----------------------------------

abstract_only_mask = (
    df["entity_type"]
    .astype(str)
    .str.lower()
    == "protein"
) & (
    df["match_location"]
    .astype(str)
    .str.lower()
    == "abstract"
)

df.loc[
    abstract_only_mask,
    "review_status"
] = "needs_review"

df.loc[
    abstract_only_mask,
    "review_reason"
] = "protein matched only in abstract; context should be checked"


# -----------------------------------
# Save
# -----------------------------------

df.to_csv(
    OUTPUT,
    index=False
)

print()
print("ENTITY LINK REVIEW FILE CREATED")
print("-------------------------------")

print(
    "Total links:",
    len(df)
)

print(
    "Likely valid:",
    (
        df["review_status"]
        == "likely_valid"
    ).sum()
)

print(
    "Needs review:",
    (
        df["review_status"]
        == "needs_review"
    ).sum()
)

print(
    "Other review:",
    (
        df["review_status"]
        == "review"
    ).sum()
)

print()
print("Saved to:", OUTPUT)