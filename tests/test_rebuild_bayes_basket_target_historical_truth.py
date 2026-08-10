from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from bayes_basket_target import ContractError
from scripts import rebuild_bayes_basket_target_historical_truth as rebuild
from bayes_basket_target.source_policy import (
    AWC_METAR_TIMING_INVALID,
    reference_only_reason,
    source_policy_reason,
)


def _write_timezone_registry(
    path: Path,
    rows: list[dict[str, str]],
) -> None:
    path.write_text(
        json.dumps({"rows": rows}, sort_keys=True),
        encoding="utf-8",
    )


def test_historical_truth_uses_station_invariant_registry_timezone(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry.json"
    _write_timezone_registry(
        registry,
        [
            {
                "station_id": station,
                "event_date": "2026-07-28",
                "settlement_timezone": "Asia/Shanghai",
            }
            for station in ("ZBAA", "ZUUU")
        ],
    )

    assert rebuild._registry_timezone(
        registry,
        station_ids=["ZBAA", "ZUUU"],
        event_dates=["2026-07-15", "2026-07-16"],
    ) == "Asia/Shanghai"


def test_historical_truth_rejects_mixed_registry_timezones(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry.json"
    _write_timezone_registry(
        registry,
        [
            {
                "station_id": "ZBAA",
                "event_date": "2026-07-28",
                "settlement_timezone": "Asia/Shanghai",
            },
            {
                "station_id": "RCSS",
                "event_date": "2026-07-28",
                "settlement_timezone": "Asia/Taipei",
            },
        ],
    )

    with pytest.raises(
        ContractError,
        match="market_registry_mixed_settlement_timezones_forbidden",
    ):
        rebuild._registry_timezone(
            registry,
            station_ids=["ZBAA", "RCSS"],
            event_dates=["2026-07-15"],
        )


def test_awc_doh_parser_rejects_synthetic_and_private_addresses():
    payload = {
        "Answer": [
            {"name": "aviationweather.gov", "type": 1, "data": "198.18.0.15"},
            {"name": "aviationweather.gov", "type": 1, "data": "10.0.0.1"},
            {"name": "aviationweather.gov", "type": 1, "data": "13.107.253.71"},
        ]
    }

    assert rebuild._parse_doh_ipv4_addresses(payload) == ("13.107.253.71",)


def test_awc_curl_config_pins_resolved_address_but_keeps_official_host(monkeypatch):
    captured: dict[str, str] = {}

    class Result:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(command, *, input, **kwargs):
        captured["config"] = input
        return Result()

    monkeypatch.setattr(rebuild.subprocess, "run", fake_run)
    monkeypatch.setattr(
        rebuild,
        "_AWC_CONNECT_TO",
        ("aviationweather.gov:443:13.107.253.71:443",),
    )

    assert rebuild._curl_json(
        "https://aviationweather.gov/api/data/metar?ids=KATL",
        timeout=5,
    ) == []
    assert 'url = "https://aviationweather.gov/api/data/metar?ids=KATL"' in captured["config"]
    assert 'connect-to = "aviationweather.gov:443:13.107.253.71:443"' in captured["config"]


def test_awc_doh_resolution_also_runs_without_research_proxy(monkeypatch):
    captured: dict[str, str] = {}

    class Result:
        returncode = 0
        stdout = json.dumps({
            "Answer": [
                {"name": "aviationweather.gov", "type": 1, "data": "150.171.109.65"},
            ]
        })
        stderr = ""

    def fake_run(command, *, input, **kwargs):
        captured["config"] = input
        return Result()

    monkeypatch.setattr(rebuild.subprocess, "run", fake_run)

    assert rebuild._resolve_awc_connect_to("", timeout_sec=5) == (
        "aviationweather.gov:443:150.171.109.65:443",
    )
    assert 'noproxy = "*"' in captured["config"]
    assert '\nproxy = ' not in captured["config"]


def test_amsc_metar_timing_and_speci_policy_and_awc_hourly_reference():
    from datetime import datetime, timezone

    hourly = datetime(2026, 7, 17, 17, 0, tzinfo=timezone.utc)
    half_hour = datetime(2026, 7, 17, 16, 30, tzinfo=timezone.utc)
    off_schedule = datetime(2026, 7, 17, 16, 15, tzinfo=timezone.utc)

    assert reference_only_reason(
        source_lane="amsc", report_type="METAR", valid_at=hourly, expected_lane="amsc"
    ) == ""
    assert reference_only_reason(
        source_lane="amsc", report_type="METAR", valid_at=half_hour, expected_lane="amsc"
    ) == "amsc_half_hour_reference_only"
    assert source_policy_reason(
        source_lane="amsc", report_type="METAR", valid_at=off_schedule, expected_lane="amsc"
    ) == "amsc_metar_timing_invalid"
    assert reference_only_reason(
        source_lane="amsc", report_type="SPECI", valid_at=off_schedule, expected_lane="amsc"
    ) == ""
    assert reference_only_reason(
        source_lane="awc", report_type="SPECI", valid_at=hourly, expected_lane="amsc"
    ) == "awc_hourly_reference_only_for_amsc_station"


def test_awc_owned_metar_and_speci_are_formal_at_their_official_minute():
    from datetime import datetime, timezone

    off_hour = datetime(2026, 7, 17, 16, 30, tzinfo=timezone.utc)

    # Provider cadences range from :00 through :20/:50 to :51-:58; the AWC
    # lane admits routine METARs at the station's own official minute.
    assert source_policy_reason(
        source_lane="awc", report_type="METAR", valid_at=off_hour, expected_lane="awc"
    ) == ""
    assert source_policy_reason(
        source_lane="awc", report_type="SPECI", valid_at=off_hour, expected_lane="awc"
    ) == ""


def test_existing_mixed_research_archive_loads_only_explicit_amsc_objects(tmp_path, monkeypatch):
    objects = tmp_path / "data/research/bayes_basket_target/historical_truth_raw/objects/aa"
    objects.mkdir(parents=True)
    rows = [
        {
            "station_id": "ZBAA",
            "source_lane": "amsc",
            "report_type": "METAR",
            "valid_at_utc": "2026-07-14T16:00:00Z",
            "reported_temperature_c": 25.0,
            "raw_sha256": "a" * 64,
            "raw_text": "METAR ZBAA 141600Z 00000MPS CAVOK 25/20 Q1000=",
            "source_poll_id": "amsc-history",
            "first_seen_at_utc": "2026-07-24T00:00:00Z",
        },
        {
            "station_id": "ZBAA",
            "source_lane": "awc",
            "report_type": "METAR",
            "valid_at_utc": "2026-07-14T16:00:00Z",
            "reported_temperature_c": 99.0,
            "raw_sha256": "b" * 64,
            "raw_text": "METAR ZBAA 141600Z 00000MPS CAVOK 99/20 Q1000=",
            "source_poll_id": "awc-reference",
            "first_seen_at_utc": "2026-07-24T00:00:00Z",
        },
    ]
    for index, row in enumerate(rows):
        with gzip.open(objects / f"{index:02d}.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(row, handle)
    monkeypatch.setattr(rebuild, "ROOT", tmp_path)

    formal, reference = rebuild._load_existing_amsc(
        {"ZBAA"},
        "2026-07-14",
        "2026-07-14",
        target_timezone="America/New_York",
    )

    assert [row["raw_sha256"] for row in formal] == ["a" * 64]
    assert reference == []


def test_existing_awc_archive_preserves_provider_precision(tmp_path, monkeypatch):
    objects = tmp_path / "data/research/bayes_basket_target/historical_truth_raw/objects/aa"
    objects.mkdir(parents=True)
    row = {
        "station_id": "KATL",
        "source_lane": "awc",
        "report_type": "METAR",
        "valid_at_utc": "2026-07-24T17:00:00Z",
        "reported_temperature_c": 31.1,
        "raw_sha256": "c" * 64,
        "raw_text": "METAR KATL 241700Z 23008G16KT 10SM SCT028TCU 31/22 A3011 RMK AO2 T03110222=",
        "source_poll_id": "awc-history",
        "first_seen_at_utc": "2026-07-25T00:00:00Z",
    }
    with gzip.open(objects / "row.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(row, handle)
    monkeypatch.setattr(rebuild, "ROOT", tmp_path)

    formal = rebuild._load_existing_awc(
        {"KATL"},
        "2026-07-24",
        "2026-07-24",
        target_timezone="America/New_York",
    )

    assert len(formal) == 1
    assert formal[0]["reported_temperature_c"] == 31.1


def test_existing_awc_archive_filters_non_hourly_metar_but_keeps_speci(tmp_path, monkeypatch):
    objects = tmp_path / "data/research/bayes_basket_target/historical_truth_raw/objects/aa"
    objects.mkdir(parents=True)
    rows = [
        {
            "station_id": "KATL",
            "source_lane": "awc",
            "report_type": "METAR",
            "valid_at_utc": "2026-07-24T17:30:00Z",
            "reported_temperature_c": 31.0,
            "raw_sha256": "d" * 64,
            "raw_text": "METAR KATL 241730Z 23008KT 10SM SCT028 31/22 A3011=",
            "source_poll_id": "awc-history",
            "first_seen_at_utc": "2026-07-25T00:00:00Z",
        },
        {
            "station_id": "KATL",
            "source_lane": "awc",
            "report_type": "SPECI",
            "valid_at_utc": "2026-07-24T17:31:00Z",
            "reported_temperature_c": 32.0,
            "raw_sha256": "e" * 64,
            "raw_text": "SPECI KATL 241731Z 23008KT 10SM SCT028 32/22 A3011=",
            "source_poll_id": "awc-history",
            "first_seen_at_utc": "2026-07-25T00:00:00Z",
        },
    ]
    for index, row in enumerate(rows):
        with gzip.open(objects / f"row-{index}.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(row, handle)
    monkeypatch.setattr(rebuild, "ROOT", tmp_path)

    formal = rebuild._load_existing_awc(
        {"KATL"},
        "2026-07-24",
        "2026-07-24",
        target_timezone="America/New_York",
    )

    assert [(row["report_type"], row["valid_at_utc"]) for row in formal] == [
        ("SPECI", "2026-07-24T17:31:00Z"),
    ]


def test_amsc_fetch_queries_complete_station_days_then_filters_exact_et_window(monkeypatch):
    seen: list[dict[str, list[str]]] = []

    def fake_fetch(*, url: str, token: str, timeout: int):
        query = parse_qs(urlparse(url).query)
        seen.append(query)
        if query["starDate"][0] == "2026-07-16 16:00:00":
            data = [
                {
                    "oDateTime": "2026-07-17 16:00:00",
                    "content": "METAR ZBAA 171600Z 00000MPS CAVOK 25/20 Q1000=",
                },
                {
                    "oDateTime": "2026-07-17 16:30:00",
                    "content": "METAR ZBAA 171630Z 00000MPS CAVOK 26/20 Q1000=",
                },
            ]
        else:
            data = [
                {
                    "oDateTime": "2026-07-18 00:00:00",
                    "content": "METAR ZBAA 180000Z 00000MPS CAVOK 26/20 Q1000=",
                },
                {
                    "oDateTime": "2026-07-18 04:00:00",
                    "content": "METAR ZBAA 180400Z 00000MPS CAVOK 27/20 Q1000=",
                },
            ]
        return {
            "code": 200,
            "msg": "操作成功",
            "data": data,
        }

    monkeypatch.setattr(rebuild, "_curl_tls_verified_payload", fake_fetch)
    rows, reference_rows, receipt = rebuild._amsc_fetch(
        "ZBAA",
        "2026-07-17",
        "ephemeral-token",
        "2026-07-24T00:00:00Z",
        target_timezone="America/New_York",
    )

    assert len(seen) == 2
    assert [query["starDate"][0] for query in seen] == [
        "2026-07-16 16:00:00",
        "2026-07-17 16:00:00",
    ]
    assert [query["endDate"][0] for query in seen] == [
        "2026-07-17 15:59:59",
        "2026-07-18 15:59:59",
    ]
    assert [row["valid_at_utc"] for row in rows] == [
        "2026-07-17T16:00:00Z",
        "2026-07-18T00:00:00Z",
    ]
    assert all(row["source_lane"] == "amsc" and row["valid_at_utc"].endswith(":00:00Z") for row in rows)
    assert [row["valid_at_utc"] for row in reference_rows] == ["2026-07-17T16:30:00Z"]
    assert reference_rows[0]["evidence_role"] == "REFERENCE_ONLY"
    assert reference_rows[0]["reference_reason"] == "amsc_half_hour_reference_only"
    assert receipt["query_physical_station_days"] == ["2026-07-17", "2026-07-18"]
    assert receipt["response_data_count"] == 4
    assert receipt["response_code"] == 200


def test_awc_reference_only_keeps_hourly_metar_out_of_formal_rows(monkeypatch):
    payload = [
        {
            "icaoId": "ZBAA",
            "temp": 25,
            "obsTime": 1784304000,
            "rawOb": "METAR ZBAA 171600Z 00000MPS CAVOK 25/20 Q1000=",
            "metarType": "METAR",
        },
        {
            "icaoId": "ZBAA",
            "temp": 26,
            "obsTime": 1784305800,
            "rawOb": "METAR ZBAA 171630Z 00000MPS CAVOK 26/20 Q1000=",
            "metarType": "METAR",
        },
    ]
    monkeypatch.setattr(rebuild, "_curl_json", lambda url, timeout: payload)
    monkeypatch.setattr(rebuild.time, "sleep", lambda seconds: None)

    rows, receipt = rebuild._awc_fetch(
        ["ZBAA"],
        ["2026-07-17"],
        "2026-07-24T00:00:00Z",
        reference_only=True,
        target_timezone="America/New_York",
    )

    assert [row["valid_at_utc"] for row in rows] == ["2026-07-17T16:00:00Z"]
    assert rows[0]["evidence_role"] == "REFERENCE_ONLY"
    assert rows[0]["reference_reason"] == "awc_hourly_reference_only_for_amsc_station"
    assert receipt["reference_only"] is True


def test_awc_fetch_discards_non_hourly_metar_but_keeps_effective_speci(monkeypatch):
    payload = [
        {
            "icaoId": "RCSS",
            "temp": 25,
            "obsTime": 1784304000,
            "rawOb": "METAR RCSS 171600Z 00000KT 9999 FEW020 25/20 Q1000=",
            "metarType": "METAR",
        },
        {
            "icaoId": "RCSS",
            "temp": 26,
            "obsTime": 1784305800,
            "rawOb": "METAR RCSS 171630Z 00000KT 9999 FEW020 26/20 Q1000=",
            "metarType": "METAR",
        },
        {
            "icaoId": "RCSS",
            "temp": 27,
            "obsTime": 1784305860,
            "rawOb": "SPECI RCSS 171631Z 00000KT 9999 FEW020 27/20 Q1000=",
            "metarType": "SPECI",
        },
    ]
    monkeypatch.setattr(rebuild, "_curl_json", lambda url, timeout: payload)
    monkeypatch.setattr(rebuild.time, "sleep", lambda seconds: None)

    rows, receipt = rebuild._awc_fetch(
        ["RCSS"],
        ["2026-07-17"],
        "2026-07-24T00:00:00Z",
        target_timezone="America/New_York",
    )

    assert [(row["report_type"], row["valid_at_utc"]) for row in rows] == [
        ("METAR", "2026-07-17T16:00:00Z"),
        ("SPECI", "2026-07-17T16:31:00Z"),
    ]
    assert receipt["returned"] == 2


def test_reference_only_cli_success_depends_on_reference_rows() -> None:
    assert rebuild._result_exit_code(
        {"observation_count": 0, "reference_observation_count": 576},
        reference_only=True,
    ) == 0
    assert rebuild._result_exit_code(
        {"observation_count": 12, "reference_observation_count": 0},
        reference_only=True,
    ) == 2
    assert rebuild._result_exit_code(
        {"observation_count": 12, "reference_observation_count": 0},
        reference_only=False,
    ) == 0


def test_rebuild_writes_reference_lane_separately_from_formal_observations(tmp_path, monkeypatch):
    lane_map = tmp_path / "lane_map.json"
    lane_map.write_text(json.dumps({"stations": {"ZBAA": "amsc"}}), encoding="utf-8")
    formal = {
        "station_id": "ZBAA",
        "source_lane": "amsc",
        "report_type": "METAR",
        "valid_at_utc": "2026-07-17T16:00:00Z",
        "reported_temperature_c": 25.0,
        "raw_sha256": "a" * 64,
        "source_poll_id": "amsc-formal",
        "first_seen_at_utc": "2026-07-24T00:00:00Z",
        "canonical_committed_at_utc": "2026-07-24T00:00:00Z",
    }
    half_hour = {
        **formal,
        "valid_at_utc": "2026-07-17T16:30:00Z",
        "reported_temperature_c": 26.0,
        "raw_sha256": "b" * 64,
        "source_poll_id": "amsc-half-hour",
        "evidence_role": "REFERENCE_ONLY",
        "reference_reason": "amsc_half_hour_reference_only",
    }
    awc_hourly = {
        **formal,
        "source_lane": "awc",
        "valid_at_utc": "2026-07-17T17:00:00Z",
        "reported_temperature_c": 27.0,
        "raw_sha256": "c" * 64,
        "source_poll_id": "awc-hourly",
    }
    monkeypatch.setattr(
        rebuild,
        "_load_existing_amsc",
        lambda stations, start, end, *, target_timezone: ([formal], [half_hour]),
    )
    monkeypatch.setattr(
        rebuild,
        "_load_existing_awc",
        lambda stations, start, end, *, reference_only=False, target_timezone: [],
    )

    def fake_awc_fetch(
        stations,
        event_dates,
        fetched_at,
        *,
        reference_only=False,
        target_timezone,
    ):
        assert stations == ["ZBAA"]
        assert event_dates == ["2026-07-17"]
        assert reference_only is True
        assert target_timezone == "America/New_York"
        return [{**awc_hourly, "evidence_role": "REFERENCE_ONLY", "reference_reason": "awc_hourly_reference_only_for_amsc_station"}], {
            "query_count": 1,
            "queries": [],
            "query_blockers": [],
            "returned": 1,
            "reference_only": True,
        }

    monkeypatch.setattr(rebuild, "_awc_fetch", fake_awc_fetch)
    output = tmp_path / "receipt.json"
    observations = tmp_path / "observations.json"
    references = tmp_path / "references.json"
    result = rebuild.rebuild(
        lane_map_path=lane_map,
        event_dates=["2026-07-17"],
        output_path=output,
        observations_path=observations,
            reference_output_path=references,
            raw_root=tmp_path / "raw",
            target_timezone="America/New_York",
        fetch_amsc=False,
        fetch_awc=True,
        fetch_formal_awc=False,
        awc_reference_stations=["ZBAA"],
    )

    formal_payload = json.loads(observations.read_text(encoding="utf-8"))
    reference_payload = json.loads(references.read_text(encoding="utf-8"))
    assert [row["raw_sha256"] for row in formal_payload["observations"]] == ["a" * 64]
    assert {row["raw_sha256"] for row in reference_payload["reference_observations"]} == {"b" * 64, "c" * 64}
    assert all(row["evidence_role"] == "REFERENCE_ONLY" for row in reference_payload["reference_observations"])
    assert result["reference_observation_count"] == 2
    assert result["source_counts"] == {"amsc": 1, "awc": 0}


def test_fresh_official_awc_raw_only_never_reads_retained_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane_map = tmp_path / "lane_map.json"
    lane_map.write_text(
        json.dumps({"stations": {"RCSS": "awc"}}), encoding="utf-8"
    )
    fetched = {
        "station_id": "RCSS",
        "source_lane": "awc",
        "report_type": "METAR",
        "valid_at_utc": "2026-07-22T15:00:00Z",
        "reported_temperature_c": 31.0,
        "raw_sha256": "d" * 64,
        "raw_text": "METAR RCSS 221500Z 00000KT 9999 FEW020 31/24 Q1008=",
        "source_poll_id": "official-awc-response",
        "first_seen_at_utc": "2026-08-01T00:00:00Z",
        "canonical_committed_at_utc": "2026-08-01T00:00:00Z",
        "source_url": "https://aviationweather.gov/api/data/metar",
        "historical_data_class": "H",
    }

    def unexpected_retained_read(*_args, **_kwargs):
        raise AssertionError("retained_raw_must_not_be_read")

    def fetch_awc(stations, event_dates, fetched_at, *, reference_only=False, target_timezone):
        assert stations == ["RCSS"]
        assert event_dates == ["2026-07-22"]
        assert reference_only is False
        assert target_timezone == "Asia/Taipei"
        return [fetched], {
            "query_count": 1,
            "queries": [{"source_url": fetched["source_url"]}],
            "query_blockers": [],
            "returned": 1,
            "reference_only": False,
        }

    monkeypatch.setattr(rebuild, "_load_existing_amsc", unexpected_retained_read)
    monkeypatch.setattr(rebuild, "_load_existing_awc", unexpected_retained_read)
    monkeypatch.setattr(rebuild, "_awc_fetch", fetch_awc)
    output = tmp_path / "receipt.json"
    observations = tmp_path / "observations.json"
    references = tmp_path / "references.json"
    result = rebuild.rebuild(
        lane_map_path=lane_map,
        event_dates=["2026-07-22"],
        output_path=output,
        observations_path=observations,
        reference_output_path=references,
        raw_root=tmp_path / "raw",
        target_timezone="Asia/Taipei",
        fetch_amsc=False,
        fetch_awc=True,
        fetch_formal_awc=True,
        station_ids=["RCSS"],
        reuse_existing_raw=False,
    )
    assert result["source_counts"] == {"amsc": 0, "awc": 1}
    assert result["boundary"]["uses_existing_retained_raw"] is False
    assert result["boundary"]["fresh_official_awc_raw_only"] is True
    assert json.loads(observations.read_text(encoding="utf-8"))["observation_count"] == 1
