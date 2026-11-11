"""
Filovirus medical countermeasures: the vaccines, antibody products and
antivirals that came out of the work this graph describes.

WHY. The graph had antibodies and epitopes but no node for the products made
from them, so "which grant-funded antibodies became licensed therapeutics" had
nothing to land on. SerenQA's benchmark KG (CKG) is drug-centric, and its
question types assume drug nodes.

WHAT GOES IN. Products that are licensed or reached human trials against a
filovirus, plus a few well-documented late-preclinical antibody cocktails.
Each is linked to:
  - the virus taxa it targets (species/genus roots; CHILD_OF covers strains);
  - the gene family it acts on (GP for vaccines and antibodies, L for the
    polymerase inhibitors);
  - its component antibodies, when they are in core.antibody (by name);
  - the ImmPort study that evaluated it, when one is loaded;
  - and the grants, patents and papers that name it (`kg_keywords`-style
    keyword matching in etl/evidence.py, using `names`).

Statuses are deliberately coarse: 'licensed', 'clinical' (tested in people,
not licensed for a filovirus), 'preclinical'. `regulatory` records the
approvals only for licensed products. Edit here and rerun `filovirus build`.
"""

from dataclasses import dataclass, field

ZAIRE = 3052462          # Orthoebolavirus zairense
EBOLA_GENERA = (3044781, 186536)   # Orthoebolavirus + legacy Ebolavirus
MARBURG = (3052505, 11269)          # Orthomarburgvirus marburgense + legacy Marburg marburgvirus


@dataclass(frozen=True)
class Countermeasure:
    countermeasure_id: int          # fixed here, never generated
    name: str
    kind: str                       # vaccine | antibody | small_molecule | sirna
    status: str                     # licensed | clinical | preclinical
    developer: str
    regulatory: str                 # approvals, licensed products only
    target_taxa: tuple[int, ...]
    target_gene_family: str | None  # symbol in curated/gene_families.py
    names: tuple[str, ...]          # matched case-insensitively as whole words
    antibodies: tuple[str, ...] = field(default=())   # core.antibody names
    studies: tuple[str, ...] = field(default=())      # core.study accessions


COUNTERMEASURES = [
    # --- vaccines ---------------------------------------------------------------
    Countermeasure(1, "Ervebo (rVSV-ZEBOV)", "vaccine", "licensed", "Merck",
                   "EMA 2019, FDA 2019", (ZAIRE,), "GP",
                   ("Ervebo", "rVSV-ZEBOV", "rVSV-ZEBOV-GP", "rVSVΔG-ZEBOV-GP", "rVSV-dG-ZEBOV-GP", "V920"),
                   studies=("SDY1373",)),
    Countermeasure(2, "Zabdeno + Mvabea (Ad26.ZEBOV / MVA-BN-Filo)", "vaccine", "licensed", "Janssen",
                   "EMA 2020", (ZAIRE,), "GP",
                   ("Zabdeno", "Mvabea", "Ad26.ZEBOV", "MVA-BN-Filo", "Ad26.ZEBOV/MVA-BN-Filo")),
    Countermeasure(3, "ChAd3-EBO-Z", "vaccine", "clinical", "NIAID / GSK", "", (ZAIRE,), "GP",
                   ("ChAd3-EBO-Z", "cAd3-EBOZ", "cAd3-EBO Z", "ChAd3-EBOZ")),
    Countermeasure(4, "ChAd3-MARV", "vaccine", "clinical", "Sabin Vaccine Institute / NIAID", "", MARBURG, "GP",
                   ("ChAd3-MARV", "cAd3-Marburg", "cAd3-MARV")),
    # --- antibodies ---------------------------------------------------------------
    Countermeasure(5, "Inmazeb (REGN-EB3)", "antibody", "licensed", "Regeneron",
                   "FDA 2020", (ZAIRE,), "GP",
                   ("Inmazeb", "REGN-EB3", "atoltivimab", "maftivimab", "odesivimab")),
    Countermeasure(6, "Ebanga (ansuvimab, mAb114)", "antibody", "licensed", "Ridgeback Biotherapeutics",
                   "FDA 2020", (ZAIRE,), "GP",
                   ("Ebanga", "ansuvimab", "mAb114", "mAb 114")),
    Countermeasure(7, "ZMapp", "antibody", "clinical", "Mapp Biopharmaceutical", "", (ZAIRE,), "GP",
                   ("ZMapp",), antibodies=("13C6-1-1", "2G4", "4G7")),
    Countermeasure(8, "MBP134", "antibody", "preclinical", "Mapp Biopharmaceutical", "", EBOLA_GENERA, "GP",
                   ("MBP134",)),
    Countermeasure(9, "MR186-YTE", "antibody", "clinical", "Mapp Biopharmaceutical", "", MARBURG, "GP",
                   ("MR186-YTE", "MR186")),
    # --- antivirals -------------------------------------------------------------
    Countermeasure(10, "Remdesivir (GS-5734)", "small_molecule", "clinical", "Gilead Sciences", "",
                   EBOLA_GENERA + MARBURG, "L", ("remdesivir", "GS-5734")),
    Countermeasure(11, "Obeldesivir (GS-5245)", "small_molecule", "clinical", "Gilead Sciences", "",
                   EBOLA_GENERA + MARBURG, "L", ("obeldesivir", "GS-5245")),
    Countermeasure(12, "Favipiravir (T-705)", "small_molecule", "clinical", "Fujifilm Toyama Chemical", "",
                   EBOLA_GENERA, "L", ("favipiravir", "T-705")),
    Countermeasure(13, "Galidesivir (BCX4430)", "small_molecule", "clinical", "BioCryst", "",
                   EBOLA_GENERA + MARBURG, "L", ("galidesivir", "BCX4430")),
    Countermeasure(14, "TKM-Ebola (TKM-100802)", "sirna", "clinical", "Tekmira", "",
                   (ZAIRE,), "L", ("TKM-Ebola", "TKM-100802", "TKM-130803")),
]

KINDS = ("vaccine", "antibody", "small_molecule", "sirna")
STATUSES = ("licensed", "clinical", "preclinical")
