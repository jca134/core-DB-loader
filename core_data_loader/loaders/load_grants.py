import pandas as pd

from core_data_loader.common.raw_common import ensure_raw_schema, load_to_raw, start_batch, complete_batch

FILEPATH = "data/grants/grants_raw.csv"
TABLE_NAME = "nih_reporter_grants"
SOURCE_CODE = "nih_reporter"

# Optional companion file, written by fetch_grant_publications.py: the
# grant -> PubMed links RePORTER publishes for each core project. Absent on a
# fresh checkout (it needs network), and the ETL simply skips grant->paper
# edges when the raw table isn't there.
PUBLICATIONS_FILEPATH = "data/grants/grant_publications.csv"
PUBLICATIONS_TABLE = "nih_reporter_grant_publications"

# Optional, written by fetch_cited_publications.py: PubMed records (with
# abstracts) for the cited papers that aren't among the papers workstream's
# 300. etl_evidence keeps only the filovirus ones.
CITED_FILEPATH = "data/grants/cited_publications.csv"
CITED_TABLE = "nih_reporter_cited_publications"

# Optional, from a teammate (2026-11): grant -> condition / symptom / assay-type
# keyword hits (the taxon and gene rows duplicate what etl_evidence computes
# itself and are ignored), and a grant-vs-paper comparison whose 'pubmed_ack'
# rows are grant -> paper citations taken from the paper's own PubMed grant list.
OPTIONAL_FILES = [
    ("data/grants/grant_entity_edges.csv", "nih_reporter_grant_entity_edges"),
    ("data/grants/grant_paper_divergence.csv", "nih_reporter_grant_paper_divergence"),
]


def main():
    ensure_raw_schema()
    batch_id, source_id = start_batch(SOURCE_CODE, notes=f"load_grants.py over {FILEPATH}")

    try:
        print(f"Loading {FILEPATH} -> raw.{TABLE_NAME}")
        # The RePORTER export is one row per (project, fiscal year), with
        # embedded JSON in principal_investigators and dotted column names
        # flattened out of the API's nested objects -- both mirrored verbatim,
        # like every other loader.
        df = pd.read_csv(FILEPATH, dtype=str, keep_default_na=False)
        load_to_raw(df, TABLE_NAME, batch_id=batch_id, source_id=source_id, file_path=FILEPATH)

        try:
            pubs = pd.read_csv(PUBLICATIONS_FILEPATH, dtype=str, keep_default_na=False)
        except FileNotFoundError:
            print(f"  (no {PUBLICATIONS_FILEPATH}; run fetch_grant_publications.py "
                  "to add grant -> paper links)")
        else:
            load_to_raw(pubs, PUBLICATIONS_TABLE, batch_id=batch_id, source_id=source_id,
                        file_path=PUBLICATIONS_FILEPATH)

        try:
            cited = pd.read_csv(CITED_FILEPATH, dtype=str, keep_default_na=False)
        except FileNotFoundError:
            print(f"  (no {CITED_FILEPATH}; run fetch_cited_publications.py "
                  "to add the filovirus papers grants cite)")
        else:
            load_to_raw(cited, CITED_TABLE, batch_id=batch_id, source_id=source_id,
                        file_path=CITED_FILEPATH)

        for filepath, table in OPTIONAL_FILES:
            try:
                df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
            except FileNotFoundError:
                print(f"  (no {filepath}; skipped)")
                continue
            load_to_raw(df, table, batch_id=batch_id, source_id=source_id, file_path=filepath)
    except Exception:
        complete_batch(batch_id, status="failed")
        raise
    else:
        complete_batch(batch_id, status="succeeded")


if __name__ == "__main__":
    main()
