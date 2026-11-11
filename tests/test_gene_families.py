"""The curated filovirus gene-family mapping (curated/gene_families.py)."""

from core_data_loader.curated.gene_families import GENE_FAMILIES, NAME_TO_FAMILY_ID, family_for

SYMBOL = {family_id: symbol for family_id, symbol, _name, _names in GENE_FAMILIES}


def fam(gene, product=None):
    family_id, basis = family_for(gene, product)
    return (SYMBOL.get(family_id), basis)


def test_the_seven_filovirus_genes_with_fixed_ids():
    assert [(i, s) for i, s, _, _ in GENE_FAMILIES] == [
        (1, "NP"), (2, "VP35"), (3, "VP40"), (4, "GP"), (5, "VP30"), (6, "VP24"), (7, "L")]


def test_every_reference_gene_symbol_has_a_family():
    for symbol in ("NP", "VP35", "VP40", "GP", "sGP", "ssGP", "VP30", "VP24", "L"):
        assert fam(symbol)[0] is not None, symbol


def test_gene_labels_match_case_insensitively():
    assert fam("vp35") == ("VP35", "gene")
    assert fam("SsGP") == ("GP", "gene")
    assert fam("N") == ("NP", "gene")


def test_marburg_product_names_map_to_their_gene():
    # Marburg's annotations name the protein, not the gene.
    assert fam(None, "polymerase complex protein") == ("VP35", "product")
    assert fam(None, "minor nucleoprotein") == ("VP30", "product")
    assert fam(None, "membrane-associated protein") == ("VP24", "product")
    assert fam(None, "matrix protein") == ("VP40", "product")


def test_gene_label_wins_over_product():
    assert fam("VP24", "matrix protein") == ("VP24", "gene")


def test_unrecognised_gene_label_falls_back_to_product():
    assert fam("mbgl", "nucleoprotein") == ("NP", "product")


def test_other_virus_families_and_constructs_stay_unmapped():
    for label in ("Gn", "Gc", "GPC", "envelope glycoprotein", "ZsG-P2A-VP40", "eGFP", "hypothetical protein"):
        assert fam(label) == (None, None), label


def test_no_name_maps_to_two_families():
    seen = {}
    for family_id, _symbol, _name, names in GENE_FAMILIES:
        for n in names:
            assert n == n.lower(), n
            assert seen.setdefault(n, family_id) == family_id, n
    assert len(seen) == len(NAME_TO_FAMILY_ID)
