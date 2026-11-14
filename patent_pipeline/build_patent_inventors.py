import pandas as pd
from pathlib import Path

INPUT = Path("data/processed/patent_evidence_expanded.csv")
OUTPUT = Path("data/processed/patent_inventors_expanded.csv")


def clean_inventor_name(name):
    if pd.isna(name):
        return ""

    name = str(name).strip()

    # Remove "et al."
    name = name.replace(" et al.", "")
    name = name.replace(" et al", "")

    # USPTO format is often:
    # Lastname; Firstname
    # Example:
    # Ahmed; Mahiuddin
    #
    # Convert to:
    # Mahiuddin Ahmed

    if ";" in name:
        parts = name.split(";")

        if len(parts) >= 2:
            last = parts[0].strip()
            first = parts[1].strip()

            name = f"{first} {last}".strip()

    return name


df = pd.read_csv(INPUT)

print("Patents loaded:", len(df))

if "inventor" not in df.columns:
    raise ValueError(
        "Could not find inventor column.\n"
        f"Columns found: {list(df.columns)}"
    )

results = []

for _, row in df.iterrows():

    inventor = clean_inventor_name(
        row["inventor"]
    )

    # Skip patents where inventor is missing
    if not inventor:
        continue

    results.append({
        "evidence_id": row["evidence_id"],
        "patent_id": row["patent_id"],
        "patent_title": row["title"],
        "inventor_name": inventor,
        "relation": "invented_by"
    })


inventors = pd.DataFrame(results)

inventors = inventors.drop_duplicates(
    subset=[
        "patent_id",
        "inventor_name"
    ]
)

inventors.to_csv(
    OUTPUT,
    index=False
)

print()
print("PATENT → INVENTOR LINKS COMPLETE")
print("--------------------------------")

print(
    "Patent-inventor links:",
    len(inventors)
)

print(
    "Patents with displayed inventor:",
    inventors["patent_id"].nunique()
)

print(
    "Unique displayed inventor names:",
    inventors["inventor_name"].nunique()
)

print()
print(
    "Saved to:",
    OUTPUT
)