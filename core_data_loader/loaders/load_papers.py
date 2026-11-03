import os

import pandas as pd

from core_data_loader.common.raw_common import ensure_raw_schema, load_to_raw, start_batch, complete_batch

# The papers workstream ships as a zip; unzip it in place and point FOLDER at
# the extracted directory. output_v2/ is the post-merge version (BV-BRC + LANL
# + ImmPort links, plus the IEDB and RCSB lookups folded in) -- the pre-merge
# output/ directory is not used.
FOLDER = "data/papers/papers_iedb_rcsb_update/output_v2"
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


# Written by fetch_publication_metadata.py. Absent on a fresh checkout (it
# needs network); without it core.publication rows have no title and no year,
# which leaves every paper unlabelled for the LLM and undated for any temporal
# benchmark.
METADATA_FILEPATH = "data/papers/publication_metadata.csv"
METADATA_TABLE = "papers_metadata"


def main():
    ensure_raw_schema()
    batch_id, source_id = start_batch(SOURCE_CODE, notes=f"load_papers.py over {FOLDER}")

    try:
        for fname, table in FILES:
            filepath = os.path.join(FOLDER, fname)
            df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
            load_to_raw(df, table, batch_id=batch_id, source_id=source_id, file_path=filepath)

        try:
            meta = pd.read_csv(METADATA_FILEPATH, dtype=str, keep_default_na=False)
        except FileNotFoundError:
            print(f"  (no {METADATA_FILEPATH}; run fetch_publication_metadata.py "
                  "to give papers titles and dates)")
        else:
            load_to_raw(meta, METADATA_TABLE, batch_id=batch_id, source_id=source_id,
                        file_path=METADATA_FILEPATH)
    except FileNotFoundError as e:
        complete_batch(batch_id, status="failed")
        raise FileNotFoundError(
            f"{e.filename} not found. Unzip the papers bundle first:\n"
            "  cd data/papers && unzip 'papers_iedb_rcsb_update (1) (1).zip'"
        ) from e
    except Exception:
        complete_batch(batch_id, status="failed")
        raise
    else:
        complete_batch(batch_id, status="succeeded")

    print("Finished loading papers files.")


if __name__ == "__main__":
    main()
