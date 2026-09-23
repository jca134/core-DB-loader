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
