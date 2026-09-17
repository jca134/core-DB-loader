import os
import pandas as pd

from core_data_loader.common.raw_common import ensure_raw_schema, load_to_raw, start_batch, complete_batch

FOLDER = "data/hfv"
SOURCE_CODE = "hfv_lanl"


def main():
    ensure_raw_schema()
    batch_id, source_id = start_batch(SOURCE_CODE, notes=f"load_hfv.py over {FOLDER}")

    try:
        for fname, table in [
            ("F15AG1.csv", "hfv_f15ag1"),
            ("F15OG1.csv", "hfv_f15og1"),
            ("F15RG1.csv", "hfv_f15rg1"),
        ]:
            filepath = os.path.join(FOLDER, fname)
            df = pd.read_csv(
                filepath,
                header=None,
                names=["strain_label", "aligned_sequence"],
                dtype=str,
                keep_default_na=False,
            )
            load_to_raw(df, table, batch_id=batch_id, source_id=source_id, file_path=filepath)

        filepath = os.path.join(FOLDER, "EbolaCTL.xlsx")
        df = pd.read_excel(filepath, sheet_name="EbolaCTL_as_text", dtype=str, keep_default_na=False)
        load_to_raw(df, "hfv_ebola_ctl", batch_id=batch_id, source_id=source_id, file_path=filepath)

        filepath = os.path.join(FOLDER, "EBOV_numbering.xlsx")
        df = pd.read_excel(filepath, sheet_name="00_NC002549EXCELcsv.csv", dtype=str, keep_default_na=False)
        load_to_raw(df, "hfv_ebov_numbering", batch_id=batch_id, source_id=source_id, file_path=filepath)

        filepath = os.path.join(FOLDER, "EbolaAntibody.xlsx")
        df = pd.read_excel(filepath, sheet_name="EbolaAntibody", dtype=str, keep_default_na=False)
        load_to_raw(df, "hfv_ebola_antibody", batch_id=batch_id, source_id=source_id, file_path=filepath)

        filepath = os.path.join(FOLDER, "EBOV_features.xlsx")
        df = pd.read_excel(filepath, sheet_name="00_EBOV_features_EXCEL", dtype=str, keep_default_na=False)
        load_to_raw(df, "hfv_ebov_features", batch_id=batch_id, source_id=source_id, file_path=filepath)

        # Rows 0-4 are title/notes text, not data — the real header is on row 5.
        filepath = os.path.join(FOLDER, "ebola-annotation-web.xlsx")
        df = pd.read_excel(filepath, sheet_name="All Data", header=5, dtype=str, keep_default_na=False)
        load_to_raw(df, "hfv_ebola_annotation_web", batch_id=batch_id, source_id=source_id, file_path=filepath)
    except Exception:
        complete_batch(batch_id, status="failed")
        raise
    else:
        complete_batch(batch_id, status="succeeded")

    print("Finished loading all HFV/LANL files.")


if __name__ == "__main__":
    main()
