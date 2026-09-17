import pandas as pd

from core_data_loader.common.raw_common import ensure_raw_schema, load_to_raw, start_batch, complete_batch

FILEPATH = "data/ncbi/sequences.csv"
TABLE_NAME = "ncbi_sequences"
SOURCE_CODE = "ncbi"


def main():
    ensure_raw_schema()
    batch_id, source_id = start_batch(SOURCE_CODE, notes=f"load_ncbi.py over {FILEPATH}")

    try:
        print(f"Loading {FILEPATH} -> raw.{TABLE_NAME}")

        df = pd.read_csv(FILEPATH, dtype=str, keep_default_na=False)
        load_to_raw(df, TABLE_NAME, batch_id=batch_id, source_id=source_id, file_path=FILEPATH)
    except Exception:
        complete_batch(batch_id, status="failed")
        raise
    else:
        complete_batch(batch_id, status="succeeded")


if __name__ == "__main__":
    main()
