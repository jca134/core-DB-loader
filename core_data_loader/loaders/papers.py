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

# Written by `filovirus fetch paper-citations`: (pmid, cited_pmid) pairs among
# every candidate paper, from PubMed reference lists.
CITATIONS_FILEPATH = data_path("papers", "paper_citations.csv")
CITATIONS_TABLE = "papers_citations"

# Written by `filovirus fetch paper-details`: the grants each paper's PubMed
# record acknowledges (GrantList), and when it went online.
GRANTS_FILEPATH = data_path("papers", "paper_grants.csv")
GRANTS_TABLE = "papers_grants"
DATES_FILEPATH = data_path("papers", "paper_dates.csv")
DATES_TABLE = "papers_dates"


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
        batch.load_optional(CITATIONS_FILEPATH, CITATIONS_TABLE,
                            "run `filovirus fetch paper-citations` to add paper -> paper citations")
        batch.load_optional(GRANTS_FILEPATH, GRANTS_TABLE,
                            "run `filovirus fetch paper-details` to add the grants papers acknowledge")
        batch.load_optional(DATES_FILEPATH, DATES_TABLE,
                            "run `filovirus fetch paper-details` to date papers by when they went online")
    print("Finished loading papers files.")


if __name__ == "__main__":
    main()
