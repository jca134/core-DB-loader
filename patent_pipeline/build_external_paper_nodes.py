import pandas as pd
from pathlib import Path

INPUT = Path("data/processed/patent_paper_not_in_dataset.csv")

PAPER_NODES = Path("data/processed/external_paper_nodes.csv")
PATENT_PAPER_EDGES = Path("data/processed/external_patent_paper_edges.csv")

df = pd.read_csv(INPUT)

# ----------------------------
# 1. Create standalone paper nodes
# ----------------------------

paper_nodes = (
    df[
        [
            "paper_title",
            "paper_year"
        ]
    ]
    .drop_duplicates()
    .reset_index(drop=True)
)

paper_nodes["external_paper_id"] = [
    f"EXT_PAPER_{i+1:04d}"
    for i in range(len(paper_nodes))
]

# Put ID first
paper_nodes = paper_nodes[
    [
        "external_paper_id",
        "paper_title",
        "paper_year"
    ]
]

paper_nodes.to_csv(
    PAPER_NODES,
    index=False
)

# ----------------------------
# 2. Connect patents to those nodes
# ----------------------------

edges = df.merge(
    paper_nodes,
    on=[
        "paper_title",
        "paper_year"
    ],
    how="left"
)

edges = edges[
    [
        "patent_id",
        "external_paper_id",
        "paper_title",
        "paper_year",
        "relation"
    ]
]

edges.to_csv(
    PATENT_PAPER_EDGES,
    index=False
)

print()
print("External paper nodes created:", len(paper_nodes))
print("Patent → external paper edges:", len(edges))

print()
print("Saved paper nodes to:", PAPER_NODES)
print("Saved edges to:", PATENT_PAPER_EDGES)