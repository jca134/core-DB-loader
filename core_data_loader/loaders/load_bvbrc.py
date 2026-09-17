import os
import pandas as pd

from core_data_loader.common.raw_common import ensure_raw_schema, load_to_raw, start_batch, complete_batch

FOLDER = "data/bvbrc"
SOURCE_CODE = "bvbrc"


def main():
    ensure_raw_schema()
    batch_id, source_id = start_batch(SOURCE_CODE, notes=f"load_bvbrc.py over {FOLDER}")

    try:
        for filename in os.listdir(FOLDER):
            if not filename.endswith(".csv"):
                continue

            filepath = os.path.join(FOLDER, filename)
            table_name = "bvbrc_" + filename.lower().replace(".csv", "")

            print(f"Loading {filename} -> raw.{table_name}")

            df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
            load_to_raw(df, table_name, batch_id=batch_id, source_id=source_id, file_path=filepath)
    except Exception:
        complete_batch(batch_id, status="failed")
        raise
    else:
        complete_batch(batch_id, status="succeeded")

    print("Finished loading all BV-BRC files.")


if __name__ == "__main__":
    main()
