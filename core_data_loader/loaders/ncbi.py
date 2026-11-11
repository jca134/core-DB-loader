"""NCBI Virus sequence catalog: data/ncbi/sequences.csv -> raw.ncbi_sequences."""

from core_data_loader.common.raw_common import ingest_batch, read_csv
from core_data_loader.config import data_path

FILEPATH = data_path("ncbi", "sequences.csv")
TABLE_NAME = "ncbi_sequences"
SOURCE_CODE = "ncbi"


def main():
    with ingest_batch(SOURCE_CODE, notes="loaders/ncbi.py") as batch:
        print(f"Loading {FILEPATH.name} -> raw.{TABLE_NAME}")
        batch.load(read_csv(FILEPATH), TABLE_NAME, FILEPATH)


if __name__ == "__main__":
    main()
