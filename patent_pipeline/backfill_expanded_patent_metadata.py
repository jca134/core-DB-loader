import pandas as pd
import re
from pathlib import Path

EVIDENCE_FILE = Path("data/processed/patent_evidence_expanded.csv")
RAW_FILE = Path("data/processed/all_patents_expanded.csv")

OUTPUT = Path("data/processed/patent_evidence_expanded.csv")


def normalize_id(value):
    if pd.isna(value):
        return ""

    return re.sub(
        r"\s+",
        "",
        str(value)
    ).upper()


evidence = pd.read_csv(EVIDENCE_FILE)
raw = pd.read_csv(RAW_FILE)

print("Evidence patents:", len(evidence))
print("Raw expanded patents:", len(raw))

print()
print("Raw columns:")
print(list(raw.columns))
print()


# Normalize patent IDs
evidence["_id"] = evidence["patent_id"].apply(normalize_id)
raw["_id"] = raw["Document ID"].apply(normalize_id)


# Try common USPTO column names
column_candidates = {

    "inventor": [
        "Inventor Name",
        "Inventor",
        "inventor"
    ],

    "assignee": [
        "Assignee Name",
        "Assignee",
        "assignee"
    ],

    "application_number": [
        "Application Number",
        "Application #",
        "application_number"
    ],

    "filing_date": [
        "Filing Date",
        "filing_date"
    ],

    "family_id": [
        "Family ID",
        "family_id"
    ]
}


found_columns = {}

for target, candidates in column_candidates.items():

    found = None

    for candidate in candidates:

        if candidate in raw.columns:
            found = candidate
            break

    found_columns[target] = found

    print(
        f"{target}:",
        found if found else "NOT FOUND"
    )


# Merge raw metadata onto evidence
for target, raw_column in found_columns.items():

    if raw_column is None:
        continue

    lookup = (
        raw[
            ["_id", raw_column]
        ]
        .drop_duplicates(
            subset=["_id"]
        )
        .set_index("_id")[raw_column]
    )

    # Make sure target column exists
    if target not in evidence.columns:
        evidence[target] = ""

    # Fill only blank cells
    blank = (
        evidence[target].isna()
        | (evidence[target].astype(str).str.strip() == "")
    )

    evidence.loc[
        blank,
        target
    ] = evidence.loc[
        blank,
        "_id"
    ].map(lookup)


# Remove helper column
evidence = evidence.drop(
    columns=["_id"]
)

evidence.to_csv(
    OUTPUT,
    index=False
)

print()
print("METADATA BACKFILL COMPLETE")
print("--------------------------")

print(
    "Patents with inventor:",
    evidence["inventor"].notna().sum()
)

print()
print("Saved to:", OUTPUT)