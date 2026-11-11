"""
LANL Hemorrhagic Fever Viruses database: three FASTA-like alignment CSVs and
five Excel workbooks (epitopes, antibodies, genome numbering and features,
annotations) -> raw.hfv_*.
"""

import pandas as pd

from core_data_loader.common.raw_common import ingest_batch, read_csv
from core_data_loader.config import data_path

FOLDER = data_path("hfv")
SOURCE_CODE = "hfv_lanl"

ALIGNMENTS = [
    ("F15AG1.csv", "hfv_f15ag1"),
    ("F15OG1.csv", "hfv_f15og1"),
    ("F15RG1.csv", "hfv_f15rg1"),
]

# (workbook, sheet, raw table, header row)
WORKBOOKS = [
    ("EbolaCTL.xlsx", "EbolaCTL_as_text", "hfv_ebola_ctl", 0),
    ("EBOV_numbering.xlsx", "00_NC002549EXCELcsv.csv", "hfv_ebov_numbering", 0),
    ("EbolaAntibody.xlsx", "EbolaAntibody", "hfv_ebola_antibody", 0),
    ("EBOV_features.xlsx", "00_EBOV_features_EXCEL", "hfv_ebov_features", 0),
    # Rows 0-4 are title/notes text, not data -- the real header is on row 5.
    ("ebola-annotation-web.xlsx", "All Data", "hfv_ebola_annotation_web", 5),
]


def main():
    with ingest_batch(SOURCE_CODE, notes="loaders/hfv.py") as batch:
        for fname, table in ALIGNMENTS:
            path = FOLDER / fname
            df = read_csv(path, header=None, names=["strain_label", "aligned_sequence"])
            batch.load(df, table, path)

        for fname, sheet, table, header in WORKBOOKS:
            path = FOLDER / fname
            df = pd.read_excel(path, sheet_name=sheet, header=header, dtype=str, keep_default_na=False)
            batch.load(df, table, path)

    print("Finished loading all HFV/LANL files.")


if __name__ == "__main__":
    main()
