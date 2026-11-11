"""
ImmPort study exports: the tab-delimited tables of each data/immport/SDY*_ALL_DATA
study -> raw.immport_<study>_<table>, plus ImmPort's disease lookup table.
"""

import re

from core_data_loader.common.raw_common import ingest_batch, read_csv
from core_data_loader.config import data_path

STUDIES_DIR = data_path("immport")
STUDY_ACCESSION_RE = re.compile(r"(SDY\d+)")

TABLES_TO_LOAD = {
    "study", "arm_or_cohort", "subject", "arm_2_subject", "biosample",
    "experiment", "expsample", "expsample_2_biosample", "protocol",
    "experiment_2_protocol", "study_2_condition_or_disease", "treatment",
    "expsample_2_treatment", "immune_exposure", "study_pubmed",
    "expsample_public_repository",
}

# lk_disease.txt is the same vocabulary in every study export; it is loaded
# once and attributed to the first study.
LK_DISEASE_SOURCE_CODE = "immport_sdy1373"


def _study_dirs():
    return sorted(STUDIES_DIR.glob("SDY*_ALL_DATA"))


def load_lk_disease():
    for study_dir in _study_dirs():
        candidates = sorted(study_dir.glob("*_Tab/Tab/lk_disease.txt"))
        if candidates:
            path = candidates[0]
            break
    else:
        print("Skipping raw.immport_lk_disease: lk_disease.txt not found in any study export")
        return

    with ingest_batch(LK_DISEASE_SOURCE_CODE, notes="loaders/immport.py lk_disease") as batch:
        batch.load(read_csv(path, sep="\t"), "immport_lk_disease", path)


def main():
    for study_dir in _study_dirs():
        m = STUDY_ACCESSION_RE.search(study_dir.name)
        if not m:
            continue
        study = m.group(1).lower()

        tab_dirs = sorted(study_dir.glob("*_Tab/Tab"))
        if not tab_dirs:
            print(f"Skipping {study_dir.name}: no Tab/ folder found")
            continue

        # One batch per study: each ImmPort study is its own core.source row
        # (immport_sdy<NNNN>), so its files are tracked as their own run.
        with ingest_batch(f"immport_{study}", notes=f"loaders/immport.py over {study_dir.name}") as batch:
            for path in sorted(tab_dirs[0].glob("*.txt")):
                table = path.stem.lower()
                if table not in TABLES_TO_LOAD:
                    continue
                raw_table = f"immport_{study}_{table}"
                df = read_csv(path, sep="\t")
                if df.empty:
                    print(f"Skipping raw.{raw_table}: 0 rows")
                    continue
                batch.load(df, raw_table, path)

    load_lk_disease()
    print("Finished loading all ImmPort studies.")


if __name__ == "__main__":
    main()
