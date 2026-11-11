"""
`filovirus` -- the one command for every stage of the project.

    filovirus init-db                 create the schemas and tables (safe to rerun)
    filovirus load [SOURCE ...]       source files in data/ -> raw.*      (default: all)
    filovirus fetch NAME ...          network lookups that write extra CSVs into data/
    filovirus build                   raw.* -> core.*, then the data-quality checks
    filovirus check                   only the data-quality checks
    filovirus export                  core.* -> Neo4j (wipes and rebuilds the graph)
    filovirus serve                   the query page, http://localhost:8765
    filovirus ask "QUESTION"          one plain-English question (needs an API key)
    filovirus rebuild                 load + build + export, the usual refresh

Each stage still runs on its own as `python -m core_data_loader.<package>.<module>`.
"""

import argparse
import importlib
import sys
import time

from sqlalchemy import text

# source -> loader module. Order matters only for readability: every loader
# replaces its own raw.* tables and nothing else.
LOADERS = {
    "bvbrc": "core_data_loader.loaders.bvbrc",
    "ncbi": "core_data_loader.loaders.ncbi",
    "hfv": "core_data_loader.loaders.hfv",
    "immport": "core_data_loader.loaders.immport",
    "ucsc": "core_data_loader.loaders.ucsc",
    "grants": "core_data_loader.loaders.grants",
    "patents": "core_data_loader.loaders.patents",
    "papers": "core_data_loader.loaders.papers",
}

# Network fetchers, in dependency order: cited-publications reads what
# grant-publications wrote. Each writes a CSV the matching loader picks up.
FETCHERS = {
    "grant-publications": ("core_data_loader.fetchers.grant_publications",
                           "NIH RePORTER: the papers each grant lists"),
    "cited-publications": ("core_data_loader.fetchers.cited_publications",
                           "PubMed: records for the papers grants cite (after grant-publications)"),
    "publication-metadata": ("core_data_loader.fetchers.publication_metadata",
                             "PubMed: titles and years for the papers bundle"),
    "patent-citations": ("core_data_loader.fetchers.patent_cited_publications",
                         "PubMed: resolve patents' free-text references to PMIDs"),
}


def _run_module(name: str) -> int:
    result = importlib.import_module(name).main()
    return result if isinstance(result, int) else 0


def cmd_init_db(_args) -> int:
    from core_data_loader.common.db_utils import engine
    from core_data_loader.config import SCHEMA_FILES, SQL_DIR

    for name in SCHEMA_FILES:
        with engine.begin() as conn:
            conn.execute(text((SQL_DIR / name).read_text()))
        print(f"  applied sql/{name}")
    print("Schema ready.")
    return 0


def cmd_load(args) -> int:
    unknown = [s for s in args.sources if s not in LOADERS]
    if unknown:
        print(f"Unknown source(s): {', '.join(unknown)}. Choose from: {', '.join(LOADERS)}",
              file=sys.stderr)
        return 2
    for source in args.sources or LOADERS:
        print(f"\n== load {source}")
        _run_module(LOADERS[source])
    return 0


def cmd_fetch(args) -> int:
    names = list(FETCHERS) if args.names == ["all"] else args.names
    unknown = [n for n in names if n not in FETCHERS]
    if unknown or not names:
        print("Choose one or more of: all, " + ", ".join(FETCHERS), file=sys.stderr)
        for name, (_module, what) in FETCHERS.items():
            print(f"  {name:<22} {what}", file=sys.stderr)
        return 2
    for name in names:
        print(f"\n== fetch {name}")
        _run_module(FETCHERS[name][0])
    print("\nRun `filovirus load` (or `filovirus rebuild`) to bring the new files in.")
    return 0


def cmd_build(args) -> int:
    from core_data_loader.etl.pipeline import build
    return build(skip_checks=args.skip_checks)


def cmd_check(_args) -> int:
    from core_data_loader.etl.pipeline import check
    return check()


def cmd_export(_args) -> int:
    return _run_module("core_data_loader.graph.export")


def cmd_serve(args) -> int:
    from core_data_loader.graph.web import serve
    serve(args.port)
    return 0


def cmd_ask(args) -> int:
    from core_data_loader.graph.ask import ask_cli, schema_prompt
    if args.show_schema:
        print(schema_prompt())
        return 0
    if not args.question:
        print('Usage: filovirus ask "QUESTION"', file=sys.stderr)
        return 2
    return ask_cli(" ".join(args.question))


def cmd_rebuild(args) -> int:
    started = time.perf_counter()
    for step in (lambda: cmd_load(argparse.Namespace(sources=[])),
                 lambda: cmd_build(argparse.Namespace(skip_checks=False)),
                 lambda: cmd_export(None) if not args.no_export else 0):
        code = step()
        if code:
            return code
    print(f"\nRebuilt in {time.perf_counter() - started:.0f}s.")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="filovirus",
        description="Build and query the filovirus knowledge graph.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Typical first run:  filovirus init-db && filovirus rebuild && filovirus serve",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("init-db", help="create the schemas and tables (safe to rerun)").set_defaults(func=cmd_init_db)

    s = sub.add_parser("load", help="load source files from data/ into raw.*")
    s.add_argument("sources", nargs="*", metavar="SOURCE", help=f"any of: {', '.join(LOADERS)} (default: all)")
    s.set_defaults(func=cmd_load)

    s = sub.add_parser("fetch", help="network lookups that add CSVs to data/ (run once, keep the files)")
    s.add_argument("names", nargs="+", metavar="NAME", help=f"all, or any of: {', '.join(FETCHERS)}")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("build", help="raw.* -> core.* in the required order, then verify")
    s.add_argument("--skip-checks", action="store_true", help="don't run sql/checks.sql afterwards")
    s.set_defaults(func=cmd_build)

    sub.add_parser("check", help="run the data-quality checks (sql/checks.sql)").set_defaults(func=cmd_check)
    sub.add_parser("export", help="rebuild the Neo4j graph from core.*").set_defaults(func=cmd_export)

    s = sub.add_parser("serve", help="run the query page (http://localhost:8765)")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("ask", help="ask one plain-English question (needs an Anthropic API key)")
    s.add_argument("question", nargs="*")
    s.add_argument("--show-schema", action="store_true", help="print the graph description Claude is given")
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("rebuild", help="load + build + export: refresh everything from data/")
    s.add_argument("--no-export", action="store_true", help="stop after the database build")
    s.set_defaults(func=cmd_rebuild)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        hint = _setup_hint(e)
        if hint is None:
            raise
        print(f"\nfilovirus {args.command}: {hint}", file=sys.stderr)
        return 1


def _setup_hint(e: Exception) -> str | None:
    """A one-line fix for the setup problems people actually hit; None for real bugs."""
    from neo4j.exceptions import AuthError, ServiceUnavailable
    from sqlalchemy.exc import OperationalError, ProgrammingError

    if isinstance(e, FileNotFoundError):
        return f"{e}\n  Source files are described in docs/data-sources.md."
    if isinstance(e, OperationalError):
        return ("can't connect to Postgres. Is it running, and does the database exist "
                "(`createdb filovirus`)? Set FILOVIRUS_DB_URL if yours differs.\n  " + str(e.orig).strip())
    if isinstance(e, ProgrammingError) and "does not exist" in str(e.orig):
        return f"{str(e.orig).strip()}\n  Run `filovirus init-db` (and `filovirus load` before `build`)."
    if isinstance(e, ServiceUnavailable):
        return "can't reach Neo4j. Start it with `brew services start neo4j` (see docs/graph.md)."
    if isinstance(e, AuthError):
        return "Neo4j rejected the login. Check NEO4J_USER / NEO4J_PASSWORD (see docs/graph.md)."
    return None


if __name__ == "__main__":
    sys.exit(main())
