import pandas as pd
from pathlib import Path

RAW_DIR = Path("data/raw")

OLD_FILES = [
    "ebola_A61K39-12_raw.csv",
    "ebola_C07K16-10_raw.csv",
    "marburg_A61K39-12_raw.csv",
    "marburg_C07K16-10_raw.csv"
]

NEW_FILES = [
    "filovirus_raw.csv",
    "ebolavirus_raw.csv",
    "marburgvirus_raw.csv"
]

# -----------------------------------
# Load original patent searches
# -----------------------------------

old_frames = []

for filename in OLD_FILES:

    path = RAW_DIR / filename

    df = pd.read_csv(path)

    df["source_search"] = filename

    old_frames.append(df)

old = pd.concat(
    old_frames,
    ignore_index=True
)

# Deduplicate original dataset
old_unique = old.drop_duplicates(
    subset=["Document ID"]
).copy()

old_ids = set(
    old_unique["Document ID"]
    .astype(str)
    .str.strip()
)

# -----------------------------------
# Load new broader searches
# -----------------------------------

new_frames = []

for filename in NEW_FILES:

    path = RAW_DIR / filename

    df = pd.read_csv(path)

    df["source_search"] = filename

    new_frames.append(df)

new = pd.concat(
    new_frames,
    ignore_index=True
)

new_unique = new.drop_duplicates(
    subset=["Document ID"]
).copy()

# -----------------------------------
# Find patents that are actually new
# -----------------------------------

new_unique["Document ID"] = (
    new_unique["Document ID"]
    .astype(str)
    .str.strip()
)

new_only = new_unique[
    ~new_unique["Document ID"].isin(old_ids)
].copy()

# -----------------------------------
# Combine old + new
# -----------------------------------

combined = pd.concat(
    [
        old_unique,
        new_only
    ],
    ignore_index=True
)

combined = combined.drop_duplicates(
    subset=["Document ID"]
)

# -----------------------------------
# Save
# -----------------------------------

new_only.to_csv(
    "data/processed/new_patents_from_expansion.csv",
    index=False
)

combined.to_csv(
    "data/processed/all_patents_expanded.csv",
    index=False
)

# -----------------------------------
# Summary
# -----------------------------------

print()
print("PATENT EXPANSION SUMMARY")
print("------------------------")

print("Original unique patents:", len(old_unique))
print("Rows from new searches:", len(new))
print("Unique patents in new searches:", len(new_unique))
print("Actually NEW patents:", len(new_only))
print("Expanded patent database:", len(combined))

print()
print("Saved new patents to:")
print("data/processed/new_patents_from_expansion.csv")

print()
print("Saved full expanded dataset to:")
print("data/processed/all_patents_expanded.csv")