"""
Every setting the pipeline reads from its environment, in one place.

Paths are absolute, resolved from the project root rather than the current
directory, so every command works from wherever it is run.

    FILOVIRUS_DB_URL   Postgres URL      default postgresql+psycopg2://$USER@localhost:5432/filovirus
    FILOVIRUS_DATA_DIR source downloads  default <project>/data
    NEO4J_URI          Neo4j bolt URL    default bolt://localhost:7687
    NEO4J_USER                           default neo4j
    NEO4J_PASSWORD                       default filovirus (local dev only)
    NCBI_EMAIL         contact address NCBI asks automated clients to send
                       with E-utilities requests; omitted when unset
    ANTHROPIC_API_KEY  optional, for plain-English graph questions
"""

import getpass
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = PROJECT_ROOT / "sql"
DATA_DIR = Path(os.environ.get("FILOVIRUS_DATA_DIR", PROJECT_ROOT / "data")).resolve()

DB_URL = os.environ.get(
    "FILOVIRUS_DB_URL",
    f"postgresql+psycopg2://{getpass.getuser()}@localhost:5432/filovirus",
)

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "filovirus")

NCBI_TOOL = "core-DB-loader"
NCBI_EMAIL = os.environ.get("NCBI_EMAIL", "")

# Schema DDL, in the order it must be applied. Every file is idempotent.
SCHEMA_FILES = [
    "01_schemas.sql",
    "02_core.sql",
    "03_ops.sql",
    "04_evidence.sql",
    "05_clinical_vocab.sql",
    "06_gene_family.sql",
    "07_countermeasures.sql",
    "08_organizations.sql",
    "09_document_dates.sql",
    "10_link_review.sql",
]
CHECKS_SQL = SQL_DIR / "checks.sql"

# The KG search vocabulary behind keyword evidence links (one row per search
# term). An input like any source file: put it at data/kg_keywords.csv. The two
# older locations are still read so existing checkouts keep working.
KG_KEYWORDS_CANDIDATES = [
    DATA_DIR / "kg_keywords.csv",
    PROJECT_ROOT / "overview" / "Context" / "kg_keywords.csv",
    PROJECT_ROOT / "exports" / "kg_keywords.csv",
]


def data_path(*parts: str) -> Path:
    """A path under DATA_DIR: data_path('grants', 'grants_raw.csv')."""
    return DATA_DIR.joinpath(*parts)
