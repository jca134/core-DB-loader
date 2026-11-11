"""
The seven filovirus gene families, and the curated names that map a protein
(or a reference gene) onto one of them.

WHY A FAMILY, NOT A GENE. core.gene is UCSC's Ebola reference gene model, so it
holds Ebola's genes only. Every filovirus -- Ebola, Sudan, Marburg, Lloviu, ...
-- has the same seven genes in the same order, but Marburg's GP is Marburg's
gene, not a copy of Ebola's. Linking Marburg proteins to core.gene would assert
something false. A gene family is the shared, virus-neutral concept: "the GP
gene of a filovirus". Each core.gene row and each core.protein row points at
its family. Which virus a protein comes from is still told by its own path,
protein -> feature -> sequence -> isolate -> taxon.

That bridge is what lets a Marburg epitope reach the grants and patents about
GP, in a few hops and without pretending Marburg is Ebola.

THE NAMES. BV-BRC labels the same gene many ways ('VP35', 'vp35', 'polymerase
complex protein', 'viral protein 35'), and 3,075 proteins have no gene label at
all, only a product name. A protein is mapped from its gene label if that is
listed here, otherwise from its product name. Names are matched
case-insensitively and whole: nothing is guessed from substrings.

Left unmapped on purpose:
  - glycoprotein names from other virus families that turn up in the BV-BRC
    Filoviridae pull (Gn, Gc, GPC, 'pre-glycoprotein polyprotein GP complex',
    'envelope glycoprotein');
  - reporter constructs ('ZsG-P2A-VP40', 'NLuc-P2A-NP', 'EGFP');
  - anything vague ('hypothetical protein', 'signal peptide').

To map a new name, add it to the right family's tuple and rerun
`filovirus build`.
"""

# (gene_family_id, symbol, name, names that map to it). The ids are fixed
# here, never generated, so they are stable across rebuilds and databases.
GENE_FAMILIES = [
    (1, "NP", "Nucleoprotein", (
        "np", "n", "nucleoprotein", "major nucleoprotein", "putative nucleoprotein",
    )),
    (2, "VP35", "Polymerase cofactor VP35", (
        "vp35", "polymerase cofactor vp35", "polymerase complex protein",
        "viral protein 35", "virus protein 35", "virion protein 35",
    )),
    (3, "VP40", "Matrix protein VP40", (
        "vp40", "matrix protein", "vp40 matrix protein", "membrane-associated protein vp40",
        "viral protein 40", "virus protein 40", "virus protein 40 matrix protein",
    )),
    (4, "GP", "Glycoprotein (GP, and the secreted sGP / ssGP from the same gene)", (
        "gp", "glycoprotein", "spike glycoprotein", "spike glycoprotein precursor",
        "virion spike glycoprotein", "virion spike glycoprotein precursor",
        "putative glycoprotein", "putative glycoprotein 1", "putative glycoprotein 2",
        "gp1", "gp2", "gp1,2",
        "sgp", "sgp_precursor", "secreted glycoprotein", "small secreted glycoprotein sgp",
        "ssgp", "small soluble glycoprotein ssgp", "putative ssgp",
        "delta",  # the delta-peptide, cleaved from sGP
    )),
    (5, "VP30", "Transcription activator VP30", (
        "vp30", "minor nucleoprotein", "transcription activator vp30", "truncated vp30",
        "viral protein 30", "virus protein 30",
    )),
    (6, "VP24", "Membrane-associated protein VP24", (
        "vp24", "membrane-associated protein", "membrane-associated protein vp24",
        "viral protein 24", "virus protein 24",
    )),
    (7, "L", "RNA-dependent RNA polymerase L", (
        "l", "l protein", "l-protein", "putative l-protein", "polymerase", "polymerase l",
        "rna polymerase", "viral polymerase", "rdrp",
        "rna-dependent rna polymerase", "rna-directed rna polymerase l",
    )),
]

NAME_TO_FAMILY_ID = {
    name: family_id
    for family_id, _symbol, _name, names in GENE_FAMILIES
    for name in names
}


def family_for(gene_label: str | None, product: str | None) -> tuple[int | None, str | None]:
    """
    (gene_family_id, basis) for one protein: basis is 'gene' when its gene
    label was recognised, 'product' when only its product name was, and
    (None, None) when neither was.
    """
    for value, basis in ((gene_label, "gene"), (product, "product")):
        family_id = NAME_TO_FAMILY_ID.get((value or "").strip().lower())
        if family_id is not None:
            return family_id, basis
    return None, None
