"""
Tests for the curated symptom / assay-type vocabularies and the parsers that
map the loaded data's free text onto them. No database.
"""

import re

import pytest

from core_data_loader.common.clinical_vocab import (
    ASSAY_TYPES, IEDB_CATEGORY, SYMPTOMS,
    match_assay_name, parse_ctl_methods, parse_symptoms,
)

# --- every distinct string in the loaded data maps fully ---------------------
# These are all six patient_symptoms values in raw.hfv_ebola_annotation_web.

@pytest.mark.parametrize("raw, expected", [
    ("headache, malaise, fever, and vomiting", ["Headache", "Malaise", "Fever", "Vomiting"]),
    ("Fever, headache, sore throat, severe diarrhorea, vomiting, liver abscess",
     ["Fever", "Headache", "Pharyngalgia", "Diarrhea", "Vomiting", "Liver abscess"]),
    ("high fever and bloody diarrhea", ["Fever", "Bloody diarrhea"]),
    ("Fever, diarrhea, vomiting, hemorrhage", ["Fever", "Diarrhea", "Vomiting", "Abnormal bleeding"]),
    ("Fever, diarrhea, vomiting", ["Fever", "Diarrhea", "Vomiting"]),
    ("Spontaneous abortion", ["Miscarriage"]),
])
def test_every_source_symptom_string_parses_completely(raw, expected):
    found, unmatched = parse_symptoms(raw)
    assert found == expected
    assert unmatched == []


def test_bloody_diarrhea_is_not_also_plain_diarrhea():
    # The hierarchy (parent_symptom_id) carries "is a diarrhea"; linking both
    # would double-count it.
    found, _ = parse_symptoms("bloody diarrhea")
    assert found == ["Bloody diarrhea"]


def test_unknown_symptom_is_reported_not_dropped():
    found, unmatched = parse_symptoms("fever, conjunctival injection")
    assert found == ["Fever"]
    assert unmatched == ["conjunctival injection"]


@pytest.mark.parametrize("blank", [None, "", "_", "NA"])
def test_blank_symptom_text(blank):
    assert parse_symptoms(blank) == ([], [])


# --- LANL CTL assay text -----------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Elispot", ["Enzyme-linked immunospot assay"]),
    ("IFNg ICC, lytic activity, EliSpot",
     ["Enzyme-linked immunospot assay", "Intracellular cytokine staining assay",
      "In vitro T cell killing assay"]),
    ("ICS IFNg, class I tetramer staining",
     ["Intracellular cytokine staining assay", "MHC tetramer assay"]),
    ("lytic  activity", ["In vitro T cell killing assay"]),
    ("lytic function", ["In vitro T cell killing assay"]),
])
def test_ctl_methods(raw, expected):
    assert parse_ctl_methods(raw) == expected


@pytest.mark.parametrize("raw", ["NA", "restiumation in virto",
                                 "IFNg secretion in splenocytes >2% above background", ""])
def test_ctl_text_naming_no_method(raw):
    # Protocol steps and result descriptions are not assay methods.
    assert parse_ctl_methods(raw) == []


def test_icc_does_not_match_inside_words():
    assert parse_ctl_methods("vaccination") == []


# --- ImmPort techniques and IEDB categories -----------------------------------

@pytest.mark.parametrize("technique", [
    "PCR", "B cell receptor repertoire sequencing assay", "Pseudovirus Neutralization Assay",
    "ELISA", "Transcription profiling by array", "Chemiluminescent Assay",
])
def test_every_immport_technique_matches(technique):
    assert match_assay_name(technique) is not None


def test_iedb_categories_point_at_real_terms():
    names = {a[0] for a in ASSAY_TYPES}
    assert set(IEDB_CATEGORY.values()) <= names


# --- vocabulary integrity ----------------------------------------------------

@pytest.mark.parametrize("vocab, prefix", [(SYMPTOMS, "HP"), (ASSAY_TYPES, "OBI")])
def test_vocab_ids_parents_and_synonyms(vocab, prefix):
    names = [v[0] for v in vocab]
    assert len(names) == len(set(names)), "duplicate name"
    ids = [v[1] for v in vocab if v[1]]
    assert len(ids) == len(set(ids)), "duplicate ontology id"
    assert all(re.fullmatch(rf"{prefix}:\d{{7}}", i) for i in ids)
    for _name, _id, parent, _syn in vocab:
        assert parent is None or parent in names
    # A synonym naming two terms would make matching order-dependent.
    owner = {}
    for name, _id, _parent, synonyms in vocab:
        for s in [name.lower(), *synonyms]:
            assert owner.setdefault(s, name) == name, f"{s!r} names {owner[s]} and {name}"
