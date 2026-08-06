from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.republisher_native_forward_probe import (
    build_native_probe_plan,
    capture_open_meteo_component,
    execute_native_probe,
    open_meteo_request_url,
    strict_proxy_request,
)


CHECKED = "2026-08-04T09:00:00Z"


def _plan(station: str = "CYYZ", timezone_name: str = "America/Toronto"):
    return build_native_probe_plan(
        station_id=station,
        event_date="2026-08-04",
        settlement_timezone=timezone_name,
        checked_at_utc=CHECKED,
    )


def _response(status: int, *, body: bytes = b"") -> dict[str, object]:
    return {
        "http_status": status,
        "requested_at_utc": "2026-08-04T08:59:50Z",
        "response_started_at_utc": "2026-08-04T08:59:51Z",
        "response_completed_at_utc": "2026-08-04T08:59:52Z",
        "response_headers": {},
        "response_bytes": len(body),
        "raw_object_sha256": hashlib.sha256(body).hexdigest(),
        "_raw_body": body,
    }


def _aifs_body(url: str) -> bytes:
    name = url.rsplit("/", 1)[-1]
    step = name.split("0000-", 1)[1].split("h-", 1)[0]
    return (
        json.dumps(
            {
                "model": "aifs-single",
                "stream": "oper",
                "type": "fc",
                "levtype": "sfc",
                "param": "2t",
                "step": step,
                "_offset": 123,
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def test_exact_station_day_cohorts_are_not_single_station_shortcuts() -> None:
    c = _plan()
    n = _plan("NZWN", "Pacific/Auckland")
    for plan in (c, n):
        by_model = {row["upstream_model_id"]: row for row in plan["products"]}
        assert len(by_model["AIFS"]["required_objects"]) == 4
        assert len(by_model["ICON"]["required_objects"]) == 24
        assert [row["ordinal"] for row in by_model["ICON"]["required_objects"]] == list(range(24))
        assert all("T_2M.grib2.bz2" in row["object_url"] for row in by_model["ICON"]["required_objects"])
        assert all("aifs-single/0p25/oper" in row["object_url"] for row in by_model["AIFS"]["required_objects"])


def test_terminal_404_410_produce_auditable_fallback_receipts() -> None:
    def requester(method, url, **kwargs):
        del method, kwargs
        return _response(404 if "aifs-single" in url else 410)

    result = execute_native_probe(_plan(), requester=requester)
    assert result["fallback_terminal_for_all_products"] is True
    assert result["native_complete_for_any_product"] is False
    assert result["indeterminate_for_any_product"] is False
    assert [row["terminal_status"] for row in result["product_results"]] == [
        "NATIVE_UNAVAILABLE",
        "NATIVE_UNAVAILABLE",
    ]
    for row in result["product_results"]:
        receipt = row["native_unavailability_receipt"]
        assert receipt["terminal_object_receipt_set_sha256"] == row[
            "terminal_object_receipt_set_sha256"
        ]
        assert receipt["required_object_count"] == len(row["object_receipts"])


def test_complete_native_cohort_forbids_republisher_fallback() -> None:
    def requester(method, url, **kwargs):
        del kwargs
        if method == "GET":
            return _response(200, body=_aifs_body(url))
        return _response(200)

    result = execute_native_probe(_plan(), requester=requester)
    assert [row["terminal_status"] for row in result["product_results"]] == [
        "NATIVE_COMPLETE_NO_FALLBACK",
        "NATIVE_COMPLETE_NO_FALLBACK",
    ]
    assert result["fallback_terminal_for_all_products"] is False
    assert result["native_complete_for_any_product"] is True
    assert all(
        row["native_unavailability_receipt"] is None
        for row in result["product_results"]
    )


@pytest.mark.parametrize("status", [301, 403, 429, 500, 503])
def test_transient_or_unauthorized_http_is_indeterminate(status: int) -> None:
    result = execute_native_probe(
        _plan(), requester=lambda *args, **kwargs: _response(status)
    )
    assert result["indeterminate_for_any_product"] is True
    assert result["fallback_terminal_for_all_products"] is False
    assert all(row["terminal_status"] == "INDETERMINATE" for row in result["product_results"])


def test_invalid_aifs_index_is_invalid_only_when_raw_is_retained() -> None:
    def requester(method, url, **kwargs):
        del kwargs
        if method == "GET":
            return _response(200, body=b'{"param":"t"}\n')
        return _response(404)

    result = execute_native_probe(_plan(), requester=requester)
    assert result["product_results"][0]["terminal_status"] == "NATIVE_INVALID"
    assert result["product_results"][1]["terminal_status"] == "NATIVE_UNAVAILABLE"
    assert result["fallback_terminal_for_all_products"] is True


def test_transport_failure_is_indeterminate_and_has_no_direct_retry() -> None:
    calls = 0

    def requester(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ContractError("native_forward_probe_indeterminate_transport:ConnectError")

    plan = _plan()
    expected = sum(row["required_object_count"] for row in plan["products"])
    result = execute_native_probe(plan, requester=requester)
    assert calls == expected
    assert result["indeterminate_for_any_product"] is True
    assert result["fallback_terminal_for_all_products"] is False


@pytest.mark.parametrize(
    "proxy",
    ["", "http://127.0.0.1:1083", "socks5://127.0.0.1:1082", "http://user@127.0.0.1:1082"],
)
def test_strict_transport_rejects_any_noncanonical_proxy_before_network(proxy: str) -> None:
    with pytest.raises(ContractError, match="proxy_invalid"):
        strict_proxy_request(
            "HEAD",
            "https://opendata.dwd.de/x",
            proxy=proxy,
            max_bytes=0,
        )


def test_open_meteo_url_binds_exact_product_station_day_and_timezone() -> None:
    url = open_meteo_request_url(
        model="AIFS",
        latitude=43.679,
        longitude=-79.629,
        event_date="2026-08-04",
        settlement_timezone="America/Toronto",
    )
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.hostname == "api.open-meteo.com"
    assert query["models"] == ["ecmwf_aifs025_single"]
    assert query["hourly"] == ["temperature_2m"]
    assert query["start_date"] == query["end_date"] == ["2026-08-04"]
    assert query["timezone"] == ["America/Toronto"]


def _open_meteo_body(*, timezone_name: str = "America/Toronto", wrong_date: bool = False) -> bytes:
    day = "2026-08-05" if wrong_date else "2026-08-04"
    return json.dumps(
        {
            "latitude": 43.68,
            "longitude": -79.63,
            "timezone": timezone_name,
            "hourly": {
                "time": [f"{day}T{hour:02d}:00" for hour in range(24)],
                "temperature_2m": [20.0 + hour / 10 for hour in range(24)],
            },
        },
        separators=(",", ":"),
    ).encode()


def test_open_meteo_capture_decodes_raw_into_auditable_daily_max() -> None:
    body = _open_meteo_body()

    def requester(method, url, **kwargs):
        del kwargs
        assert method == "GET"
        query = parse_qs(urlsplit(url).query)
        assert query["models"] == ["ecmwf_aifs025_single"]
        return _response(200, body=body)

    captured = capture_open_meteo_component(
        model="AIFS",
        station_id="CYYZ",
        event_date="2026-08-04",
        settlement_timezone="America/Toronto",
        requester=requester,
    )
    receipt = captured["component_receipt"]
    assert receipt["forecast_daily_max_c"] == "22.300000000000"
    assert receipt["hourly_valid_time_count"] == 24
    assert receipt["raw_object_sha256"] == hashlib.sha256(body).hexdigest()
    assert captured["raw_body"] == body


@pytest.mark.parametrize(
    "body,error",
    [
        (_open_meteo_body(timezone_name="UTC"), "timezone_mismatch"),
        (_open_meteo_body(wrong_date=True), "hourly_scope_mismatch"),
        (b"{}", "timezone_mismatch"),
    ],
)
def test_open_meteo_capture_rejects_wrong_scope(body: bytes, error: str) -> None:
    with pytest.raises(ContractError, match=error):
        capture_open_meteo_component(
            model="AIFS",
            station_id="CYYZ",
            event_date="2026-08-04",
            settlement_timezone="America/Toronto",
            requester=lambda *args, **kwargs: _response(200, body=body),
        )
