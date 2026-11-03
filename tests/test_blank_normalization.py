"""
Tests for blank_strings_to_null in core_data_loader.common.etl_common.

The loaders read sources with keep_default_na=False, so a blank CSV cell
arrives as '' rather than NaN. Columns that skipped blank_to_none kept those
empty strings: 66,206 of core.feature.product and 4,903 of core.sequence's
4,907 sequence_type values, where `WHERE product IS NULL` found none of them.
"""

import datetime

import pandas as pd

from core_data_loader.common.etl_common import blank_strings_to_null


def test_empty_strings_become_null():
    out = blank_strings_to_null(pd.DataFrame({"a": ["x", "", None]}))
    assert out["a"].tolist()[0] == "x"
    assert out["a"].isna().tolist() == [False, True, True]


def test_it_sees_pandas3_str_dtype_columns():
    # The bug this guards: pandas 3 gives a list of Python strings the `str`
    # dtype, not `object`. A `dtype == object` test skips the column entirely
    # and the function silently does nothing.
    df = pd.DataFrame({"a": ["x", "", "y"]})
    assert df["a"].dtype != object, "precondition: pandas 3 uses str dtype here"
    assert blank_strings_to_null(df)["a"].isna().sum() == 1


def test_whitespace_only_values_are_left_alone():
    # Only an exactly-empty string means "the source had nothing here".
    # Trimming is a separate decision and belongs to the caller.
    out = blank_strings_to_null(pd.DataFrame({"a": ["  ", "\t"]}))
    assert out["a"].tolist() == ["  ", "\t"]


def test_numeric_columns_are_untouched():
    out = blank_strings_to_null(pd.DataFrame({"n": [1, 2, 3]}))
    assert out["n"].tolist() == [1, 2, 3]


def test_date_columns_survive():
    # Date columns land in `object` dtype, which the function also scans;
    # eq("") is False throughout, so nothing should change.
    d = [datetime.date(2020, 1, 1), None]
    out = blank_strings_to_null(pd.DataFrame({"d": d}))
    assert out["d"].tolist()[0] == datetime.date(2020, 1, 1)


def test_the_input_frame_is_not_mutated():
    df = pd.DataFrame({"a": ["", "x"]})
    blank_strings_to_null(df)
    assert df["a"].tolist() == ["", "x"]


def test_a_frame_with_no_text_columns_is_returned_as_is():
    df = pd.DataFrame({"n": [1, 2]})
    assert blank_strings_to_null(df) is df
