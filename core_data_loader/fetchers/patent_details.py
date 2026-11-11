"""
Fetches each patent's abstract and its publication, filing and priority dates
from its public Google Patents page, and writes them to
data/patents/patent_details.csv for loaders/patents.py to pick up.

    filovirus fetch patent-details

WHY. The USPTO exports carry titles but no abstracts, so a patent whose title
is generic ("COMPOUNDS AND THERAPEUTIC USES THEREOF") links to nothing; 16
were isolated nodes. And the 100 patents from the expanded abstract search
came with no publication date, which keeps them out of any time-based split.

HOW. One request per patent to https://patents.google.com/patent/<number>/en,
which robots.txt allows (`Allow: /patent/`), paced at one request every
REQUEST_PAUSE seconds. The page's own metadata is read -- the
DC.description meta tag (abstract) and the <time itemprop=...> tags -- not
its layout, so a redesign is more likely to leave fields blank than to
misread them. A patent that fails to fetch is reported and left out; rerun to
retry, and only the missing ones are fetched again.

Network, free, no key. Run once and keep the CSV.
"""

import csv
import html
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

from core_data_loader.config import data_path

PATENTS_DIR = data_path("patents")
EXPORT_FILES = ["ebola_A61K39-12_raw.csv", "ebola_C07K16-10_raw.csv",
                "marburg_A61K39-12_raw.csv", "marburg_C07K16-10_raw.csv"]
EXPANDED_FILE = "patent_evidence_expanded.csv"
OUTPUT_FILEPATH = data_path("patents", "patent_details.csv")

PAGE_URL = "https://patents.google.com/patent/{number}/en"
USER_AGENT = "core-DB-loader (filovirus research knowledge graph)"
REQUEST_PAUSE = 2.0   # seconds between requests
COLUMNS = ["document_id", "publication_date", "filing_date", "priority_date", "abstract", "url"]

NUMBER_RE = re.compile(r"^\s*([A-Z]{2})\s*(\d+)\s*([A-Z]\d?)\s*$")


def compact_number(document_id: str) -> str | None:
    """'US 20180291088 A1' or 'US20180291088A1' -> 'US20180291088A1'."""
    m = NUMBER_RE.match(document_id or "")
    return "".join(m.groups()) if m else None


class _PatentPage(HTMLParser):
    """Collects the DC.description meta tag and the first date of each itemprop kind."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.abstract = ""
        self.dates = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name") == "DC.description" and not self.abstract:
            self.abstract = " ".join(html.unescape(a.get("content") or "").split())
        elif tag == "time" and a.get("itemprop") and a.get("datetime"):
            self.dates.setdefault(a["itemprop"], a["datetime"])


def parse_page(page: str) -> dict:
    """The fields this fetcher keeps, from one patent page's HTML."""
    parser = _PatentPage()
    parser.feed(page)
    return {
        "publication_date": parser.dates.get("publicationDate", ""),
        "filing_date": parser.dates.get("filingDate", ""),
        "priority_date": parser.dates.get("priorityDate", ""),
        "abstract": parser.abstract,
    }


def patent_numbers() -> list[str]:
    numbers = set()
    for name in EXPORT_FILES:
        with open(PATENTS_DIR / name, newline="", encoding="utf-8") as f:
            numbers.update(r["Document ID"] for r in csv.DictReader(f))
    expanded = PATENTS_DIR / EXPANDED_FILE
    if expanded.exists():
        with open(expanded, newline="", encoding="utf-8") as f:
            numbers.update(r["patent_id"] for r in csv.DictReader(f))
    return sorted(filter(None, (compact_number(n) for n in numbers)))


def fetch(number: str) -> str:
    request = urllib.request.Request(PAGE_URL.format(number=number), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def main():
    numbers = patent_numbers()
    done = {}
    if OUTPUT_FILEPATH.exists():
        with open(OUTPUT_FILEPATH, newline="", encoding="utf-8") as f:
            done = {r["document_id"]: r for r in csv.DictReader(f)}
    todo = [n for n in numbers if n not in done]
    print(f"{len(numbers)} patents; {len(done)} already fetched, fetching {len(todo)}")

    failed = []
    for i, number in enumerate(todo, 1):
        try:
            fields = parse_page(fetch(number))
        except (urllib.error.URLError, TimeoutError) as e:
            failed.append((number, str(e)))
        else:
            done[number] = {"document_id": number, **fields, "url": PAGE_URL.format(number=number)}
        if i % 20 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(REQUEST_PAUSE)

    with open(OUTPUT_FILEPATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(done[n] for n in sorted(done))

    rows = list(done.values())
    print(f"Wrote {len(rows)} patents to {OUTPUT_FILEPATH}: "
          f"{sum(bool(r['abstract']) for r in rows)} with an abstract, "
          f"{sum(bool(r['publication_date']) for r in rows)} with a publication date")
    for number, error in failed:
        print(f"  ! {number}: {error}")


if __name__ == "__main__":
    main()
