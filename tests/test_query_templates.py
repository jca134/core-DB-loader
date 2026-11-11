"""Question templates and API-key storage for the query page (no Neo4j, no network)."""

import re
import stat

import pytest

from core_data_loader.query import api_key
from core_data_loader.query.templates import (
    OPTION_QUERIES, TEMPLATES, VIRUS_IDS, resolve_params, TEMPLATES_BY_ID,
)

OPTIONS = {"gene": [{"value": "GP", "label": "GP"}], "condition": [{"value": 1, "label": "Ebola"}]}


def test_every_blank_in_a_question_is_a_declared_param_and_used_in_the_cypher():
    for t in TEMPLATES:
        blanks = set(re.findall(r"\{(\w+)\}", t["question"]))
        names = {p["name"] for p in t["params"]}
        assert blanks == names, t["id"]
        for name in names:
            assert f"${name}" in t["cypher"], (t["id"], name)


def test_every_param_type_has_an_option_source():
    for t in TEMPLATES:
        for p in t["params"]:
            assert p["type"] in OPTION_QUERIES or p["type"] in ("virus", "text"), t["id"]


def test_template_ids_are_unique():
    assert len(TEMPLATES_BY_ID) == len(TEMPLATES)


def test_both_ebola_genus_ids_are_searched():
    # The taxonomy has the old and new genus names; grants link to both.
    assert set(VIRUS_IDS["ebola_all"]) == {3044781, 186536}
    assert set(VIRUS_IDS["marburg_all"]) == {3044783, 186537}


def test_dropdown_values_outside_the_options_are_rejected():
    t = TEMPLATES_BY_ID["antibodies_for_gene"]
    assert resolve_params(t, {"gene": "GP"}, OPTIONS) == {"gene": "GP"}
    with pytest.raises(ValueError):
        resolve_params(t, {"gene": "GP' OR 1=1"}, OPTIONS)


def test_ids_keep_their_type_when_resolved():
    t = TEMPLATES_BY_ID["grants_for_condition"]
    assert resolve_params(t, {"condition": "1"}, OPTIONS) == {"condition": 1}


def test_virus_resolves_to_its_taxon_ids_and_text_needs_three_chars():
    assert resolve_params(TEMPLATES_BY_ID["grants_for_virus"], {"virus": "sudan"}, {}) == {"virus": [3052460]}
    with pytest.raises(ValueError):
        resolve_params(TEMPLATES_BY_ID["search_titles"], {"text": " ab "}, {})


def test_saved_key_is_owner_only_and_env_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(api_key, "KEY_FILE", tmp_path / "cfg" / "anthropic_api_key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert api_key.current_key() == (None, None)
    api_key.save_key("sk-ant-test\n")
    assert stat.S_IMODE(api_key.KEY_FILE.stat().st_mode) == 0o600
    assert api_key.current_key() == ("sk-ant-test", "saved")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert api_key.current_key() == ("sk-ant-env", "environment")
    assert api_key.forget_key() is True and not api_key.KEY_FILE.exists()


def test_obviously_wrong_keys_are_rejected_without_a_network_call():
    client, problem = api_key.check_key("sk-ant-...".replace("sk-ant-", "nope"), "m")
    assert client is None and "sk-ant-" in problem
