import pandas as pd
import re
from pathlib import Path

OLD_EVIDENCE = Path("data/processed/patent_evidence.csv")
EXPANDED_RAW = Path("data/processed/all_patents_expanded.csv")

OUTPUT = Path("data/processed/patent_evidence_expanded.csv")


# --------------------------------------------------
# Helper functions
# --------------------------------------------------

def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def normalize_doc_id(value):
    """
    Convert things like:
    US 20180291088 A1
    US20180291088A1

    into one comparable form:
    US20180291088A1
    """
    return re.sub(
        r"\s+",
        "",
        clean_text(value)
    ).upper()


def make_google_patents_url(document_id):
    compact = normalize_doc_id(document_id)

    return f"https://patents.google.com/patent/{compact}/en"


# --------------------------------------------------
# Load files
# --------------------------------------------------

old = pd.read_csv(OLD_EVIDENCE)

expanded = pd.read_csv(EXPANDED_RAW)

print("Old evidence rows:", len(old))
print("Expanded patent rows:", len(expanded))


# --------------------------------------------------
# Find the document-ID column in old evidence
# --------------------------------------------------

possible_old_id_columns = [
    "patent_id",
    "document_id",
    "Document ID",
    "patent_number",
    "publication_number"
]

old_doc_col = None

for col in possible_old_id_columns:
    if col in old.columns:
        old_doc_col = col
        break

if old_doc_col is None:
    raise ValueError(
        "Could not find the patent document ID column "
        "inside patent_evidence.csv.\n"
        f"Columns found: {list(old.columns)}"
    )


# Expanded USPTO export should use Document ID
if "Document ID" not in expanded.columns:
    raise ValueError(
        "Could not find 'Document ID' in all_patents_expanded.csv.\n"
        f"Columns found: {list(expanded.columns)}"
    )


# --------------------------------------------------
# Normalize IDs for comparison
# --------------------------------------------------

old["normalized_document_id"] = (
    old[old_doc_col]
    .apply(normalize_doc_id)
)

expanded["normalized_document_id"] = (
    expanded["Document ID"]
    .apply(normalize_doc_id)
)


old_ids = set(
    old["normalized_document_id"]
)


# --------------------------------------------------
# Identify the 100 new patents
# --------------------------------------------------

new_patents = expanded[
    ~expanded["normalized_document_id"].isin(old_ids)
].copy()

print("New patents found:", len(new_patents))


# --------------------------------------------------
# Determine the next PATENT_XXXX ID
# --------------------------------------------------

existing_numbers = []

for evidence_id in old["evidence_id"]:

    match = re.search(
        r"PATENT_(\d+)",
        str(evidence_id)
    )

    if match:
        existing_numbers.append(
            int(match.group(1))
        )


if existing_numbers:
    next_number = max(existing_numbers) + 1
else:
    next_number = len(old) + 1


print("Next evidence ID:", f"PATENT_{next_number:04d}")


# --------------------------------------------------
# Build evidence rows for new patents
# --------------------------------------------------

new_rows = []


for _, row in new_patents.iterrows():

    document_id = clean_text(
        row["Document ID"]
    )

    # USPTO export title column
    title = ""

    for candidate in [
        "Title",
        "Invention Title",
        "title"
    ]:
        if candidate in expanded.columns:
            title = clean_text(
                row[candidate]
            )
            break


    # Date
    date = ""

    for candidate in [
        "Publication Date",
        "Date",
        "publication_date"
    ]:
        if candidate in expanded.columns:
            date = clean_text(
                row[candidate]
            )
            break


    # Abstract
    abstract = ""

    for candidate in [
        "Abstract",
        "abstract"
    ]:
        if candidate in expanded.columns:
            abstract = clean_text(
                row[candidate]
            )
            break


    text_for_matching = (
        title + " " + abstract
    ).lower()


    # Basic taxon assignment
    matched_keyword = ""
    matched_entity_type = ""
    matched_entity_id = ""

    if "marburg" in text_for_matching:

        matched_keyword = "Marburg virus"
        matched_entity_type = "taxon"
        matched_entity_id = "3052505"

    elif (
        "ebola" in text_for_matching
        or "ebolavirus" in text_for_matching
    ):

        matched_keyword = "Ebola virus"
        matched_entity_type = "taxon"
        matched_entity_id = "3052462"

    elif "filovirus" in text_for_matching:

        matched_keyword = "filovirus"
        matched_entity_type = "taxon"
        matched_entity_id = ""


    evidence_id = (
        f"PATENT_{next_number:04d}"
    )

    next_number += 1


    new_rows.append({

        "evidence_id": evidence_id,

        old_doc_col: document_id,

        "source_type": "patent",

        "title": title,

        "date": date,

        "url": make_google_patents_url(
            document_id
        ),

        "matched_entity_type":
            matched_entity_type,

        "matched_entity_id":
            matched_entity_id,

        "matched_keyword":
            matched_keyword,

        "match_method":
            "broader USPTO abstract search",

        "relation":
            "related"
    })


new_evidence = pd.DataFrame(
    new_rows
)


# --------------------------------------------------
# Make columns compatible with old evidence
# --------------------------------------------------

for col in old.columns:

    if col not in new_evidence.columns:
        new_evidence[col] = ""


for col in new_evidence.columns:

    if col not in old.columns:
        old[col] = ""


# Same column order
new_evidence = new_evidence[
    old.columns
]


# --------------------------------------------------
# Combine old + new
# --------------------------------------------------

combined = pd.concat(
    [
        old,
        new_evidence
    ],
    ignore_index=True
)


# Remove helper column if present
combined = combined.drop(
    columns=[
        "normalized_document_id"
    ],
    errors="ignore"
)


# --------------------------------------------------
# Save
# --------------------------------------------------

combined.to_csv(
    OUTPUT,
    index=False
)


print()
print("EXPANDED PATENT EVIDENCE COMPLETE")
print("--------------------------------")

print(
    "Original evidence patents:",
    len(old)
)

print(
    "New evidence patents added:",
    len(new_evidence)
)

print(
    "Total evidence patents:",
    len(combined)
)

print()
print(
    "Saved to:",
    OUTPUT
)