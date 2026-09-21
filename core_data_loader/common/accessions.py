"""
GenBank accession handling shared by ETL scripts that reconcile isolates
across BV-BRC, NCBI, and LANL/HFV.

Kept dependency-free (no pandas, no SQLAlchemy) so the matching/dedup logic
here can be unit tested without a live database or a raw-table DataFrame.
"""

import re

from core_data_loader.common.db_utils import blank_to_none

_VERSION_SUFFIX_RE = re.compile(r"\.\d+$")


def base_accession(acc):
    """
    Strip a GenBank version suffix (e.g. 'KM034562.1' -> 'KM034562'). BV-BRC
    reports genome accessions unversioned while NCBI and LANL both include
    the version, so matching on the raw string never lines up the same
    physical accession across sources -- matching on this base form does.
    """
    acc = blank_to_none(acc)
    return _VERSION_SUFFIX_RE.sub("", acc) if acc else None


# BV-BRC's own pipeline occasionally reloads the same genome under a new
# genome_id, leaving the old and new genome_id both in bvbrc_genome /
# bvbrc_genome_sequence with the same GenBank accession (e.g. one
# 'Deprecated' plus one 'Complete', or several 'Complete' resubmissions a
# few hours apart) -- a BV-BRC-internal duplicate, distinct from a
# BV-BRC/NCBI cross-source match. BVBRC_STATUS_RANK picks a winner: prefer
# a non-deprecated status, then the most recently inserted row.
BVBRC_STATUS_RANK = {"Complete": 0, "WGS": 1, "Partial": 2, "Deprecated": 3}


def resolve_bvbrc_duplicate_genomes(genome_ids_by_accession: dict, status_by_genome: dict,
                                     inserted_by_genome: dict) -> dict:
    """
    Given every bvbrc genome_id grouped by the base accession(s) it reports
    (genome_ids_by_accession: {accession: [genome_id, ...]}), picks one
    genome_id per accession-with-more-than-one-genome_id as canonical.

    Returns {genome_id: canonical_genome_id} for every genome_id that
    should fold into another; a genome_id with no entry here is already
    canonical (including every genome_id that doesn't share its accession
    with any other).
    """
    canonical_for = {}
    for genome_ids in genome_ids_by_accession.values():
        ids = list(dict.fromkeys(genome_ids))  # de-dup while preserving first-seen order
        if len(ids) < 2:
            continue
        best_rank = min(BVBRC_STATUS_RANK.get(status_by_genome.get(gid), 2) for gid in ids)
        candidates = [gid for gid in ids if BVBRC_STATUS_RANK.get(status_by_genome.get(gid), 2) == best_rank]
        winner = max(candidates, key=lambda gid: inserted_by_genome.get(gid) or "")
        for gid in ids:
            if gid != winner:
                canonical_for[gid] = winner
    return canonical_for


def pick_field(*candidates):
    """
    candidates: (source_id, value) pairs in priority order -- the same
    priority a merged isolate row's field-by-field 'X or Y' already encodes
    inline (e.g. BV-BRC's own field before NCBI's matched row fills the
    blank). Returns (value, source_id) for the first non-None value, or
    (None, None) if every candidate is None.

    Exists because a merged row's single source_id can only name the row's
    anchor source (see build_isolates_and_sequences in etl_bvbrc_hfv.py) --
    this lets a caller record, per field, which source actually won it.
    """
    for source_id, value in candidates:
        if value is not None:
            return value, source_id
    return None, None
