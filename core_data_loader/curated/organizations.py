"""
Organizations: the institutions that receive grants and own patents, as one
node each, however the sources spell them.

WHY. The same players appear across sources under different names:
"The United States of America as represented by the Secretary of the Army"
on a patent, "SCRIPPS RESEARCH INSTITUTE, THE" on a grant. As free text on
each document they connect nothing; as nodes they answer "who received the
NIH money and also patented the result".

HOW. Every name goes through normalize(): case, punctuation, legal suffixes
("Inc.", "LLC", "B.V."), legal preambles ("Trustees of", "Board of Regents
of") and RePORTER abbreviations ("UNIV", "MED BR") are evened out. The result
is the organization's key. ALIASES then merges keys that normalization can't
know are the same institution. A patent's applicant field sometimes lists
people ("Hiatt; Andrew"); those are not organizations and are skipped.

To merge two spellings, add the normalized form of one as an ALIASES key
pointing at the other's normalized form, then rerun `filovirus build`.
`filovirus build` prints the organizations shared by grants and patents.
"""

import re
import unicodedata

LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "LLC", "LTD", "LIMITED", "CORP", "CORPORATION", "CO", "COMPANY",
    "BV", "B V", "NV", "AS", "A S", "AB", "AG", "SE", "SA", "SAS", "GMBH", "PLC", "LP", "LLP", "PC",
}
PREAMBLES = (
    "THE ", "TRUSTEES OF THE ", "TRUSTEES OF ", "BOARD OF TRUSTEES OF THE ", "BOARD OF TRUSTEES OF ",
    "THE BOARD OF TRUSTEES OF THE ", "THE BOARD OF REGENTS OF THE ", "BOARD OF REGENTS OF THE ",
    "BOARD OF REGENTS OF ", "THE REGENTS OF THE ", "REGENTS OF THE ", "PRESIDENT AND FELLOWS OF ",
    "NATIONAL UNIVERSITY CORPORATION ", "RHE TRUSTEES OF ", "GOVERNING COUNCIL OF ", "GOVERNING COUNCEL OF ",
    "CHANCELLOR MASTERS AND SCHOLARS OF ", "MASTERS AND SCHOLARS OF ", "BOARD OF SUPERVISORS OF ",
)
ABBREVIATIONS = {
    "UNIV": "UNIVERSITY", "MED": "MEDICAL", "BR": "BRANCH", "INST": "INSTITUTE", "CTR": "CENTER",
    "SCH": "SCHOOL", "HOSP": "HOSPITAL", "RES": "RESEARCH", "LAB": "LABORATORY", "LABS": "LABORATORIES",
    "DEPT": "DEPARTMENT", "NATL": "NATIONAL", "TECH": "TECHNOLOGY", "SCI": "SCIENCES", "HLTH": "HEALTH",
}

# normalized key -> the normalized key it is the same institution as.
ALIASES = {
    # US government, as patent assignees spell it vs. as grants do.
    "UNITED STATES OF AMERICA AS REPRESENTED BY SECRETARY OF ARMY": "US ARMY",
    "GOVERNMENT OF UNITED STATES AS REPRESENTED BY SECRETARY OF ARMY": "US ARMY",
    "US ARMY MEDICAL RESEARCH INSTITUTE OF INFECTIOUS DISEASES": "US ARMY",
    "USAMRIID": "US ARMY",
    "USA AS REPRESENTED BY SECRETARY DEPARTMENT OF HEALTH AND HUMAN SERVICES": "US DEPARTMENT OF HEALTH AND HUMAN SERVICES",
    "UNITED STATES OF AMERICA AS REPRESENTED BY SECRETARY DEPARTMENT OF HEALTH AND HUMAN SERVICES":
        "US DEPARTMENT OF HEALTH AND HUMAN SERVICES",
    "UNITED STATES OF AMERICA AS REPRESENTED BY SECRETARY OF DEPARTMENT OF HEALTH AND HUMAN SERVICES":
        "US DEPARTMENT OF HEALTH AND HUMAN SERVICES",
    # Same institution, campus or legal-entity name differs between sources.
    "BOSTON UNIVERSITY MEDICAL CAMPUS": "BOSTON UNIVERSITY",
    "LELAND STANFORD JUNIOR UNIVERSITY": "STANFORD UNIVERSITY",
    "UNIVERSITY OF TEXAS MEDICAL BRANCH GALVESTON": "UNIVERSITY OF TEXAS MEDICAL BRANCH",
    "UNIVERSITY OF TEXAS MEDICAL BRANCH AT GALVESTON": "UNIVERSITY OF TEXAS MEDICAL BRANCH",
    "UNIVERSITY OF NORTH CAROLINA CHAPEL HILL": "UNIVERSITY OF NORTH CAROLINA AT CHAPEL HILL",
    "VANDERBILT UNIVERSITY MEDICAL CENTER": "VANDERBILT UNIVERSITY",
    "ALBERT EINSTEIN COLLEGE OF MEDICINE OF YESHIVA UNIVERSITY": "ALBERT EINSTEIN COLLEGE OF MEDICINE",
    "COLUMBIA UNIVERSITY IN CITY OF NEW YORK": "COLUMBIA UNIVERSITY",
    "COLUMBIA UNIVERSITY HEALTH SCIENCES": "COLUMBIA UNIVERSITY",
    "GENERAL HOSPITAL": "MASSACHUSETTS GENERAL HOSPITAL",        # "The General Hospital Corporation" is MGH
    "WARF WISCONSIN ALUMNI RESEARCH FOUNDATION": "WISCONSIN ALUMNI RESEARCH FOUNDATION",
    "WISCONSIN ALUMNI RESEARCH FOUNDATION WARF": "WISCONSIN ALUMNI RESEARCH FOUNDATION",
    "WISTARE INSTITUTE OF ANATOMY AND BIOLOGY": "WISTAR INSTITUTE OF ANATOMY AND BIOLOGY",
}

# The US federal government is spelled a dozen ways on patents, typos and
# run-together words included ("THEGOVERNMENTOF UNITEDSTATESOF AMERICA ...").
# Rules, not aliases: a key naming the US government (or a bare federal
# department) and the Army / HHS is that agency. State agencies ("MICHIGAN
# STATE DEPARTMENT OF HEALTH AND HUMAN SERVICES") are left alone.
US_GOVERNMENT_RE = re.compile(r"UNITED ?STATES|^US |^USA |GOVERN?M?N?MENT|AS ?REPRE ?SENTED")
STATE_RE = re.compile(r"\b(STATE|COMMONWEALTH|COUNTY|CITY)\b(?! OF AMERICA)")
ARMY_RE = re.compile(r"\bARMY\b|MATERI[AE]L COMMAND")
HHS_RE = re.compile(r"HEALTH ?AND ?HUMAN|\bHHS\b|HUMANSER|SECRETARY OF (?:THE )?DEPARTMENT OF HEALTH$")

# Display names for merged keys that read badly when generated.
DISPLAY = {
    "US ARMY": "US Army (incl. USAMRIID)",
    "US DEPARTMENT OF HEALTH AND HUMAN SERVICES": "US Department of Health and Human Services",
    "US GOVERNMENT": "US Government (agency not stated)",
}

KIND_RULES = [
    # NIH institutes, centers and divisions file grants under their own names
    # ("NATIONAL EYE INSTITUTE", "DIVISION OF CANCER TREATMENT").
    ("government", r"\b(US ARMY|UNITED STATES|US GOVERNMENT|US DEPARTMENT|DEPARTMENT|CENTERS FOR DISEASE|"
                   r"FOOD AND DRUG|NAVY|AIR FORCE|COMMISSARIAT|ACADEMY OF MILITARY|PLA|NIH|NIAID|NCI|"
                   r"NATIONAL INSTITUTES? OF|PUBLIC HEALTH|BUREAU OF|CABINET|CNRS|INSERM|INSTITUT NATIONAL|"
                   r"FEDERATED STATES)\b|"
                   r"^NATIONAL [A-Z ]*(INSTITUTE|INSTITUTES|CENTER)\b|^DIVISION OF\b|^OFFICE OF\b|^CLINICAL CENTER$"),
    ("university", r"\b(UNIVERSITY|UNIVERSITE|UNIVERSITAT|COLLEGE|ECOLE|SCHOOL OF MEDICINE|MEDICAL SCHOOL|"
                   r"INSTITUTE OF TECHNOLOGY)\b"),
    ("hospital", r"\b(HOSPITAL|HOSPITALIER|MEDICAL CENTER|CHILDREN S MEDICAL|CLINIC)\b"),
    ("company", r"\b(BIOSCIENCES?|BIOTECH\w*|BIOTHERAPEUTICS|BIOSOLUTIONS|BIOPHARMA\w*|BIOLOGICS|BIOLOGICALS|"
                r"PHARMA\w*|THERAPEUTICS|VACCINES|TECHNOLOGIES|LABORATORIES|GENENTECH|REGENERON|LEIDOS|BIOHUB)\b"),
    ("nonprofit", r"\b(INSTITUTE|INSTITUT|ZENTRUM|FOUNDATION|FDN|RESEARCH|SOCIETY|INTERNATIONAL)\b"),
]
# A legal suffix on any source spelling ("Inc.", "LLC", "A/S", "Corp") says
# company once the rules above have had their say -- "Dana-Farber Cancer
# Institute, Inc." is still a nonprofit institute.
LEGAL_SUFFIX_RE = re.compile(
    r"[\s,](Inc|LLC|L\.L\.C|Ltd|Limited|Corp|Corporation|Co|B\.?V|N\.?V|A/?S|AB|AG|SE|SA|SAS|GmbH|PLC)\.?\s*$",
    re.IGNORECASE)

PERSON_RE = re.compile(r"^[A-Za-z' -]+; ?[A-Za-z' .-]+$")   # "Hiatt; Andrew"
SUFFIX_RE = re.compile(r"^\s*(Inc|LLC|Ltd|Corp|Co|B\.?V|N\.?V|A/?S|AG|SE|SA|GmbH|PLC|L\.?P)\.?\s*$", re.IGNORECASE)


def split_names(field: str | None) -> list[str]:
    """
    One source field -> its organization names. Patent fields join several
    assignees with commas, and commas also sit inside a name ("Genentech,
    Inc."), so a fragment that is only a legal suffix rejoins the name before
    it. Person names ("Hiatt; Andrew") are dropped.
    """
    if not field or not field.strip():
        return []
    # USPTO joins assignees with a bare comma ("...B.V.,Bavarian Nordic A/S");
    # a comma inside one name is followed by a space ("Genentech, Inc.",
    # "The U.S.A., as represented by the Secretary, ...").
    names = []
    for part in re.split(r",(?=\S)", field):
        if names and SUFFIX_RE.match(part):
            names[-1] = f"{names[-1]},{part}"
        elif part.strip():
            names.append(part.strip())
    # A two-letter fragment is a state left over from an address ("Ft Detrick,Md,..."), not an organization.
    return [n for n in dict.fromkeys(names)
            if not PERSON_RE.match(n) and normalize(n) and not re.fullmatch(r"[A-Za-z]{2}\.?", n)]


def normalize(name: str) -> str:
    """The organization key for one spelling of its name; '' if there is none."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()   # Für -> Fur
    s = re.sub(r"[^A-Z0-9 ]+", " ", name.upper().replace("&", " AND ").replace("U.S.A.", "USA")
               .replace("U.S.", "US"))
    s = " ".join(s.split())
    s = re.sub(r"\s+THE$", "", s)                           # "SCRIPPS RESEARCH INSTITUTE, THE"
    changed = True
    while changed:
        changed = False
        for p in PREAMBLES:
            if s.startswith(p):
                s, changed = s[len(p):], True
    words = [ABBREVIATIONS.get(w, w) for w in s.split()]
    while words and (words[-1] in LEGAL_SUFFIXES or " ".join(words[-2:]) in LEGAL_SUFFIXES):
        words = words[:-2] if " ".join(words[-2:]) in LEGAL_SUFFIXES else words[:-1]
    s = " ".join(w for w in words if w != "THE")
    s = re.sub(r"\bOF (THE )?", "OF ", s)
    key = " ".join(s.split())
    if not STATE_RE.search(key):
        federal = bool(US_GOVERNMENT_RE.search(key)) or key.startswith("DEPARTMENT")
        if federal and ARMY_RE.search(key) or key.startswith("US ARMY"):
            return "US ARMY"
        if federal and HHS_RE.search(key):
            return "US DEPARTMENT OF HEALTH AND HUMAN SERVICES"
        if US_GOVERNMENT_RE.search(key) and re.search(r"UNITED ?STATES|^US |^USA ", key):
            return "US GOVERNMENT"                  # no agency named, or cut off before it
    return ALIASES.get(key, key)


def kind_of(key: str, spellings=()) -> str:
    for kind, pattern in KIND_RULES:
        if re.search(pattern, key):
            return kind
    if any(LEGAL_SUFFIX_RE.search(s) for s in spellings):
        return "company"
    return "other"


def display_name(key: str, spellings: list[str]) -> str:
    """A readable name: the curated one, else the most common source spelling."""
    if key in DISPLAY:
        return DISPLAY[key]
    best = max(spellings, key=lambda s: (spellings.count(s), not s.isupper(), -len(s)))
    return best.strip().rstrip(",")
