"""Pure parts of the plain-English query layer (no Neo4j, no API calls)."""

from datetime import date

from core_data_loader.graph.ask import MAX_CELL_CHARS, _clean
from core_data_loader.graph.web import PAGE


def test_long_text_is_truncated_before_it_reaches_the_model():
    out = _clean("x" * (MAX_CELL_CHARS + 50))
    assert len(out) == MAX_CELL_CHARS + 3 and out.endswith("...")


def test_nested_values_and_dates_are_json_ready():
    assert _clean({"d": date(2020, 1, 2), "l": ["a" * 400]}) == {"d": "2020-01-02", "l": ["a" * MAX_CELL_CHARS + "..."]}


def test_web_page_ships_with_the_module():
    assert PAGE.exists()
