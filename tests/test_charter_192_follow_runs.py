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
from bayes_basket_target.native_archive_lead import capture_lead_bucket
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
def _no_ai_network(monkeypatch) -> None:
    """AIFS ENS / AIGEFS runs on this lane fail like an unpublished object."""

    def refuse(**_kw):
        raise RuntimeError("HTTPStatusError:404")

    monkeypatch.setattr(capture, "capture_aifs_station_days", refuse)
    monkeypatch.setattr(capture, "capture_aigefs_station_days", refuse)


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

    def fake_ecmwf(**kw):
        calls.append(("ecmwf", kw["lead_days"], kw["run_cycle_hour"], len(kw["station_scopes"])))
        run = follow_run.follow_run_init(DAY, kw["lead_days"], kw["run_cycle_hour"])
        identity = {"schema": follow_run.archive_schema(follow_run.ECMWF_FAMILY), "event_date": DAY,
                    "run_init_utc": run.isoformat().replace("+00:00", "Z"),
                    "training_only": True, "holdout_eligible": False,
                    "captures": [{"station_id": s["station_id"]} for s in kw["station_scopes"]]}
        return {**identity, "content_sha256": content_sha256(identity)}

    monkeypatch.setattr(capture, "capture_icon_eps_station_days", fake_icon)
    monkeypatch.setattr(capture, "capture_geps_station_days", fake_eccc)
    monkeypatch.setattr(capture, "capture_ecmwf_station_days", fake_ecmwf)
    # Charters 204/212 put AIFS ENS and AIGEFS on this lane after the test
    # was written; unfaked, their real collectors went to the network.
    _no_ai_network(monkeypatch)
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
    # charter 193 amendment: ECMWF's D-3 18Z is banked whatever the rungs;
    # charter 263 (四): for New York's day too (read on past +72h)
    assert states[(follow_run.ECMWF_FAMILY, 3, "2026-07-29T18:00:00Z")] == "CAPTURED"
    assert [c for c in calls if c[0] == "ecmwf"] == [("ecmwf", 3, 18, len(rows))]
    assert sorted(written) == [
        f"ecmwf_{DAY}_d3_18z.json", f"icon_{DAY}_d1_12z.json", f"icon_{DAY}_d1_18z.json",
    ]
    # a second lap finds them on disk and downloads nothing
    calls.clear()
    again = capture.capture_follow_runs(
        registry_path=registry_path, event_date=DAY, rungs=rungs,
        capture_root=tmp_path, timeout_seconds=30.0,
        as_of_utc="2026-08-01T05:30:00Z", icon_download_workers=8,
    )
    assert [c for c in calls if c[0] in {"icon", "ecmwf"}] == []
    assert {o["state"] for o in again["outcomes"] if o["family"] == follow_run.ICON_FAMILY and o["lead_days"] == 1} == {"RETAINED"}
    assert {o["state"] for o in again["outcomes"] if o["family"] == follow_run.ECMWF_FAMILY} == {"RETAINED"}


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
    monkeypatch.setattr(capture, "capture_ecmwf_station_days", lambda **_kw: (_ for _ in ()).throw(RuntimeError("stop")))
    _no_ai_network(monkeypatch)
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
    # Charter 263 (四)'s gap check reads the registry; no held file, no gap.
    host = types.SimpleNamespace(_unfollowed_event_run_gaps=lambda **_kw: set())
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
    # a rung every family already reads at its own cycle queues nothing once
    # the per-event-date runs (ECMWF D-3 18Z) are on disk ...
    (tmp_path / "ecmwf_2026-09-24_d3_18z.json").write_text("{}")
    # ... and charter 204's AIFS ENS and charter 212's AIGEFS runs, followed
    # per event date too (written before AIGEFS joined, so it queued AIGEFS)
    for family in (follow_run.AIFS_FAMILY, follow_run.AIGEFS_FAMILY):
        for lead, hour in follow_run.event_follow_runs(family):
            follow_run.follow_archive_path(tmp_path, family, "2026-09-24", lead, hour).write_text("{}")
    assert enqueue(host, **{**kwargs, "event_date": "2026-09-24",
                            "run_ladder": [(1, None, _t("2026-09-23", 0))]}) == "NO_FOLLOW_RUNG"
    # ... and still queues for a date whose D-3 18Z is missing
    assert enqueue(host, **{**kwargs, "event_date": "2026-09-25",
                            "run_ladder": [(1, None, _t("2026-09-24", 0))]}) == "QUEUED"
    release.set()
    future = host._official_native_follow_inflight["2026-09-23"]
    assert future.result(5)["state"] == "COMMITTED_NO_LIVE"
    host._official_native_follow_pool.shutdown(wait=True)
    rungless = next(c for c in commands if "2026-09-25" in c)
    assert "--follow-rung" not in rungless
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


def test_icon_six_hour_runs_are_seated_by_default_and_can_be_held_back(monkeypatch, tmp_path) -> None:
    # User ruling 2026-09-21/22: ICON 06Z/18Z may be seated -- on the same
    # certificate as every other follow run.
    monkeypatch.delenv(follow_run.SECONDARY_SERVING_ENV, raising=False)
    registry_path, commands = _setup_pass(monkeypatch, tmp_path)
    baseline = _pass(tmp_path, registry_path)                      # commands[0]
    icon_18 = _follow_file(tmp_path, f"icon_{DAY}_d1_18z.json", "2026-07-31T18:00:00Z", _station_ids())
    icon_12 = _follow_file(tmp_path, f"icon_{DAY}_d1_12z.json", "2026-07-31T12:00:00Z", _station_ids())
    _follow_file(tmp_path, f"icon_{DAY}_d1_06z.json", "2026-07-31T06:00:00Z", _station_ids())
    assert follow_run.follow_serving_allowed(follow_run.ICON_FAMILY, 6)
    assert follow_run.follow_serving_allowed(follow_run.ICON_FAMILY, 18)
    # default on, nothing certified: byte-identical to the pass without follow
    uncertified = _pass(tmp_path, registry_path, follow_new_runs=True,
                        follow_serving_ready=lambda **_kw: False)  # commands[1]
    assert _builder(commands[1:2]) == _builder(commands[0:1])
    assert {k: v for k, v in uncertified.items() if k != "captured_at_utc"} == {
        k: v for k, v in baseline.items() if k != "captured_at_utc"
    }
    # certified: the 6-hourly 18Z is the newest and is seated
    receipt = _pass(tmp_path, registry_path, follow_new_runs=True,
                    follow_serving_ready=lambda **_kw: True)       # commands[2]
    assert set(receipt["follow_serving"][follow_run.ICON_FAMILY].values()) == {"2026-07-31T18:00:00Z"}
    assert str(icon_12) not in _builder(commands[2:3])
    # "off" holds the 6-hour cycles back; the certified 12Z beneath is taken
    monkeypatch.setenv(follow_run.SECONDARY_SERVING_ENV, "off")
    receipt = _pass(tmp_path, registry_path, follow_new_runs=True,
                    follow_serving_ready=lambda **_kw: True)       # commands[3]
    assert set(receipt["follow_serving"][follow_run.ICON_FAMILY].values()) == {"2026-07-31T12:00:00Z"}
    assert str(icon_18) not in _builder(commands[3:4])
    assert follow_run.follow_serving_allowed(follow_run.ICON_FAMILY, 12)
    assert follow_run.follow_serving_allowed(follow_run.ECCC_FAMILY, 12)
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


# --------------------------------------------------------------------------
# Charter 193 amendment: ECMWF D-3 18Z for a D+2 day, banked for training.
# --------------------------------------------------------------------------
def test_ecmwf_d3_18z_has_its_own_identity_and_admission() -> None:
    from bayes_basket_target import ecmwf_ifs_archive as ecmwf

    assert ecmwf.extraction_version_for_lead(3, 18) == (
        "ecmwf_open_data_ifs_0p25_enfo_pf50_mx2t3_18z_d3_v1"
    )
    assert ecmwf.SUPPORTED_LEAD_DAYS == (0, 1, 2)   # the ladder's leads are untouched
    admitted = lambda version, cycle="18Z": oos._historical_transport_scope_allowed(  # noqa: E731
        system_id="ECMWF_IFS_ENS_OPEN_PF50",
        transport_provider_id="ECMWF_OPEN_DATA_S3",
        lead_bucket="h24_48",
        run_cycle=cycle,
        extraction_version=version,
    )
    assert admitted("ecmwf_open_data_ifs_0p25_enfo_pf50_mx2t3_18z_d3_v1")
    assert not admitted("ecmwf_open_data_ifs_0p25_enfo_pf50_mx2t3_12z_d3_v1", "12Z")
    # every existing ECMWF identity is admitted exactly as before
    assert admitted("ecmwf_open_data_ifs_0p25_enfo_pf50_mx2t3_18z_d2_v1")
    assert oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS["ECMWF_IFS_ENS_OPEN_PF50"]["leads"]["d3"] == [{
        "run_cycle": "18Z",
        "extraction_version": "ecmwf_open_data_ifs_0p25_enfo_pf50_mx2t3_18z_d3_v1",
        "serving_follow": True,
    }]
    assert scheduler.contracted_run_inits("2026-09-24", lead_days=2)[
        "ECMWF_IFS_ENS_OPEN_PF50"
    ] == datetime(2026, 9, 22, 18, tzinfo=UTC)


def test_ecmwf_accepts_lead_three_only_at_18z() -> None:
    from bayes_basket_target import ecmwf_ifs_archive as ecmwf
    from bayes_basket_target.contracts import ContractError

    for cycle in (None, 0, 12):
        with pytest.raises(ContractError, match="ecmwf_archive_lead_days_invalid"):
            ecmwf.capture_ecmwf_station_days(
                event_date="2026-09-24", station_scopes=[],
                archive_query_at_utc="2026-09-22T06:00:00Z",
                lead_days=3, run_cycle_hour=cycle,
            )
    assert ecmwf._lead_bucket_for_run(
        event_date="2026-09-24", run_init=_t("2026-09-21", 18)
    ) == "d3"
    with pytest.raises(ContractError):
        ecmwf._lead_bucket_for_run(
            event_date="2026-09-24", run_init=_t("2026-09-21", 12)
        )


def test_ecmwf_d3_18z_covers_what_plus_72h_reaches() -> None:
    from bayes_basket_target import ecmwf_ifs_archive as ecmwf

    run = _t("2026-09-21", 18)
    seal = lambda tz: ecmwf.follow_period_end_steps(  # noqa: E731
        event_date="2026-09-24", settlement_timezone=tz, run_init_utc=run
    )
    assert len(seal("Asia/Shanghai")) == 8
    assert len(seal("Europe/Berlin")) == 7
    # Charter 263 (四): +72h cannot seal New York's day, so the read goes on
    # to FOLLOW_ONLY_MAX_FORECAST_STEP instead of skipping the station.
    new_york = seal("America/New_York")
    assert new_york and max(new_york) > 72
    assert max(new_york) <= ecmwf.FOLLOW_ONLY_MAX_FORECAST_STEP
    # Chengdu: 46h to its local midnight -- its own (h24_48, 18Z) scope,
    # which D-2 18Z (22h, h12_24) never reaches
    assert capture_lead_bucket(
        event_date="2026-09-24", settlement_timezone="Asia/Shanghai", run_init=run
    ) == "h24_48"
    assert capture_lead_bucket(
        event_date="2026-09-24", settlement_timezone="Asia/Shanghai",
        run_init=_t("2026-09-22", 18),
    ) == "h12_24"


def test_a_banked_ecmwf_d3_run_does_not_change_the_pass(monkeypatch, tmp_path) -> None:
    # Held only when ECMWF has no run of its own for the date: a pass that
    # captured one is the pass it was, certified or not.
    registry_path, commands = _setup_pass(monkeypatch, tmp_path)
    _pass(tmp_path, registry_path)
    _follow_file(tmp_path, f"ecmwf_{DAY}_d3_18z.json", "2026-07-29T18:00:00Z",
                 _station_ids(), schema=follow_run.archive_schema(follow_run.ECMWF_FAMILY))
    _pass(tmp_path, registry_path, follow_new_runs=True, follow_serving_ready=lambda **_kw: True)
    assert _builder(commands[:1]) == _builder(commands[1:])


def _d2_pass(tmp_path: Path, registry_path: Path, **kwargs) -> dict[str, Any]:
    return capture.capture_official_native_forward(
        registry_path=registry_path,
        event_date=DAY,
        gefs_output=tmp_path / f"gefs_{DAY}_d2.json",
        ecmwf_output=tmp_path / f"ecmwf_{DAY}_d2.json",
        eccc_output=tmp_path / f"eccc_{DAY}_d2.json",
        icon_output=tmp_path / f"icon_{DAY}_d2.json",
        inbox_output=tmp_path / "inbox.json",
        timeout_seconds=30.0,
        lead_days=2,
        **{"run_cycle_hour": 0, **kwargs},
    )


def _ecmwf_not_landed(monkeypatch, tmp_path: Path) -> tuple[Path, list[list[str]], list]:
    # 05Z D-2: GEFS D-2 00Z is in, ECMWF's D-2 00Z is not, and ECMWF has no
    # run of its own for the date anywhere.
    registry_path, commands = _setup_pass(monkeypatch, tmp_path)
    monkeypatch.setattr(
        capture, "capture_gefs_station_days",
        lambda **_kw: {**_archive(GEFS, "2026-07-30T00:00:00Z"),
                       "captures": [{"station_id": s} for s in _station_ids()]},
    )
    monkeypatch.setattr(
        capture, "capture_ecmwf_station_days",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("HTTPStatusError:404")),
    )
    verified: list = []
    monkeypatch.setattr(
        capture, "_verify_official_inbox",
        lambda _path, **kw: verified.append(sorted(kw["station_ids"]))
        or {"content_sha256": "0" * 64, "packets": []},
    )
    return registry_path, commands, verified


def _ecmwf_d3(tmp_path: Path, stations: list[str]) -> Path:
    return _follow_file(
        tmp_path, f"ecmwf_{DAY}_d3_18z.json", "2026-07-29T18:00:00Z", stations,
        schema=follow_run.archive_schema(follow_run.ECMWF_FAMILY),
    )


def test_a_d2_pass_without_ecmwf_holds_the_certified_d3_18z_run(monkeypatch, tmp_path) -> None:
    registry_path, commands, verified = _ecmwf_not_landed(monkeypatch, tmp_path)
    stations = _station_ids()
    banked = _ecmwf_d3(tmp_path, stations[:-1])     # the run covers all but one
    certified = set(stations[:2]) | {stations[-1]}  # the uncovered one cannot seat
    asked: list = []
    receipt = _d2_pass(
        tmp_path, registry_path,
        follow_new_runs=True,
        follow_serving_ready=lambda **kw: asked.append(
            (kw["family"], kw["run_init"])
        ) or (kw["family"] == follow_run.ECMWF_FAMILY and kw["station_id"] in certified),
    )
    seated = sorted(stations[:2])
    builder = _builder(commands)
    assert builder[builder.index("--archive") + 3] == str(banked)   # ECMWF's archive
    assert sorted(
        builder[i + 1] for i, part in enumerate(builder) if part == "--station-id"
    ) == seated
    assert verified == [seated]
    assert receipt["station_count"] == 2
    assert receipt["family_held_from_other_lead"] == {"ECMWF_OPEN_DATA_S3": banked.name}
    assert receipt["ecmwf_event_run_hold"]["run_init_utc"] == "2026-07-29T18:00:00Z"
    assert sorted(receipt["ecmwf_event_run_hold"]["stations"]) == seated
    assert "ECMWF_OPEN_DATA_S3" in receipt["family_held_on_earlier_run"]
    assert receipt["run_init_utc_by_family"]["ECMWF_OPEN_DATA_S3"] == "2026-07-29T18:00:00Z"
    # certificates are asked of the D-3 18Z run itself
    assert (follow_run.ECMWF_FAMILY, datetime(2026, 7, 29, 18, tzinfo=UTC)) in asked


def test_without_a_certificate_the_d2_pass_fails_as_it_always_did(monkeypatch, tmp_path) -> None:
    registry_path, commands, _verified = _ecmwf_not_landed(monkeypatch, tmp_path)
    _ecmwf_d3(tmp_path, _station_ids())
    for kwargs in (
        {"follow_new_runs": True, "follow_serving_ready": lambda **_kw: False},
        {"follow_new_runs": False, "follow_serving_ready": lambda **_kw: True},
        {},
    ):
        with pytest.raises(
            capture.ContractError,
            match="required_family_unavailable:ECMWF_OPEN_DATA_S3",
        ):
            _d2_pass(tmp_path, registry_path, **kwargs)
    assert not commands


def test_ecmwf_holding_any_run_of_its_own_never_reads_the_d3_run(monkeypatch, tmp_path) -> None:
    # Charter 148's other-lead hold (or this lead's own file) outranks it: a
    # D-2 run is newer than D-3 18Z, so nothing moves backwards.
    registry_path, commands, _verified = _ecmwf_not_landed(monkeypatch, tmp_path)
    _ecmwf_d3(tmp_path, _station_ids())
    own = {**_archive(ECMWF, "2026-07-30T00:00:00Z"),
           "captures": [{"station_id": s} for s in _station_ids()]}
    monkeypatch.setattr(
        capture, "_load_retained_archive",
        lambda path, **_kw: own if Path(path).name == f"ecmwf_{DAY}_d2.json" else None,
    )
    asked: list = []
    receipt = _d2_pass(
        tmp_path, registry_path, follow_new_runs=True,
        follow_serving_ready=lambda **kw: asked.append(kw["family"]) or True,
    )
    assert "ecmwf_event_run_hold" not in receipt
    assert follow_run.ECMWF_FAMILY not in asked
    assert "--station-id" not in _builder(commands)
    assert str(tmp_path / f"ecmwf_{DAY}_d2.json") in _builder(commands)


# --------------------------------------------------------------------------
# Charter 193 (三) 不倒退: a pass never writes or serves a run older than the
# one the date's file already holds (2026-09-23 04:01Z regression).
# --------------------------------------------------------------------------
def _retained(path: Path, schema: str, run: str, stations: list[str]) -> Path:
    identity = {
        "schema": schema,
        "event_date": DAY,
        "run_init_utc": run,
        "training_only": True,
        "holdout_eligible": False,
        "station_count": len(stations),
        "captures": [{"station_id": station} for station in stations],
    }
    path.write_text(json.dumps({**identity, "content_sha256": content_sha256(identity)}))
    return path


def test_an_archive_is_never_replaced_by_an_older_run_of_the_same_date(tmp_path) -> None:
    path = _retained(tmp_path / f"ecmwf_{DAY}_d2.json", ECMWF, "2026-07-30T18:00:00Z", ["ZUUU"])
    before = path.read_text()
    with pytest.raises(capture.ContractError, match="archive_would_regress"):
        capture._write_archive_no_regress(
            path, _archive(ECMWF, "2026-07-30T12:00:00Z"), event_date=DAY
        )
    assert path.read_text() == before
    # the same run, or a newer one, is written as before
    same = json.loads(before)
    capture._write_archive_no_regress(path, same, event_date=DAY)
    assert json.loads(path.read_text())["run_init_utc"] == "2026-07-30T18:00:00Z"
    newer = {**_archive(ECMWF, "2026-07-31T00:00:00Z"), "event_date": DAY}
    assert capture._write_archive_no_regress(path, newer, event_date=DAY) is True
    # another date's file never blocks
    other = _retained(tmp_path / "other.json", ECMWF, "2026-07-30T18:00:00Z", ["ZUUU"])
    other_payload = json.loads(other.read_text())
    other.write_text(json.dumps({**other_payload, "event_date": "2026-08-02"}))
    assert capture._write_archive_no_regress(
        other, _archive(ECMWF, "2026-07-30T12:00:00Z"), event_date=DAY
    ) is True


def _older_rung_pass(monkeypatch, tmp_path: Path, retained_stations: list[str]):
    _install_station_registry(monkeypatch)
    monkeypatch.setattr(capture, "load_market_registry_snapshot", lambda _p: _registry())
    stations = _station_ids()
    for name, schema in (("gefs", GEFS), ("ecmwf", ECMWF)):
        _retained(tmp_path / f"{name}_{DAY}_d2.json", schema, "2026-07-30T18:00:00Z", retained_stations)
    captured: list[str] = []

    def _capture(name: str, schema: str, run: str):
        def run_capture(**_kw):
            captured.append(name)
            return {**_archive(schema, run), "event_date": DAY,
                    "captures": [{"station_id": s} for s in stations]}
        return run_capture

    monkeypatch.setattr(capture, "capture_gefs_station_days", _capture("gefs", GEFS, "2026-07-30T12:00:00Z"))
    monkeypatch.setattr(capture, "capture_ecmwf_station_days", _capture("ecmwf", ECMWF, "2026-07-30T12:00:00Z"))
    monkeypatch.setattr(capture, "capture_geps_station_days", _capture("eccc", ECCC, "2026-07-30T12:00:00Z"))
    monkeypatch.setattr(capture, "capture_icon_eps_station_days", _capture("icon", ICON, "2026-07-30T12:00:00Z"))
    written: list[str] = []
    monkeypatch.setattr(
        capture, "write_bounded_json_if_changed",
        lambda path, _payload: written.append(Path(path).name) or True,
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        capture.subprocess, "run",
        lambda command, **_kw: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="built", stderr=""),
    )
    monkeypatch.setattr(capture, "_verify_official_inbox", lambda *_a, **_k: {"content_sha256": "0" * 64, "packets": []})
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    run = lambda: _d2_pass(tmp_path, registry_path, run_cycle_hour=12)  # noqa: E731
    return run, captured, written


def test_an_older_rung_serves_the_newer_run_the_date_already_holds(monkeypatch, tmp_path) -> None:
    # The 09-23 04:01Z walk: the 18Z rung failed, the 12Z rung ran.  It now
    # holds the 18Z both files already carry instead of fetching 12Z.
    run, captured, written = _older_rung_pass(monkeypatch, tmp_path, _station_ids())
    receipt = run()
    assert "gefs" not in captured and "ecmwf" not in captured
    assert receipt["run_init_utc_by_family"]["NOAA_GEFS_PDS_S3"] == "2026-07-30T18:00:00Z"
    assert receipt["run_init_utc_by_family"]["ECMWF_OPEN_DATA_S3"] == "2026-07-30T18:00:00Z"
    assert receipt["family_kept_newer_retained"] == {
        "ECMWF_OPEN_DATA_S3": "2026-07-30T18:00:00Z",
        "NOAA_GEFS_PDS_S3": "2026-07-30T18:00:00Z",
    }
    # nothing older reached either file
    assert json.loads((tmp_path / f"ecmwf_{DAY}_d2.json").read_text())["run_init_utc"] == "2026-07-30T18:00:00Z"


def test_an_older_rung_that_cannot_hold_the_newer_run_fails_closed(monkeypatch, tmp_path) -> None:
    # The newer file misses a station this pass asks for, so the older run is
    # captured -- and refused at the write: the date keeps what it had.
    run, captured, written = _older_rung_pass(monkeypatch, tmp_path, _station_ids()[:1])
    with pytest.raises(capture.ContractError, match="archive_would_regress:gefs_"):
        run()
    assert captured == ["gefs"]
    assert f"gefs_{DAY}_d2.json" not in written


# --------------------------------------------------------------------------
# Charter 193 (三) 谁新用谁 inside a pass (2026-09-23 08:00Z):
# the rung's run not yet published -> the newest published older cycle of
# the same lead day, if newer than what the family holds; and a pass whose
# required families are both held on wider archives builds only its own set.
# --------------------------------------------------------------------------
def _fallback_pass(monkeypatch, tmp_path: Path, *, ecmwf_errors: dict[int, str], held_run: str):
    _install_station_registry(monkeypatch)
    monkeypatch.setattr(capture, "load_market_registry_snapshot", lambda _p: _registry())
    stations = _station_ids()
    _retained(tmp_path / f"ecmwf_{DAY}_d2.json", ECMWF, held_run, stations)
    asked: list[int | None] = []

    def ecmwf_capture(**kw):
        hour = kw.get("run_cycle_hour")
        asked.append(hour)
        if hour in ecmwf_errors:
            raise RuntimeError(ecmwf_errors[hour])
        return {**_archive(ECMWF, f"2026-07-30T{hour:02d}:00:00Z"), "event_date": DAY,
                "captures": [{"station_id": s} for s in stations]}

    monkeypatch.setattr(capture, "capture_ecmwf_station_days", ecmwf_capture)
    monkeypatch.setattr(capture, "capture_gefs_station_days",
                        lambda **kw: {**_archive(GEFS, "2026-07-30T06:00:00Z"), "event_date": DAY,
                                      "captures": [{"station_id": s} for s in stations]})
    monkeypatch.setattr(capture, "capture_geps_station_days", lambda **kw: _archive(ECCC, "2026-07-30T00:00:00Z"))
    monkeypatch.setattr(capture, "capture_icon_eps_station_days", lambda **kw: _archive(ICON, "2026-07-30T00:00:00Z"))
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
    receipt = _d2_pass(tmp_path, registry_path, run_cycle_hour=6)
    return receipt, asked


def test_an_unpublished_rung_run_falls_back_to_a_newer_published_cycle(monkeypatch, tmp_path) -> None:
    # Rung D-2 06Z: ECMWF 06Z 404, its 00Z is out; the file holds D-3 18Z.
    receipt, asked = _fallback_pass(
        monkeypatch, tmp_path,
        ecmwf_errors={6: "HTTPStatusError:Client error '404 Not Found'"},
        held_run="2026-07-29T18:00:00Z",
    )
    assert asked == [6, 0]
    assert receipt["run_init_utc_by_family"]["ECMWF_OPEN_DATA_S3"] == "2026-07-30T00:00:00Z"
    assert receipt["family_fell_back_within_lead"] == {"ECMWF_OPEN_DATA_S3": "2026-07-30T00:00:00Z"}
    # the rung's own run is still reported as not served
    assert "ECMWF_OPEN_DATA_S3" in receipt["family_held_on_earlier_run"]


def test_no_fallback_at_or_below_the_held_run_or_on_a_network_failure(monkeypatch, tmp_path) -> None:
    # held 00Z already: nothing between it and 06Z to ask for
    receipt, asked = _fallback_pass(
        monkeypatch, tmp_path,
        ecmwf_errors={6: "HTTPStatusError:Client error '404 Not Found'"},
        held_run="2026-07-30T00:00:00Z",
    )
    assert asked == [6]
    assert receipt["run_init_utc_by_family"]["ECMWF_OPEN_DATA_S3"] == "2026-07-30T00:00:00Z"
    assert "family_fell_back_within_lead" not in receipt
    # a transport failure is not an answer about the run: hold, do not walk
    receipt, asked = _fallback_pass(
        monkeypatch, tmp_path,
        ecmwf_errors={6: "ConnectError:[Errno 60] Operation timed out"},
        held_run="2026-07-29T18:00:00Z",
    )
    assert asked == [6]
    assert receipt["run_init_utc_by_family"]["ECMWF_OPEN_DATA_S3"] == "2026-07-29T18:00:00Z"


def test_both_required_families_held_on_wider_archives_build_only_this_pass(monkeypatch, tmp_path) -> None:
    # A same-day later rung: GEFS and ECMWF both hold archives that cover more
    # stations than this rung's run can; the builder is told this pass's set.
    run, captured, written = _older_rung_pass(monkeypatch, tmp_path, _station_ids() + ["ZZZZ"])
    verified: list = []
    monkeypatch.setattr(
        capture, "_verify_official_inbox",
        lambda _path, **kw: verified.append(sorted(kw["station_ids"]))
        or {"content_sha256": "0" * 64, "packets": []},
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        capture.subprocess, "run",
        lambda command, **_kw: commands.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="built", stderr=""),
    )
    run()
    builder = _builder(commands)
    named = sorted(builder[i + 1] for i, part in enumerate(builder) if part == "--station-id")
    assert named == sorted(_station_ids()) == verified[0]


def _pool(conn: sqlite3.Connection, *, station: str, season_bin: str, lead_bucket: str, cycles: dict, generation: int = 1) -> None:
    conn.execute("PRAGMA foreign_keys=OFF")
    ident = f"pool-{station}-{lead_bucket}-{sorted(cycles.items())}"
    conn.execute(
        "INSERT INTO calibration_native_ensemble_activation("
        "activation_id,artifact_kind,artifact_id,station_id,"
        "ensemble_system_id_or_star,season_bin,lead_bucket,run_cycle,"
        "run_cycle_by_system_json,run_cycle_by_system_sha256,"
        "settlement_definition,available_system_set_hash_or_star,generation,"
        "valid_from_utc,valid_until_utc,activated_at_utc,authority_scope,"
        "content_sha256) VALUES(?,'CROSS_NATIVE_ENSEMBLE',?,?,'*',?,?,"
        "'MIXED_NATIVE_CYCLES',?,?,'official_daily_tmax_c',?,?,"
        "'2026-09-14T00:00:00Z','2026-10-14T00:00:00Z','2026-09-14T00:00:00Z',"
        "'PRODUCTION_NO_LIVE_ONLY',?)",
        (
            f"act-{ident}", ident, station, season_bin, lead_bucket,
            follow_run._json(cycles), content_sha256(cycles), "h" * 64,
            generation, content_sha256(["pool", ident]),
        ),
    )


def test_a_six_hour_icon_run_is_seated_only_beside_weights_on_its_own_grid(ssot) -> None:
    # Charter 193 (三): ICON 06Z's own fit is not enough; P0 will not seat it
    # on a pool fitted with ICON at 00Z/12Z, so without a 06Z/18Z pool the
    # station keeps ICON's 3-hourly run.
    from bayes_basket_target.station_season import station_season_bin

    season = station_season_bin("ZUUU", "2026-09-23")
    run = _t("2026-09-22", 18)  # ZUUU: 22h before local midnight -> h12_24
    bucket = capture_lead_bucket(event_date="2026-09-23", settlement_timezone="Asia/Shanghai", run_init=run)
    _certify(ssot, station="ZUUU", season_bin=season, lead_bucket=bucket, cycle="18Z", fit_cases=20, n_effective=10.0)
    assert not _ready(ssot, run)
    _pool(ssot, station="ZUUU", season_bin=season, lead_bucket=bucket,
          cycles={"DWD_ICON_GLOBAL_EPS": "00Z", "NOAA_GEFS_05": "18Z"})
    assert not _ready(ssot, run)   # ICON on the 3-hour grid in every pool
    _pool(ssot, station="ZUUU", season_bin=season, lead_bucket=bucket,
          cycles={"DWD_ICON_GLOBAL_EPS": "06Z", "NOAA_GEFS_05": "06Z"}, generation=2)
    assert _ready(ssot, run)


# --------------------------------------------------------------------------
# A follow file grows to the stations listed after its first read.
# --------------------------------------------------------------------------
def _follow_archive(stations: list[str], query: str, *, receipt: str) -> dict:
    run = follow_run.follow_run_init(DAY, 1, 12).isoformat().replace("+00:00", "Z")
    identity = {
        "schema": ICON, "event_date": DAY, "run_init_utc": run,
        "run_cycle": "12Z", "archive_lead_bucket": "d1",
        "ensemble_system_id": icon.SYSTEM_ID,
        "independence_family_id": icon.INDEPENDENCE_FAMILY_ID,
        "member_count": 40, "training_only": True, "holdout_eligible": False,
        "single_download_per_global_field": True,
        "station_count": len(stations), "station_ids": list(stations),
        "union_forecast_hours": [12, 15] if receipt == "a" else [15, 18],
        "archive_query_at_utc": query, "object_receipt_set_sha256": receipt * 64,
        "total_response_bytes": 10,
        "captures": [
            {"station_id": s, "archive_query_at_utc": query, "x": s} for s in stations
        ],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_a_supplement_keeps_every_retained_capture_and_adds_only_new_stations() -> None:
    retained = _follow_archive(["ZUUU"], "2026-08-01T03:00:00Z", receipt="a")
    supplement = _follow_archive(["KLGA"], "2026-08-01T05:30:00Z", receipt="b")
    merged = capture.merge_follow_supplement(retained, supplement)
    assert merged["captures"][0] == retained["captures"][0]
    assert merged["captures"][1] == supplement["captures"][0]
    assert merged["station_ids"] == ["ZUUU", "KLGA"]
    assert merged["station_count"] == 2
    assert merged["union_forecast_hours"] == [12, 15, 18]
    assert merged["total_response_bytes"] == 20
    # the file never claims an arrival earlier than a capture it holds
    assert merged["archive_query_at_utc"] == "2026-08-01T05:30:00Z"
    # the first read's receipts stay at top level, the second's are its own
    assert merged["object_receipt_set_sha256"] == "a" * 64
    assert merged["supplemental_reads"] == [{
        "station_count": 1, "station_ids": ["KLGA"],
        "union_forecast_hours": [15, 18],
        "archive_query_at_utc": "2026-08-01T05:30:00Z",
        "object_receipt_set_sha256": "b" * 64, "total_response_bytes": 10,
    }]
    assert merged["content_sha256"] == content_sha256(
        {k: v for k, v in merged.items() if k != "content_sha256"}
    )


@pytest.mark.parametrize("change", [
    {"run_init_utc": "2026-07-31T00:00:00Z"},
    {"extraction_version": "other"},
    {"captures": [{"station_id": "ZUUU"}]},
    {"archive_query_at_utc": "2026-08-01T02:00:00Z"},
])
def test_a_supplement_that_is_not_the_same_run_read_later_is_refused(change) -> None:
    retained = _follow_archive(["ZUUU"], "2026-08-01T03:00:00Z", receipt="a")
    supplement = {**_follow_archive(["KLGA"], "2026-08-01T05:30:00Z", receipt="b"), **change}
    with pytest.raises(capture.ContractError):
        capture.merge_follow_supplement(retained, supplement)


def test_a_retained_follow_file_reads_only_the_stations_listed_since(monkeypatch, tmp_path) -> None:
    _install_station_registry(monkeypatch)
    rows = _registry()["rows"]
    monkeypatch.setattr(capture, "load_market_registry_snapshot", lambda _p: {"rows": rows})
    listed = sorted({str(row["station_id"]) for row in rows})
    assert len(listed) >= 2
    asked: list[list[str]] = []

    def fake_icon(**kw):
        stations = [s["station_id"] for s in kw["station_scopes"]]
        asked.append(stations)
        return _follow_archive(stations, kw["archive_query_at_utc"], receipt="b")

    monkeypatch.setattr(capture, "capture_icon_eps_station_days", fake_icon)
    stop = lambda **_kw: (_ for _ in ()).throw(RuntimeError("HTTPStatusError:404"))  # noqa: E731
    monkeypatch.setattr(capture, "capture_geps_station_days", stop)
    monkeypatch.setattr(capture, "capture_ecmwf_station_days", stop)
    _no_ai_network(monkeypatch)
    path = follow_run.follow_archive_path(tmp_path, follow_run.ICON_FAMILY, DAY, 1, 12)
    first = _follow_archive(listed[:1], "2026-08-01T03:00:00Z", receipt="a")
    path.write_text(json.dumps(first), encoding="utf-8")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")

    receipt = capture.capture_follow_runs(
        registry_path=registry_path, event_date=DAY, rungs=[(1, 12)],
        capture_root=tmp_path, timeout_seconds=30.0,
        as_of_utc="2026-08-01T05:30:00Z",
    )
    outcome = next(
        o for o in receipt["outcomes"]
        if o["family"] == follow_run.ICON_FAMILY and o["path"] == path.name
    )
    assert outcome["state"] == "SUPPLEMENTED"
    assert asked == [listed[1:]]
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["captures"][0] == first["captures"][0]
    assert sorted(follow_run.archive_station_ids(on_disk)) == listed
    assert str(path) in receipt["archive_paths"]

    asked.clear()
    again = capture.capture_follow_runs(
        registry_path=registry_path, event_date=DAY, rungs=[(1, 12)],
        capture_root=tmp_path, timeout_seconds=30.0,
        as_of_utc="2026-08-01T06:00:00Z",
    )
    assert asked == []
    assert next(
        o for o in again["outcomes"]
        if o["family"] == follow_run.ICON_FAMILY and o["path"] == path.name
    )["state"] == "RETAINED"


def test_an_older_follow_file_of_the_date_is_revisited_once_a_newer_cycle_leads(monkeypatch, tmp_path) -> None:
    # The rung now names 18Z; the 12Z file is no rung target any more but
    # still lacks a station listed since its read.
    _install_station_registry(monkeypatch)
    rows = _registry()["rows"]
    monkeypatch.setattr(capture, "load_market_registry_snapshot", lambda _p: {"rows": rows})
    listed = sorted({str(row["station_id"]) for row in rows})
    asked: list[tuple[int, list[str]]] = []

    def fake_icon(**kw):
        stations = [s["station_id"] for s in kw["station_scopes"]]
        asked.append((kw["run_cycle_hour"], stations))
        if kw["run_cycle_hour"] != 12:
            raise RuntimeError("HTTPStatusError:404")
        return _follow_archive(stations, kw["archive_query_at_utc"], receipt="b")

    monkeypatch.setattr(capture, "capture_icon_eps_station_days", fake_icon)
    stop = lambda **_kw: (_ for _ in ()).throw(RuntimeError("HTTPStatusError:404"))  # noqa: E731
    monkeypatch.setattr(capture, "capture_geps_station_days", stop)
    monkeypatch.setattr(capture, "capture_ecmwf_station_days", stop)
    _no_ai_network(monkeypatch)
    path = follow_run.follow_archive_path(tmp_path, follow_run.ICON_FAMILY, DAY, 1, 12)
    path.write_text(json.dumps(
        _follow_archive(listed[:1], "2026-08-01T03:00:00Z", receipt="a")
    ), encoding="utf-8")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}", encoding="utf-8")
    receipt = capture.capture_follow_runs(
        registry_path=registry_path, event_date=DAY, rungs=[(1, 18)],
        capture_root=tmp_path, timeout_seconds=30.0,
        as_of_utc="2026-08-01T05:30:00Z",
    )
    assert (12, listed[1:]) in asked
    assert next(
        o for o in receipt["outcomes"] if o["path"] == path.name
    )["state"] == "SUPPLEMENTED"
    # past the age bound the file is left as it is
    asked.clear()
    path.write_text(json.dumps(
        _follow_archive(listed[:1], "2026-08-01T03:00:00Z", receipt="a")
    ), encoding="utf-8")
    later = capture.capture_follow_runs(
        registry_path=registry_path, event_date=DAY, rungs=[(1, 18)],
        capture_root=tmp_path, timeout_seconds=30.0,
        as_of_utc="2026-08-01T13:00:00Z",
    )
    assert all(cycle != 12 for cycle, _ in asked)
    assert all(o["path"] != path.name for o in later["outcomes"])
