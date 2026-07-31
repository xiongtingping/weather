from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target import ContractError
from bayes_basket_target.event_contracts import validate_forecast_revision_chain
from scripts import capture_bayes_basket_target_awc_taf_revision as capture_mod


def test_forward_awc_taf_uses_local_receipt_and_bounded_physical_fields(
    monkeypatch,
) -> None:
    start = int(datetime(2026, 7, 26, 21, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 7, 27, 6, tzinfo=timezone.utc).timestamp())
    rows = [{
        "icaoId": "RKPK",
        "issueTime": "2026-07-26T11:00:00Z",
        "dbPopTime": "2026-07-26T11:05:00Z",
        "validTimeFrom": "2026-07-26T11:00:00Z",
        "validTimeTo": "2026-07-27T18:00:00Z",
        "rawTAF": "TAF RKPK 261100Z 2612/2718 TX35/2706Z",
        "fcsts": [{
            "timeFrom": start,
            "timeTo": end,
            "wspd": 8,
            "probability": 30,
            "wxString": "-RA",
            "clouds": [{"cover": "BKN", "base": 3000}],
        }],
    }]
    monkeypatch.setattr(
        capture_mod,
        "_fetch_json",
        lambda _url, *, timeout_sec: (rows, "a" * 64, 512),
    )
    capture = capture_mod.capture_forward_awc_taf(
        station_id="RKPK",
        event_date="2026-07-27",
        settlement_timezone="Asia/Seoul",
    )
    revision = capture["forecast_revision"]
    assert capture["source_class"] == "official_forward_native_receipt"
    assert capture["boundary"]["native_local_receipt"] is True
    assert revision["received_at_utc"] == capture["local_received_at_utc"]
    assert capture["provider_available_at_utc"] == "2026-07-26T11:05:00Z"
    assert capture["taf_fields"]["tx_c"] == 35.0
    assert capture["taf_fields"]["cloud_cover_fraction"] == 0.75


def test_forward_awc_taf_preserves_native_receipt_without_optional_tx(
    monkeypatch,
) -> None:
    start = int(
        datetime(2026, 7, 30, 4, tzinfo=timezone.utc).timestamp()
    )
    end = int(
        datetime(2026, 7, 30, 19, tzinfo=timezone.utc).timestamp()
    )
    rows = [{
        "icaoId": "EDDM",
        "issueTime": "2026-07-30T05:00:00Z",
        "dbPopTime": "2026-07-30T05:03:00Z",
        "validTimeFrom": "2026-07-30T05:00:00Z",
        "validTimeTo": "2026-07-31T06:00:00Z",
        "rawTAF": "TAF EDDM 300500Z 3006/3106 24008KT BKN030",
        "fcsts": [{
            "timeFrom": start,
            "timeTo": end,
            "wspd": 8,
            "wxString": "",
            "clouds": [{"cover": "BKN", "base": 3000}],
        }],
    }]
    monkeypatch.setattr(
        capture_mod,
        "_fetch_json",
        lambda _url, *, timeout_sec: (rows, "a" * 64, 512),
    )

    capture = capture_mod.capture_forward_awc_taf(
        station_id="EDDM",
        event_date="2026-07-30",
        settlement_timezone="Europe/Berlin",
    )

    assert capture["taf_fields"]["tx_c"] is None
    assert capture["taf_fields"]["cloud_cover_fraction"] == 0.75
    assert capture["taf_fields"]["wind_speed_mps"] > 0
    assert capture["boundary"]["native_local_receipt"] is True


def test_forward_awc_taf_rejects_stale_provider_timestamp(
    monkeypatch,
) -> None:
    start = int(
        datetime(2026, 7, 31, 0, tzinfo=timezone.utc).timestamp()
    )
    end = int(
        datetime(2026, 7, 31, 18, tzinfo=timezone.utc).timestamp()
    )
    rows = [{
        "icaoId": "SBGR",
        "issueTime": "2026-07-30T22:00:00Z",
        "dbPopTime": "2026-07-30T21:23:14Z",
        "validTimeFrom": "2026-07-31T00:00:00Z",
        "validTimeTo": "2026-08-01T06:00:00Z",
        "rawTAF": "TAF SBGR 302200Z 3100/0106 13007KT 9999 BKN011",
        "fcsts": [{
            "timeFrom": start,
            "timeTo": end,
            "wspd": 7,
            "wxString": "",
            "clouds": [{"cover": "BKN", "base": 1100}],
        }],
    }]
    monkeypatch.setattr(
        capture_mod,
        "_fetch_json",
        lambda _url, *, timeout_sec: (rows, "a" * 64, 512),
    )

    with pytest.raises(
        ContractError,
        match="awc_forward_taf_provider_timestamp_order_invalid",
    ):
        capture_mod.capture_forward_awc_taf(
            station_id="SBGR",
            event_date="2026-07-31",
            settlement_timezone="America/Sao_Paulo",
        )


def test_forward_taf_inbox_deduplicates_and_builds_linear_revisions(
    tmp_path,
    monkeypatch,
) -> None:
    first = {
        "schema": capture_mod.FORWARD_SCHEMA,
        "source_class": "official_forward_native_receipt",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "local_received_at_utc": "2026-07-26T11:05:00Z",
        "raw_taf": "TAF RKPK 261100Z 2612/2718 TX35/2706Z",
        "forecast_revision": {
            "schema": "bayes_basket_forecast_revision_v1",
            "revision_id": "temporary-1",
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "revision_kind": "TAF",
            "sequence": 1,
            "issued_at_utc": "2026-07-26T11:00:00Z",
            "received_at_utc": "2026-07-26T11:05:00Z",
            "valid_from_utc": "2026-07-26T11:00:00Z",
            "valid_until_utc": "2026-07-27T18:00:00Z",
            "source_object_sha256": "a" * 64,
            "supersedes_revision_id": None,
        },
        "taf_fields": {
            "tx_c": 35.0,
            "cloud_cover_fraction": 0.2,
            "precipitation_probability": 0.0,
            "wind_speed_mps": 3.0,
        },
        "boundary": {
            "native_local_receipt": True,
            "retrospective_replay_only": False,
        },
    }
    first["content_sha256"] = content_sha256(first)
    second = deepcopy(first)
    second["local_received_at_utc"] = "2026-07-26T17:05:00Z"
    second["raw_taf"] = "TAF RKPK 261700Z 2618/2718 TX36/2706Z"
    second["forecast_revision"]["issued_at_utc"] = "2026-07-26T17:00:00Z"
    second["forecast_revision"]["received_at_utc"] = "2026-07-26T17:05:00Z"
    second["forecast_revision"]["source_object_sha256"] = "b" * 64
    second["content_sha256"] = content_sha256({
        key: value for key, value in second.items()
        if key != "content_sha256"
    })
    inbox = tmp_path / "taf-inbox.json"
    capture_mod.refresh_forward_taf_inbox(
        captures=[first, first, second],
        inbox_path=inbox,
    )
    payload = __import__("json").loads(inbox.read_text(encoding="utf-8"))
    assert payload["capture_count"] == 2
    chain = validate_forecast_revision_chain(
        item["forecast_revision"] for item in payload["captures"]
    )
    assert [item.sequence for item in chain] == [1, 2]
    assert chain[1].supersedes_revision_id == chain[0].revision_id


def test_forward_taf_inbox_repairs_first_observed_cor_as_local_chain_root(
    tmp_path,
) -> None:
    capture = {
        "schema": capture_mod.FORWARD_SCHEMA,
        "source_class": "official_forward_native_receipt",
        "station_id": "EDDM",
        "event_date": "2026-07-31",
        "settlement_timezone": "Europe/Berlin",
        "local_received_at_utc": "2026-07-30T18:49:41Z",
        "raw_taf": "TAF COR EDDM 301703Z 3018/3124 24008KT",
        "raw_taf_sha256": "a" * 64,
        "forecast_revision": {
            "schema": "bayes_basket_forecast_revision_v1",
            "revision_id": "malformed-upstream-cor-root",
            "station_id": "EDDM",
            "event_date": "2026-07-31",
            "revision_kind": "COR",
            "sequence": 1,
            "issued_at_utc": "2026-07-30T17:03:00Z",
            "received_at_utc": "2026-07-30T18:49:41Z",
            "valid_from_utc": "2026-07-30T18:00:00Z",
            "valid_until_utc": "2026-08-01T00:00:00Z",
            "source_object_sha256": "a" * 64,
            "supersedes_revision_id": None,
        },
        "taf_fields": {
            "tx_c": None,
            "cloud_cover_fraction": 0.5,
            "precipitation_probability": 0.0,
            "wind_speed_mps": 4.0,
        },
        "boundary": {
            "native_local_receipt": True,
            "retrospective_replay_only": False,
        },
    }
    capture["content_sha256"] = content_sha256(capture)
    inbox_path = tmp_path / "taf-inbox.json"
    initial = {
        "schema": capture_mod.INBOX_SCHEMA,
        "capture_count": 1,
        "captures": [capture],
        "boundary": {
            "forward_only": True,
            "max_captures": 256,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    initial["content_sha256"] = content_sha256(initial)
    inbox_path.write_text(
        __import__("json").dumps(initial),
        encoding="utf-8",
    )

    repaired = capture_mod.refresh_forward_taf_inbox(
        captures=[],
        inbox_path=inbox_path,
    )
    row = repaired["captures"][0]
    revision = row["forecast_revision"]

    assert revision["revision_kind"] == "TAF"
    assert revision["sequence"] == 1
    assert revision["supersedes_revision_id"] is None
    assert row["native_revision_kind"] == "COR"
    assert (
        validate_forecast_revision_chain([revision])[0].revision_kind
        == "TAF"
    )
