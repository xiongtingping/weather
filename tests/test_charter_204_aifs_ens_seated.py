"""Charter 204: AIFS ENS seated and served -- what each layer must hold.

* the OOS contract admits AIFS rows under their own identities, and the
  capture scheduler's readiness names no AIFS run (follow-only family);
* the follow lane follows every AIFS run per event date at its own
  publication and seats the newest certified one, ADDED to the station's
  set (there is no AIFS rung archive to replace);
* the P0 root curve is formed from the curve-bearing systems when AIFS sits
  curveless, and is byte-identical when every seated system carries a curve.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bayes_basket_target import ecmwf_aifs_ens_archive as aifs
from bayes_basket_target import follow_run
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target import native_ensemble_p0 as p0
from bayes_basket_target import official_native_forward_packet as packet
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.official_nwp_capture_scheduler import contracted_run_inits
from scripts import capture_official_native_ensemble_forward as capture

UTC = timezone.utc


def _t(day: str, hour: int) -> datetime:
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, tzinfo=UTC)


# --------------------------------------------------------------------------
# Contract and scheduler
# --------------------------------------------------------------------------
def test_contract_admits_every_aifs_cycle_and_lead_under_its_own_identity() -> None:
    contract = oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS["ECMWF_AIFS_ENS_GLOBAL"]
    assert contract["transport_provider_id"] == aifs.TRANSPORT_PROVIDER_ID
    for lead in (0, 1, 2):
        entries = contract["leads"][f"d{lead}"]
        assert [e["run_cycle"] for e in entries] == ["00Z", "06Z", "12Z", "18Z"]
        assert all(e["serving_follow"] is True for e in entries)
        for hour in (0, 6, 12, 18):
            assert oos._historical_transport_scope_allowed(
                system_id="ECMWF_AIFS_ENS_GLOBAL",
                transport_provider_id="ECMWF_OPEN_DATA_S3",
                lead_bucket="h12_24",
                run_cycle=f"{hour:02d}Z",
                extraction_version=aifs.extraction_version_for_lead(lead, hour),
            )
    # a research-era identity or PF50's identity never reads as AIFS
    assert not oos._historical_transport_scope_allowed(
        system_id="ECMWF_AIFS_ENS_GLOBAL",
        transport_provider_id="ECMWF_OPEN_DATA_S3",
        lead_bucket="h12_24",
        run_cycle="06Z",
        extraction_version="research_" + aifs.extraction_version_for_lead(1, 6),
    )
    assert not oos._historical_transport_scope_allowed(
        system_id="ECMWF_AIFS_ENS_GLOBAL",
        transport_provider_id="ECMWF_OPEN_DATA_S3",
        lead_bucket="h12_24",
        run_cycle="00Z",
        extraction_version="ecmwf_open_data_ifs_0p25_enfo_pf50_mx2t3_00z_d1_v1",
    )


def test_scheduler_readiness_names_no_aifs_run() -> None:
    for lead in (0, 1, 2):
        runs = contracted_run_inits("2026-09-24", lead_days=lead)
        assert "ECMWF_AIFS_ENS_GLOBAL" not in runs
        assert {"ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"} <= set(runs)


def test_string_constants_agree_with_the_collector() -> None:
    assert packet.ECMWF_AIFS_ENS_MULTISTATION_SCHEMA == aifs.MULTISTATION_SCHEMA
    assert packet.SUPPORTED_ARCHIVE_SCHEMAS[aifs.MULTISTATION_SCHEMA] == aifs.TRANSPORT_PROVIDER_ID
    # charter 212 added NOAA AIGEFS beside it
    assert aifs.SYSTEM_ID in p0.CURVELESS_SYSTEM_IDS
    assert follow_run.system_id(follow_run.AIFS_FAMILY) == aifs.SYSTEM_ID
    assert follow_run.archive_schema(follow_run.AIFS_FAMILY) == aifs.MULTISTATION_SCHEMA


# --------------------------------------------------------------------------
# Follow lane
# --------------------------------------------------------------------------
def test_every_aifs_run_is_followed_per_event_date_at_its_own_publication() -> None:
    runs = follow_run.event_follow_runs(follow_run.AIFS_FAMILY)
    assert runs == tuple((lead, hour) for lead in (0, 1, 2) for hour in (0, 6, 12, 18))
    paths = follow_run.follow_archive_paths(Path("/x"), follow_run.AIFS_FAMILY, "2026-09-24")
    assert len(paths) == 12
    assert paths[0][0] > paths[-1][0]  # newest run first
    assert paths[-1][1].name == "aifs_2026-09-24_d2_00z.json"
    # nothing rung-based: the ladder has no AIFS rung
    for lead in (0, 1, 2):
        assert follow_run.follow_cycle_for_rung(follow_run.AIFS_FAMILY, lead, 12) is None
    run = _t("2026-09-23", 6)
    assert not follow_run.follow_publication_due(
        follow_run.AIFS_FAMILY, run, as_of=run + timedelta(hours=6, minutes=10)
    )
    assert follow_run.follow_publication_due(
        follow_run.AIFS_FAMILY, run, as_of=run + timedelta(hours=6, minutes=20)
    )


def test_aifs_run_covers_a_day_by_its_instants(monkeypatch) -> None:
    covers = follow_run._run_covers(
        follow_run.AIFS_FAMILY,
        event_date="2026-09-24", settlement_timezone="America/New_York",
        lead_days=1, run_init=_t("2026-09-23", 0),
    )
    assert covers
    # a pre-day run short of the four instants does not seal the day
    assert not follow_run._run_covers(
        follow_run.AIFS_FAMILY,
        event_date="2026-09-28", settlement_timezone="America/New_York",
        lead_days=5, run_init=_t("2026-09-23", 0),
    )
    assert follow_run.follow_serving_allowed(follow_run.AIFS_FAMILY, 6)
    monkeypatch.setenv(follow_run.AIFS_SERVING_ENV, "off")
    assert not follow_run.follow_serving_allowed(follow_run.AIFS_FAMILY, 6)


def test_a_same_day_aifs_run_in_a_longer_leads_bucket_is_banked_not_seated() -> None:
    # EGLC (BST, UTC+1): the local day 2026-09-24 starts 23Z on the 23rd.
    # D-1 12Z is 11 h before it; D 12Z is 13 h after it -- charter 192's
    # rule keeps the bucket's certificate on the earlier run.
    earlier = follow_run.scope_trained_by_longer_lead(
        follow_run.AIFS_FAMILY,
        event_date="2026-09-24", settlement_timezone="Europe/London",
        run_init=_t("2026-09-23", 12),
    )
    same_day = follow_run.scope_trained_by_longer_lead(
        follow_run.AIFS_FAMILY,
        event_date="2026-09-24", settlement_timezone="Europe/London",
        run_init=_t("2026-09-24", 12),
    )
    assert not earlier
    assert same_day == (
        follow_run.capture_lead_bucket(
            event_date="2026-09-24", settlement_timezone="Europe/London",
            run_init=_t("2026-09-23", 12),
        )
        == follow_run.capture_lead_bucket(
            event_date="2026-09-24", settlement_timezone="Europe/London",
            run_init=_t("2026-09-24", 12),
        )
    )


def test_choose_station_run_adds_the_newest_certified_aifs_run_without_a_base() -> None:
    runs = [(_t("2026-09-23", 12), "12z"), (_t("2026-09-23", 6), "06z"), (_t("2026-09-23", 0), "00z")]
    certified = {"06z", "00z"}
    chosen = follow_run.choose_station_run(
        base_run_init=None, candidates=runs, ready=lambda _run, key: key in certified
    )
    assert chosen == "06z"
    assert follow_run.choose_station_run(
        base_run_init=None, candidates=runs, ready=lambda _run, _key: False
    ) is None


def test_follow_capture_banks_aifs_runs_through_the_collector(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, int, int]] = []

    def fake_capture(**kw):
        calls.append(("aifs", int(kw["lead_days"]), int(kw["run_cycle_hour"])))
        run_init = _t("2026-09-24", 0) - timedelta(days=int(kw["lead_days"])) + timedelta(hours=int(kw["run_cycle_hour"]))
        body = {
            "schema": aifs.MULTISTATION_SCHEMA, "event_date": kw["event_date"],
            "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
            "captures": [{"station_id": s["station_id"]} for s in kw["station_scopes"]],
            "training_only": True, "holdout_eligible": False,
            "total_response_bytes": 1,
        }
        return {**body, "content_sha256": content_sha256(body)}

    def other_family_capture(**kw):
        raise AssertionError("only AIFS is due in this window")

    monkeypatch.setattr(capture, "capture_aifs_station_days", fake_capture)
    # AIGEFS joined this lane with charter 212, after this test; unlisted,
    # its real collector went to the network.
    for name in ("capture_icon_eps_station_days", "capture_ecmwf_station_days", "capture_geps_station_days",
                 "capture_aigefs_station_days"):
        monkeypatch.setattr(capture, name, other_family_capture)
    monkeypatch.setattr(capture, "load_market_registry_snapshot", lambda _p: {"rows": []})
    monkeypatch.setattr(capture, "build_station_scopes", lambda *_a, **_k: [
        {"station_id": "KLGA", "settlement_timezone": "America/New_York",
         "latitude": 40.8, "longitude": -73.9, "registry_rows": [{"x": 1}]},
        {"station_id": "NZWN", "settlement_timezone": "Pacific/Auckland",
         "latitude": -41.3, "longitude": 174.8, "registry_rows": [{"x": 1}]},
    ])
    monkeypatch.setattr(capture.nwp_arrival_probe, "load_state", lambda _p: None)
    monkeypatch.setattr(capture, "assert_process_memory_budget", lambda *a, **k: None)
    # ICON/ECCC follow their rungs (none given), ECMWF's D-3 18Z is not due
    # yet at this hour; AIFS 2026-09-23 06Z (lead 1) is 6h20m old -> due.
    registry = tmp_path / "registry.json"
    registry.write_text("{}")
    result = capture.capture_follow_runs(
        registry_path=registry,
        event_date="2026-09-24",
        rungs=[],
        capture_root=tmp_path,
        timeout_seconds=30.0,
        as_of_utc="2026-09-23T12:20:00Z",
    )
    captured = [o for o in result["outcomes"] if o["family"] == follow_run.AIFS_FAMILY and o["state"] == "CAPTURED"]
    assert calls  # the collector was asked
    assert {(o["lead_days"], o["run_init_utc"]) for o in captured} >= {
        (1, "2026-09-23T06:00:00Z"), (1, "2026-09-23T00:00:00Z"), (2, "2026-09-22T18:00:00Z"),
    }
    assert all(
        o["state"] in {"CAPTURED", "NOT_DUE", "NO_COVERABLE_STATION"}
        for o in result["outcomes"] if o["family"] == follow_run.AIFS_FAMILY
    )
    banked = [Path(p).name for p in result["archive_paths"]]
    assert "aifs_2026-09-24_d1_06z.json" in banked
    # a second job leaves the banked run alone
    again = capture.capture_follow_runs(
        registry_path=registry, event_date="2026-09-24", rungs=[],
        capture_root=tmp_path, timeout_seconds=30.0, as_of_utc="2026-09-23T12:25:00Z",
    )
    assert {o["state"] for o in again["outcomes"] if o["path"] == "aifs_2026-09-24_d1_06z.json"} == {"RETAINED"}


# --------------------------------------------------------------------------
# Root curve
# --------------------------------------------------------------------------
def _curve_points(times, values):
    return [{"valid_time_utc": t, "temperature_c": v} for t, v in zip(times, values)]


def test_root_curve_rule_text_pins_the_curve_to_curve_bearing_systems() -> None:
    # The block is exercised end to end by test_native_ensemble_p0's compile
    # fixtures; here the rule is pinned at the source: a curveless system is
    # tolerated only when registered, and the curve weights are renormalised
    # over the curve-bearing systems.
    import inspect

    src = inspect.getsource(p0.compile_native_ensemble_p0)
    assert "curveless_system_ids" in src
    assert "system_id in CURVELESS_SYSTEM_IDS" in src
    assert "for system_id in curve_system_ids" in src
    assert "curve_weight_total" in src
