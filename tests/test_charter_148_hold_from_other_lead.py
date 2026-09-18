"""Charter 148: a family that cannot serve a rung holds the run another lead's archive serves.

2026-09-18: GEFS 00Z was banked into the d0 file at 04:13Z, but ECMWF's 00Z
lands about seven hours after init and its d0 file did not exist yet, so every
d0 pass died on the required-family check and the ladder re-served d1's 18Z
GEFS until the whole 00Z set landed together around 08:30Z.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from bayes_basket_target.contracts import ContractError
from scripts import capture_official_native_ensemble_forward as capture
from tests.test_capture_official_native_ensemble_forward import (
    _install_station_registry,
    _registry,
)

GEFS = "noaa_gefs_official_historical_multistation_members_v2"
ECMWF = "ecmwf_ifs_open_pf50_historical_multistation_members_v1"
ICON = "dwd_icon_eps_official_historical_multistation_members_v1"
ECCC = "eccc_geps_official_historical_multistation_members_v1"


def _archive(schema: str, run: str) -> dict[str, Any]:
    return {"schema": schema, "run_init_utc": run, "captures": []}


def _setup(monkeypatch, tmp_path: Path, *, on_disk: dict[str, dict[str, Any]], fresh: dict[str, dict[str, Any]]):
    """``on_disk``: file name -> archive any-run holds may read.  ``fresh``: capture fn -> archive (absent = 404)."""

    _install_station_registry(monkeypatch)
    monkeypatch.setattr(capture, "load_market_registry_snapshot", lambda _p: _registry())
    reads: list[tuple[str, str | None]] = []

    def retained(path, *, event_date, station_ids, expected_schema, as_of_utc=None, max_reuse_age_seconds=None, expected_run_init_utc=None):
        name = Path(path).name
        reads.append((name, expected_run_init_utc))
        if expected_run_init_utc is not None:
            return None                      # nothing is reusable at the rung's own run
        archive = on_disk.get(name)
        return None if archive is None or archive["schema"] != expected_schema else archive

    monkeypatch.setattr(capture, "_load_retained_archive", retained)
    for fn in ("capture_gefs_station_days", "capture_ecmwf_station_days", "capture_geps_station_days", "capture_icon_eps_station_days"):
        def make(fn=fn):
            def run(**_kw):
                if fn not in fresh:
                    raise RuntimeError("HTTPStatusError:404")
                return fresh[fn]
            return run
        monkeypatch.setattr(capture, fn, make())
    written: list[str] = []
    monkeypatch.setattr(capture, "write_bounded_json_if_changed", lambda path, _payload: written.append(Path(path).name) or True)
    commands: list[list[str]] = []
    monkeypatch.setattr(capture.subprocess, "run", lambda command, **_kw: commands.append(list(command)) or subprocess.CompletedProcess(command, 0, stdout="built", stderr=""))
    monkeypatch.setattr(capture, "_verify_official_inbox", lambda *_a, **_kw: {"content_sha256": "0" * 64, "packets": []})
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    return registry_path, reads, written, commands


def _run(tmp_path: Path, registry_path: Path, *, lead: int = 0) -> dict[str, Any]:
    day = "2026-08-01"
    return capture.capture_official_native_forward(
        registry_path=registry_path,
        event_date=day,
        gefs_output=tmp_path / f"gefs_{day}_d{lead}.json",
        ecmwf_output=tmp_path / f"ecmwf_{day}_d{lead}.json",
        eccc_output=tmp_path / f"eccc_{day}_d{lead}.json",
        icon_output=tmp_path / f"icon_{day}_d{lead}.json",
        inbox_output=tmp_path / "inbox.json",
        timeout_seconds=30.0,
        lead_days=lead,
        run_cycle_hour=0,
    )


def test_other_lead_paths_follow_the_worker_naming() -> None:
    assert [p.name for p in capture._other_lead_archive_paths(Path("/r/ecmwf_2026-08-01_d0.json"))] == ["ecmwf_2026-08-01_d1.json", "ecmwf_2026-08-01_d2.json"]
    assert [p.name for p in capture._other_lead_archive_paths(Path("/r/icon_2026-08-01_d2.json"))] == ["icon_2026-08-01_d0.json", "icon_2026-08-01_d1.json"]
    assert capture._other_lead_archive_paths(Path("/r/ecmwf.json")) == []


def test_a_same_day_rung_goes_out_with_the_fresh_gefs_and_the_held_ecmwf(monkeypatch, tmp_path) -> None:
    registry_path, _reads, written, commands = _setup(
        monkeypatch, tmp_path,
        on_disk={
            "ecmwf_2026-08-01_d1.json": _archive(ECMWF, "2026-07-31T18:00:00Z"),
            "ecmwf_2026-08-01_d2.json": _archive(ECMWF, "2026-07-30T18:00:00Z"),
            "eccc_2026-08-01_d1.json": _archive(ECCC, "2026-07-31T00:00:00Z"),
        },
        fresh={
            "capture_gefs_station_days": _archive(GEFS, "2026-08-01T00:00:00Z"),
            "capture_icon_eps_station_days": _archive(ICON, "2026-08-01T00:00:00Z"),
        },
    )
    receipt = _run(tmp_path, registry_path)
    assert receipt["state"] == "COMMITTED_NO_LIVE"
    assert receipt["run_init_utc_by_family"]["NOAA_GEFS_PDS_S3"] == "2026-08-01T00:00:00Z"
    # the freshest run the family is serving, from the d1 file, not d2's
    assert receipt["run_init_utc_by_family"]["ECMWF_OPEN_DATA_S3"] == "2026-07-31T18:00:00Z"
    assert receipt["family_held_from_other_lead"] == {"ECCC_DD_WEATHER": "eccc_2026-08-01_d1.json", "ECMWF_OPEN_DATA_S3": "ecmwf_2026-08-01_d1.json"}
    # held archives are read in place, never copied into this lead's file
    assert "ecmwf_2026-08-01_d0.json" not in written and "eccc_2026-08-01_d0.json" not in written
    assert "gefs_2026-08-01_d0.json" in written and "icon_2026-08-01_d0.json" in written
    builder = next(c for c in commands if any("build_official_native_forward_inbox" in part for part in c))
    archives = [builder[i + 1] for i, part in enumerate(builder) if part == "--archive"]
    assert str(tmp_path / "ecmwf_2026-08-01_d1.json") in archives
    assert str(tmp_path / "gefs_2026-08-01_d0.json") in archives
    assert builder[builder.index("--eccc-geps-archive") + 1] == str(tmp_path / "eccc_2026-08-01_d1.json")


def test_a_pass_with_nothing_of_its_own_lead_still_fails_so_the_ladder_walks_down(monkeypatch, tmp_path) -> None:
    registry_path, _reads, _written, _commands = _setup(
        monkeypatch, tmp_path,
        on_disk={
            "gefs_2026-08-01_d1.json": _archive(GEFS, "2026-07-31T18:00:00Z"),
            "ecmwf_2026-08-01_d1.json": _archive(ECMWF, "2026-07-31T18:00:00Z"),
            "eccc_2026-08-01_d1.json": _archive(ECCC, "2026-07-31T00:00:00Z"),
            "icon_2026-08-01_d1.json": _archive(ICON, "2026-07-31T00:00:00Z"),
        },
        fresh={},
    )
    with pytest.raises(ContractError, match="nothing_new_at_this_lead"):
        _run(tmp_path, registry_path)


def test_a_required_family_with_nothing_anywhere_still_fails(monkeypatch, tmp_path) -> None:
    registry_path, _reads, _written, _commands = _setup(
        monkeypatch, tmp_path,
        on_disk={},
        fresh={"capture_gefs_station_days": _archive(GEFS, "2026-08-01T00:00:00Z")},
    )
    with pytest.raises(ContractError, match="required_family_unavailable:ECMWF_OPEN_DATA_S3"):
        _run(tmp_path, registry_path)


def test_icon_that_cannot_advance_is_held_instead_of_dropped(monkeypatch, tmp_path) -> None:
    registry_path, _reads, written, commands = _setup(
        monkeypatch, tmp_path,
        on_disk={"icon_2026-08-01_d1.json": _archive(ICON, "2026-07-31T00:00:00Z")},
        fresh={
            "capture_gefs_station_days": _archive(GEFS, "2026-08-01T00:00:00Z"),
            "capture_ecmwf_station_days": _archive(ECMWF, "2026-08-01T00:00:00Z"),
            "capture_geps_station_days": _archive(ECCC, "2026-08-01T00:00:00Z"),
        },
    )
    receipt = _run(tmp_path, registry_path)
    assert receipt["run_init_utc_by_family"]["DWD_ICON_EPS"] == "2026-07-31T00:00:00Z"
    assert "DWD_OPENDATA_ICON_EPS" in receipt["family_held_on_earlier_run"]
    assert receipt["family_held_from_other_lead"] == {"DWD_OPENDATA_ICON_EPS": "icon_2026-08-01_d1.json"}
    assert "icon_2026-08-01_d0.json" not in written
    builder = next(c for c in commands if any("build_official_native_forward_inbox" in part for part in c))
    assert str(tmp_path / "icon_2026-08-01_d1.json") in builder


def test_a_rung_every_family_answers_holds_nothing(monkeypatch, tmp_path) -> None:
    registry_path, _reads, _written, _commands = _setup(
        monkeypatch, tmp_path,
        on_disk={"ecmwf_2026-08-01_d1.json": _archive(ECMWF, "2026-07-31T18:00:00Z")},
        fresh={
            "capture_gefs_station_days": _archive(GEFS, "2026-08-01T00:00:00Z"),
            "capture_ecmwf_station_days": _archive(ECMWF, "2026-08-01T00:00:00Z"),
            "capture_geps_station_days": _archive(ECCC, "2026-08-01T00:00:00Z"),
            "capture_icon_eps_station_days": _archive(ICON, "2026-08-01T00:00:00Z"),
        },
    )
    receipt = _run(tmp_path, registry_path)
    assert receipt["family_held_from_other_lead"] == {}
    assert set(receipt["run_init_utc_by_family"].values()) == {"2026-08-01T00:00:00Z"}
