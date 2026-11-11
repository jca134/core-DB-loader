"""
The papers workstream's bundle (papers -> KG entity links) and, when fetched,
PubMed metadata for those papers.
"""

from core_data_loader.common.raw_common import ingest_batch, read_csv
from core_data_loader.config import data_path

# The bundle ships as a zip; unzip it in place. output_v2/ is the post-merge
# version (BV-BRC + LANL + ImmPort links, plus the IEDB and RCSB lookups folded
# in) -- the pre-merge output/ directory is not used.
FOLDER = data_path("papers", "papers_iedb_rcsb_update", "output_v2")
SOURCE_CODE = "pubmed"

FILES = [
    # One row per paper: 300 distinct PMIDs, deduplicated across every route
    # they were discovered by.
    ("unique_papers_v2.csv", "papers_unique"),
    # One row per (paper, entity) link, 4385 of them, keyed by each source's
    # own identifier (BV-BRC genome_id, IEDB epitope id, PDB id, ...).
    ("master_references_v2.csv", "papers_references"),
    # Which source each evidence row came from, keyed by evidence_id. Kept
    # beside the links because the agreed 10-column evidence format has no
    # column for it.
    ("evidence_link_sources.csv", "papers_evidence_sources"),
]

# Written by `filovirus fetch publication-metadata`. Without it core.publication
# rows have no title and no year.
METADATA_FILEPATH = data_path("papers", "publication_metadata.csv")
METADATA_TABLE = "papers_metadata"


def main():
    if not FOLDER.is_dir():
        raise FileNotFoundError(
            f"{FOLDER} not found. Unzip the papers bundle first:\n"
            f"  cd {FOLDER.parents[1]} && unzip 'papers_iedb_rcsb_update (1) (1).zip'"
        )
    with ingest_batch(SOURCE_CODE, notes="loaders/papers.py") as batch:
        for fname, table in FILES:
            batch.load(read_csv(FOLDER / fname), table, FOLDER / fname)
        batch.load_optional(METADATA_FILEPATH, METADATA_TABLE,
                            "run `filovirus fetch publication-metadata` to give papers titles and dates")
    print("Finished loading papers files.")


if __name__ == "__main__":
    main()
