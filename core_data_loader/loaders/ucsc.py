"""
UCSC Genome Browser eboVir3 tables: each <table>.sql (MySQL DDL, for the
column names) + <table>.txt.gz (tab-separated rows) -> raw.ucsc_<table>.
"""

import gzip
import io
import re

import pandas as pd

from core_data_loader.common.raw_common import ingest_batch
from core_data_loader.config import data_path

DATABASE_DIR = data_path("ucsc", "database")
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
    with ingest_batch(SOURCE_CODE, notes="loaders/ucsc.py") as batch:
        for table in TABLES:
            sql_path = DATABASE_DIR / f"{table}.sql"
            rows_path = DATABASE_DIR / f"{table}.txt.gz"

            if not (sql_path.exists() and rows_path.exists()):
                print(f"Skipping {table}: .sql or .txt.gz not found")
                continue

            columns = parse_columns(sql_path)
            raw_table = "ucsc_" + camel_to_snake(table)

            with gzip.open(rows_path, "rt", encoding="utf-8", errors="replace") as f:
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
                io.StringIO(content),
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
            batch.load(df, raw_table, rows_path, clean_col_names=False)

    print("Finished loading curated UCSC tables.")


if __name__ == "__main__":
    main()
