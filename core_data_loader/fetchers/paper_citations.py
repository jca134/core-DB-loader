"""
Fetches which papers cite which, among every paper the knowledge graph could
hold, and writes the pairs to data/papers/paper_citations.csv for
loaders/papers.py to pick up.

    filovirus fetch paper-citations

WHAT. A paper's reference list, from PubMed's elink (pubmed_pubmed_refs). It
exists only for papers whose full text is in PubMed Central, roughly half of
them; for the rest PubMed has no reference list, and no edge is invented.

WHICH PAPERS. Every candidate the build might put in core.publication: the
papers bundle, the papers grants cite (cited_publications.csv) and the papers
patents cite (patent_cited_publications.csv). Only references to another
candidate are kept, since a cites edge needs both ends in the graph. The build
then keeps the pairs whose two papers both made it into core.publication.

ROBUSTNESS. ~20,000 papers is ~200 requests, and NCBI occasionally holds a
connection open without answering -- a plain read timeout never fires, and the
whole run hangs. So each request runs in its own daemon thread with a hard
deadline (abandoned and retried when it passes), up to WORKERS requests run at
once (still under NCBI's 3 requests/second: each takes several seconds
server-side), and finished batches are checkpointed, so an interrupted run
resumes instead of starting over.

Network, free, no key. Run after the other fetchers (it reads their CSVs);
run once and keep the CSV.
"""

import csv
import http.client
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from core_data_loader.config import data_path
from core_data_loader.fetchers.pubmed import REQUEST_PAUSE, reference_lists

CANDIDATE_FILES = [
    data_path("papers", "papers_iedb_rcsb_update", "output_v2", "unique_papers_v2.csv"),
    data_path("grants", "cited_publications.csv"),
    data_path("patents", "patent_cited_publications.csv"),
]
OUTPUT_FILEPATH = data_path("papers", "paper_citations.csv")
CHECKPOINT = data_path("papers", "paper_citations.checkpoint.csv")   # pmid, cited_pmid ('' = none)
BATCH_SIZE = 100
WORKERS = 3
DEADLINE = 90        # seconds a single request may take, start to finish
RETRIES = 3
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, ValueError)


def candidate_pmids() -> list[str]:
    pmids = set()
    for path in CANDIDATE_FILES:
        if not path.exists():
            print(f"  (no {path.name}; its papers are not included)")
            continue
        with open(path, newline="", encoding="utf-8") as f:
            pmids.update(r["pmid"].strip() for r in csv.DictReader(f) if r.get("pmid", "").strip().isdigit())
    return sorted(pmids, key=int)


def _with_deadline(batch: list[str]) -> dict:
    """reference_lists(batch), or TimeoutError if it isn't back within DEADLINE."""
    result = {}

    def run():
        try:
            result["value"] = reference_lists(batch)
        except Exception as e:      # handed back to the caller below
            result["error"] = e

    worker = threading.Thread(target=run, daemon=True)   # a hung request is abandoned, not joined
    worker.start()
    worker.join(DEADLINE)
    if worker.is_alive():
        raise TimeoutError(f"no answer within {DEADLINE}s")
    if "error" in result:
        raise result["error"]
    return result["value"]


def fetch_batch(batch: list[str]):
    for attempt in range(RETRIES):
        try:
            return batch, _with_deadline(batch), None
        except NETWORK_ERRORS as e:
            error = e
            time.sleep(2 * (attempt + 1))
    return batch, {}, error


def main():
    pmids = candidate_pmids()
    candidates = set(pmids)

    done, pairs = set(), set()
    if CHECKPOINT.exists():
        with open(CHECKPOINT, newline="", encoding="utf-8") as f:
            for pmid, cited in csv.reader(f):
                done.add(pmid)
                if cited:
                    pairs.add((pmid, cited))
    todo = [p for p in pmids if p not in done]
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    print(f"{len(pmids)} candidate papers; {len(done)} already checked, fetching {len(todo)} "
          f"in {len(batches)} requests ({WORKERS} at a time)", flush=True)

    failed = []
    with open(CHECKPOINT, "a", newline="", encoding="utf-8") as checkpoint, \
            ThreadPoolExecutor(max_workers=WORKERS) as pool:
        writer = csv.writer(checkpoint)
        futures = []
        for batch in batches:
            futures.append(pool.submit(fetch_batch, batch))
            time.sleep(REQUEST_PAUSE)          # stagger submissions: < 3 requests/second
        for n, future in enumerate(futures, 1):
            batch, refs, error = future.result()
            if error is not None:
                failed.append((batch[0], batch[-1], str(error)))
                continue
            for pmid in batch:
                cited = [c for c in refs.get(pmid, []) if c in candidates and c != pmid]
                writer.writerows([(pmid, c) for c in cited] or [(pmid, "")])
                pairs.update((pmid, c) for c in cited)
            checkpoint.flush()
            if n % 20 == 0 or n == len(futures):
                print(f"  {len(done) + min(n * BATCH_SIZE, len(todo))}/{len(pmids)} checked "
                      f"-> {len(pairs)} citations between candidates", flush=True)

    with open(OUTPUT_FILEPATH, "w", newline="", encoding="utf-8") as f:
        out = csv.writer(f)
        out.writerow(["pmid", "cited_pmid"])
        out.writerows(sorted(pairs, key=lambda p: (int(p[0]), int(p[1]))))
    print(f"Wrote {len(pairs)} citations to {OUTPUT_FILEPATH}", flush=True)
    if failed:
        print(f"  ! {len(failed)} requests failed; rerun to retry just those:")
        for first, last, error in failed[:5]:
            print(f"    {first}..{last}: {error}")
    else:
        CHECKPOINT.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
