import pandas as pd
from pathlib import Path

OLD_MATCHES = Path(
    "data/processed/patent_paper_matches.csv"
)

NEW_MATCHES = Path(
    "data/processed/new_patent_citations_matched_to_team.csv"
)

OLD_EXTERNAL = Path(
    "data/processed/external_patent_paper_edges.csv"
)

NEW_EXTERNAL = Path(
    "data/processed/new_external_patent_paper_edges.csv"
)

MASTER_MATCHES = Path(
    "data/processed/master_patent_team_paper_edges.csv"
)

MASTER_EXTERNAL = Path(
    "data/processed/master_patent_external_literature_edges.csv"
)


# -----------------------------
# 1. Combine edges to team papers
# -----------------------------

old_matches = pd.read_csv(OLD_MATCHES)
new_matches = pd.read_csv(NEW_MATCHES)

# Standardize old column names
old_matches = old_matches.rename(
    columns={
        "paper_title": "team_paper_title",
        "paper_year": "team_paper_year"
    }
)

team_edges = pd.concat(
    [
        old_matches,
        new_matches
    ],
    ignore_index=True,
    sort=False
)

team_edges = team_edges.drop_duplicates(
    subset=[
        "patent_id",
        "pmid"
    ]
)

team_edges.to_csv(
    MASTER_MATCHES,
    index=False
)


# -----------------------------
# 2. Combine external-literature edges
# -----------------------------

old_external = pd.read_csv(OLD_EXTERNAL)
new_external = pd.read_csv(NEW_EXTERNAL)

# Standardize naming
old_external = old_external.rename(
    columns={
        "paper_title": "citation_text"
    }
)

external_edges = pd.concat(
    [
        old_external,
        new_external
    ],
    ignore_index=True,
    sort=False
)

external_edges = external_edges.drop_duplicates(
    subset=[
        "patent_id",
        "citation_text"
    ]
)

external_edges.to_csv(
    MASTER_EXTERNAL,
    index=False
)


# -----------------------------
# Summary
# -----------------------------

print()
print("MASTER PATENT → PAPER FILES COMPLETE")
print("------------------------------------")

print(
    "Patent → existing team paper edges:",
    len(team_edges)
)

print(
    "Patent → external literature edges:",
    len(external_edges)
)

print(
    "Patents connected to team papers:",
    team_edges["patent_id"].nunique()
)

print(
    "Patents connected to external literature:",
    external_edges["patent_id"].nunique()
)

print()
print("Saved team-paper edges to:")
print(MASTER_MATCHES)

print()
print("Saved external-literature edges to:")
print(MASTER_EXTERNAL)