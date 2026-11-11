"""
USPTO patent search exports (four organism x CPC-class searches) plus the
patents workstream's optional companion files.
"""

import pandas as pd

from core_data_loader.common.raw_common import ingest_batch, read_csv
from core_data_loader.config import data_path

FOLDER = data_path("patents")
TABLE_NAME = "uspto_patents"
SOURCE_CODE = "uspto"

# One export per (organism, CPC class) search. The filename is the only record
# of which search produced a row -- the CSV itself has no column for it -- so it
# is carried into raw.* as query_organism/query_cpc. etl.evidence turns
# query_organism into the taxon link for patents whose title names no taxon.
#
#   A61K39/12  viral antigens or vaccines
#   C07K16/10  immunoglobulins/antibodies against RNA viruses
FILES = [
    ("ebola_A61K39-12_raw.csv", "ebola", "A61K39/12"),
    ("ebola_C07K16-10_raw.csv", "ebola", "C07K16/10"),
    ("marburg_A61K39-12_raw.csv", "marburg", "A61K39/12"),
    ("marburg_C07K16-10_raw.csv", "marburg", "C07K16/10"),
]

# Optional companion files from the patents workstream (2026-11). Each is
# skipped if absent, and the ETL skips whatever depends on it.
#
#   patent_evidence_expanded.csv   the 72 patents above plus 100 more from a
#                                  broader USPTO abstract search ("filovirus",
#                                  not CPC-filtered). Those 100 have no
#                                  publication date, CPC codes or page count.
#   patent_entity_links.csv        keyword hits in each patent's title AND
#                                  abstract; the abstracts themselves are not
#                                  supplied, so each abstract keyword is re-run
#                                  through the shared matcher (its entity ids
#                                  are stale and are never used).
#   patent_team_paper_edges.csv    patent -> paper citations already resolved
#                                  to PMIDs, for papers already in the KG.
#   patent_cited_publications.csv  every reference a patent cites, resolved to
#                                  PubMed where possible -- written by
#                                  `filovirus fetch patent-citations` from
#                                  patent_external_literature_edges.csv.
#   patent_details.csv             each patent's abstract and publication /
#                                  filing / priority dates, from its Google
#                                  Patents page (`filovirus fetch patent-details`).
OPTIONAL_FILES = [
    ("patent_evidence_expanded.csv", "uspto_patents_expanded"),
    ("patent_entity_links.csv", "uspto_patent_entity_links"),
    ("patent_team_paper_edges.csv", "uspto_patent_team_papers"),
    ("patent_cited_publications.csv", "uspto_patent_cited_publications"),
    ("patent_details.csv", "uspto_patent_details"),
]

# Search-UI checkbox columns, blank in every row of every export.
UI_COLUMNS = ("Select", "+", "X", "1", "2", "3", "4", "5", "C")


def main():
    with ingest_batch(SOURCE_CODE, notes="loaders/patents.py") as batch:
        frames = []
        for fname, organism, cpc in FILES:
            df = read_csv(FOLDER / fname)
            df = df.loc[:, [c for c in df.columns if c not in UI_COLUMNS]]
            df["query_organism"] = organism
            df["query_cpc"] = cpc
            df["source_file"] = fname
            frames.append(df)
            print(f"  {fname}: {len(df)} rows")

        # One raw table rather than four: the same patent legitimately appears in
        # more than one export (an antibody patent classified in both C07K16/10
        # and A61K39/12), and keeping them together lets the ETL see every search
        # a given document came from. Deduped on Document ID in the ETL, not here
        # -- raw.* mirrors the sources verbatim.
        batch.load(pd.concat(frames, ignore_index=True), TABLE_NAME, FOLDER)

        for fname, table in OPTIONAL_FILES:
            path = FOLDER / fname
            if not path.exists():
                print(f"  (no {fname}; skipped)")
                continue
            df = read_csv(path)
            batch.load(df.loc[:, [c for c in df.columns if c not in UI_COLUMNS]], table, path)

    print("Finished loading USPTO patent exports.")


if __name__ == "__main__":
    main()
