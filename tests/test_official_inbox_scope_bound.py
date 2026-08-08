"""The official inbox is bounded by the market, not by another inbox.

Official capture exists precisely to cover scopes the republisher cannot. Using
the republisher inbox as the scope bound made coverage self-limiting: a scope it
had not yet covered could never accept the official packet meant to cover it.
ZUUU 2026-08-07 was captured successfully, then silently dropped at merge,
leaving the station with no P0 at all -- 32 registry scopes were in that state.
"""

import json
from pathlib import Path

from bayes_basket_target.production_worker import (
    _active_forward_scopes,
    _active_registry_scopes,
)


from datetime import date


HORIZON = date(2026, 8, 6)


def _registry(tmp_path: Path, rows) -> Path:
    path = tmp_path / "market_registry_forward.json"
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return path


def _inbox(tmp_path: Path, packets) -> Path:
    path = tmp_path / "native_ensemble_forward_inbox.json"
    path.write_text(json.dumps({"packets": packets}), encoding="utf-8")
    return path


def test_registry_scopes_include_what_the_republisher_lacks(tmp_path: Path) -> None:
    registry = _registry(tmp_path, [
        {"station_id": "ZUUU", "event_date": "2026-08-07"},
        {"station_id": "KATL", "event_date": "2026-08-07"},
    ])
    republisher = _inbox(tmp_path, [
        {"station_id": "KATL", "event_date": "2026-08-07"},
    ])

    from_registry = _active_registry_scopes(registry, horizon=HORIZON)
    from_republisher = _active_forward_scopes(republisher, horizon=HORIZON)

    assert ("ZUUU", "2026-08-07") in from_registry
    assert ("ZUUU", "2026-08-07") not in from_republisher


def test_closed_days_are_excluded(tmp_path: Path) -> None:
    registry = _registry(tmp_path, [
        {"station_id": "ZUUU", "event_date": "2026-08-01"},
        {"station_id": "ZUUU", "event_date": "2026-08-07"},
    ])

    scopes = _active_registry_scopes(registry, horizon=HORIZON)

    assert ("ZUUU", "2026-08-01") not in scopes
    assert ("ZUUU", "2026-08-07") in scopes


def test_station_ids_are_normalised(tmp_path: Path) -> None:
    registry = _registry(tmp_path, [
        {"station_id": " zuuu ", "event_date": "2026-08-07"},
    ])

    assert ("ZUUU", "2026-08-07") in _active_registry_scopes(
        registry, horizon=HORIZON
    )


def test_missing_or_malformed_registry_yields_no_scope(tmp_path: Path) -> None:
    # An empty bound means the merge keeps only what it captured, which is safe;
    # it must never raise and take the capture thread down with it.
    assert _active_registry_scopes(tmp_path / "absent.json", horizon=HORIZON) == set()

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert _active_registry_scopes(broken, horizon=HORIZON) == set()

    no_rows = tmp_path / "no_rows.json"
    no_rows.write_text(json.dumps({"packets": []}), encoding="utf-8")
    assert _active_registry_scopes(no_rows, horizon=HORIZON) == set()


def test_incomplete_rows_are_skipped_not_fatal(tmp_path: Path) -> None:
    registry = _registry(tmp_path, [
        {"station_id": "", "event_date": "2026-08-07"},
        {"station_id": "ZUUU", "event_date": ""},
        "not-a-row",
        {"station_id": "ZUUU", "event_date": "2026-08-07"},
    ])

    assert _active_registry_scopes(registry, horizon=HORIZON) == {
        ("ZUUU", "2026-08-07")
    }
