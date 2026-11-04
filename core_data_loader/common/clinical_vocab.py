"""
Curated symptom and assay-type vocabularies, and the parsers that map the
loaded data's free text onto them. Seeds core.symptom / core.assay_type; see
sql/05_clinical_vocab.sql and etl_clinical_vocab.py.

SCOPE: only terms the loaded data actually uses. Symptoms come from the 6
distinct patient_symptoms strings in LANL's annotation file (22 isolates).
Assay types come from ImmPort's 6 measurement techniques, IEDB's 3 epitope
assay categories, and the methods named in LANL's CTL file. Adding a term for
grant-text matching alone is fine, but say so in its comment.

IDS: every hpo_id / obi_id was checked against EBI's Ontology Lookup Service
on 2026-11-03 (label shown in the comment). Where OBI has no term at the right
granularity the id is None -- a near-miss id would assert a different assay.

Matching is phrase-based on lowercased text, so synonyms are lowercase-safe
and include the source's own misspellings ('diarrhorea').
"""

import re

from sqlalchemy import text

# (name, hpo_id, parent name or None, synonyms)
SYMPTOMS = [
    ("Fever", "HP:0001945", None, ["fever", "febrile", "pyrexia", "high fever"]),
    ("Headache", "HP:0002315", None, ["headache", "headaches", "cephalalgia"]),
    ("Malaise", "HP:0033834", None, ["malaise"]),
    ("Vomiting", "HP:0002013", None, ["vomiting", "emesis"]),
    ("Diarrhea", "HP:0002014", None, ["diarrhea", "diarrhoea", "diarrhorea"]),
    # A kind of diarrhea by definition, so it gets Diarrhea as parent.
    ("Bloody diarrhea", "HP:0025085", "Diarrhea",
     ["bloody diarrhea", "bloody diarrhoea", "hemorrhagic diarrhea"]),
    # HPO's label is Pharyngalgia; "sore throat" is what the source says.
    ("Pharyngalgia", "HP:0033050", None, ["sore throat", "pharyngalgia", "throat pain"]),
    # HPO has no plain "Hemorrhage"; Abnormal bleeding is its general term.
    ("Abnormal bleeding", "HP:0001892", None,
     ["hemorrhage", "haemorrhage", "bleeding", "hemorrhaging"]),
    ("Liver abscess", "HP:0100523", None, ["liver abscess", "hepatic abscess"]),
    # HPO's label is Miscarriage; the source says "spontaneous abortion".
    ("Miscarriage", "HP:0005268", None, ["spontaneous abortion", "miscarriage"]),
]

# (name, obi_id, parent name or None, synonyms)
ASSAY_TYPES = [
    # IEDB's three epitope assay categories (core.epitope_assay.assay_type).
    ("T cell epitope assay", "OBI:0003128", None, ["t cell assay", "t cell epitope assay"]),
    ("B cell epitope assay", "OBI:0003127", None, ["b cell assay", "b cell epitope assay"]),
    ("MHC ligand assay", "OBI:0002075", None, ["mhc binding", "mhc binding assay", "mhc ligand assay"]),
    # Methods named in LANL's CTL file. No parent: ELISpot and ICS are used for
    # B cells too, so filing them under T cell would be wrong in general. The
    # T cell category is linked separately, from each record's assay_type.
    ("Enzyme-linked immunospot assay", "OBI:0600031", None,
     ["elispot", "eli-spot", "enzyme-linked immunospot"]),
    # OBI only has ICS per cytokine ("...measuring epitope specific IL-6
    # production by T cells"), never the method itself.
    ("Intracellular cytokine staining assay", None, None,
     ["intracellular cytokine staining", "ics", "icc"]),
    ("In vitro T cell killing assay", "OBI:0002062", None,
     ["lytic activity", "lytic function", "cytotoxicity assay", "chromium release", "ctl killing"]),
    ("MHC tetramer assay", "OBI:1110179", None,
     ["tetramer staining", "mhc tetramer", "mhc multimer", "tetramer"]),
    # ImmPort measurement techniques (core.experiment.measurement_technique).
    ("Enzyme-linked immunosorbent assay", "OBI:0000661", None, ["elisa", "enzyme-linked immunosorbent assay"]),
    ("Polymerase chain reaction assay", "OBI:0002692", None, ["pcr", "polymerase chain reaction", "rt-pcr", "qpcr"]),
    ("Pathogen neutralization assay", "OBI:0003796", None,
     ["neutralization assay", "neutralisation assay", "virus neutralization", "neutralizing antibody assay"]),
    # OBI has no pseudovirus neutralization term; its parent says what it is.
    ("Pseudovirus neutralization assay", None, "Pathogen neutralization assay",
     ["pseudovirus neutralization assay", "pseudovirus neutralization", "pseudotyped virus neutralization"]),
    ("Chemiluminescent assay", "OBI:0003779", None, ["chemiluminescent assay", "chemiluminescence assay"]),
    ("B cell receptor repertoire sequencing assay", "OBI:0002991", None,
     ["b cell receptor repertoire sequencing assay", "bcr repertoire sequencing", "bcr-seq"]),
    ("Transcription profiling by array assay", "OBI:0001463", None,
     ["transcription profiling by array", "gene expression microarray", "expression microarray"]),
]

# IEDB's category values -> assay_type name.
IEDB_CATEGORY = {
    "T cell": "T cell epitope assay",
    "B cell": "B cell epitope assay",
    "MHC binding": "MHC ligand assay",
}

# Methods named in LANL CTL assay text. Patterns, not exact phrases: the
# field is free text ("IFNg ICC, lytic activity, EliSpot", "lytic  activity",
# "EliSpot IFNg"). "ICC" in this file means intracellular cytokine staining --
# every ICC row also names IFNg, the cytokine being stained.
CTL_METHOD_PATTERNS = [
    (re.compile(r"eli\s*-?\s*spot", re.I), "Enzyme-linked immunospot assay"),
    (re.compile(r"\bic[cs]\b", re.I), "Intracellular cytokine staining assay"),
    (re.compile(r"\blytic\b", re.I), "In vitro T cell killing assay"),
    (re.compile(r"\btetramer\b", re.I), "MHC tetramer assay"),
]

# Words that qualify a symptom without changing which symptom it is.
_QUALIFIERS = {"high", "severe", "mild", "acute", "persistent"}


def _phrase_index(vocab):
    """lowercased name / synonym -> canonical name."""
    index = {}
    for name, _id, _parent, synonyms in vocab:
        for phrase in [name, *synonyms]:
            index[phrase.lower()] = name
    return index


SYMPTOM_INDEX = _phrase_index(SYMPTOMS)
ASSAY_INDEX = _phrase_index(ASSAY_TYPES)


def parse_symptoms(raw: str) -> tuple[list[str], list[str]]:
    """
    'Fever, headache, sore throat, severe diarrhorea' -> (canonical names,
    unmatched fragments). Unmatched fragments are returned, not dropped, so the
    caller can report them -- a new spelling should be added as a synonym, not
    silently lost.
    """
    if raw is None or raw.strip() in ("", "_", "NA"):
        return [], []
    found, unmatched = [], []
    for fragment in re.split(r",|\band\b", raw.lower()):
        words = [w for w in fragment.split() if w not in _QUALIFIERS]
        phrase = " ".join(words)
        if not phrase:
            continue
        # Try the phrase with and without dropped qualifiers: "high fever" is a
        # synonym in its own right, "severe diarrhorea" only after stripping.
        name = SYMPTOM_INDEX.get(" ".join(fragment.split())) or SYMPTOM_INDEX.get(phrase)
        if name:
            if name not in found:
                found.append(name)
        else:
            unmatched.append(fragment.strip())
    return found, unmatched


def parse_ctl_methods(raw: str) -> list[str]:
    """Every method a LANL CTL assay string names, in vocabulary order."""
    if not raw:
        return []
    return [name for pattern, name in CTL_METHOD_PATTERNS if pattern.search(raw)]


def match_assay_name(raw: str) -> str | None:
    """An exact (case-insensitive) name or synonym, e.g. ImmPort's 'ELISA'."""
    return ASSAY_INDEX.get((raw or "").strip().lower())


def _upsert(conn, table, id_col, ext_col, parent_col, vocab) -> dict:
    """Upsert on name so ids survive reruns; returns name -> id."""
    ids = {}
    for name, ext_id, _parent, synonyms in vocab:
        ids[name] = conn.execute(
            text(
                f"""
                INSERT INTO core.{table} (name, {ext_col}, synonyms)
                VALUES (:name, :ext_id, :synonyms)
                ON CONFLICT (name) DO UPDATE
                    SET {ext_col} = EXCLUDED.{ext_col}, synonyms = EXCLUDED.synonyms
                RETURNING {id_col}
                """
            ),
            {"name": name, "ext_id": ext_id, "synonyms": synonyms},
        ).scalar()
    # Parents in a second pass, so order in the list doesn't matter.
    for name, _ext_id, parent, _synonyms in vocab:
        conn.execute(
            text(f"UPDATE core.{table} SET {parent_col} = :parent WHERE {id_col} = :id"),
            {"parent": ids[parent] if parent else None, "id": ids[name]},
        )
    # A term removed from the list is removed from the table (its links cascade).
    conn.execute(
        text(f"DELETE FROM core.{table} WHERE NOT (name = ANY(:names))"),
        {"names": list(ids)},
    )
    return ids


def ensure_vocab(conn) -> tuple[dict, dict]:
    """Seed both vocabularies; returns (symptom name -> id, assay name -> id)."""
    symptoms = _upsert(conn, "symptom", "symptom_id", "hpo_id", "parent_symptom_id", SYMPTOMS)
    assays = _upsert(conn, "assay_type", "assay_type_id", "obi_id", "parent_assay_type_id", ASSAY_TYPES)
    return symptoms, assays
