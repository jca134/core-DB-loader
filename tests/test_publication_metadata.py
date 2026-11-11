"""
Tests for the PubMed esummary parsing in
core_data_loader.fetchers.publication_metadata.

PubMed's `pubdate` is free text with several shapes, and the year pulled out of
it becomes core.publication.year -- which decides what any time-based split
treats as already known. Misreading it silently shifts the cutoff.
"""

from core_data_loader.fetchers.publication_metadata import parse_record


def _rec(**kw):
    base = {"uid": "12345678", "title": "A paper.", "fulljournalname": "Science",
            "pubdate": "2014 Jun 12", "authors": [], "articleids": []}
    base.update(kw)
    return base


def test_parses_a_full_record():
    r = parse_record(_rec())
    assert r["pmid"] == "12345678"
    assert r["journal"] == "Science"
    assert r["year"] == "2014"


def test_strips_the_trailing_period_pubmed_puts_on_titles():
    # Left in place, every node label in the graph ends with a stray full stop.
    assert parse_record(_rec(title="Ebola VP35 structure."))["title"] == "Ebola VP35 structure"


def test_year_only_pubdate():
    assert parse_record(_rec(pubdate="2021"))["year"] == "2021"


def test_year_and_month_pubdate():
    assert parse_record(_rec(pubdate="2016 Jan"))["year"] == "2016"


def test_non_numeric_pubdate_yields_no_year_rather_than_a_wrong_one():
    # Better undated -- which temporal splits treat as always-known -- than
    # fabricating a year from something that is not one.
    assert parse_record(_rec(pubdate="Summer 2019"))["year"] == ""
    assert parse_record(_rec(pubdate=""))["year"] == ""


def test_falls_back_to_source_when_fulljournalname_is_absent():
    rec = _rec(source="J Virol")
    del rec["fulljournalname"]
    assert parse_record(rec)["journal"] == "J Virol"


def test_joins_authors_and_picks_the_doi_out_of_articleids():
    r = parse_record(_rec(
        authors=[{"name": "Doe J"}, {"name": "Roe A"}, {"noname": True}],
        articleids=[{"idtype": "pubmed", "value": "123"},
                    {"idtype": "doi", "value": "10.1000/x"}],
    ))
    assert r["authors"] == "Doe J; Roe A"
    assert r["doi"] == "10.1000/x"


def test_missing_doi_is_empty_not_an_error():
    assert parse_record(_rec(articleids=[{"idtype": "pubmed", "value": "1"}]))["doi"] == ""
