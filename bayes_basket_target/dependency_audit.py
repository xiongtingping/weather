"""Static dependency audit for the Bayes basket market lane.

The target lane consumes a resident, official CLOB WebSocket manifest.  It
must not grow a second market reader through REST, historical tick tables, or
an exchange client import.  This module is deliberately static: it parses
source files only and never opens a socket or a database connection.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable


SCHEMA = "bayes_basket_target_official_ws_dependency_audit_v1"
DEFAULT_ROOTS = ("bayes_basket_target",)
LEGACY_IMPORT_ROOTS = frozenset({"adaptive_basket_shadow"})
# Weather-source ingestion is allowed to use bounded HTTP transports.  The
# market-lane audit must distinguish those modules from a second CLOB/market
# reader; otherwise adding an NWP source falsely violates the official-WS-only
# market contract.
NON_MARKET_NETWORK_MODULES = frozenset(
    {
        "bayes_basket_target/native_ensemble_history.py",
        "bayes_basket_target/native_ensemble_previous_runs.py",
        "bayes_basket_target/ecmwf_ifs_archive.py",
        "bayes_basket_target/noaa_gefs_archive.py",
        "bayes_basket_target/open_meteo_ensemble.py",
        # Sources added after the list was written (found red 2026-09-17):
        # each fetches weather or observation data from its own archive --
        # DWD open data, ECCC Datamart, RISH Kyoto, the NOAA S3 buckets, the
        # Open-Meteo CMA endpoint, aviationweather.gov -- and none reads a
        # quote, a book or a tick.
        "bayes_basket_target/awc_historical_gap_fetch.py",
        "bayes_basket_target/cma_grapes_point_archive.py",
        "bayes_basket_target/dwd_icon_eps_archive.py",
        "bayes_basket_target/dwd_icon_eu_point_archive.py",
        "bayes_basket_target/eccc_geps_archive.py",
        "bayes_basket_target/historical_awc_replay.py",
        "bayes_basket_target/icon_eps_native_arrival_index.py",
        "bayes_basket_target/jma_msm_archive.py",
        # Charter 162 (2026-09-20): the second door for METAR and TAF when
        # every aviationweather.gov channel fails on one expired certificate.
        # tgftp.nws.noaa.gov serves reports, not quotes.
        "bayes_basket_target/noaa_tgftp_taf.py",
        "bayes_basket_target/noaa_conus_archive.py",
        # Charter 195 (2026-09-22): HEADs and one directory listing on the
        # same NWP servers the archives above download from, to learn when a
        # run has landed.  No quote, no book, no tick.
        "bayes_basket_target/nwp_arrival_probe.py",
        # Charter 197 (2026-09-22): one HEAD per upstream root to tell a dead
        # tunnel from a quiet provider -- the CLOB root included, as a
        # reachability probe only; no quote, book or tick is read over it.
        "bayes_basket_target/supply_watch.py",
        "bayes_basket_target/republisher_native_forward_probe.py",
        "bayes_basket_target/republisher_no_live_p0_contract.py",
    }
)
# The account's own balance, not the market.  Charter 141 (2026-09-17) sizes
# on the real account: cash from the loopback order gateway's /health and the
# value of held positions from the data API.  Neither is a quote or a book,
# so neither is a second market reader; kept apart from the weather sources
# so the reason each module is exempt stays legible.
ACCOUNT_NETWORK_MODULES = frozenset(
    {
        "bayes_basket_target/account_equity.py",
    }
)
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "aiohttp",
        "http",
        "httpx",
        "requests",
        "urllib",
        "urllib3",
        "websocket",
        "websockets",
        "pycurl",
    }
)
FORBIDDEN_TICK_TOKENS = (
    "event_market_ticks",
    "market_ticks",
    "latest_market_tick",
)
FORBIDDEN_CALL_NAMES = frozenset(
    {
        "get_book",
        "get_order_book",
        "fetch_book",
        "fetch_order_book",
        "fetch_market_ticks",
    }
)
_WORD = re.compile(r"\b(?:event_market_ticks|market_ticks|latest_market_tick)\b")


def _module_name(value: str) -> str:
    return value.split(".", 1)[0].lower()


def _iter_python_files(roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        path = Path(root)
        if path.is_file() and path.suffix == ".py":
            files.append(path)
        elif path.is_dir():
            files.extend(path.rglob("*.py"))
    return sorted(set(files), key=lambda item: str(item))


def audit_official_ws_dependencies(
    *,
    project_root: Path | None = None,
    roots: Iterable[str | Path] | None = None,
) -> dict[str, object]:
    """Return a deterministic static receipt for the target market lane."""

    base = Path(project_root or Path(__file__).resolve().parents[1])
    scan_roots = tuple(roots or DEFAULT_ROOTS)
    files = _iter_python_files(
        path if Path(path).is_absolute() else base / Path(path)
        for path in scan_roots
    )
    auditor_path = Path(__file__).resolve()
    files = [path for path in files if path.resolve() != auditor_path]
    import_hits: list[dict[str, object]] = []
    legacy_import_hits: list[dict[str, object]] = []
    tick_hits: list[dict[str, object]] = []
    call_hits: list[dict[str, object]] = []
    syntax_hits: list[dict[str, object]] = []

    for path in files:
        relative = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            syntax_hits.append({"file": relative, "line": int(exc.lineno or 0), "error": str(exc)})
            continue
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                imported = []
            for module in imported:
                if _module_name(module) in LEGACY_IMPORT_ROOTS:
                    legacy_import_hits.append({"file": relative, "line": int(node.lineno), "module": module})
                if (
                    relative not in NON_MARKET_NETWORK_MODULES
                    and relative not in ACCOUNT_NETWORK_MODULES
                    and _module_name(module) in FORBIDDEN_IMPORT_ROOTS
                ):
                    import_hits.append({"file": relative, "line": int(node.lineno), "module": module})

            if isinstance(node, ast.Call):
                function = node.func
                name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
                if name in FORBIDDEN_CALL_NAMES:
                    call_hits.append({"file": relative, "line": int(node.lineno), "call": name})

            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                parent = parents.get(node)
                if (
                    isinstance(parent, (ast.Tuple, ast.List, ast.Set))
                    and isinstance(parents.get(parent), ast.Assign)
                    and any(
                        isinstance(target, ast.Name)
                        and target.id == "PROTECTED_MARKERS"
                        for target in parents[parent].targets
                    )
                ):
                    # A deny-list marker proves rejection of a legacy source;
                    # it is not a historical-tick read dependency.
                    continue
                matches = sorted(set(_WORD.findall(node.value)))
                for token in matches:
                    tick_hits.append({"file": relative, "line": int(node.lineno), "token": token})

    # Keep duplicate hits deterministic when a module contains the same
    # literal in more than one AST node or a repeated import alias.
    for values in (legacy_import_hits, import_hits, tick_hits, call_hits, syntax_hits):
        values[:] = sorted(
            {tuple(sorted(item.items())) for item in values},
            key=lambda item: tuple(str(value) for value in item),
        )
        values[:] = [dict(item) for item in values]

    return {
        "schema": SCHEMA,
        "market_authority": "OFFICIAL_CLOB_WS_MANIFEST",
        "source_contract": "official_polymarket_clob_websocket",
        "roots": [str(Path(path)) for path in scan_roots],
        "files_scanned": [str(path.relative_to(base)) if path.is_relative_to(base) else str(path) for path in files],
        "network_import_hits": import_hits,
        "legacy_import_hits": legacy_import_hits,
        "historical_tick_hits": tick_hits,
        "forbidden_market_call_hits": call_hits,
        "syntax_hits": syntax_hits,
        "rest_reads": bool(import_hits or call_hits),
        "historical_tick_reads": bool(tick_hits),
        "official_ws_only": not (import_hits or tick_hits or call_hits or syntax_hits),
        "target_dependency_closed": not (legacy_import_hits or syntax_hits),
        "authorizes_order": False,
    }


__all__ = ["SCHEMA", "audit_official_ws_dependencies"]
