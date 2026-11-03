"""
Fetches the publications NIH RePORTER associates with each grant in
data/grants/grants_raw.csv, and writes them to
data/grants/grant_publications.csv for load_grants.py to pick up.

This is the only script in the project that needs network access, and the only
source of a direct grant -> paper edge: every other document-to-document
relationship in the evidence layer is two hops through a shared entity. It is
kept separate from load_grants.py so the normal load stays offline and
reproducible -- run it once, commit the CSV to your data/ directory, and the
ETL uses it from then on.

    uv run python -m core_data_loader.loaders.fetch_grant_publications

The RePORTER v2 publications endpoint is free and needs no API key. Core
project numbers are sent in batches; failures are reported per batch and do not
abort the run, so a partial result is still written.
"""

import json
import time
import urllib.error
import urllib.request

import pandas as pd

GRANTS_FILEPATH = "data/grants/grants_raw.csv"
OUTPUT_FILEPATH = "data/grants/grant_publications.csv"

ENDPOINT = "https://api.reporter.nih.gov/v2/publications/search"
BATCH_SIZE = 50
PAGE_LIMIT = 500      # RePORTER's maximum rows per response
REQUEST_PAUSE = 0.5   # seconds between requests, to stay well inside rate limits


def fetch_batch(core_project_nums: list[str]) -> list[dict]:
    """Every publication RePORTER lists for these core project numbers."""
    results = []
    offset = 0
    while True:
        payload = json.dumps({
            "criteria": {"core_project_nums": core_project_nums},
            "offset": offset,
            "limit": PAGE_LIMIT,
        }).encode()
        request = urllib.request.Request(
            ENDPOINT, data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.load(response)

        page = body.get("results") or []
        results.extend(page)
        total = (body.get("meta") or {}).get("total", 0)
        offset += len(page)
        if not page or offset >= total:
            return results
        time.sleep(REQUEST_PAUSE)


def main():
    grants = pd.read_csv(GRANTS_FILEPATH, dtype=str, keep_default_na=False)
    cores = sorted({c for c in grants["core_project_num"] if c})
    print(f"Fetching publications for {len(cores)} core project numbers")

    rows = []
    failed = []
    for i in range(0, len(cores), BATCH_SIZE):
        batch = cores[i:i + BATCH_SIZE]
        try:
            results = fetch_batch(batch)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            # One bad batch shouldn't cost the whole run; report it and move on.
            failed.append((batch[0], batch[-1], str(e)))
            continue
        rows.extend(
            {
                "core_project_num": r.get("coreproject"),
                "pmid": r.get("pmid"),
                "appl_id": r.get("applid"),
            }
            for r in results
        )
        print(f"  {i + len(batch)}/{len(cores)} cores -> {len(rows)} publication links")
        time.sleep(REQUEST_PAUSE)

    df = pd.DataFrame(rows, columns=["core_project_num", "pmid", "appl_id"]).drop_duplicates()
    df.to_csv(OUTPUT_FILEPATH, index=False)
    print(f"Wrote {len(df)} rows ({df['pmid'].nunique()} distinct PMIDs) -> {OUTPUT_FILEPATH}")

    for first, last, error in failed:
        print(f"  ! batch {first}..{last} failed: {error}")


if __name__ == "__main__":
    main()
