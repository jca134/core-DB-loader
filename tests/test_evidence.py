"""
Regression tests for the evidence layer's pure logic -- the parts that decide
how a grant is grouped, how relevant it is, and what a keyword match means.
Nothing here touches the database; the resolution steps that do are exercised
by running etl/evidence.py against the live DB.
"""

import pandas as pd
import pytest

from core_data_loader.common.kg_keywords import (
    AMBIGUOUS_PENALTY,
    CONFIDENCE,
    SUPPLEMENTAL_TERMS,
    KeywordMatcher,
    _match_method,
    _word_boundary_pattern,
)
from core_data_loader.etl.evidence import (
    award_key,
    contact_pi,
    keep_hit,
    organism_context_established,
    relevance_tier,
)

# --- award_key ------------------------------------------------------------
# RePORTER truncates contract core numbers to 11 characters, merging distinct
# contracts; 26120150000 really covers five unrelated task orders.

def test_award_key_uses_core_project_num_for_ordinary_grants():
    assert award_key("R01AI123456", "5R01AI123456-03", "R01") == "R01AI123456"


def test_award_key_splits_contracts_that_share_a_truncated_core():
    a = award_key("26120150000", "261201500003I-0-26100013-1", "N01")
    b = award_key("26120150000", "261201500003I-0-26100033-1", "N01")
    assert a != b, "distinct task orders must not merge into one award"
    assert a == "261201500003I-0-26100013"


def test_award_key_groups_one_contract_across_its_records():
    # Same task order, different trailing segment -> same award.
    assert (award_key("26120150000", "261201500003I-0-26100059-1", "N01")
            == award_key("26120150000", "261201500003I-0-26100059-2", "N01"))


def test_award_key_handles_contract_project_num_with_no_separator():
    assert award_key("1AI0683-1-0", "Y1AI0683", "Y01") == "Y1AI0683"


def test_award_key_falls_back_to_core_when_contract_project_num_is_blank():
    assert award_key("26120150000", "", "N01") == "26120150000"


# --- relevance_tier -------------------------------------------------------

def test_relevance_tier_strong_from_title():
    assert relevance_tier("Aerosolized Ebola Virus Infection of Mice", []) == "strong"


def test_relevance_tier_moderate_needs_two_abstract_mentions():
    assert relevance_tier("Vaccine Development", ["Marburg virus and Marburg vaccines"]) == "moderate"


def test_relevance_tier_weak_on_a_single_mention():
    assert relevance_tier("Vaccine Development", ["One ebolavirus mention only"]) == "weak"


def test_relevance_tier_none_when_nothing_matches():
    assert relevance_tier("Influenza Surveillance", ["No relevant pathogen here at all"]) is None


def test_relevance_tier_does_not_match_inside_a_longer_word():
    # 'MARV' must not fire on 'MARVEL'; that's what the boundary guards are for.
    assert relevance_tier("MARVEL domain proteins", ["MARVEL again"]) is None


def test_relevance_tier_counts_mentions_across_several_fiscal_years():
    assert relevance_tier("Vaccine Development", ["one ebola", "another marburg"]) == "moderate"


def test_relevance_tier_checks_every_fiscal_years_title():
    # RePORTER titles change between years; a later rename still counts.
    assert relevance_tier(["Generic Vaccine Work", "Ebola Vaccine Work"], []) == "strong"


def test_relevance_tier_still_accepts_a_single_title_string():
    assert relevance_tier("Ebola Vaccine Work", []) == "strong"


# --- keyword boundaries ---------------------------------------------------

@pytest.mark.parametrize("term,text,expected", [
    ("VP40", "the VP40 matrix protein", True),
    ("VP40", "VP40-specific antibodies", True),      # hyphen separates tokens
    ("VP40", "xxVP40xx", False),                      # embedded in a word
    ("GP", "GP-directed therapy", True),              # hyphen separates, so this matches
    ("GP", "EBOLAVIRUS GP PEPTIDES", True),
    ("GP", "GPCR signalling", False),
    ("1-5D", "antibody 1-5D binds", True),
    ("1-5D", "antibody 1-5DX binds", False),
    ("Ebola virus", "the ebola virus genome", True),  # case-insensitive
])
def test_word_boundary_pattern(term, text, expected):
    assert bool(_word_boundary_pattern(term).search(text)) is expected


def test_word_boundary_pattern_escapes_regex_metacharacters():
    # A term like 'VP35(C-terminal)' must be matched literally, not compiled
    # as a group -- otherwise the pattern silently matches the wrong thing.
    assert _word_boundary_pattern("VP35(C)").search("see VP35(C) here")
    assert not _word_boundary_pattern("VP35(C)").search("see VP35C here")


# --- match_method ---------------------------------------------------------

@pytest.mark.parametrize("term_type,expected", [
    ("db_gene_label", "gene_label"),
    ("db_alias", "synonym"),
    ("taxon_alias", "synonym"),
    ("db_name", "keyword"),
    ("db_product", "keyword"),
])
def test_match_method(term_type, expected):
    assert _match_method(term_type) == expected


# --- matcher scoring ------------------------------------------------------

def _matcher(rows):
    """A KeywordMatcher with resolution stubbed out -- no DB access."""
    matcher = KeywordMatcher.__new__(KeywordMatcher)
    matcher.keywords = pd.DataFrame(rows)
    matcher.terms = [
        {
            "entity_type": r["entity_type"],
            "entity_id": r["entity_id"],
            "search_term": r["search_term"],
            "term_type": r["term_type"],
            "requires_context": r["requires_context"],
            "pattern": _word_boundary_pattern(r["search_term"]),
        }
        for r in rows
    ]
    return matcher


UNAMBIGUOUS = {"entity_type": "taxon", "entity_id": "186538", "search_term": "Ebola virus",
               "term_type": "db_name", "requires_context": False}
AMBIGUOUS = {"entity_type": "gene", "entity_id": "4", "search_term": "GP",
             "term_type": "db_name", "requires_context": True}


def test_title_matches_outrank_abstract_matches():
    m = _matcher([UNAMBIGUOUS])
    title = m.match("Ebola virus vaccine", "title")[0]
    abstract = m.match("Ebola virus vaccine", "abstract")[0]
    assert title["confidence"] > abstract["confidence"]
    assert title["confidence"] == CONFIDENCE["title"]


def test_ambiguous_terms_are_penalised():
    m = _matcher([AMBIGUOUS])
    hit = m.match("the GP antigen", "title")[0]
    assert hit["confidence"] == pytest.approx(CONFIDENCE["title"] * AMBIGUOUS_PENALTY)
    assert hit["requires_context"] is True


def test_match_returns_nothing_for_blank_text():
    m = _matcher([UNAMBIGUOUS])
    assert m.match("", "title") == []
    assert m.match(None, "title") == []


def test_match_reports_every_hit_for_the_caller_to_collapse():
    m = _matcher([UNAMBIGUOUS, AMBIGUOUS])
    hits = m.match("Ebola virus GP vaccine", "title")
    assert {h["entity_type"] for h in hits} == {"taxon", "gene"}


# --- contact_pi -----------------------------------------------------------

class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_contact_pi_prefers_the_flat_column():
    row = _Row(contact_pi_name="HEIMBROOK, DAVID ", principal_investigators="[]")
    assert contact_pi(row) == "HEIMBROOK, DAVID"


def test_contact_pi_falls_back_to_the_embedded_json():
    row = _Row(contact_pi_name="", principal_investigators=(
        '[{"full_name": "JANE DOE", "is_contact_pi": false},'
        ' {"full_name": "JOHN ROE", "is_contact_pi": true}]'
    ))
    assert contact_pi(row) == "JOHN ROE"


def test_contact_pi_survives_malformed_json():
    # RePORTER occasionally truncates this column; a bad cell must not abort a run.
    row = _Row(contact_pi_name="", principal_investigators='[{"full_name": "TRUNC')
    assert contact_pi(row) is None


# --- organism context -----------------------------------------------------
# kg_keywords flags ambiguous terms as needing organism context; these keep
# that promise. Without it, the bare letters L and N alone produced 110 links.

def _hit(entity_type, requires_context, term="x"):
    return {"entity_type": entity_type, "entity_id": "1", "matched_term": term,
            "term_type": "db_name", "requires_context": requires_context,
            "match_field": "abstract", "match_method": "keyword", "confidence": 0.5}


def test_context_established_by_any_taxon_hit():
    assert organism_context_established([_hit("taxon", False), _hit("gene", True)])


def test_context_not_established_without_a_taxon():
    assert not organism_context_established([_hit("gene", True), _hit("antibody", True)])


def test_context_established_by_a_patent_query_scope_taxon():
    # The query_scope taxon is itself requires_context, but a patent returned
    # by a filovirus CPC search is about filoviruses regardless of its title.
    assert organism_context_established([_hit("taxon", True)])


def test_ambiguous_hit_dropped_without_context():
    assert not keep_hit(_hit("gene", True), has_context=False)


def test_ambiguous_hit_kept_with_context():
    assert keep_hit(_hit("gene", True), has_context=True)


def test_unambiguous_hit_kept_either_way():
    assert keep_hit(_hit("taxon", False), has_context=False)
    assert keep_hit(_hit("taxon", False), has_context=True)


def test_context_on_empty_hits_is_false_and_drops_nothing():
    assert not organism_context_established([])


def test_relevance_tier_matches_a_hyphenated_mention():
    # Same boundary rule as the keyword matcher: hyphens separate tokens.
    assert relevance_tier("Ebola-virus vaccine platforms", []) == "strong"


# --- supplemental vocabulary ----------------------------------------------
# kg_keywords carries formal names but not the shorthand grant titles use.
# Measured: 52 of the 53 grants that had no edge at all said bare "Ebola",
# and only 6 used the phrase "Ebola virus".

def test_supplemental_terms_are_all_taxa_with_real_ncbi_ids():
    for term, entity_type, entity_id, _term_type in SUPPLEMENTAL_TERMS:
        assert entity_type == "taxon", f"{term} resolves by name, not a stable id"
        assert isinstance(entity_id, int) and entity_id > 0


def test_supplemental_terms_map_to_genus_or_family_not_species():
    # "Ebola" in a title does not mean Zaire specifically. Mapping it to the
    # genus lets core.taxon's tree reach species without inventing precision.
    by_term = {t: tid for t, _, tid, _ in SUPPLEMENTAL_TERMS}
    assert by_term["Ebola"] == 186536       # Ebolavirus genus
    assert by_term["Marburg"] == 186537     # Marburgvirus genus
    assert by_term["filovirus"] == 11266    # Filoviridae family


def test_bare_ebola_does_not_match_inside_ebolavirus():
    # 'Ebolavirus' has its own include-scope term; matching both would
    # double-count the same mention.
    pattern = _word_boundary_pattern("Ebola")
    assert not pattern.search("Ebolavirus replication")
    assert pattern.search("Ebola vaccine development")
    assert pattern.search("Ebola-specific antibodies")


def test_plural_and_adjectival_filovirus_forms_are_listed_separately():
    # Terms are matched as whole tokens, so "filovirus" does not cover
    # "filoviruses"; each surface form needs its own entry.
    terms = {t for t, _, _, _ in SUPPLEMENTAL_TERMS}
    assert {"filovirus", "filoviruses", "filoviral"} <= terms
