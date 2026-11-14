import pandas as pd
import requests
import re
import time
from bs4 import BeautifulSoup
from pathlib import Path

INPUT = Path("data/processed/patent_evidence_expanded.csv")
OUTPUT = Path("data/processed/patent_evidence_with_abstracts.csv")

df = pd.read_csv(INPUT)

headers = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
    )
}


def compact_patent_id(x):
    return re.sub(r"\s+", "", str(x).strip())


abstracts = []

print("Patents to process:", len(df))
print()

for i, row in df.iterrows():

    patent_id = compact_patent_id(row["patent_id"])

    url = f"https://patents.google.com/patent/{patent_id}/en"

    print(f"[{i+1}/{len(df)}] {patent_id}")

    abstract = ""

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=30
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        # Google Patents normally stores the abstract here
        abstract_tag = soup.find(
            attrs={"itemprop": "abstract"}
        )

        if abstract_tag:
            abstract = abstract_tag.get_text(
                " ",
                strip=True
            )

        print(
            "   abstract:",
            "FOUND" if abstract else "NOT FOUND"
        )

    except Exception as e:
        print("   ERROR:", e)

    abstracts.append(abstract)

    time.sleep(0.8)


df["abstract"] = abstracts

df.to_csv(
    OUTPUT,
    index=False
)

print()
print("ABSTRACT EXTRACTION COMPLETE")
print("----------------------------")

print(
    "Patents with abstract:",
    df["abstract"]
    .astype(str)
    .str.strip()
    .ne("")
    .sum()
)

print("Total patents:", len(df))
print()
print("Saved to:", OUTPUT)