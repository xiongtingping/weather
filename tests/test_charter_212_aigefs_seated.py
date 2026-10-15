"""Charter 212: NOAA AIGEFS seated and served -- the same holds as for AIFS ENS.

* the OOS contract admits AIGEFS rows under their own identities and the
  capture scheduler's readiness names no AIGEFS run;
* the follow lane follows every AIGEFS run per event date at its own
  publication, ADDED to the station's set; the arrival probe knows the family;
* AIGEFS is curveless in the P0 root curve like AIFS.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bayes_basket_target import follow_run
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target import native_ensemble_p0 as p0
from bayes_basket_target import noaa_aigefs_archive as ag
from bayes_basket_target import nwp_arrival_probe as probe
from bayes_basket_target import official_native_forward_packet as packet
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.official_nwp_capture_scheduler import contracted_run_inits
from scripts import capture_official_native_ensemble_forward as capture

UTC = timezone.utc


def _t(day: str, hour: int) -> datetime:
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, tzinfo=UTC)


def test_contract_admits_every_aigefs_cycle_and_lead_under_its_own_identity() -> None:
    contract = oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS["NOAA_AIGEFS_OPS_025"]
    assert contract["transport_provider_id"] == ag.TRANSPORT_PROVIDER_ID == "NOAA_GEFS_PDS_S3"
    for lead in (0, 1, 2):
        entries = contract["leads"][f"d{lead}"]
        assert [e["run_cycle"] for e in entries] == ["00Z", "06Z", "12Z", "18Z"]
        assert all(e["serving_follow"] is True for e in entries)
        for hour in (0, 6, 12, 18):
            assert oos._historical_transport_scope_allowed(
                system_id="NOAA_AIGEFS_OPS_025", transport_provider_id="NOAA_GEFS_PDS_S3",
                lead_bucket="h12_24", run_cycle=f"{hour:02d}Z",
                extraction_version=ag.extraction_version_for_lead(lead, hour),
            )
    # a research-era identity, and GEFS's own identity, never read as AIGEFS
    assert not oos._historical_transport_scope_allowed(
        system_id="NOAA_AIGEFS_OPS_025", transport_provider_id="NOAA_GEFS_PDS_S3",
        lead_bucket="h12_24", run_cycle="06Z",
        extraction_version="research_" + ag.extraction_version_for_lead(1, 6),
    )
    assert not oos._historical_transport_scope_allowed(
        system_id="NOAA_AIGEFS_OPS_025", transport_provider_id="NOAA_GEFS_PDS_S3",
        lead_bucket="h12_24", run_cycle="00Z",
        extraction_version="noaa_gefs_pds_0p50_00z_d1_station_tmax_v2",
    )


def test_scheduler_readiness_names_no_aigefs_run() -> None:
    for lead in (0, 1, 2):
        runs = contracted_run_inits("2026-09-25", lead_days=lead)
        assert "NOAA_AIGEFS_OPS_025" not in runs and "ECMWF_AIFS_ENS_GLOBAL" not in runs
        assert {"ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"} <= set(runs)


def test_string_constants_agree_with_the_collector() -> None:
    assert packet.NOAA_AIGEFS_MULTISTATION_SCHEMA == ag.MULTISTATION_SCHEMA
    assert packet.SUPPORTED_ARCHIVE_SCHEMAS[ag.MULTISTATION_SCHEMA] == ag.TRANSPORT_PROVIDER_ID
    assert p0.CURVELESS_SYSTEM_IDS == {"ECMWF_AIFS_ENS_GLOBAL", ag.SYSTEM_ID}
    assert follow_run.system_id(follow_run.AIGEFS_FAMILY) == ag.SYSTEM_ID
    assert follow_run.archive_schema(follow_run.AIGEFS_FAMILY) == ag.MULTISTATION_SCHEMA
    assert probe.AIGEFS_FAMILY == follow_run.AIGEFS_FAMILY
    assert probe.SPECS[probe.AIGEFS_FAMILY].system_id == ag.SYSTEM_ID
    assert content_sha256(ag.SYSTEM_IDENTITY) == (
        "bbef336b65a38c1e359d0e66c9f169140c9fbbb9cce9fe96ca7b0527ef1f0bf3"
    )


def test_every_aigefs_run_is_followed_per_event_date_at_its_own_publication(monkeypatch) -> None:
    assert follow_run.event_follow_runs(follow_run.AIGEFS_FAMILY) == tuple(
        (lead, hour) for lead in (0, 1, 2) for hour in (0, 6, 12, 18)
    )
    paths = follow_run.follow_archive_paths(Path("/x"), follow_run.AIGEFS_FAMILY, "2026-09-25")
    assert len(paths) == 12 and paths[-1][1].name == "aigefsops_2026-09-25_d2_00z.json"
    for lead in (0, 1, 2):
        assert follow_run.follow_cycle_for_rung(follow_run.AIGEFS_FAMILY, lead, 12) is None
    run = _t("2026-09-24", 6)
    assert not follow_run.follow_publication_due(
        follow_run.AIGEFS_FAMILY, run, as_of=run + timedelta(hours=4, minutes=5)
    )
    # Charter 270: NOMADS runs are complete by ~init + 4h05m.
    assert follow_run.follow_publication_due(
        follow_run.AIGEFS_FAMILY, run, as_of=run + timedelta(hours=4, minutes=20)
    )
    assert follow_run._run_covers(
        follow_run.AIGEFS_FAMILY, event_date="2026-09-25",
        settlement_timezone="America/New_York", lead_days=1, run_init=_t("2026-09-24", 0),
    )
    assert follow_run.follow_serving_allowed(follow_run.AIGEFS_FAMILY, 6)
    monkeypatch.setenv(follow_run.AIGEFS_SERVING_ENV, "off")
    assert not follow_run.follow_serving_allowed(follow_run.AIGEFS_FAMILY, 6)
    # the AIFS switch does not reach AIGEFS and vice versa
    assert follow_run.follow_serving_allowed(follow_run.AIFS_FAMILY, 6)


def test_probe_knows_the_family_and_its_objects() -> None:
    assert probe.needed_max_step(
        probe.AIGEFS_FAMILY, run_init=_t("2026-09-24", 6), event_date="2026-09-25",
        station_timezones={"KLGA": "America/New_York", "NZWN": "Pacific/Auckland"},
    ) == 42
    grib, index = probe.aigefs_object_urls(_t("2026-09-24", 6), 30, 42)
    assert grib.endswith("/com/aigefs/prod/aigefs.20260924/06/mem030/model/atmos/grib2/aigefs.t06z.sfc.f042.grib2")
    assert index == grib + ".idx"
    runs = probe.candidate_runs(probe.AIGEFS_FAMILY, _t("2026-09-24", 13))
    assert _t("2026-09-24", 6) in runs and _t("2026-09-24", 12) not in runs


def test_follow_capture_banks_aigefs_runs_through_the_collector(monkeypatch, tmp_path) -> None:
    def fake_capture(**kw):
        run_init = _t("2026-09-25", 0) - timedelta(days=int(kw["lead_days"])) + timedelta(hours=int(kw["run_cycle_hour"]))
        body = {
            "schema": ag.MULTISTATION_SCHEMA, "event_date": kw["event_date"],
            "run_init_utc": run_init.isoformat().replace("+00:00", "Z"),
            "captures": [{"station_id": s["station_id"]} for s in kw["station_scopes"]],
            "training_only": True, "holdout_eligible": False, "total_response_bytes": 1,
        }
        return {**body, "content_sha256": content_sha256(body)}

    def other(**kw):
        raise AssertionError("only AIGEFS is due in this window")

    monkeypatch.setattr(capture, "capture_aigefs_station_days", fake_capture)
    for name in ("capture_icon_eps_station_days", "capture_ecmwf_station_days",
                 "capture_geps_station_days", "capture_aifs_station_days"):
        monkeypatch.setattr(capture, name, other)
    monkeypatch.setattr(capture, "load_market_registry_snapshot", lambda _p: {"rows": []})
    monkeypatch.setattr(capture, "build_station_scopes", lambda *_a, **_k: [
        {"station_id": "KLGA", "settlement_timezone": "America/New_York",
         "latitude": 40.8, "longitude": -73.9, "registry_rows": [{"x": 1}]},
    ])
    monkeypatch.setattr(capture.nwp_arrival_probe, "load_state", lambda _p: None)
    monkeypatch.setattr(capture, "assert_process_memory_budget", lambda *a, **k: None)
    registry = tmp_path / "registry.json"
    registry.write_text("{}")
    # 2026-09-24 06Z is 7h05m old (AIGEFS due at 7h); the AIFS 06Z run at
    # 7h05m is due too -- so AIFS must already be on disk for this window.
    for lead, hour in follow_run.event_follow_runs(follow_run.AIFS_FAMILY):
        (tmp_path / f"aifs_2026-09-25_d{lead}_{hour:02d}z.json").write_text("{}")
    monkeypatch.setattr(follow_run, "load_follow_archive",
                        lambda path, **kw: {"captures": [{"station_id": "KLGA"}]} if path.name.startswith("aifs_") else None)
    result = capture.capture_follow_runs(
        registry_path=registry, event_date="2026-09-25", rungs=[],
        capture_root=tmp_path, timeout_seconds=30.0, as_of_utc="2026-09-24T13:05:00Z",
    )
    captured = {(o["lead_days"], o["run_init_utc"]) for o in result["outcomes"]
                if o["family"] == follow_run.AIGEFS_FAMILY and o["state"] == "CAPTURED"}
    assert (1, "2026-09-24T06:00:00Z") in captured and (2, "2026-09-23T18:00:00Z") in captured
    assert "aigefsops_2026-09-25_d1_06z.json" in [Path(p).name for p in result["archive_paths"]]


def test_root_curve_rule_lists_aigefs_as_curveless() -> None:
    import inspect

    src = inspect.getsource(p0.compile_native_ensemble_p0)
    assert "system_id in CURVELESS_SYSTEM_IDS" in src
    assert "NOAA_AIGEFS_OPS_025" in p0.CURVELESS_SYSTEM_IDS
