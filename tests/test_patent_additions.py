"""
Regression tests for the 2026-11 patent/grant additions: patent id
normalisation, the disease-name mask behind grant symptom links, and the
title matching that resolves patents' free-text references to PubMed. Each
case is a trap found in the real data.
"""

import re

from core_data_loader.etl.etl_evidence import DISEASE_NAME_RE, normalize_document_id, _family_id
from core_data_loader.loaders.fetch_patent_cited_publications import (
    ERRATUM_RE, NOT_A_PAPER_RE, _fold, title_score, title_segments,
)


# --- patent ids -------------------------------------------------------------

def test_compact_and_spaced_document_ids_normalise_to_one_form():
    assert normalize_document_id("US20180291088A1") == "US 20180291088 A1"
    assert normalize_document_id("US 20180291088 A1") == "US 20180291088 A1"
    assert normalize_document_id("US10000000B2") == "US 10000000 B2"


def test_unparseable_document_id_is_passed_through_not_dropped():
    assert normalize_document_id("not-a-patent") == "not-a-patent"
    assert normalize_document_id("") is None


def test_family_id_float_suffix_is_stripped():
    assert _family_id("78828615.0") == "78828615"
    assert _family_id("78828615") == "78828615"
    assert _family_id("") is None


# --- disease-name mask --------------------------------------------------------

def _symptom_fever(text: str) -> bool:
    return bool(re.search(r"\bfever\b", DISEASE_NAME_RE.sub(" ", text), re.I))


def test_fever_inside_a_disease_name_is_not_the_symptom():
    for text in ("Ebola hemorrhagic fever", "viral haemorrhagic fevers",
                 "yellow fever virus", "Crimean-Congo Hemorhagic Fever",  # misspelt in a real abstract
                 "Lassa fever", "Rift Valley fever viruses"):
        assert not _symptom_fever(text), text


def test_fever_as_a_symptom_survives_the_mask():
    assert _symptom_fever("characterized by fever, hemorrhage and shock")
    assert _symptom_fever("Lassa fever patients present with high fever")


# --- reference resolution -----------------------------------------------------

def test_accents_fold_for_author_checks():
    assert _fold("Müller") == "muller"


def test_short_record_title_is_not_contained_in_a_book_citation():
    # A record titled "Cold Spring Harbor Laboratory" matched every citation
    # of a Cold Spring Harbor book by containment.
    citation = "Sambrook et al., Molecular Cloning: A Laboratory Manual, Cold Spring Harbor Laboratory Press"
    assert title_score(citation, "Cold Spring Harbor Laboratory") < 0.85


def test_real_title_contained_in_citation_scores_full():
    title = "Sequence analysis of the Ebola virus genome organization and expression"
    assert title_score(f'Sanchez, A. et al., "{title}," Virus Res. 1993', title) == 1.0


def test_quoted_title_is_the_first_candidate():
    segs = title_segments('Sanchez, A. et al., " Sequence Analysis of the Ebola Virus Genome, " Virus Res. 1993')
    assert segs[0].startswith("Sequence Analysis of the Ebola Virus Genome")


def test_errata_and_non_literature_are_recognised():
    assert ERRATUM_RE.match("Correction: Ebola virus entry requires NPC1")
    assert not ERRATUM_RE.match("Ebola virus entry requires NPC1")
    assert NOT_A_PAPER_RE.search("GenBank Accession No. AF086833")
    assert not NOT_A_PAPER_RE.search("Marzi et al., Science, April 2015, 348(6233):439-442")
