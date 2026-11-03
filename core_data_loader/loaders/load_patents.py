import os

import pandas as pd

from core_data_loader.common.raw_common import ensure_raw_schema, load_to_raw, start_batch, complete_batch

FOLDER = "data/patents"
TABLE_NAME = "uspto_patents"
SOURCE_CODE = "uspto"

# One export per (organism, CPC class) search. The filename is the only record
# of which search produced a row -- the CSV itself has no column for it -- so it
# is carried into raw.* as query_organism/query_cpc. etl_evidence turns
# query_organism into the taxon link for patents whose title names no taxon.
#
#   A61K39/12  viral antigens or vaccines
#   C07K16/10  immunoglobulins/antibodies against RNA viruses
FILES = [
    ("ebola_A61K39-12_raw.csv", "ebola", "A61K39/12"),
    ("ebola_C07K16-10_raw.csv", "ebola", "C07K16/10"),
    ("marburg_A61K39-12_raw.csv", "marburg", "A61K39/12"),
    ("marburg_C07K16-10_raw.csv", "marburg", "C07K16/10"),
]


def main():
    ensure_raw_schema()
    batch_id, source_id = start_batch(SOURCE_CODE, notes=f"load_patents.py over {FOLDER}")

    try:
        frames = []
        for fname, organism, cpc in FILES:
            filepath = os.path.join(FOLDER, fname)
            df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
            # The export's leading UI columns ("Select", "+", "X", "1".."5")
            # are checkbox state from the search interface, blank in every row.
            df = df.loc[:, [c for c in df.columns if c not in ("Select", "+", "X", "1", "2", "3", "4", "5", "C")]]
            df["query_organism"] = organism
            df["query_cpc"] = cpc
            df["source_file"] = fname
            frames.append(df)
            print(f"  {fname}: {len(df)} rows")

        # One raw table rather than four: the same patent legitimately appears in
        # more than one export (an antibody patent classified in both C07K16/10
        # and A61K39/12), and keeping them together lets the ETL see every search
        # a given document came from. Deduped on Document ID in the ETL, not here
        # -- raw.* mirrors the sources verbatim.
        combined = pd.concat(frames, ignore_index=True)
        load_to_raw(combined, TABLE_NAME, batch_id=batch_id, source_id=source_id, file_path=FOLDER)
    except Exception:
        complete_batch(batch_id, status="failed")
        raise
    else:
        complete_batch(batch_id, status="succeeded")

    print("Finished loading USPTO patent exports.")


if __name__ == "__main__":
    main()
