"""BV-BRC Filoviridae exports: every data/bvbrc/*.csv -> raw.bvbrc_<file name>."""

from core_data_loader.common.raw_common import ingest_batch, read_csv
from core_data_loader.config import data_path

FOLDER = data_path("bvbrc")
SOURCE_CODE = "bvbrc"


def main():
    with ingest_batch(SOURCE_CODE, notes=f"loaders/bvbrc.py over {FOLDER.name}/") as batch:
        for path in sorted(FOLDER.glob("*.csv")):
            table = "bvbrc_" + path.stem.lower()
            print(f"Loading {path.name} -> raw.{table}")
            batch.load(read_csv(path), table, path)
    print("Finished loading all BV-BRC files.")


if __name__ == "__main__":
    main()
