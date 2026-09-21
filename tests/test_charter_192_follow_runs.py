"""Charter 192: ICON and ECCC follow their newest run at every lead, only added.

User rulings 2026-09-21: every global run is captured and the newest one that
covers the target day is used, for D+0, D+1 and D+2 alike; nothing may go
older than it is today.  The constraints this file pins:

* the 00Z d1/d0 and 12Z d2 identities -- and so their ~30 days of history --
  are byte-identical, and the OOS reader admits every old row exactly as
  before while also admitting the new cycles in their own scopes;
* the capture scheduler's readiness does not move;
* a followed run replaces a station's run only when it is strictly newer and
  its own (hour bucket x cycle) fit is certified -- with nothing certified the
  pass builds the packets it always built;
* the follow downloads bank training history off the pass.
"""
from __future__ import annotations

import bz2
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bayes_basket_target import dwd_icon_eps_archive as icon
from bayes_basket_target import eccc_geps_archive as eccc
from bayes_basket_target import follow_run
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target import official_nwp_capture_scheduler as scheduler
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts import capture_official_native_ensemble_forward as capture
from tests.test_capture_official_native_ensemble_forward import (
    _install_station_registry,
    _registry,
    _station_ids,
)

UTC = timezone.utc
DAY = "2026-08-01"
GEFS = "noaa_gefs_official_historical_multistation_members_v2"
ECMWF = "ecmwf_ifs_open_pf50_historical_multistation_members_v1"
ICON = icon.MULTISTATION_SCHEMA
ECCC = eccc.MULTISTATION_SCHEMA


# --------------------------------------------------------------------------
# Identity and admission: only added, never swapped.
# --------------------------------------------------------------------------
def test_the_lead_own_cycle_identity_is_byte_identical() -> None:
    before = {
        0: "dwd_icon_global_eps_00z_d0_station_tmax_3h_v3",
        1: "dwd_icon_global_eps_00z_d1_station_tmax_3h_v3",
        2: "dwd_icon_global_eps_12z_d2_station_tmax_3h_v3",
    }
    for lead, text in before.items():
        assert icon.extraction_version_for_lead(lead) == text
        assert icon.extraction_version_for_lead(
            lead, icon.RUN_CYCLE_HOUR_BY_LEAD[lead]
        ) == text
    assert icon.EXTRACTION_VERSION == before[2]


def test_followed_cycles_carry_their_own_identity_and_grid() -> None:
    names = {
        (lead, hour): icon.extraction_version_for_lead(lead, hour)
        for lead in (0, 1, 2)
        for hour in (0, 6, 12, 18)
    }
    # the one retired 18Z d2 day keeps its own, different, name
    assert "dwd_icon_global_eps_18z_d2_station_tmax_v1" not in names.values()
    assert names[(2, 0)] == "dwd_icon_global_eps_00z_d2_station_tmax_3h_v3"
    assert len(set(names.values())) == len(names)
    assert names[(1, 12)] == "dwd_icon_global_eps_12z_d1_station_tmax_3h_v3"
    assert names[(1, 18)] == "dwd_icon_global_eps_18z_d1_station_tmax_6h_v1"
    assert names[(0, 6)] == "dwd_icon_global_eps_06z_d0_station_tmax_6h_v1"
    assert icon.lead_step_hours(6) == icon.lead_step_hours(18) == 6
    assert icon.lead_step_hours(0) == icon.lead_step_hours(12) == 3


def _admitted(lead_bucket: str, cycle: str, version: str) -> bool:
    return oos._historical_transport_scope_allowed(
        system_id="DWD_ICON_GLOBAL_EPS",
        transport_provider_id="DWD_OPENDATA_ICON_EPS",
        lead_bucket=lead_bucket,
        run_cycle=cycle,
        extraction_version=version,
    )


def test_oos_admits_every_old_icon_row_as_before_and_the_new_cycles_apart() -> None:
    # the rows in the archive today (calendar and hour vocabularies)
    assert _admitted("h12_24", "00Z", "dwd_icon_global_eps_00z_d1_station_tmax_3h_v3")
    assert _admitted("h00_12", "00Z", "dwd_icon_global_eps_00z_d0_station_tmax_3h_v3")
    assert _admitted("h24_48", "12Z", "dwd_icon_global_eps_12z_d2_station_tmax_3h_v3")
    assert _admitted("d1", "00Z", "dwd_icon_global_eps_00z_d1_station_tmax_3h_v3")
    # the retired 6-hour 18Z d2 day stays out
    assert not _admitted("d2", "18Z", "dwd_icon_global_eps_18z_d2_station_tmax_v1")
    # the followed cycles, each only under its own identity
    assert _admitted("h00_12", "12Z", "dwd_icon_global_eps_12z_d1_station_tmax_3h_v3")
    assert _admitted("h00_12", "18Z", "dwd_icon_global_eps_18z_d1_station_tmax_6h_v1")
    assert not _admitted("h00_12", "12Z", "dwd_icon_global_eps_00z_d1_station_tmax_3h_v3")
    assert not _admitted("h00_12", "18Z", "dwd_icon_global_eps_18z_d1_station_tmax_3h_v3")


def test_eccc_contract_already_admits_its_12z_at_d1_and_d0() -> None:
    leads = oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS["ECCC_GEM_GLOBAL_ENS"]["leads"]
    for lead in ("d0", "d1"):
        assert {entry["run_cycle"] for entry in leads[lead]} == {"00Z", "12Z"}


def test_follow_cycles_do_not_move_the_scheduler_readiness(monkeypatch) -> None:
    day = "2026-09-23"
    runs = {
        lead: scheduler.contracted_run_inits(day, lead_days=lead)[
            "DWD_ICON_GLOBAL_EPS"
        ]
        for lead in (0, 1, 2)
    }
    assert runs[0] == datetime(2026, 9, 23, 0, tzinfo=UTC)
    assert runs[1] == datetime(2026, 9, 22, 0, tzinfo=UTC)
    assert runs[2] == datetime(2026, 9, 21, 12, tzinfo=UTC)
    ready_now = {
        lead: scheduler.scope_ready_at_utc(day, lead_days=lead)
        for lead in (0, 1, 2)
    }
    # The same answer with the follow entries removed altogether.
    stripped = {
        system: {
            **contract,
            "leads": {
                lead: [
                    entry for entry in entries
                    if not entry.get("serving_follow")
                ]
                for lead, entries in contract["leads"].items()
            },
        }
        for system, contract in oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS.items()
    }
    monkeypatch.setattr(scheduler, "OFFICIAL_ARCHIVE_SCOPE_CONTRACTS", stripped)
    assert {
        lead: scheduler.scope_ready_at_utc(day, lead_days=lead)
        for lead in (0, 1, 2)
    } == ready_now


# --------------------------------------------------------------------------
# Which run a rung reaches, and which a station may take.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "family,lead,rung,expected",
    [
        (follow_run.ICON_FAMILY, 1, 18, 18),
        (follow_run.ICON_FAMILY, 1, 12, 12),
        (follow_run.ICON_FAMILY, 1, 6, 6),
        (follow_run.ICON_FAMILY, 1, None, None),
        (follow_run.ICON_FAMILY, 0, 12, 12),
        # D+2: the lead's own 12Z is the pass's; 00/06Z before it, 18Z after
        (follow_run.ICON_FAMILY, 2, 0, 0),
        (follow_run.ICON_FAMILY, 2, 6, 6),
        (follow_run.ICON_FAMILY, 2, 12, None),
        (follow_run.ICON_FAMILY, 2, None, 18),  # the ladder's d2 default rung
        (follow_run.ECCC_FAMILY, 1, 18, 12),    # no 18Z: the newest it has
        (follow_run.ECCC_FAMILY, 1, 6, None),
        (follow_run.ECCC_FAMILY, 0, 12, 12),
        (follow_run.ECCC_FAMILY, 2, 6, 0),
        (follow_run.ECCC_FAMILY, 2, 18, None),  # its newest by 18Z is its own 12Z
    ],
)
def test_a_rung_reaches_the_newest_published_cycle_at_or_before_it(
    family, lead, rung, expected
) -> None:
    assert follow_run.follow_cycle_for_rung(family, lead, rung) == expected


def _t(day: str, hour: int) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)


def test_a_station_takes_the_newest_certified_run_and_never_an_older_one() -> None:
    base = _t("2026-09-22", 0)
    candidates = [(_t("2026-09-22", 12), "12z"), (_t("2026-09-22", 6), "06z")]
    only_06 = lambda run, key: key == "06z"  # noqa: E731
    assert follow_run.choose_station_run(
        base_run_init=base, candidates=candidates, ready=only_06
    ) == "06z"
    assert follow_run.choose_station_run(
        base_run_init=base, candidates=candidates, ready=lambda *_: False
    ) is None
    # a follow run no newer than what the pass chose is never considered
    asked: list[str] = []
    assert follow_run.choose_station_run(
        base_run_init=_t("2026-09-23", 0),
        candidates=candidates,
        ready=lambda run, key: asked.append(key) or True,
    ) is None
    assert asked == []


# --------------------------------------------------------------------------
# What a followed run can cover.
# --------------------------------------------------------------------------
def test_icon_secondary_cycles_are_read_on_their_six_hour_grid() -> None:
    # ZUUU's 2026-09-23 from 09-22 18Z: 02, 08, 14 and 20 Beijing time
    assert icon.follow_forecast_hours(
        event_date="2026-09-23",
        settlement_timezone="Asia/Shanghai",
        run_init_utc=_t("2026-09-22", 18),
    ) == [0, 6, 12, 18]
    # the same day's 06Z reaches 14:00 and 20:00 only: ZUUU keeps its run
    assert icon.follow_forecast_hours(
        event_date="2026-09-23",
        settlement_timezone="Asia/Shanghai",
        run_init_utc=_t("2026-09-23", 6),
    ) is None
    # Wellington's day began before an 18Z run: the same-day floor applies
    assert icon.follow_forecast_hours(
        event_date="2026-09-23",
        settlement_timezone="Pacific/Auckland",
        run_init_utc=_t("2026-09-22", 18),
    ) == [0, 6, 12]
    # the 12Z d1 run covers the whole of Wellington's day on the 3h grid
    assert len(icon.follow_forecast_hours(
        event_date="2026-09-23",
        settlement_timezone="Pacific/Auckland",
        run_init_utc=_t("2026-09-22", 12),
    )) == 8
    # a same-day 12Z run still covers New York's afternoon
    assert len(icon.follow_forecast_hours(
        event_date="2026-09-23",
        settlement_timezone="America/New_York",
        run_init_utc=_t("2026-09-23", 12),
    ) or []) >= 3


def test_eccc_follow_coverage_leaves_out_what_its_seal_would_refuse() -> None:
    assert eccc.follow_forecast_hours(
        event_date="2026-09-23",
        settlement_timezone="Asia/Tokyo",
        run_init_utc=_t("2026-09-23", 12),
    ) is None
    assert len(eccc.follow_forecast_hours(
        event_date="2026-09-23",
        settlement_timezone="Asia/Shanghai",
        run_init_utc=_t("2026-09-22", 12),
    )) == 8


# --------------------------------------------------------------------------
# The certificate: exact own-bucket, own-cycle activation and a mature fit.
# --------------------------------------------------------------------------
def _certify(
    conn: sqlite3.Connection,
    *,
    station: str,
    season_bin: str,
    lead_bucket: str,
    cycle: str,
    fit_cases: int,
    generation: int = 1,
    n_effective: float = 9.0,
) -> None:
    system = "DWD_ICON_GLOBAL_EPS"
    artifact_id = f"artifact-{station}-{lead_bucket}-{cycle}-{generation}"
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO calibration_native_ensemble_system_artifact("
        "system_artifact_id,ensemble_system_id,station_id,season_bin,"
        "lead_bucket,run_cycle,settlement_definition,calibration_method,"
        "bias_parameters_json,spread_parameters_json,"
        "reliability_parameters_json,member_qc_contract_sha256,"
        "train_start_date,train_end_date,holdout_start_date,holdout_end_date,"
        "rolling_origin_fold_count,effective_sample_n,oos_metrics_json,"
        "valid_from_utc,valid_until_utc,content_sha256) "
        "VALUES(?,?,?,?,?,?,'official_daily_tmax_c','m','{}','{}',?,?,"
        "'2026-09-01','2026-09-10','2026-09-11','2026-09-13',1,8.0,'{}',"
        "'2026-09-14T00:00:00Z','2026-10-14T00:00:00Z',?)",
        (
            artifact_id, system, station, season_bin, lead_bucket, cycle,
            json.dumps({"fit_case_count": fit_cases, "train_n_effective": n_effective}),
            "a" * 64,
            content_sha256([artifact_id, fit_cases]),
        ),
    )
    conn.execute(
        "INSERT INTO calibration_native_ensemble_activation("
        "activation_id,artifact_kind,artifact_id,station_id,"
        "ensemble_system_id_or_star,season_bin,lead_bucket,run_cycle,"
        "run_cycle_by_system_json,run_cycle_by_system_sha256,"
        "settlement_definition,available_system_set_hash_or_star,generation,"
        "valid_from_utc,valid_until_utc,activated_at_utc,authority_scope,"
        "content_sha256) VALUES(?,'NATIVE_ENSEMBLE_SYSTEM_CALIBRATION',?,?,?,"
        "?,?,?,?,?,'official_daily_tmax_c','*',?,'2026-09-14T00:00:00Z',"
        "'2026-10-14T00:00:00Z','2026-09-14T00:00:00Z',"
        "'PRODUCTION_NO_LIVE_ONLY',?)",
        (
            f"act-{artifact_id}", artifact_id, station, system, season_bin,
            lead_bucket, cycle,
            follow_run._json({system: cycle}),
            content_sha256({system: cycle}),
            generation,
            content_sha256(["act", artifact_id]),
        ),
    )


@pytest.fixture
def ssot() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    return conn


def _ready(conn: sqlite3.Connection, run: datetime) -> bool:
    return follow_run.follow_serving_ready(
        conn,
        family=follow_run.ICON_FAMILY,
        station_id="ZUUU",
        event_date="2026-09-23",
        settlement_timezone="Asia/Shanghai",
        run_init=run,
        as_of_utc="2026-09-22T16:00:00Z",
    )


def test_a_followed_run_needs_its_own_bucket_and_cycle_certified(ssot) -> None:
    from bayes_basket_target.station_season import station_season_bin

    season = station_season_bin("ZUUU", "2026-09-23")
    run = _t("2026-09-22", 12)  # ZUUU: 4h before local midnight -> h00_12
    assert not _ready(ssot, run)
    # the 00Z fit in the same bucket is not this run's certificate
    _certify(ssot, station="ZUUU", season_bin=season, lead_bucket="h00_12",
             cycle="00Z", fit_cases=30)
    assert not _ready(ssot, run)
    # nor the same cycle in another bucket (the d2 12Z scope)
    _certify(ssot, station="ZUUU", season_bin=season, lead_bucket="h24_48",
             cycle="12Z", fit_cases=30)
    assert not _ready(ssot, run)
    # its own scope at the OOS floor is not mature enough to displace 00Z
    _certify(ssot, station="ZUUU", season_bin=season, lead_bucket="h00_12",
             cycle="12Z", fit_cases=follow_run.FOLLOW_RUN_MIN_FIT_CASES - 2)
    assert not _ready(ssot, run)
    # a large count resting on stale cases (a scope revived after a gap)
    _certify(ssot, station="ZUUU", season_bin=season, lead_bucket="h00_12",
             cycle="12Z", fit_cases=30, n_effective=3.5, generation=2)
    assert not _ready(ssot, run)
    _certify(ssot, station="ZUUU", season_bin=season, lead_bucket="h00_12",
             cycle="12Z", fit_cases=follow_run.FOLLOW_RUN_MIN_FIT_CASES,
             generation=3)
    assert _ready(ssot, run)


def test_a_same_day_run_sharing_its_scope_with_the_day_before_is_not_seated() -> None:
    # Berlin: D-1 12Z is +10h and D 12Z is -14h to the local day, both h00_12;
    # the scope trains on D-1 12Z, the first captured.
    kwargs = dict(event_date="2026-09-23", settlement_timezone="Europe/Berlin")
    assert follow_run.scope_trained_by_longer_lead(
        follow_run.ICON_FAMILY, run_init=_t("2026-09-23", 12), **kwargs
    )
    assert not follow_run.scope_trained_by_longer_lead(
        follow_run.ICON_FAMILY, run_init=_t("2026-09-22", 12), **kwargs
    )
    assert follow_run.scope_trained_by_longer_lead(
        follow_run.ECCC_FAMILY, run_init=_t("2026-09-23", 12), **kwargs
    )
    # New York's D 12Z sits in h00_12 while D-1 12Z is h12_24: its own scope
    assert not follow_run.scope_trained_by_longer_lead(
        follow_run.ICON_FAMILY, run_init=_t("2026-09-23", 12),
        event_date="2026-09-23", settlement_timezone="America/New_York",
    )
    # Chengdu's d1 runs never share with d2
    for hour in (6, 12, 18):
        assert not follow_run.scope_trained_by_longer_lead(
            follow_run.ICON_FAMILY, run_init=_t("2026-09-22", hour),
            event_date="2026-09-23", settlement_timezone="Asia/Shanghai",
        )


def test_the_certificate_refuses_a_shared_scope_even_when_it_exists(ssot) -> None:
    from bayes_basket_target.station_season import station_season_bin

    season = station_season_bin("EDDM", "2026-09-23")
    _certify(ssot, station="EDDM", season_bin=season, lead_bucket="h00_12",
             cycle="12Z", fit_cases=20, n_effective=10.0)
    common = dict(family=follow_run.ICON_FAMILY, station_id="EDDM",
                  event_date="2026-09-23", settlement_timezone="Europe/Berlin",
                  as_of_utc="2026-09-23T16:00:00Z")
    assert follow_run.follow_serving_ready(ssot, run_init=_t("2026-09-22", 12), **common)
    assert not follow_run.follow_serving_ready(ssot, run_init=_t("2026-09-23", 12), **common)


# --------------------------------------------------------------------------
# The pass: with nothing certified it is the pass it was.
# --------------------------------------------------------------------------
def _archive(schema: str, run: str) -> dict[str, Any]:
    return {"schema": schema, "run_init_utc": run, "captures": []}


def _follow_file(root: Path, name: str, run: str, stations: list[str], schema: str = ICON) -> Path:
    identity = {
        "schema": schema,
        "event_date": DAY,
        "run_init_utc": run,
        "training_only": True,
        "holdout_eligible": False,
        "captures": [{"station_id": station} for station in stations],
    }
    path = root / name
    path.write_text(json.dumps({**identity, "content_sha256": content_sha256(identity)}))
    return path


def _setup_pass(monkeypatch, tmp_path: Path):
    _install_station_registry(monkeypatch)
    monkeypatch.setattr(capture, "load_market_registry_snapshot", lambda _p: _registry())
    monkeypatch.setattr(capture, "_load_retained_archive", lambda *_a, **_k: None)
    fresh = {
        "capture_gefs_station_days": _archive(GEFS, "2026-07-31T12:00:00Z"),
        "capture_ecmwf_station_days": _archive(ECMWF, "2026-07-31T12:00:00Z"),
        "capture_geps_station_days": _archive(ECCC, "2026-07-31T00:00:00Z"),
        "capture_icon_eps_station_days": {
            **_archive(ICON, "2026-07-31T00:00:00Z"),
            "captures": [{"station_id": s} for s in _station_ids()],
        },
    }
    for name, value in fresh.items():
        monkeypatch.setattr(capture, name, lambda value=value, **_kw: value)
    monkeypatch.setattr(capture, "write_bounded_json_if_changed", lambda *_a: True)
    commands: list[list[str]] = []
    monkeypatch.setattr(
        capture.subprocess, "run",
        lambda command, **_kw: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="built", stderr=""),
    )
    monkeypatch.setattr(capture, "_verify_official_inbox", lambda *_a, **_k: {"content_sha256": "0" * 64, "packets": []})
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    return registry_path, commands


def _pass(tmp_path: Path, registry_path: Path, **kwargs) -> dict[str, Any]:
    return capture.capture_official_native_forward(
        registry_path=registry_path,
        event_date=DAY,
        gefs_output=tmp_path / f"gefs_{DAY}_d1.json",
        ecmwf_output=tmp_path / f"ecmwf_{DAY}_d1.json",
        eccc_output=tmp_path / f"eccc_{DAY}_d1.json",
        icon_output=tmp_path / f"icon_{DAY}_d1.json",
        inbox_output=tmp_path / "inbox.json",
        timeout_seconds=30.0,
        lead_days=1,
        run_cycle_hour=12,
        **kwargs,
    )


def _builder(commands: list[list[str]]) -> list[str]:
    return next(c for c in commands if any("build_official_native_forward_inbox" in p for p in c))


def test_uncertified_follow_runs_leave_the_pass_byte_identical(monkeypatch, tmp_path) -> None:
    registry_path, commands = _setup_pass(monkeypatch, tmp_path)
    before = _pass(tmp_path, registry_path)
    _follow_file(tmp_path, f"icon_{DAY}_d1_12z.json", "2026-07-31T12:00:00Z", _station_ids())
    _follow_file(tmp_path, f"eccc_{DAY}_d1_12z.json", "2026-07-31T12:00:00Z", _station_ids(), schema=ECCC)
    asked: list[tuple[str, str]] = []
    after = _pass(
        tmp_path, registry_path,
        follow_new_runs=True,
        follow_serving_ready=lambda **kw: asked.append((kw["family"], kw["station_id"])) or False,
    )
    assert _builder(commands[:1]) == _builder(commands[1:])
    assert "follow_serving" not in after
    assert {k: v for k, v in before.items() if k != "captured_at_utc"} == {
        k: v for k, v in after.items() if k != "captured_at_utc"
    }
    # every station was asked about both families' newer run, and refused
    assert len(asked) == 2 * len(_station_ids())


def test_a_certified_follow_run_serves_only_the_stations_it_is_certified_for(monkeypatch, tmp_path) -> None:
    registry_path, commands = _setup_pass(monkeypatch, tmp_path)
    stations = _station_ids()
    certified = stations[0]
    icon_12 = _follow_file(tmp_path, f"icon_{DAY}_d1_12z.json", "2026-07-31T12:00:00Z", stations)
    receipt = _pass(
        tmp_path, registry_path,
        follow_new_runs=True,
        follow_serving_ready=lambda **kw: kw["station_id"] == certified
        and kw["family"] == follow_run.ICON_FAMILY,
    )
    builder = _builder(commands)
    assert builder[builder.index("--follow-archive") + 1] == str(icon_12)
    assignments = [builder[i + 1] for i, part in enumerate(builder) if part == "--follow-assignment"]
    assert assignments == [f"{certified}={icon_12}"]
    assert receipt["follow_serving"] == {
        follow_run.ICON_FAMILY: {certified: "2026-07-31T12:00:00Z"}
    }
    # the base ICON archive is still the family's archive for everyone else
    assert str(tmp_path / f"icon_{DAY}_d1.json") in builder


def test_a_follow_run_older_than_the_pass_run_is_never_taken(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(follow_run.SECONDARY_SERVING_ENV, "on")
    registry_path, commands = _setup_pass(monkeypatch, tmp_path)
    monkeypatch.setattr(
        capture, "capture_icon_eps_station_days",
        lambda **_kw: {**_archive(ICON, "2026-08-01T00:00:00Z"),
                       "captures": [{"station_id": s} for s in _station_ids()]},
    )
    _follow_file(tmp_path, f"icon_{DAY}_d1_18z.json", "2026-07-31T18:00:00Z", _station_ids())
    receipt = _pass(tmp_path, registry_path, follow_new_runs=True,
                    follow_serving_ready=lambda **_kw: True)
    assert "follow_serving" not in receipt
    assert "--follow-archive" not in _builder(commands)


# --------------------------------------------------------------------------
# The follow lane: banks the runs, off the pass.
# --------------------------------------------------------------------------
def test_the_follow_lane_banks_each_due_run_once_and_only_where_it_covers(monkeypatch, tmp_path) -> None:
    _install_station_registry(monkeypatch)
    rows = _registry()["rows"]
    for row, tz in zip(rows, ["Asia/Shanghai", "America/New_York"] * len(rows)):
        row["settlement_timezone"] = tz
    monkeypatch.setattr(capture, "load_market_registry_snapshot", lambda _p: {"rows": rows})
    calls: list[tuple[str, int, int, int]] = []

    def fake_icon(**kw):
        calls.append(("icon", kw["lead_days"], kw["run_cycle_hour"], len(kw["station_scopes"])))
        assert kw["download_workers"] == 8
        run = follow_run.follow_run_init(DAY, kw["lead_days"], kw["run_cycle_hour"])
        identity = {"schema": ICON, "event_date": DAY,
                    "run_init_utc": run.isoformat().replace("+00:00", "Z"),
                    "training_only": True, "holdout_eligible": False,
                    "captures": [{"station_id": s["station_id"]} for s in kw["station_scopes"]]}
        return {**identity, "content_sha256": content_sha256(identity)}

    def fake_eccc(**kw):
        calls.append(("eccc", kw["lead_days"], kw["run_cycle_hour"], len(kw["station_scopes"])))
        raise RuntimeError("HTTPStatusError:404")

    monkeypatch.setattr(capture, "capture_icon_eps_station_days", fake_icon)
    monkeypatch.setattr(capture, "capture_geps_station_days", fake_eccc)
    written: list[str] = []
    real_write = capture.write_bounded_json_if_changed
    monkeypatch.setattr(capture, "write_bounded_json_if_changed",
                        lambda path, payload: written.append(Path(path).name) or real_write(path, payload))
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    # 2026-08-01T05:00Z: rungs (0,None) (1,18) (1,12) (1,6); the d0 06Z run is not due
    rungs = [(0, 6), (0, None), (1, 18), (1, 12)]
    receipt = capture.capture_follow_runs(
        registry_path=registry_path, event_date=DAY, rungs=rungs,
        capture_root=tmp_path, timeout_seconds=30.0,
        as_of_utc="2026-08-01T05:00:00Z", icon_download_workers=8,
    )
    states = {(o["family"], o["lead_days"], o["run_init_utc"]): o["state"] for o in receipt["outcomes"]}
    assert states[(follow_run.ICON_FAMILY, 0, "2026-08-01T06:00:00Z")] == "NOT_DUE"
    assert states[(follow_run.ICON_FAMILY, 1, "2026-07-31T18:00:00Z")] == "CAPTURED"
    assert states[(follow_run.ICON_FAMILY, 1, "2026-07-31T12:00:00Z")] == "CAPTURED"
    assert states[(follow_run.ECCC_FAMILY, 1, "2026-07-31T12:00:00Z")] == "FAILED"
    # ECCC's 18Z rung and 12Z rung are the same 12Z run: asked once
    assert [c for c in calls if c[0] == "eccc"] == [("eccc", 1, 12, len(rows))]
    assert sorted(written) == [f"icon_{DAY}_d1_12z.json", f"icon_{DAY}_d1_18z.json"]
    # a second lap finds them on disk and downloads nothing
    calls.clear()
    again = capture.capture_follow_runs(
        registry_path=registry_path, event_date=DAY, rungs=rungs,
        capture_root=tmp_path, timeout_seconds=30.0,
        as_of_utc="2026-08-01T05:30:00Z", icon_download_workers=8,
    )
    assert [c for c in calls if c[0] == "icon"] == []
    assert {o["state"] for o in again["outcomes"] if o["family"] == follow_run.ICON_FAMILY and o["lead_days"] == 1} == {"RETAINED"}


def test_a_same_day_follow_run_is_banked_for_the_stations_it_can_cover(monkeypatch, tmp_path) -> None:
    _install_station_registry(monkeypatch)
    rows = _registry()["rows"]
    for row, tz in zip(rows, ["Asia/Shanghai", "America/New_York"] * len(rows)):
        row["settlement_timezone"] = tz
    monkeypatch.setattr(capture, "load_market_registry_snapshot", lambda _p: {"rows": rows})
    seen: list[set[str]] = []

    def fake_icon(**kw):
        seen.append({s["settlement_timezone"] for s in kw["station_scopes"]})
        raise RuntimeError("stop")

    monkeypatch.setattr(capture, "capture_icon_eps_station_days", fake_icon)
    monkeypatch.setattr(capture, "capture_geps_station_days", lambda **_kw: (_ for _ in ()).throw(RuntimeError("stop")))
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    capture.capture_follow_runs(
        registry_path=registry_path, event_date=DAY, rungs=[(0, 12)],
        capture_root=tmp_path, timeout_seconds=30.0,
        as_of_utc="2026-08-01T16:00:00Z",
    )
    # a same-day 12Z run is past Beijing's day; New York's afternoon is ahead
    assert seen == [{"America/New_York"}]


# --------------------------------------------------------------------------
# The ICON collector reads a followed cycle on its own grid and identity.
# --------------------------------------------------------------------------
def _registry_rows(station: str, day: str) -> list[dict[str, Any]]:
    return [
        {"station_id": station, "event_date": day, "settlement_unit": "C",
         "outcome": name, "registry_json": json.dumps({"lower": lower, "upper": upper})}
        for name, lower, upper in (("low", None, 19), ("mid", 20, 20), ("high", 21, None))
    ]


def test_an_icon_follow_capture_reads_the_six_hour_objects_under_its_own_identity(monkeypatch) -> None:
    urls: list[str] = []
    monkeypatch.setattr(icon, "_eccodes", lambda: object())
    monkeypatch.setattr(icon, "_grid_index", lambda *_a, **_k: {"ZUUU": 0})
    monkeypatch.setattr(icon, "_fetch", lambda _client, url: urls.append(url) or bz2.compress(b"x" * 40))
    monkeypatch.setattr(icon, "_split_grib_messages", lambda raw: [(i, 1) for i in range(40)])
    counter = {"n": 0}

    def values(_library, _raw):
        counter["n"] += 1
        member = (counter["n"] - 1) % 40 + 1
        return member, [273.15 + 20.0 + member / 100.0]

    monkeypatch.setattr(icon, "_message_values", values)
    archive = icon.capture_icon_eps_station_days(
        event_date="2026-09-23",
        station_scopes=[{"station_id": "ZUUU", "settlement_timezone": "Asia/Shanghai",
                         "registry_rows": _registry_rows("ZUUU", "2026-09-23")}],
        archive_query_at_utc="2026-09-23T00:30:00Z",
        lead_days=1,
        run_cycle_hour=18,
        download_workers=2,
    )
    assert archive["run_init_utc"] == "2026-09-22T18:00:00Z"
    assert archive["run_cycle"] == "18Z"
    assert archive["extraction_version"] == "dwd_icon_global_eps_18z_d1_station_tmax_6h_v1"
    assert archive["union_forecast_hours"] == [0, 6, 12, 18]
    assert sorted(u.split("_2026092218_")[1][:3] for u in urls) == ["000", "006", "012", "018"]
    assert all("/18/t_2m/" in u for u in urls)
    station = archive["captures"][0]
    assert station["extraction_version"] == archive["extraction_version"]
    assert station["archive_lead_bucket"] == "h00_12"


def test_icon_ingest_stamps_the_cycle_the_capture_came_from(monkeypatch) -> None:
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(icon, "native_ensemble_foundation_schema_status", lambda _c: {"ok": True})
    monkeypatch.setattr(icon, "_insert_content_addressed",
                        lambda _conn, *, table, primary_key, row: written.append(row) or 1)

    class Conn:
        def execute(self, sql, params=()):
            class Cursor:
                def fetchone(self_inner):
                    return ("DWD", icon.INDEPENDENCE_FAMILY_ID, icon.EXPECTED_MEMBER_COUNT)

                def fetchall(self_inner):
                    return []
            return Cursor()

    identity = {
        "schema": icon.SCHEMA,
        "station_id": "ZUUU",
        "event_date": "2026-09-23",
        "settlement_timezone": "Asia/Shanghai",
        "run_init_utc": "2026-09-22T12:00:00Z",
        "run_cycle": "12Z",
        "archive_lead_bucket": "h00_12",
        "members": [
            {"member_id": f"member_{i:02d}", "member_role": "PERTURBED", "raw_tmax_c": 20.0}
            for i in range(1, 41)
        ],
        "source_class": "OFFICIAL_NATIVE_ARCHIVE",
        "training_only": True,
        "holdout_eligible": False,
        "archive_query_at_utc": "2026-09-22T15:30:00Z",
        "source_prefix": icon.BASE_URL + "/12/t_2m/",
        "field_receipt_set_sha256": "b" * 64,
    }
    capture_row = {**identity, "content_sha256": content_sha256(identity)}
    icon.ingest_icon_eps_station_day(Conn(), capture_row)
    assert {row["extraction_version"] for row in written} == {
        "dwd_icon_global_eps_12z_d1_station_tmax_3h_v3"
    }
    assert {row["run_cycle"] for row in written} == {"12Z"}


# --------------------------------------------------------------------------
# The worker queues the follow lane after a pass, one date at a time.
# --------------------------------------------------------------------------
def test_the_worker_queues_one_follow_capture_per_date_off_the_pass(monkeypatch, tmp_path) -> None:
    import threading
    import types

    from bayes_basket_target import production_worker as worker

    release = threading.Event()
    commands: list[list[str]] = []

    def fake_run(command, **_kw):
        commands.append(list(command))
        release.wait(5)
        return subprocess.CompletedProcess(command, 0, stdout='{"state":"COMMITTED_NO_LIVE"}', stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    host = types.SimpleNamespace()
    ladder = [
        (1, 18, _t("2026-09-22", 18)),
        (1, 12, _t("2026-09-22", 12)),
        (1, 6, _t("2026-09-22", 6)),
        (1, None, _t("2026-09-22", 0)),
    ]
    kwargs = dict(
        event_date="2026-09-23", run_ladder=ladder,
        registry_path=tmp_path / "registry.json", capture_root=tmp_path, env={},
    )
    enqueue = worker.TargetProductionWorker._enqueue_follow_run_capture
    assert enqueue(host, **kwargs) == "QUEUED"
    assert enqueue(host, **kwargs) == "ALREADY_QUEUED"
    # a rung every family already reads at its own cycle queues nothing
    assert enqueue(host, **{**kwargs, "event_date": "2026-09-24",
                            "run_ladder": [(1, None, _t("2026-09-23", 0))]}) == "NO_FOLLOW_RUNG"
    release.set()
    future = host._official_native_follow_inflight["2026-09-23"]
    assert future.result(5)["state"] == "COMMITTED_NO_LIVE"
    host._official_native_follow_pool.shutdown(wait=True)
    command = commands[0]
    assert "--follow-capture-only" in command and "--commit" in command
    assert command[command.index("--icon-download-workers") + 1] == str(worker.FOLLOW_ICON_DOWNLOAD_WORKERS)
    assert [command[i + 1] for i, p in enumerate(command) if p == "--follow-rung"] == ["1:18", "1:12", "1:6"]
    # the lane is the capture script's, with nothing of the pass's own
    assert "--gefs-output" not in command and "--inbox-output" not in command


def test_the_kill_switch_turns_the_follow_lane_off(monkeypatch) -> None:
    monkeypatch.delenv(follow_run.FOLLOW_ENV, raising=False)
    assert follow_run.follow_enabled()
    monkeypatch.setenv(follow_run.FOLLOW_ENV, "off")
    assert not follow_run.follow_enabled()


def test_icon_six_hour_runs_are_banked_but_not_seated_until_ruled(monkeypatch, tmp_path) -> None:
    registry_path, commands = _setup_pass(monkeypatch, tmp_path)
    icon_18 = _follow_file(tmp_path, f"icon_{DAY}_d1_18z.json", "2026-07-31T18:00:00Z", _station_ids())
    icon_12 = _follow_file(tmp_path, f"icon_{DAY}_d1_12z.json", "2026-07-31T12:00:00Z", _station_ids())
    monkeypatch.delenv(follow_run.SECONDARY_SERVING_ENV, raising=False)
    receipt = _pass(tmp_path, registry_path, follow_new_runs=True,
                    follow_serving_ready=lambda **_kw: True)
    # certified 18Z is passed over; the certified 12Z beneath it is taken
    assert set(receipt["follow_serving"][follow_run.ICON_FAMILY].values()) == {"2026-07-31T12:00:00Z"}
    assert str(icon_18) not in _builder(commands)
    monkeypatch.setenv(follow_run.SECONDARY_SERVING_ENV, "on")
    receipt = _pass(tmp_path, registry_path, follow_new_runs=True,
                    follow_serving_ready=lambda **_kw: True)
    assert set(receipt["follow_serving"][follow_run.ICON_FAMILY].values()) == {"2026-07-31T18:00:00Z"}
    assert str(icon_12) not in _builder(commands[1:])
    # ECCC's 12Z and ICON's 12Z are never held back by that switch
    assert follow_run.follow_serving_allowed(follow_run.ECCC_FAMILY, 12)
    monkeypatch.delenv(follow_run.SECONDARY_SERVING_ENV, raising=False)
    assert follow_run.follow_serving_allowed(follow_run.ICON_FAMILY, 12)
    assert not follow_run.follow_serving_allowed(follow_run.ICON_FAMILY, 6)


def test_a_d2_follow_run_reads_only_steps_its_grid_is_published_at() -> None:
    # ICON's primary cycles are 3-hourly only to +72h: a D-2 00Z run cannot
    # seal New York's day on that grid and leaves it out ...
    assert icon.follow_forecast_hours(
        event_date="2026-09-23",
        settlement_timezone="America/New_York",
        run_init_utc=_t("2026-09-21", 0),
    ) is None
    # ... but covers Chengdu's whole day
    assert icon.follow_forecast_hours(
        event_date="2026-09-23",
        settlement_timezone="Asia/Shanghai",
        run_init_utc=_t("2026-09-21", 0),
    ) == [42, 45, 48, 51, 54, 57, 60, 63]
    # the 6-hourly cycles run to +120h
    assert icon.follow_forecast_hours(
        event_date="2026-09-23",
        settlement_timezone="America/Los_Angeles",
        run_init_utc=_t("2026-09-21", 6),
    ) == [54, 60, 66, 72]


def test_a_d2_packet_without_icon_takes_a_certified_earlier_run(monkeypatch, tmp_path) -> None:
    # Before D-2 12Z publishes the pass has no ICON at all for D+2; a
    # certified D-2 00Z follow run is newer than nothing.
    registry_path, commands = _setup_pass(monkeypatch, tmp_path)
    monkeypatch.setattr(
        capture, "capture_icon_eps_station_days",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("HTTPStatusError:404")),
    )
    icon_00 = _follow_file(tmp_path, f"icon_{DAY}_d2_00z.json", "2026-07-30T00:00:00Z", _station_ids())
    receipt = capture.capture_official_native_forward(
        registry_path=registry_path,
        event_date=DAY,
        gefs_output=tmp_path / f"gefs_{DAY}_d2.json",
        ecmwf_output=tmp_path / f"ecmwf_{DAY}_d2.json",
        eccc_output=tmp_path / f"eccc_{DAY}_d2.json",
        icon_output=tmp_path / f"icon_{DAY}_d2.json",
        inbox_output=tmp_path / "inbox.json",
        timeout_seconds=30.0,
        lead_days=2,
        run_cycle_hour=0,
        follow_new_runs=True,
        follow_serving_ready=lambda **kw: kw["family"] == follow_run.ICON_FAMILY,
    )
    assert "DWD_ICON_EPS" not in receipt["run_init_utc_by_family"]
    assert set(receipt["follow_serving"][follow_run.ICON_FAMILY].values()) == {"2026-07-30T00:00:00Z"}
    builder = _builder(commands)
    assert builder[builder.index("--follow-archive") + 1] == str(icon_00)
