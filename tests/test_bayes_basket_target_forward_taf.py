from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from bayes_basket_target.contracts import content_sha256
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
