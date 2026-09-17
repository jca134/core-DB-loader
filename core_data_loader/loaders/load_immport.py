import glob
import os
import re
import pandas as pd

from core_data_loader.common.raw_common import ensure_raw_schema, load_to_raw, start_batch, complete_batch

STUDY_ACCESSION_RE = re.compile(r"(SDY\d+)")

TABLES_TO_LOAD = {
    "study", "arm_or_cohort", "subject", "arm_2_subject", "biosample",
    "experiment", "expsample", "expsample_2_biosample", "protocol",
    "experiment_2_protocol", "study_2_condition_or_disease", "treatment",
    "expsample_2_treatment", "immune_exposure", "study_pubmed",
    "expsample_public_repository",
}

LK_DISEASE_SOURCE_CODE = "immport_sdy1373"


def load_lk_disease():
    for study_dir in sorted(glob.glob("data/immport/SDY*_ALL_DATA")):
        candidates = glob.glob(os.path.join(study_dir, "*_Tab", "Tab", "lk_disease.txt"))
        if candidates:
            filepath = candidates[0]
            break
    else:
        print("Skipping raw.immport_lk_disease: lk_disease.txt not found in any study export")
        return

    batch_id, source_id = start_batch(LK_DISEASE_SOURCE_CODE, notes=f"load_immport.py over {filepath}")
    try:
        df = pd.read_csv(filepath, sep="\t", dtype=str, keep_default_na=False)
        load_to_raw(df, "immport_lk_disease", batch_id=batch_id, source_id=source_id, file_path=filepath)
    except Exception:
        complete_batch(batch_id, status="failed")
        raise
    else:
        complete_batch(batch_id, status="succeeded")


def main():
    ensure_raw_schema()

    for study_dir in sorted(glob.glob("data/immport/SDY*_ALL_DATA")):
        m = STUDY_ACCESSION_RE.search(os.path.basename(study_dir))
        if not m:
            continue
        study_accession = m.group(1).lower()

        tab_dirs = glob.glob(os.path.join(study_dir, "*_Tab", "Tab"))
        if not tab_dirs:
            print(f"Skipping {study_dir}: no Tab/ folder found")
            continue
        tab_dir = tab_dirs[0]

        # One batch per study: each ImmPort study is its own core.source row
        # (immport_sdy<NNNN>), so its files get tracked as their own run.
        source_code = f"immport_{study_accession}"
        batch_id, source_id = start_batch(source_code, notes=f"load_immport.py over {study_dir}")

        try:
            for txt_path in sorted(glob.glob(os.path.join(tab_dir, "*.txt"))):
                table = os.path.splitext(os.path.basename(txt_path))[0].lower()
                if table not in TABLES_TO_LOAD:
                    continue
                raw_table = f"immport_{study_accession}_{table}"

                df = pd.read_csv(txt_path, sep="\t", dtype=str, keep_default_na=False)

                if df.empty:
                    print(f"Skipping raw.{raw_table}: 0 rows")
                    continue

                load_to_raw(df, raw_table, batch_id=batch_id, source_id=source_id, file_path=txt_path)
        except Exception:
            complete_batch(batch_id, status="failed")
            raise
        else:
            complete_batch(batch_id, status="succeeded")

    load_lk_disease()

    print("Finished loading all ImmPort studies.")


if __name__ == "__main__":
    main()
