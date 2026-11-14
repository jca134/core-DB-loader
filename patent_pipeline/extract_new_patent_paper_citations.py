import pandas as pd
import requests
import re
import time
from bs4 import BeautifulSoup
from pathlib import Path

NEW_PATENTS_FILE = Path(
    "data/processed/new_patents_from_expansion.csv"
)

OUTPUT = Path(
    "data/processed/new_patent_paper_citations_raw.csv"
)

FAILED_OUTPUT = Path(
    "data/processed/patent_citation_scrape_failures.csv"
)


def normalize_patent_id(value):
    return re.sub(
        r"\s+",
        "",
        str(value).strip()
    )


def clean_text(text):
    if text is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(text)
    ).strip()


patents = pd.read_csv(NEW_PATENTS_FILE)

results = []
failures = []

headers = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/120.0 Safari/537.36"
    )
}

print("New patents to process:", len(patents))
print()


for i, row in patents.iterrows():

    patent_id_original = str(
        row["Document ID"]
    ).strip()

    patent_id = normalize_patent_id(
        patent_id_original
    )

    patent_title = clean_text(
        row.get("Title", "")
    )

    url = (
        f"https://patents.google.com/"
        f"patent/{patent_id}/en"
    )

    print(
        f"[{i + 1}/{len(patents)}] {patent_id}"
    )

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

        citations = []

        # -----------------------------------------
        # Find heading containing:
        # Non-Patent Citations
        # -----------------------------------------

        heading = soup.find(
            lambda tag:
            tag.name in [
                "h2",
                "h3",
                "heading"
            ]
            and "Non-Patent Citations"
            in tag.get_text(" ", strip=True)
        )

        if heading:

            # Look through elements after heading
            current = heading.find_next()

            while current:

                # Stop if next major section begins
                if (
                    current.name in ["h2", "h3"]
                    and current != heading
                ):
                    break

                # Google Patents citation rows
                if current.name == "tr":

                    text = clean_text(
                        current.get_text(
                            " ",
                            strip=True
                        )
                    )

                    # Ignore header row
                    if (
                        text
                        and text.lower() != "title"
                    ):
                        citations.append(text)

                current = current.find_next()


        # -----------------------------------------
        # Backup:
        # Look for elements whose text directly
        # contains citation entries
        # -----------------------------------------

        if not citations:

            section_text = soup.get_text(
                "\n",
                strip=True
            )

            if "Non-Patent Citations" in section_text:

                start = section_text.find(
                    "Non-Patent Citations"
                )

                chunk = section_text[
                    start:start + 15000
                ]

                lines = [
                    clean_text(x)
                    for x in chunk.split("\n")
                ]

                lines = [
                    x for x in lines
                    if len(x) > 30
                ]

                # Stop around next section
                for line in lines:

                    if line.startswith(
                        "Also Published As"
                    ):
                        break

                    if line.startswith(
                        "Cited By"
                    ):
                        break

                    if line not in [
                        "Non-Patent Citations",
                        "Title"
                    ]:
                        citations.append(line)


        citations = list(
            dict.fromkeys(citations)
        )

        print(
            "   citations found:",
            len(citations)
        )


        for citation in citations:

            year_match = re.search(
                r"\b(?:19|20)\d{2}\b",
                citation
            )

            year = (
                year_match.group(0)
                if year_match
                else ""
            )

            results.append({
                "patent_id":
                    patent_id_original,

                "patent_title":
                    patent_title,

                "citation_text":
                    citation,

                "paper_year":
                    year,

                "relation":
                    "cites",

                "source":
                    "Google Patents",

                "source_url":
                    url
            })


    except Exception as e:

        print("   ERROR:", e)

        failures.append({
            "patent_id":
                patent_id_original,

            "patent_title":
                patent_title,

            "url":
                url,

            "error":
                str(e)
        })


    time.sleep(1)


# -----------------------------------------
# Save
# -----------------------------------------

citations_df = pd.DataFrame(
    results,
    columns=[
        "patent_id",
        "patent_title",
        "citation_text",
        "paper_year",
        "relation",
        "source",
        "source_url"
    ]
)

citations_df = citations_df.drop_duplicates(
    subset=[
        "patent_id",
        "citation_text"
    ]
)

citations_df.to_csv(
    OUTPUT,
    index=False
)


failures_df = pd.DataFrame(
    failures,
    columns=[
        "patent_id",
        "patent_title",
        "url",
        "error"
    ]
)

failures_df.to_csv(
    FAILED_OUTPUT,
    index=False
)


print()
print("PATENT CITATION EXTRACTION COMPLETE")
print("-----------------------------------")

print(
    "New patents checked:",
    len(patents)
)

print(
    "Patent → literature citation edges:",
    len(citations_df)
)

print(
    "Patents with at least one citation:",
    citations_df["patent_id"].nunique()
    if len(citations_df)
    else 0
)

print(
    "Patents that failed to load:",
    len(failures_df)
)

print()
print("Saved citations to:", OUTPUT)
print("Saved failures to:", FAILED_OUTPUT)