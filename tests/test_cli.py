"""The `filovirus` command: every subcommand parses and every module it names exists."""

import importlib

import pytest

from core_data_loader.cli import FETCHERS, LOADERS, parser


@pytest.mark.parametrize("module", list(LOADERS.values()) + [m for m, _ in FETCHERS.values()])
def test_every_loader_and_fetcher_module_exists_and_has_main(module):
    assert callable(importlib.import_module(module).main)


@pytest.mark.parametrize("argv", [
    ["init-db"], ["load"], ["load", "grants", "patents"], ["fetch", "all"], ["build", "--skip-checks"],
    ["check"], ["compare", "filovirus_test"], ["export"], ["graph-report"], ["review-stats"], ["serve", "--port", "9000"], ["ask", "what?"], ["rebuild", "--no-export"],
])
def test_subcommands_parse(argv):
    args = parser().parse_args(argv)
    assert callable(args.func)


def test_build_steps_are_importable():
    from core_data_loader.etl.pipeline import STEPS
    for module, _why in STEPS:
        assert callable(importlib.import_module(module).main)


def test_compare_accepts_a_bare_database_name_or_a_url():
    from core_data_loader.etl.compare import resolve_url
    assert resolve_url("postgresql+psycopg2://u@h:5432/x").endswith("/x")
    assert resolve_url("filovirus_test").endswith(":5432/filovirus_test") or resolve_url("filovirus_test").endswith("/filovirus_test")
