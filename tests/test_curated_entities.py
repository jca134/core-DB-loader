"""Curated countermeasures and organization-name normalization (no database)."""

from core_data_loader.common.link_keys import link_key
from core_data_loader.curated.countermeasures import COUNTERMEASURES, KINDS, STATUSES
from core_data_loader.curated.gene_families import GENE_FAMILIES
from core_data_loader.curated.organizations import kind_of, normalize, split_names

# --- countermeasures -------------------------------------------------------------

def test_countermeasure_ids_and_names_are_unique():
    assert len({c.countermeasure_id for c in COUNTERMEASURES}) == len(COUNTERMEASURES)
    assert len({c.name for c in COUNTERMEASURES}) == len(COUNTERMEASURES)
    names = [n.lower() for c in COUNTERMEASURES for n in c.names]
    assert len(names) == len(set(names)), "a name may point at only one countermeasure"


def test_countermeasure_fields_are_valid():
    families = {symbol for _, symbol, _, _ in GENE_FAMILIES}
    for c in COUNTERMEASURES:
        assert c.kind in KINDS and c.status in STATUSES, c.name
        assert c.target_taxa, c.name
        assert c.target_gene_family in families | {None}, c.name
        assert bool(c.regulatory) == (c.status == "licensed"), f"{c.name}: approvals only for licensed products"


# --- organizations ------------------------------------------------------------------

def test_us_army_spellings_merge_including_typos():
    for spelling in ("The United States of America as represented by the Secretary of the Army",
                     "The Govermnment Of The United States As Represented By The Secretary Of The Army",
                     "U.S. ARMY MEDICAL RESEARCH AND MATERIAL COMMAND"):
        assert normalize(spelling) == "US ARMY", spelling


def test_hhs_spellings_merge_but_state_agencies_do_not():
    for spelling in ("The U.S.A., as represented by the Secretary, Department of Health and Human Services",
                     "TheGovernmentof the UnitedStatesof America asRepre -sentedby theSecretaryoftheDept.ofHealth&HumanSer",
                     "Department of Health & HumanServices"):
        assert normalize(spelling) == "US DEPARTMENT OF HEALTH AND HUMAN SERVICES", spelling
    assert normalize("MICHIGAN STATE DEPARTMENT OF HEALTH AND HUMAN SERVICES") != \
        "US DEPARTMENT OF HEALTH AND HUMAN SERVICES"


def test_grant_and_patent_spellings_of_one_institution_meet():
    assert normalize("SCRIPPS RESEARCH INSTITUTE, THE") == normalize("The Scripps Research Institute")
    assert normalize("BOSTON UNIVERSITY MEDICAL CAMPUS") == normalize("Trustees of Boston University")
    assert normalize("Integrated BioTherapeutics, Inc.") == normalize("INTEGRATED BIOTHERAPEUTICS, INC.")


def test_assignee_fields_split_on_bare_commas_and_drop_people():
    assert split_names("Janssen Vaccines & Prevention B.V.,Bavarian Nordic A/S") == \
        ["Janssen Vaccines & Prevention B.V.", "Bavarian Nordic A/S"]
    assert split_names("Genentech, Inc.") == ["Genentech, Inc."]
    assert split_names("Hiatt; Andrew,Zeitlin; Larry") == []


def test_organization_kinds():
    assert kind_of("US ARMY") == "government"
    assert kind_of("EMORY UNIVERSITY") == "university"
    assert kind_of("REGENERON PHARMACEUTICALS") == "company"


# --- link keys ----------------------------------------------------------------------

def test_link_keys_use_natural_ids_and_skip_unknown_rows():
    ids = {"grant_award": {"7": "R01AI123456"}, "gene": {"3": "GP"}}
    assert link_key(ids, "grant_award", 7, "gene", "3", "related") == "grant_award:R01AI123456|gene:GP|related"
    assert link_key(ids, "grant_award", 8, "gene", "3", "related") is None


def test_organization_kinds_cover_nih_institutes_states_and_suffixed_companies():
    assert kind_of(normalize("NATIONAL EYE INSTITUTE")) == "government"
    assert kind_of(normalize("EUNICE KENNEDY SHRIVER NATIONAL INSTITUTE OF CHILD HEALTH & HUMAN DEVELOPMENT")) == "government"
    assert kind_of(normalize("NATIONAL CENTER FOR ADVANCING TRANSLATIONAL SCIENCES")) == "government"
    assert kind_of(normalize("DELAWARE DIVISION OF PUBLIC HEALTH")) == "government"
    assert kind_of(normalize("WISTAR INSTITUTE OF ANATOMY AND BIOLOGY")) == "nonprofit"
    assert kind_of(normalize("Bavarian Nordic A/S"), ["Bavarian Nordic A/S"]) == "company"
    assert kind_of(normalize("Dana-Farber Cancer Institute, Inc."), ["Dana-Farber Cancer Institute, Inc."]) == "nonprofit"
    assert kind_of(normalize("HARVARD MEDICAL SCHOOL")) == "university"


def test_address_fragments_are_not_organizations():
    field = "The Government of the United States as represented by the Secretary of the Army, Ft Detrick,Md,Humabs BioMed SA"
    names = split_names(field)
    assert "Md" not in names and "Humabs BioMed SA" in names
