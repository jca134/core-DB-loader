"""Unit tests for the value-parsing helpers in core_data_loader.common.db_utils."""

from datetime import date

from core_data_loader.common.db_utils import safe_date, safe_int, safe_year


class TestSafeDate:
    def test_iso_date(self):
        assert safe_date("2014-08-01") == date(2014, 8, 1)

    def test_day_month_name_year(self):
        assert safe_date("12-Oct-2014") == date(2014, 10, 12)

    def test_partial_dates_are_not_invented(self):
        assert safe_date("2014") is None
        assert safe_date("2014-08") is None


class TestSafeYear:
    def test_full_date(self):
        assert safe_year("2014-08-01") == 2014

    def test_year_only_and_year_month(self):
        assert safe_year("2014") == 2014
        assert safe_year("2014-08") == 2014

    def test_range_and_blank_are_none(self):
        assert safe_year("2014-2015") is None
        assert safe_year("") is None
        assert safe_year(None) is None


class TestSafeInt:
    def test_overflowing_float_is_none(self):
        assert safe_int("inf") is None
        assert safe_int("1e400") is None


def test_safe_date_parses_unzoned_iso_timestamps():
    # NIH RePORTER's format. Previously unhandled, so every grant date in the
    # database was silently NULL -- safe_date returns None rather than raising,
    # which makes a missing format invisible until someone queries the column.
    assert safe_date("2008-09-26T00:00:00") == date(2008, 9, 26)
    assert safe_date("2016-04-09T00:00:00") == date(2016, 4, 9)


def test_safe_date_still_parses_the_zoned_variants():
    assert safe_date("2014-08-01T00:00:00Z") == date(2014, 8, 1)
    assert safe_date("2014-08-01T00:00:00.000Z") == date(2014, 8, 1)


def test_safe_year_reads_unzoned_iso_timestamps_too():
    assert safe_year("2008-09-26T00:00:00") == 2008
