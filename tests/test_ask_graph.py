"""Pure parts of the plain-English query layer (no Neo4j, no API calls)."""

from datetime import date

from core_data_loader.graph import api_key
from core_data_loader.graph.ask import MAX_CELL_CHARS, _clean


def test_long_text_is_truncated_before_it_reaches_the_model():
    out = _clean("x" * (MAX_CELL_CHARS + 50))
    assert len(out) == MAX_CELL_CHARS + 3 and out.endswith("...")


def test_nested_values_and_dates_are_json_ready():
    assert _clean({"d": date(2020, 1, 2), "l": ["a" * 400]}) == {"d": "2020-01-02", "l": ["a" * MAX_CELL_CHARS + "..."]}


def test_env_key_wins_over_a_saved_one(tmp_path, monkeypatch):
    monkeypatch.setattr(api_key, "KEY_FILE", tmp_path / "anthropic_api_key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert api_key.current_key() == (None, None)
    api_key.KEY_FILE.write_text("sk-ant-test\n")
    assert api_key.current_key() == ("sk-ant-test", "saved")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert api_key.current_key() == ("sk-ant-env", "environment")


def test_obviously_wrong_keys_are_rejected_without_a_network_call():
    client, problem = api_key.check_key("nope", "m")
    assert client is None and "sk-ant-" in problem
