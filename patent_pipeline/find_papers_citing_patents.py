import pandas as pd
import requests
import time
import re
from pathlib import Path

PAPER_FILE = Path("filovirus_papers.csv")
PATENT_FILE = Path("data/processed/patent_evidence.csv")

OUTPUT = Path("data/processed/paper_patent_reverse_links.csv")

papers = pd.read_csv(PAPER_FILE)
patents = pd.read_csv(PATENT_FILE)

papers = papers[papers["pmcid"].notna()].copy()

papers["pmc_numeric"] = (
    papers["pmcid"]
    .astype(str)
    .str.replace("PMC", "", regex=False)
    .str.strip()
)

team_pmcids = set(papers["pmc_numeric"])

results = []

print("Team papers with PMC full text:", len(papers))
print("Patents to search:", len(patents))
print()


def patent_variants(patent_id):
    patent_id = str(patent_id).strip()

    # Example:
    # US 20180291088 A1

    compact = re.sub(r"\s+", "", patent_id)

    # Remove trailing kind code like A1, B1, B2
    base_with_country = re.sub(
        r"(A1|A2|B1|B2)$",
        "",
        compact
    )

    # Remove US
    number_only = re.sub(
        r"^US",
        "",
        base_with_country
    )

    return list({
        patent_id,
        compact,
        base_with_country,
        number_only
    })


for i, patent in patents.iterrows():

    patent_id = str(patent["patent_id"]).strip()

    variants = patent_variants(patent_id)

    print(
        f"[{i + 1}/{len(patents)}] Searching {patent_id}"
    )

    patent_hits = set()

    for variant in variants:

        params = {
            "db": "pmc",
            "term": f'"{variant}"',
            "retmode": "json",
            "retmax": 100
        }

        try:

            response = requests.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params=params,
                timeout=30
            )

            response.raise_for_status()

            data = response.json()

            hits = set(
                data["esearchresult"]["idlist"]
            )

            patent_hits.update(hits)

        except Exception as e:

            print(
                "   ERROR searching",
                variant,
                ":",
                e
            )

        time.sleep(0.35)

    matches = patent_hits.intersection(team_pmcids)

    for pmc_numeric in matches:

        paper = papers[
            papers["pmc_numeric"] == pmc_numeric
        ].iloc[0]

        results.append({
            "pmid": paper["pmid"],
            "pmcid": paper["pmcid"],
            "paper_title": paper["title"],
            "paper_year": paper["year"],
            "patent_id": patent_id,
            "patent_title": patent["title"],
            "relation": "mentions_or_cites_patent",
            "match_method": "patent number found in PMC full text"
        })

        print(
            "   MATCH:",
            paper["title"]
        )


columns = [
    "pmid",
    "pmcid",
    "paper_title",
    "paper_year",
    "patent_id",
    "patent_title",
    "relation",
    "match_method"
]

if results:

    results_df = pd.DataFrame(results)

    results_df = results_df.drop_duplicates(
        subset=[
            "pmcid",
            "patent_id"
        ]
    )

else:

    results_df = pd.DataFrame(
        columns=columns
    )


results_df.to_csv(
    OUTPUT,
    index=False
)

print()
print("Reverse patent search complete.")
print(
    "Paper → patent links found:",
    len(results_df)
)
print(
    "Saved to:",
    OUTPUT
)

if len(results_df) > 0:

    print()

    print(
        results_df[
            [
                "paper_title",
                "patent_id",
                "patent_title"
            ]
        ].to_string(index=False)
    )