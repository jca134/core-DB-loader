"""
Unit tests for core_data_loader.common.accessions -- the accession-matching
and BV-BRC reload-dedup logic that etl_bvbrc_hfv.py's cross-source and
BV-BRC-internal isolate/sequence merges depend on.

Deliberately dependency-free (no pandas, no live database): every case here
was previously only checkable by running the full ETL against the real
filovirus DB and hand-inspecting row counts.
"""

from core_data_loader.common.accessions import base_accession, resolve_bvbrc_duplicate_genomes, pick_field


class TestBaseAccession:
    def test_strips_genbank_version_suffix(self):
        assert base_accession("KM034562.1") == "KM034562"

    def test_leaves_unversioned_accession_unchanged(self):
        assert base_accession("KM034562") == "KM034562"

    def test_strips_multi_digit_version_suffix(self):
        assert base_accession("NC_002549.12") == "NC_002549"

    def test_blank_and_none_return_none(self):
        assert base_accession(None) is None
        assert base_accession("") is None
        assert base_accession("   ") is None

    def test_strips_surrounding_whitespace(self):
        assert base_accession("  KM034562.1  ") == "KM034562"


class TestResolveBvbrcDuplicateGenomes:
    def test_no_sharing_genome_ids_is_a_no_op(self):
        accession_groups = {"KM034562": ["g1"], "NC_002549": ["g2"]}
        status = {"g1": "Complete", "g2": "Complete"}
        inserted = {"g1": "2026-01-01T00:00:00Z", "g2": "2026-01-01T00:00:00Z"}

        assert resolve_bvbrc_duplicate_genomes(accession_groups, status, inserted) == {}

    def test_complete_wins_over_deprecated_regardless_of_order(self):
        accession_groups = {"KM034562": ["deprecated_id", "complete_id"]}
        status = {"deprecated_id": "Deprecated", "complete_id": "Complete"}
        inserted = {"deprecated_id": "2020-01-01T00:00:00Z", "complete_id": "2019-01-01T00:00:00Z"}

        result = resolve_bvbrc_duplicate_genomes(accession_groups, status, inserted)

        assert result == {"deprecated_id": "complete_id"}

    def test_most_recently_inserted_wins_when_status_ties(self):
        # Mirrors the real "Bundibugyo virus CL023421" / OZ491540 case: three
        # 'Complete' resubmissions of the same genome a few hours apart.
        accession_groups = {"OZ491540": ["g_09am", "g_11am", "g_08pm"]}
        status = {"g_09am": "Complete", "g_11am": "Complete", "g_08pm": "Complete"}
        inserted = {
            "g_09am": "2026-07-10T09:02:22.705Z",
            "g_11am": "2026-07-10T11:00:56.677Z",
            "g_08pm": "2026-07-13T20:58:54.981Z",
        }

        result = resolve_bvbrc_duplicate_genomes(accession_groups, status, inserted)

        assert result == {"g_09am": "g_08pm", "g_11am": "g_08pm"}

    def test_status_beats_recency(self):
        # An older 'Complete' record should still win over a more recently
        # inserted 'Deprecated' one -- status is the primary sort key.
        accession_groups = {"KM034562": ["old_complete", "new_deprecated"]}
        status = {"old_complete": "Complete", "new_deprecated": "Deprecated"}
        inserted = {"old_complete": "2020-01-01T00:00:00Z", "new_deprecated": "2026-01-01T00:00:00Z"}

        result = resolve_bvbrc_duplicate_genomes(accession_groups, status, inserted)

        assert result == {"new_deprecated": "old_complete"}

    def test_missing_status_does_not_crash_and_ranks_between_wgs_and_deprecated(self):
        accession_groups = {"KM034562": ["unknown_status", "deprecated_id"]}
        status = {"deprecated_id": "Deprecated"}  # unknown_status has no entry at all
        inserted = {"unknown_status": "2020-01-01T00:00:00Z", "deprecated_id": "2020-01-01T00:00:00Z"}

        result = resolve_bvbrc_duplicate_genomes(accession_groups, status, inserted)

        assert result == {"deprecated_id": "unknown_status"}

    def test_duplicate_genome_ids_in_the_same_group_only_counted_once(self):
        # genome_sequence can list the same genome_id more than once (e.g.
        # multiple contigs); that shouldn't create a self-referencing entry.
        accession_groups = {"KM034562": ["g1", "g1", "g2"]}
        status = {"g1": "Deprecated", "g2": "Complete"}
        inserted = {"g1": "2020-01-01T00:00:00Z", "g2": "2020-01-01T00:00:00Z"}

        result = resolve_bvbrc_duplicate_genomes(accession_groups, status, inserted)

        assert result == {"g1": "g2"}


class TestPickField:
    def test_first_candidate_wins_when_present(self):
        assert pick_field((1, "bvbrc value"), (2, "ncbi value")) == ("bvbrc value", 1)

    def test_falls_through_to_later_candidate_when_earlier_ones_are_none(self):
        assert pick_field((1, None), (2, "ncbi value")) == ("ncbi value", 2)

    def test_all_none_returns_none_value_and_none_source(self):
        assert pick_field((1, None), (2, None)) == (None, None)

    def test_single_candidate_source_only_table(self):
        # e.g. isolate.geo_location, which only ever comes from NCBI's matched row.
        assert pick_field((2, "Guinea")) == ("Guinea", 2)
        assert pick_field((2, None)) == (None, None)

    def test_no_candidates_returns_none_value_and_none_source(self):
        assert pick_field() == (None, None)
