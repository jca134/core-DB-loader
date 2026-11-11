"""
Tests for the curated LANL-target mapping in
core_data_loader.curated.antibody_targets.

LANL reports an antibody's target as a bare product label. core.antibody_epitope
has a protein_id column for it, but a label like "glycoprotein" cannot pick one
of the 32004 accession-keyed core.protein rows, so that column is null on all
149 rows. The mapping below is what makes "which antibodies target GP"
answerable at all.
"""

import pytest

from core_data_loader.curated.antibody_targets import LANL_PROTEIN_TO_GENE_SYMBOL

# The nine symbols in UCSC's reference gene model, which core.gene holds.
GENE_MODEL = {"GP", "L", "NP", "sGP", "ssGP", "VP24", "VP30", "VP35", "VP40"}


def test_every_target_maps_to_a_symbol_in_the_gene_model():
    for label, symbol in LANL_PROTEIN_TO_GENE_SYMBOL.items():
        assert symbol in GENE_MODEL, f"{label} -> {symbol} is not a core.gene symbol"


def test_keys_are_lowercase_so_lookup_is_case_insensitive():
    # The resolver lowercases the raw label before looking it up; an uppercase
    # key here would silently never match.
    for label in LANL_PROTEIN_TO_GENE_SYMBOL:
        assert label == label.lower()


@pytest.mark.parametrize("label,symbol", [
    ("glycoprotein", "GP"),
    ("nucleoprotein", "NP"),
    ("vp35", "VP35"),
    ("vp40 (matrix)", "VP40"),
])
def test_the_four_labels_that_cover_the_real_data(label, symbol):
    # These four account for 148 of the 149 rows in hfv_ebola_antibody.
    assert LANL_PROTEIN_TO_GENE_SYMBOL[label] == symbol


def test_unknown_is_not_mapped():
    # "unknown" is LANL's placeholder for a target they did not record.
    # Mapping it to anything would invent a claim the source never made.
    assert "unknown" not in LANL_PROTEIN_TO_GENE_SYMBOL


def test_glycoprotein_maps_to_gp_not_sgp_or_ssgp():
    # GP, sGP and ssGP are distinct products of the same gene and the
    # keyword vocabulary keeps them apart; the bare label means GP.
    assert LANL_PROTEIN_TO_GENE_SYMBOL["glycoprotein"] == "GP"
