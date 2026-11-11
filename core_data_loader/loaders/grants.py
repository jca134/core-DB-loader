"""
NIH RePORTER grants (data/grants/grants_raw.csv) plus the optional companion
files that add grant -> paper and grant -> clinical-term links.
"""

from core_data_loader.common.raw_common import ingest_batch, read_csv
from core_data_loader.config import data_path

FILEPATH = data_path("grants", "grants_raw.csv")
TABLE_NAME = "nih_reporter_grants"
SOURCE_CODE = "nih_reporter"

# Optional companion files, each skipped if absent (the ETL then skips
# whatever depends on it): (path, raw table, how to get it).
OPTIONAL_FILES = [
    # The grant -> PubMed links RePORTER publishes for each core project.
    (data_path("grants", "grant_publications.csv"), "nih_reporter_grant_publications",
     "run `filovirus fetch grant-publications` to add grant -> paper links"),
    # PubMed records (with abstracts) for the cited papers that aren't among
    # the papers bundle's 300. etl.evidence keeps only the filovirus ones.
    (data_path("grants", "cited_publications.csv"), "nih_reporter_cited_publications",
     "run `filovirus fetch cited-publications` to add the filovirus papers grants cite"),
    # From a teammate (2026-11): grant -> condition / symptom / assay-type
    # keyword hits (its taxon and gene rows duplicate what etl.evidence computes
    # and are ignored), and a grant-vs-paper comparison whose 'pubmed_ack' rows
    # are grant -> paper citations taken from the paper's own PubMed grant list.
    (data_path("grants", "grant_entity_edges.csv"), "nih_reporter_grant_entity_edges", ""),
    (data_path("grants", "grant_paper_divergence.csv"), "nih_reporter_grant_paper_divergence", ""),
]


def main():
    with ingest_batch(SOURCE_CODE, notes="loaders/grants.py") as batch:
        print(f"Loading {FILEPATH.name} -> raw.{TABLE_NAME}")
        # One row per (project, fiscal year), with embedded JSON in
        # principal_investigators and dotted column names flattened out of the
        # API's nested objects -- mirrored verbatim, like every other source.
        batch.load(read_csv(FILEPATH), TABLE_NAME, FILEPATH)
        for path, table, hint in OPTIONAL_FILES:
            batch.load_optional(path, table, hint)


if __name__ == "__main__":
    main()
