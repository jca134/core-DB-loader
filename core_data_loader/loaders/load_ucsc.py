"""
Loads a curated subset of the UCSC eboVir3 genome-browser track tables into
raw.ucsc_<table>. Column names are parsed directly out of UCSC's own MySQL
dump files (data/ucsc/database/<table>.sql) so raw mirrors the source
exactly, same as load_bvbrc.py does for the BV-BRC CSVs.

Deliberately NOT loaded (browser-internal, or out of scope for now):
- trackDb, hgFindSpec, tableList, tableDescriptions, extFile, bigFiles, grp,
  history, muPIT: UCSC Genome Browser internal plumbing, not biological data.
- multiz160way / multiz160wayFrames / mafSnp160way / mafSnpStrainName160way:
  the alignment itself lives in the .maf.gz files, not worth a relational
  mirror at this stage.
- iedbsupp1_<HLA>* / iedbsupp2_<HLA>* (~60 tables): per-HLA-allele IEDB
  benchmark supplement tables, not referenced anywhere else in our data.
  Revisit if a use case shows up.
"""

import gzip
import os
import re
import pandas as pd

from core_data_loader.common.raw_common import ensure_raw_schema, load_to_raw, start_batch, complete_batch

DATABASE_DIR = "data/ucsc/database"
SOURCE_CODE = "ucsc"

TABLES = [
    "gold", "gap", "cytoBandIdeo", "chromInfo",
    "pdb", "spStruct", "spAnnot", "spMut",
    "geneDesc", "ncbiGene", "ncbiGenePfam",
    "iedbBcell", "iedbBcellNeg", "iedbTcellI", "iedbTcellII", "iedbSupp3",
    "gire2014", "gire2014Missense", "gire2014SpecificSnps",
    "gireZebov", "gireIntraHost",
    "strainName160way", "newSequences",
]

COLUMN_LINE_RE = re.compile(r"^\s*`([a-zA-Z0-9_]+)`\s+\w")


def camel_to_snake(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def parse_columns(sql_path: str) -> list[str]:
    with open(sql_path, "r", encoding="utf-8", errors="replace") as f:
        text_content = f.read()

    m = re.search(r"CREATE TABLE `\w+` \((.*?)\)\s*ENGINE", text_content, re.S)
    if not m:
        raise ValueError(f"Could not find CREATE TABLE body in {sql_path}")

    columns = []
    for line in m.group(1).splitlines():
        col_match = COLUMN_LINE_RE.match(line)
        if col_match:
            columns.append(col_match.group(1))
    if not columns:
        raise ValueError(f"No columns parsed from {sql_path}")
    return columns


def main():
    ensure_raw_schema()
    batch_id, source_id = start_batch(SOURCE_CODE, notes=f"load_ucsc.py over {DATABASE_DIR}")

    try:
        for table in TABLES:
            sql_path = os.path.join(DATABASE_DIR, f"{table}.sql")
            data_path = os.path.join(DATABASE_DIR, f"{table}.txt.gz")

            if not (os.path.exists(sql_path) and os.path.exists(data_path)):
                print(f"Skipping {table}: .sql or .txt.gz not found")
                continue

            columns = parse_columns(sql_path)
            raw_table = "ucsc_" + camel_to_snake(table)

            with gzip.open(data_path, "rt", encoding="utf-8", errors="replace") as f:
                content = f.read()

            if not content.strip():
                print(f"Skipping {table}: file is empty (0 rows for this genome, e.g. no assembly gaps)")
                continue

            first_line_fields = content.splitlines()[0].split("\t")
            if len(first_line_fields) < len(columns) and first_line_fields[0].startswith("/gbdb/"):
                # "Big" track format: the .txt.gz just holds a pointer to an
                # external .bb/.bw/.vcf.gz blob under /gbdb/, not the actual
                # row data. That blob isn't part of this download, so there's
                # nothing to load here.
                print(f"Skipping {table}: external bigBed/bigWig/VCF-backed track "
                      f"(.txt.gz only contains a pointer to {first_line_fields[0]}, "
                      f"no row data in this snapshot)")
                continue

            df = pd.read_csv(
                pd.io.common.StringIO(content),
                sep="\t",
                header=None,
                names=columns,
                dtype=str,
                keep_default_na=False,
                quoting=3,  # QUOTE_NONE — UCSC dumps aren't CSV-quoted
            )

            # Keep UCSC's own camelCase column names as-is instead of running
            # them through clean_columns; renaming them would break the
            # mapping back to UCSC's own docs/DDL.
            load_to_raw(df, raw_table, clean_col_names=False,
                        batch_id=batch_id, source_id=source_id, file_path=data_path)
    except Exception:
        complete_batch(batch_id, status="failed")
        raise
    else:
        complete_batch(batch_id, status="succeeded")

    print("Finished loading curated UCSC tables.")


if __name__ == "__main__":
    main()
