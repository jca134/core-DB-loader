"""
Catalog of every external dataset this pipeline ingests, and the one place
that upserts core.source rows for them.

Layering: sits below both raw_common and etl_common. load_*.py (via
raw_common) needs a source_id to log ops.ingest_batch/ops.ingest_file before
any core.* row exists; etl_*.py (via etl_common, which re-exports this
module) needs the same source_ids for core.* rows and ops.entity_provenance.
Living below both keeps raw_common from having to depend on etl_common.
"""

from sqlalchemy import text

from core_data_loader.common.db_utils import engine

SOURCES = [
    ("bvbrc", "BV-BRC", "https://www.bv-brc.org", "PATRIC/BV-BRC Filoviridae pull"),
    ("ncbi", "NCBI Virus", "https://www.ncbi.nlm.nih.gov", "NCBI Filoviridae sequence catalog"),
    ("hfv_lanl", "LANL Hemorrhagic Fever Viruses DB", "https://hfv.lanl.gov", "LANL HFV curated alignments/epitopes/antibodies"),
    ("ucsc", "UCSC eboVir3 Genome Browser", "https://genome.ucsc.edu", "UCSC eboVir3 assembly tracks"),
    ("immport_sdy1373", "ImmPort SDY1373", "https://www.immport.org", "ImmPort study SDY1373"),
    ("immport_sdy1932", "ImmPort SDY1932", "https://www.immport.org", "ImmPort study SDY1932"),
    ("immport_sdy1976", "ImmPort SDY1976", "https://www.immport.org", "ImmPort study SDY1976"),
]


def ensure_sources() -> dict:
    # Upsert instead of insert so re-running a load/ETL script doesn't
    # duplicate core.source rows or blow up on the unique constraint.
    with engine.begin() as conn:
        for code, name, url, desc in SOURCES:
            conn.execute(
                text(
                    """
                    INSERT INTO core.source (source_code, source_name, source_url, description)
                    VALUES (:code, :name, :url, :desc)
                    ON CONFLICT (source_code) DO UPDATE
                        SET source_name = EXCLUDED.source_name,
                            source_url = EXCLUDED.source_url,
                            description = EXCLUDED.description
                    """
                ),
                {"code": code, "name": name, "url": url, "desc": desc},
            )
        rows = conn.execute(text("SELECT source_code, source_id FROM core.source")).fetchall()
    return {code: sid for code, sid in rows}
