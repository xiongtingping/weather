from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from bayes_basket_target import ecmwf_ifs_archive
from bayes_basket_target.contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.event_contracts import ForecastRevision
from bayes_basket_target.calibration import (
    bind_taf_artifact_to_live_nwp_compatibility,
)
from bayes_basket_target.materialization import materialize_p0m
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
from bayes_basket_target.native_ensemble_oos import (
    DEFAULT_MIN_HOLDOUT_DAYS,
    DEFAULT_MIN_TRAIN_DAYS,
    NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS,
    NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS,
    NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE,
    _historical_transport_scope_allowed,
    _load_cases,
    _select_exact_shared_holdout_cohort,
    build_native_ensemble_oos,
    persist_native_ensemble_oos,
)
from bayes_basket_target.native_ensemble_history import (
    ingest_historical_archive,
)
from bayes_basket_target.native_ensemble_p0 import (
    compile_native_ensemble_p0,
    persist_native_ensemble_forward_prediction,
)
from bayes_basket_target.official_native_forward_packet import (
    SCHEMA as OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
)
from bayes_basket_target.open_meteo_ensemble import ENSEMBLE_SYSTEMS
from bayes_basket_target.probability_packet import (
    build_probability_envelope,
)
from bayes_basket_target.p1_compiler import (
    compile_station_p1_packets,
    materialization_packet,
)
from bayes_basket_target.production_io import (
    assert_formal_probability_model_authority,
    target_atomic_facts_from_cycle,
)
from bayes_basket_target.production_worker import (
    _compile_native_p0_with_republisher_fallback,
)
from bayes_basket_target.regime_materialization import (
    build_regime_artifact,
)
from bayes_basket_target.ssot import (
    DDL as TARGET_SSOT_DDL,
    apply_native_ensemble_foundation_schema,
    apply_production_ssot_schema,
    apply_target_ssot_schema,
    native_ensemble_foundation_schema_status,
)
from bayes_basket_target.taf_revision import (
    TAF_ARTIFACT_SCHEMA,
    TAF_CORRELATION_METHOD,
    TAF_METHOD_VERSION,
    TAF_NWP_CROSSFIT_ARTIFACT_KIND,
    TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION,
    TafRevisionArtifact,
    compile_taf_revision_proof,
)
from bayes_basket_target.taf_native_ensemble_oos import (
    EXACT_JOIN_SCHEMA,
    evaluate_taf_native_ensemble_oos,
)
from bayes_basket_target.native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
)
from bayes_basket_target.native_ensemble_training_facts import (
    _historical_taf_truth_authority,
    build_taf_native_exact_join,
    ingest_forward_taf_inbox,
    ingest_historical_taf_calibration,
    ingest_historical_taf_calibration_with_quarantine,
    ingest_historical_taf_correction_quarantine,
)


SYSTEMS = {
    "ECMWF_IFS_TEST": ("ECMWF", 3),
    "ECMWF_AIFS_TEST": ("ECMWF", 3),
    "NOAA_GEFS_TEST": ("NOAA_GEFS", 3),
}


def _aggregator_source_authority() -> dict[str, object]:
    return {
        "source_role": "aggregator_republisher",
        "native_run_init_known": False,
        "native_publication_at_known": False,
        "native_arrival_index_eligible": False,
        "formal_p0_eligible": False,
        "production_no_live_calibration_candidate": True,
        "evidence_role": "observational_candidate_not_oos",
        "oos_event_temporal_scope": "observational_candidate_not_oos",
        "counts_as_strict_oos": False,
        "historical_oos_contract_bound": False,
        "can_substitute_for_formal_promotion": False,
    }


def test_official_oos_transport_cycle_lead_contract_is_exact() -> None:
    assert _historical_transport_scope_allowed(
        system_id="NOAA_GEFS_05",
        transport_provider_id="NOAA_GEFS_PDS_S3",
        lead_bucket="d2",
        run_cycle="18Z",
    )
    assert not _historical_transport_scope_allowed(
        system_id="NOAA_GEFS_05",
        transport_provider_id="OPEN_METEO",
        lead_bucket="d2",
        run_cycle="18Z",
    )
    assert _historical_transport_scope_allowed(
        system_id="ECMWF_IFS_ENS_OPEN_PF50",
        transport_provider_id="ECMWF_OPEN_DATA_S3",
        lead_bucket="d2",
        run_cycle="18Z",
    )
    assert not _historical_transport_scope_allowed(
        system_id="ECMWF_IFS_ENS_OPEN_PF50",
        transport_provider_id="ECMWF_OPEN_DATA_S3",
        lead_bucket="d2",
        run_cycle="00Z",
    )
    assert _historical_transport_scope_allowed(
        system_id="ECCC_GEM_GLOBAL_ENS",
        transport_provider_id="ECCC_DD_WEATHER",
        lead_bucket="d2",
        run_cycle="00Z",
    )
    assert not _historical_transport_scope_allowed(
        system_id="ECCC_GEM_GLOBAL_ENS",
        transport_provider_id="ECCC_DD_WEATHER",
        lead_bucket="d2",
        run_cycle="18Z",
    )
    assert _historical_transport_scope_allowed(
        system_id="ECMWF_AIFS_025",
        transport_provider_id="OPEN_METEO",
        lead_bucket="d0",
        run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
    )


def test_cross_oos_uses_largest_exact_shared_holdout_cohort() -> None:
    first_window = [
        {"event_date": f"2026-07-{day:02d}"}
        for day in range(22, 30)
    ]
    shifted_window = [
        {"event_date": f"2026-07-{day:02d}"}
        for day in range(23, 31)
    ]
    systems = {
        "ECMWF": {"independence_family_id": "ECMWF"},
        "NOAA": {"independence_family_id": "NOAA_GEFS"},
        "ECCC": {"independence_family_id": "ECCC_GEM"},
    }
    selected = _select_exact_shared_holdout_cohort(
        ready_rows=[
            ("ECMWF", "18Z", {"predictions": first_window}),
            ("NOAA", "18Z", {"predictions": first_window}),
            ("ECCC", "00Z", {"predictions": shifted_window}),
        ],
        systems=systems,
        min_holdout_days=8,
    )

    assert selected is not None
    holdout, rows = selected
    assert holdout == tuple(
        f"2026-07-{day:02d}" for day in range(22, 30)
    )
    assert [row[0] for row in rows] == ["ECMWF", "NOAA"]


def test_cross_oos_includes_available_official_third_family() -> None:
    holdout = [
        {"event_date": f"2026-07-{day:02d}"}
        for day in range(23, 31)
    ]
    systems = {
        "ECMWF_IFS_ENS_OPEN_PF50": {
            "independence_family_id": "ECMWF",
        },
        "NOAA_GEFS_05": {"independence_family_id": "NOAA_GEFS"},
        "ECCC_GEM_GLOBAL_ENS": {
            "independence_family_id": "ECCC_GEM",
        },
    }
    selected = _select_exact_shared_holdout_cohort(
        ready_rows=[
            (system_id, "18Z", {"predictions": holdout})
            for system_id in sorted(systems)
        ],
        systems=systems,
        min_holdout_days=8,
    )

    assert selected is not None
    assert {row[0] for row in selected[1]} == set(systems)


def test_historical_cutoff_drops_scopes_with_no_remaining_cases() -> None:
    """A historical replay must ignore scopes that start after its cutoff."""

    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)

    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
        min_train_days=NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS,
        min_holdout_days=NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS,
        max_event_date="2026-06-20",
    )

    assert report["cross_candidates"]
    assert all(
        int(candidate.get("future_event_count", 0)) == 0
        for candidate in report["cross_candidates"]
    )
    assert all(
        str(candidate["holdout_end_date"]) <= "2026-06-20"
        for candidate in report["cross_candidates"]
    )


class _FakeEcmwfResponse:
    def __init__(
        self,
        body: bytes = b"ok",
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        url: str = "https://example.test/object",
    ) -> None:
        self.body = body
        self.status_code = status_code
        self.url = url
        self.headers = headers or {"Content-Length": str(len(body))}

    def __enter__(self) -> "_FakeEcmwfResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = httpx.Request("GET", self.url)
        response = httpx.Response(
            self.status_code,
            request=request,
            headers=self.headers,
        )
        raise httpx.HTTPStatusError(
            f"HTTP {self.status_code}",
            request=request,
            response=response,
        )

    def iter_bytes(self):
        yield self.body


class _FakeEcmwfClient:
    def __init__(self, actions: list[object]) -> None:
        self.actions = list(actions)
        self.calls: list[tuple[str, str, dict[str, str], float]] = []

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> object:
        self.calls.append((method, url, headers, timeout))
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


def test_ecmwf_get_bytes_retries_only_bounded_transient_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeEcmwfClient([
        httpx.ConnectError("temporary DNS failure"),
        _FakeEcmwfResponse(body=b"ok"),
    ])
    sleeps: list[float] = []
    monkeypatch.setattr(ecmwf_ifs_archive, "_thread_http_client", lambda: client)
    monkeypatch.setattr(ecmwf_ifs_archive.time_module, "sleep", sleeps.append)

    body, response_headers = ecmwf_ifs_archive._get_bytes(
        f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
        timeout_seconds=2.0,
        max_bytes=16,
    )

    assert body == b"ok"
    assert response_headers["Content-Length"] == "2"
    assert [call[1] for call in client.calls] == [
        f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
        f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
    ]
    assert sleeps == [ecmwf_ifs_archive.RETRY_BACKOFF_BASE_SECONDS]


def test_ecmwf_get_bytes_caps_transport_attempts_per_s3_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeEcmwfClient([
        httpx.ConnectError("temporary DNS failure")
        for _ in range(ecmwf_ifs_archive.MAX_TRANSPORT_ATTEMPTS * 2)
    ])
    sleeps: list[float] = []
    monkeypatch.setattr(ecmwf_ifs_archive, "_thread_http_client", lambda: client)
    monkeypatch.setattr(ecmwf_ifs_archive.time_module, "sleep", sleeps.append)

    with pytest.raises(ContractError, match="transport_failed:ConnectError"):
        ecmwf_ifs_archive._get_bytes(
            f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
            timeout_seconds=2.0,
            max_bytes=16,
        )

    assert len(client.calls) == ecmwf_ifs_archive.MAX_TRANSPORT_ATTEMPTS * 2
    attempts = ecmwf_ifs_archive.MAX_TRANSPORT_ATTEMPTS
    assert [call[1] for call in client.calls[:attempts]] == [
        f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
    ] * attempts
    assert [call[1] for call in client.calls[attempts:]] == [
        f"{ecmwf_ifs_archive.FALLBACK_BASE_URL}/object",
    ] * attempts
    expected_endpoint_sleeps = [
        min(
            ecmwf_ifs_archive.RETRY_BACKOFF_BASE_SECONDS * (2**attempt),
            ecmwf_ifs_archive.MAX_RETRY_BACKOFF_SECONDS,
        )
        for attempt in range(attempts - 1)
    ]
    assert sleeps == expected_endpoint_sleeps * 2


def test_ecmwf_get_bytes_keeps_regional_global_s3_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeEcmwfClient([
        *[_FakeEcmwfResponse(
            status_code=503,
            url=f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
        ) for _ in range(ecmwf_ifs_archive.MAX_TRANSPORT_ATTEMPTS)],
        _FakeEcmwfResponse(body=b"ok"),
    ])
    sleeps: list[float] = []
    monkeypatch.setattr(ecmwf_ifs_archive, "_thread_http_client", lambda: client)
    monkeypatch.setattr(ecmwf_ifs_archive.time_module, "sleep", sleeps.append)

    body, _response_headers = ecmwf_ifs_archive._get_bytes(
        f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
        timeout_seconds=2.0,
        max_bytes=16,
    )

    assert body == b"ok"
    assert [call[1] for call in client.calls] == [
        f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
    ] * ecmwf_ifs_archive.MAX_TRANSPORT_ATTEMPTS + [
        f"{ecmwf_ifs_archive.FALLBACK_BASE_URL}/object",
    ]
    assert sleeps == [
        min(
            ecmwf_ifs_archive.S3_SLOW_DOWN_BACKOFF_BASE_SECONDS * (2**attempt),
            ecmwf_ifs_archive.MAX_S3_SLOW_DOWN_BACKOFF_SECONDS,
        )
        for attempt in range(ecmwf_ifs_archive.MAX_TRANSPORT_ATTEMPTS - 1)
    ]


def test_ecmwf_get_bytes_retries_bounded_s3_slow_down_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeEcmwfClient([
        _FakeEcmwfResponse(
            status_code=503,
            url=f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
        ),
        _FakeEcmwfResponse(body=b"ok"),
    ])
    sleeps: list[float] = []
    monkeypatch.setattr(ecmwf_ifs_archive, "_thread_http_client", lambda: client)
    monkeypatch.setattr(ecmwf_ifs_archive.time_module, "sleep", sleeps.append)

    body, _response_headers = ecmwf_ifs_archive._get_bytes(
        f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
        timeout_seconds=2.0,
        max_bytes=16,
    )

    assert body == b"ok"
    assert [call[1] for call in client.calls] == [
        f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
        f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
    ]
    assert sleeps == [ecmwf_ifs_archive.S3_SLOW_DOWN_BACKOFF_BASE_SECONDS]


def test_ecmwf_get_bytes_does_not_retry_or_mask_http_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeEcmwfClient([
        _FakeEcmwfResponse(
            status_code=404,
            url=f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
        ),
    ])
    sleeps: list[float] = []
    monkeypatch.setattr(ecmwf_ifs_archive, "_thread_http_client", lambda: client)
    monkeypatch.setattr(ecmwf_ifs_archive.time_module, "sleep", sleeps.append)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        ecmwf_ifs_archive._get_bytes(
            f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
            timeout_seconds=2.0,
            max_bytes=16,
        )

    assert raised.value.response.status_code == 404
    assert len(client.calls) == 1
    assert sleeps == []


def test_ecmwf_get_bytes_does_not_retry_content_length_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeEcmwfClient([
        _FakeEcmwfResponse(
            body=b"ok",
            headers={"Content-Length": "17"},
            url=f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
        ),
    ])
    sleeps: list[float] = []
    monkeypatch.setattr(ecmwf_ifs_archive, "_thread_http_client", lambda: client)
    monkeypatch.setattr(ecmwf_ifs_archive.time_module, "sleep", sleeps.append)

    with pytest.raises(ContractError, match="response_too_large"):
        ecmwf_ifs_archive._get_bytes(
            f"{ecmwf_ifs_archive.PRIMARY_BASE_URL}/object",
            timeout_seconds=2.0,
            max_bytes=16,
        )

    assert len(client.calls) == 1
    assert sleeps == []


def _native_p0_forward_packet(
    *,
    event_date: str,
    captured_at_utc: str,
) -> dict[str, object]:
    intervals = {
        "19°C or below": [None, 19.5],
        "20°C": [19.5, 20.5],
        "21°C": [20.5, 21.5],
        "22°C or higher": [21.5, None],
    }
    systems = []
    for system_id, (family_id, member_count) in SYSTEMS.items():
        systems.append({
            "ensemble_system_id": system_id,
            "independence_family_id": family_id,
            "member_count": member_count,
            "members": [
                {
                    "ensemble_system_id": system_id,
                    "member_id": (
                        "control"
                        if index == 0
                        else f"perturbed_{index:02d}"
                    ),
                    "member_role": (
                        "CONTROL" if index == 0 else "PERTURBED"
                    ),
                    "raw_tmax_c": (
                        20.0 + (-0.2, 0.0, 0.2)[index]
                    ),
                }
                for index in range(member_count)
            ],
        })
    stable = {
        "schema": "open_meteo_native_ensemble_forward_packet_v1",
        "station_id": "TEST",
        "event_date": event_date,
        "settlement_timezone": "Etc/UTC",
        "aggregator_snapshot_at_utc": captured_at_utc,
        "evidence_role": "observational_candidate_not_oos",
        "oos_event_temporal_scope": "observational_candidate_not_oos",
        "strict_oos_sample_eligible": False,
        "can_substitute_for_formal_promotion": False,
        "bucket_intervals": intervals,
        "systems": systems,
    }
    return {
        **stable,
        "source_authority": _aggregator_source_authority(),
        "snapshot_content_sha256": content_sha256(stable),
    }


def _historical_archive(*, revised: bool) -> dict[str, object]:
    system_id = "ECMWF_IFS_ENS_GLOBAL"
    config = ENSEMBLE_SYSTEMS[system_id]
    members = [
        {
            "member_id": (
                "control"
                if index == 0
                else f"perturbed_{index:02d}"
            ),
            "member_role": (
                "CONTROL" if index == 0 else "PERTURBED"
            ),
            "raw_tmax_c": (
                20.5
                if revised and index == 0
                else 20.0 + index / 10.0
            ),
        }
        for index in range(int(config["expected_member_count"]))
    ]
    member_identity = [
        {
            "member_id": row["member_id"],
            "member_role": row["member_role"],
            "raw_tmax_c": row["raw_tmax_c"],
        }
        for row in members
    ]
    intervals = {"20°C or higher": [19.5, None]}
    bucket_sha = content_sha256(intervals)
    system = {
        "ensemble_system_id": system_id,
        "independence_family_id": "ECMWF",
        "member_count": len(members),
        "members": members,
        "member_set_sha256": content_sha256(member_identity),
        "raw_bucket_probabilities": {"20°C or higher": 1.0},
    }
    capture_identity = {
        "station_id": "TEST",
        "event_date": "2026-06-01",
        "settlement_timezone": "Etc/UTC",
        "archive_valid_day_bucket": "d0",
        "systems": [system],
        "unavailable_products": [],
    }
    capture = {
        **capture_identity,
        "capture_content_sha256": content_sha256(capture_identity),
    }
    query_at = (
        "2026-07-02T00:00:00Z"
        if revised
        else "2026-07-01T00:00:00Z"
    )
    response_sha = ("b" if revised else "a") * 64
    identity = {
        "schema": "open_meteo_native_ensemble_historical_archive_v1",
        "station_id": "TEST",
        "event_dates": ["2026-06-01"],
        "event_date_count": 1,
        "successful_event_date_count": 1,
        "unavailable_event_date_count": 0,
        "settlement_timezone": "Etc/UTC",
        "latitude": 0.0,
        "longitude": 0.0,
        "archive_query_at_utc": query_at,
        "request_url": "https://ensemble-api.open-meteo.test",
        "response_sha256": response_sha,
        "bucket_contract_sha256": bucket_sha,
        "bucket_intervals": intervals,
        "captures": [capture],
        "archive_contract": {
            "documentation_url": "https://open-meteo.com/",
            "provider_describes_past_days_as_archived_forecasts": True,
            "native_run_init_known": False,
            "native_publication_at_known": False,
            "native_arrival_index_eligible": False,
            "training_only": True,
            "holdout_eligible": False,
            "formal_p0_eligible": False,
        },
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _insert_fixture(
    conn: sqlite3.Connection,
    *,
    cycle_by_system: dict[str, str] | None = None,
    lead_bucket: str = "d1",
) -> None:
    apply_target_ssot_schema(conn)
    apply_production_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    station_identity = {
        "station_id": "TEST",
        "icao_code": "TEST",
        "city_label": "Test",
        "timezone_name": "America/New_York",
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "registry_version": "test-v1",
        "source_role": "TEST",
    }
    conn.execute(
        "INSERT INTO calibration_station_registry VALUES(?,?,?,?,?,?,?,?,?)",
        (
            *station_identity.values(),
            content_sha256(station_identity),
        ),
    )
    snapshot_sha = "1" * 64
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            snapshot_sha,
            "test",
            "shared_live_registry_export_only",
            "2026-05-01T00:00:00Z",
            "/test",
            1,
            "2026-05-01T00:00:01Z",
            "2" * 64,
        ),
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES("
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_sha,
            "test-weather",
            "2026-07-02",
            "20°C",
            "test",
            "TEST",
            "Etc/UTC",
            "C",
            "awc",
            1,
            "",
            "https://example.test",
            "3" * 64,
            "yes",
            "no",
            1,
            1,
            "{}",
            "2026-07-01T00:00:00Z",
        ),
    )
    # The historical republisher fixture uses d0 OOS contracts.  Keep a
    # second immutable registry row for the same station on the adjacent day
    # so the aggregator P0 compile tests can exercise a closed-contract match
    # without widening the production lead/cycle contract.
    conn.execute(
        "INSERT INTO basket_target_market_registry "
        "SELECT snapshot_sha256,slug,'2026-07-01',outcome,city_slug,"
        "station_id,settlement_timezone,settlement_unit,source_lane,"
        "source_eligible,source_block_reason,resolution_url,rules_sha256,"
        "yes_token,no_token,market_active,market_accepting_orders,"
        "registry_json,refreshed_at "
        "FROM basket_target_market_registry WHERE event_date='2026-07-02'"
    )
    for system_id, (family_id, member_count) in SYSTEMS.items():
        provider_id = "NOAA" if family_id == "NOAA_GEFS" else family_id
        system_identity = {
            "ensemble_system_id": system_id,
            "canonical_name": system_id,
            "independence_family_id": family_id,
            "provider_id": provider_id,
            "source_class": "NATIVE_ENSEMBLE_PROVIDER",
            "aggregator_or_republisher": 0,
            "expected_member_count": member_count,
            "member_identity_contract_version": "test",
            "publication_contract_version": "test",
            "valid_from_utc": "2026-01-01T00:00:00Z",
            "valid_until_utc": "9999-12-31T23:59:59Z",
        }
        conn.execute(
            "INSERT INTO calibration_native_ensemble_system "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *system_identity.values(),
                content_sha256(system_identity),
            ),
        )
        republisher_identity = {
            "ensemble_system_id": system_id,
            "canonical_name": "republisher-" + system_id,
            "independence_family_id": family_id,
            "upstream_provider_id": provider_id,
            "transport_provider_id": "OPEN_METEO",
            "source_role": "aggregator_republisher",
            "expected_member_count": member_count,
            "member_identity_contract_version": "test",
            "publication_contract_version": "test-republisher",
            "native_arrival_index_eligible": 0,
            "formal_p0_eligible": 0,
            "valid_from_utc": "2026-01-01T00:00:00Z",
            "valid_until_utc": "9999-12-31T23:59:59Z",
        }
        conn.execute(
            "INSERT INTO calibration_republisher_ensemble_product "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *republisher_identity.values(),
                content_sha256(republisher_identity),
            ),
        )
    start = date(2026, 6, 1)
    for index in range(28):
        day = start + timedelta(days=index)
        day_text = day.isoformat()
        actual = float(20 + 2 * (index % 4))
        committed = datetime(
            day.year,
            day.month,
            day.day,
            tzinfo=timezone.utc,
        ) + timedelta(days=1, hours=3)
        truth_identity = {
            "station_id": "TEST",
            "local_date": day_text,
            # Column order must match the table: settlement_timezone sits
            # directly after local_date, and these values are splatted
            # positionally.
            "settlement_timezone": "UTC",
            "official_tmax_c": actual,
            "source_lane": "AWC",
            "settlement_definition": "official_daily_tmax_c",
            "revision_no": 0,
            "published_at_utc": committed.isoformat().replace(
                "+00:00", "Z"
            ),
            "received_at_utc": committed.isoformat().replace(
                "+00:00", "Z"
            ),
            "canonical_committed_at_utc": committed.isoformat().replace(
                "+00:00", "Z"
            ),
            "timestamps_complete": 1,
            "qc_status": "ACCEPTED",
            "source_role": "TEST",
            "source_payload_sha256": content_sha256(
                {"day": day_text}
            ),
            "observation_ids_json": "[]",
            "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
        }
        truth_sha = content_sha256(truth_identity)
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "truth-" + truth_sha[:24],
                *truth_identity.values(),
                truth_sha,
            ),
            )
        snapshot = datetime(
            day.year,
            day.month,
            day.day,
            tzinfo=timezone.utc,
        ) - timedelta(hours=12)
        snapshot_text = snapshot.isoformat().replace("+00:00", "Z")
        # Open-Meteo has no provable native run cycle.  The historical fixture
        # therefore uses the explicit d0/unknown-cycle republisher contract;
        # `lead_bucket` and `cycle_by_system` remain accepted only so callers
        # exercising legacy forward fixtures do not silently alter lineage.
        for system_index, system_id in enumerate(SYSTEMS):
            contract_id = f"fixture-republisher-oos-{system_id}-{day_text}"
            truth_id = "truth-" + truth_sha[:24]
            contract_identity = {
                "contract_id": contract_id,
                "system": system_id,
                "station": "TEST",
                "day": day_text,
                "truth_id": truth_id,
            }
            conn.execute(
                "INSERT INTO calibration_republisher_historical_oos_contract "
                "(historical_oos_contract_id,contract_version,"
                "ensemble_system_id,station_id,target_local_date,"
                "archive_lead_bucket,run_cycle,station_day_closed,"
                "station_day_end_utc,station_day_closed_at_utc,awc_truth_id,"
                "awc_truth_revision_no,awc_truth_asof_utc,"
                "awc_truth_source_lane,receipt_class,temporal_scope,"
                "future_event_count,historical_oos_eligible,"
                "republisher_receipt_sha256,content_sha256,created_at_utc) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    contract_id,
                    "fixture-v1",
                    system_id,
                    "TEST",
                    day_text,
                    "d0",
                    "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                    1,
                    (datetime(
                        day.year,
                        day.month,
                        day.day,
                        tzinfo=timezone.utc,
                    ) + timedelta(days=1)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    committed.isoformat().replace("+00:00", "Z"),
                    truth_id,
                    0,
                    committed.isoformat().replace("+00:00", "Z"),
                    "AWC",
                    "HISTORICAL_ASOF_REPUBLISHER_RECEIPT",
                    "CLOSED_HISTORICAL_AWC_TRUTH",
                    0,
                    1,
                    "a" * 64,
                    content_sha256(contract_identity),
                    (committed + timedelta(hours=1)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                ),
            )
            for member_index, offset in enumerate((-0.2, 0.0, 0.2)):
                member_id = (
                    "control"
                    if member_index == 0
                    else f"perturbed_{member_index:02d}"
                )
                member_identity = {
                    "system": system_id,
                    "station": "TEST",
                    "day": day_text,
                    "member": member_id,
                    "value": (
                        actual + offset + 0.05 * system_index
                        + (0.01 if lead_bucket == "d2" else 0.0)
                    ),
                }
                member_sha = content_sha256(member_identity)
                conn.execute(
                    "INSERT INTO calibration_republisher_ensemble_historical_member_run "
                    "(historical_member_run_id,ensemble_system_id,station_id,"
                    "target_local_date,archive_lead_bucket,run_cycle,member_id,"
                    "member_role,raw_tmax_c,settlement_definition,"
                    "extraction_version,provider_id,transport_provider_id,"
                    "native_run_init_utc,offline_oos_eligible,"
                    "archive_query_at_utc,source_url,raw_object_ref,"
                    "source_object_sha256,qc_status,authority_scope,"
                    "historical_oos_contract_id,historical_oos_eligible,"
                    "holdout_eligible,content_sha256) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "historical-member-" + member_sha[:24],
                        system_id,
                        "TEST",
                        day_text,
                        "d0",
                        "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                        member_id,
                        "CONTROL" if member_index == 0 else "PERTURBED",
                        (
                            actual + offset + 0.05 * system_index
                            + (0.01 if lead_bucket == "d2" else 0.0)
                        ),
                        "official_daily_tmax_c",
                        "test",
                        system_id,
                        "OPEN_METEO",
                        None,
                        0,
                        snapshot_text,
                        "https://example.test",
                        "test",
                        member_sha,
                        "ACCEPTED",
                        "CALIBRATION_TRAIN_ONLY",
                        contract_id,
                        1,
                        0,
                        member_sha,
                    ),
                )
    conn.commit()


def test_historical_republisher_revision_freezes_first_snapshot() -> None:
    conn = sqlite3.connect(":memory:")
    apply_target_ssot_schema(conn)
    station_identity = {
        "station_id": "TEST",
        "icao_code": "TEST",
        "city_label": "Test",
        "timezone_name": "Etc/UTC",
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "registry_version": "test-v1",
        "source_role": "TEST",
    }
    conn.execute(
        "INSERT INTO calibration_station_registry "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (*station_identity.values(), content_sha256(station_identity)),
    )

    first = ingest_historical_archive(
        conn,
        _historical_archive(revised=False),
    )
    frozen_value = conn.execute(
        "SELECT raw_tmax_c FROM "
        "calibration_republisher_ensemble_historical_member_run "
        "WHERE station_id='TEST' AND member_id='control'"
    ).fetchone()[0]
    revised = ingest_historical_archive(
        conn,
        _historical_archive(revised=True),
    )

    assert first["counts"]["member_runs"] == 51
    assert first["revision_observation_count"] == 0
    assert revised["counts"] == {
        "systems": 0,
        "member_runs": 0,
        "member_buckets": 0,
    }
    assert revised["revision_observation_count"] == 1
    assert revised["first_observed_training_snapshot_frozen"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM "
        "calibration_republisher_ensemble_historical_member_run "
        "WHERE station_id='TEST'"
    ).fetchone()[0] == 51
    assert conn.execute(
        "SELECT raw_tmax_c FROM "
        "calibration_republisher_ensemble_historical_member_run "
        "WHERE station_id='TEST' AND member_id='control'"
    ).fetchone()[0] == frozen_value


def test_official_historical_taf_ingress_preserves_asof_authority() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    taf_authority = _historical_taf_truth_authority("AWC")
    sample = {
        "station_id": "TEST",
        "event_date": "2026-06-01",
        "settlement_timezone": "Etc/UTC",
        "source_lane": "AWC",
        "truth_label_authority": taf_authority["truth_label_authority"],
        "truth_authority": taf_authority,
        "reference_only": False,
        "decision_anchor_utc": "2026-06-01T01:00:00Z",
        "taf": {
            "issue_time_utc": "2026-05-31T23:00:00Z",
            "available_time_utc": "2026-05-31T23:05:00Z",
            "tx_c": 20.0,
            "tx_valid_at_utc": "2026-06-01T12:00:00Z",
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
            "raw_taf": "TAF TEST 312300Z 0100/0124 00000KT CAVOK TX20/0112Z",
            "raw_taf_sha256": "a" * 64,
        },
        "truth": {
            "source_lane": "AWC",
            "truth_label_authority": taf_authority["truth_label_authority"],
            "tmax_c": 20.0,
        },
        "receipts": {
            "taf_response_sha256": "b" * 64,
        },
    }
    identity = {
        "schema": "bayes_basket_awc_taf_calibration_dataset_v1",
        "provider": "NOAA Aviation Weather Center",
        "source_class": "official_date_addressed_historical_replay",
            "station_id": "TEST",
            "settlement_timezone": "Etc/UTC",
            "source_lane": "AWC",
            "truth_label_authority": taf_authority["truth_label_authority"],
            "truth_authority": taf_authority,
            "start_date": "2026-06-01",
        "end_date": "2026-06-01",
        "sample_count": 1,
        "samples": [sample],
        "excluded_sample_count": 0,
        "excluded_samples": [],
        "blockers": [],
        "resource_bounds": {},
        "boundary": {
            "native_local_receipt": False,
            "official_awc_database_availability_preserved": True,
                "historical_replay_only": True,
                "truth_days_closed": True,
                "reference_only": False,
                "enters_realtime_truth": False,
                "enters_p1": False,
                "enters_hard_no": False,
                "replacement_required_when_canonical_history_available": False,
                "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    artifact = {
        **identity,
        "content_sha256": content_sha256(identity),
    }

    first = ingest_historical_taf_calibration(conn, artifact)
    replay = ingest_historical_taf_calibration(conn, artifact)

    assert first["wrote_count"] == 1
    assert replay["wrote_count"] == 0
    assert conn.execute(
        "SELECT source_class,truth_label_authority,authority_scope "
        "FROM calibration_historical_taf_capture"
    ).fetchone() == (
        "official_date_addressed_historical_replay",
        taf_authority["truth_label_authority"],
        "CALIBRATION_TRAIN_ONLY",
    )

    legacy_identity = {
        key: value
        for key, value in identity.items()
        if key not in {"source_lane", "truth_label_authority", "truth_authority"}
    }
    legacy = {
        **legacy_identity,
        "content_sha256": content_sha256(legacy_identity),
    }
    with pytest.raises(ContractError, match="station_lane_authority_invalid"):
        ingest_historical_taf_calibration(conn, legacy)


def test_historical_taf_amd_is_quarantined_not_ingested() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    authority = _historical_taf_truth_authority("AWC")
    sample = {
        "station_id": "TEST",
        "event_date": "2026-06-01",
        "settlement_timezone": "Etc/UTC",
        "source_lane": "AWC",
        "truth_label_authority": authority["truth_label_authority"],
        "truth_authority": authority,
        "reference_only": False,
        "decision_anchor_utc": "2026-06-01T01:00:00Z",
        "taf": {
            "issue_time_utc": "2026-05-31T23:00:00Z",
            "available_time_utc": "2026-05-31T23:05:00Z",
            "tx_c": 20.0,
            "tx_valid_at_utc": "2026-06-01T12:00:00Z",
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
            "raw_taf": "TAF AMD TEST 312300Z 0100/0124 CAVOK TX20/0112Z",
            "raw_taf_sha256": "a" * 64,
        },
        "truth": {
            "source_lane": "AWC",
            "truth_label_authority": authority["truth_label_authority"],
            "tmax_c": 20.0,
        },
        "receipts": {"taf_response_sha256": "b" * 64},
    }
    identity = {
        "schema": "bayes_basket_awc_taf_calibration_dataset_v1",
        "provider": "NOAA Aviation Weather Center",
        "source_class": "official_date_addressed_historical_replay",
        "station_id": "TEST",
        "settlement_timezone": "Etc/UTC",
        "source_lane": "AWC",
        "truth_label_authority": authority["truth_label_authority"],
        "truth_authority": authority,
        "start_date": "2026-06-01",
        "end_date": "2026-06-01",
        "sample_count": 1,
        "samples": [sample],
        "excluded_sample_count": 0,
        "excluded_samples": [],
        "blockers": [],
        "resource_bounds": {},
        "boundary": {
            "native_local_receipt": False,
            "official_awc_database_availability_preserved": True,
            "historical_replay_only": True,
            "truth_days_closed": True,
            "reference_only": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
            "replacement_required_when_canonical_history_available": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    artifact = {**identity, "content_sha256": content_sha256(identity)}

    # Reproduce the append-only ordering that made a standalone correction
    # importer unsafe: an ordinary TAF was retained first, then an official
    # AMD for the same station-day arrived.  The shared ingress must append
    # the quarantine even though it cannot delete that earlier receipt.
    normal_sample = {
        **sample,
        "taf": {
            **sample["taf"],
            "raw_taf": "TAF TEST 312300Z 0100/0124 CAVOK TX20/0112Z",
            "raw_taf_sha256": "c" * 64,
        },
    }
    normal_identity = {**identity, "samples": [normal_sample]}
    normal_artifact = {
        **normal_identity,
        "content_sha256": content_sha256(normal_identity),
    }
    previous = ingest_historical_taf_calibration(conn, normal_artifact)
    assert previous["wrote_count"] == 1

    ingress = ingest_historical_taf_calibration_with_quarantine(conn, artifact)

    assert ingress["quarantine_wrote_count"] == 1
    assert ingress["quarantine_correction_count"] == 1
    assert ingress["capture_count"] == 0
    assert ingress["skipped_correction_station_days"] == ["2026-06-01"]
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_historical_taf_correction_quarantine"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_historical_taf_capture"
    ).fetchone()[0] == 1

    # Rebuilding the bounded archive changes only the outer envelope hash;
    # the same raw correction/sample must remain an idempotent replay.
    retry_identity = {**identity, "end_date": "2026-06-02"}
    retry_artifact = {
        **retry_identity,
        "content_sha256": content_sha256(retry_identity),
    }
    replay = ingest_historical_taf_correction_quarantine(conn, retry_artifact)
    assert replay["wrote_count"] == 0
    assert replay["replay_count"] == 1


def test_historical_taf_correction_quarantine_can_repair_one_day_only() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    authority = _historical_taf_truth_authority("AWC")

    def sample(event_date: str, raw_taf: str, raw_sha: str) -> dict:
        return {
            "station_id": "TEST",
            "event_date": event_date,
            "settlement_timezone": "Etc/UTC",
            "source_lane": "AWC",
            "truth_label_authority": authority["truth_label_authority"],
            "truth_authority": authority,
            "reference_only": False,
            "taf": {"raw_taf": raw_taf, "raw_taf_sha256": raw_sha},
        }

    samples = [
        sample("2026-06-01", "TAF AMD TEST 312300Z 0100/0124", "a" * 64),
        sample("2026-06-02", "TAF COR TEST 010100Z 0200/0224", "b" * 64),
    ]
    identity = {
        "schema": "bayes_basket_awc_taf_calibration_dataset_v1",
        "source_class": "official_date_addressed_historical_replay",
        "station_id": "TEST",
        "settlement_timezone": "Etc/UTC",
        "source_lane": "AWC",
        "truth_label_authority": authority["truth_label_authority"],
        "truth_authority": authority,
        "samples": samples,
    }
    artifact = {**identity, "content_sha256": content_sha256(identity)}

    receipt = ingest_historical_taf_correction_quarantine(
        conn,
        artifact,
        event_dates={"2026-06-02"},
    )

    assert receipt["correction_count"] == 1
    assert conn.execute(
        "SELECT event_date FROM calibration_historical_taf_correction_quarantine"
    ).fetchall() == [("2026-06-02",)]


def test_forward_taf_is_observational_and_never_enters_p0m_oos() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    assert len(report["cross_candidates"]) == 1
    assert report["cross_candidates"][0]["state"] == (
        "PRODUCTION_NO_LIVE_OOS_PASS"
    )
    assert report["formal_p0_eligible"] is False
    assert report["formal_promotion_min_train_days"] == (
        DEFAULT_MIN_TRAIN_DAYS
    )
    assert report["formal_promotion_min_holdout_days"] == (
        DEFAULT_MIN_HOLDOUT_DAYS
    )
    persisted = persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    assert persisted["counts"] == {
        "system_artifacts": 3,
        "selection_evidence": 1,
        "cross_artifacts": 1,
        "oos_evaluations": 1,
        "oos_predictions": 8,
        "activations": 4,
    }
    same_cycle_mapping = {
        system_id: "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
        for system_id in sorted(SYSTEMS)
    }
    same_cycle_mapping_json = json.dumps(
        same_cycle_mapping,
        sort_keys=True,
        separators=(",", ":"),
    )
    same_cycle_mapping_sha = content_sha256(same_cycle_mapping)
    for table in (
        "calibration_cross_native_ensemble_selection_evidence",
        "calibration_cross_native_ensemble_artifact",
        "calibration_cross_native_ensemble_oos_prediction",
    ):
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            "WHERE run_cycle_by_system_json=? "
            "AND run_cycle_by_system_sha256=?",
            (same_cycle_mapping_json, same_cycle_mapping_sha),
        ).fetchone()[0] > 0
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
        "AND run_cycle_by_system_json=? "
        "AND run_cycle_by_system_sha256=?",
        (same_cycle_mapping_json, same_cycle_mapping_sha),
    ).fetchone()[0] == 1
    captures = []
    for index in range(20, 28):
        day = date(2026, 6, 1) + timedelta(days=index)
        decision = datetime(
            day.year,
            day.month,
            day.day,
            tzinfo=timezone.utc,
        ) - timedelta(hours=12)
        available = decision + timedelta(hours=6)
        revision = {
            "schema": "bayes_basket_forecast_revision_v1",
            "revision_id": f"taf-test-{day.isoformat()}",
            "station_id": "TEST",
            "event_date": day.isoformat(),
            "revision_kind": "TAF",
            "sequence": 1,
            "issued_at_utc": (
                available - timedelta(minutes=5)
            ).isoformat().replace("+00:00", "Z"),
            "received_at_utc": available.isoformat().replace(
                "+00:00", "Z"
            ),
            "valid_from_utc": available.isoformat().replace(
                "+00:00", "Z"
            ),
            "valid_until_utc": (
                available + timedelta(days=1)
            ).isoformat().replace("+00:00", "Z"),
            "source_object_sha256": content_sha256({
                "taf": day.isoformat()
            }),
            "supersedes_revision_id": None,
        }
        capture_identity = {
            "schema": "bayes_basket_target_forward_taf_capture_v1",
            "source_class": "official_forward_native_receipt",
            "station_id": "TEST",
            "event_date": day.isoformat(),
            "settlement_timezone": "Etc/UTC",
            "provider_available_at_utc": available.isoformat().replace(
                "+00:00", "Z"
            ),
            "forecast_revision": revision,
            "taf_fields": {
                "tx_c": float(20 + 2 * (index % 4)),
                "cloud_cover_fraction": 0.25,
                "precipitation_probability": 0.1,
                "wind_speed_mps": 4.0,
            },
        }
        captures.append({
            **capture_identity,
            "content_sha256": content_sha256(capture_identity),
        })
    taf_receipt = ingest_forward_taf_inbox(conn, {
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": captures,
    })
    assert taf_receipt["wrote_count"] == 8
    candidate = report["cross_candidates"][0]
    exact_join = build_taf_native_exact_join(
        conn,
        station_id="TEST",
        season_bin="JJA",
        lead_bucket=candidate["lead_bucket"],
        run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
        settlement_definition="official_daily_tmax_c",
        settlement_timezone="Etc/UTC",
        ensemble_system_ids=candidate["selected_system_ids"],
        independence_family_ids=candidate["independence_family_ids"],
        family_weight_caps=candidate["family_weight_caps"],
        bucket_intervals={
            "19°C or below": [None, 19.5],
            "20°C": [19.5, 20.5],
            "21°C": [20.5, 21.5],
            "22°C or higher": [21.5, None],
        },
    )
    assert exact_join["sample_count"] == 0
    assert exact_join["observational_forward_event_count"] == 8
    assert exact_join["observational_forward_event_dates"] == sorted(
        day.isoformat() for day in (
            date(2026, 6, 21) + timedelta(days=index)
            for index in range(8)
        )
    )
    assert {
        exact_join["station_id"],
        exact_join["season_bin"],
        exact_join["lead_bucket"],
        exact_join["run_cycle"],
        exact_join["settlement_definition"],
        exact_join["settlement_timezone"],
    } == {
        "TEST",
        "JJA",
        "d0",
        "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
        "official_daily_tmax_c",
        "Etc/UTC",
    }
    assert exact_join["samples"] == []
    # A later AMD quarantines its whole station-day for P0-M calibration;
    # retaining the earlier ordinary TAF would otherwise manufacture a
    # seemingly clean exact-OOS row after an unreviewed revision.
    amended_day = date(2026, 6, 21)
    amended_available = datetime(
        2026, 6, 20, 20, tzinfo=timezone.utc
    )
    amendment_revision = {
        "schema": "bayes_basket_forecast_revision_v1",
        "revision_id": "taf-amd-2026-06-21",
        "station_id": "TEST",
        "event_date": amended_day.isoformat(),
        "revision_kind": "AMD",
        "sequence": 2,
        "issued_at_utc": (
            amended_available - timedelta(minutes=5)
        ).isoformat().replace("+00:00", "Z"),
        "received_at_utc": amended_available.isoformat().replace(
            "+00:00", "Z"
        ),
        "valid_from_utc": amended_available.isoformat().replace(
            "+00:00", "Z"
        ),
        "valid_until_utc": (
            amended_available + timedelta(days=1)
        ).isoformat().replace("+00:00", "Z"),
        "source_object_sha256": content_sha256({
            "taf_amd": amended_day.isoformat()
        }),
        "supersedes_revision_id": "taf-test-2026-06-21",
    }
    amendment_identity = {
        "schema": "bayes_basket_target_forward_taf_capture_v1",
        "source_class": "official_forward_native_receipt",
        "station_id": "TEST",
        "event_date": amended_day.isoformat(),
        "settlement_timezone": "Etc/UTC",
        "provider_available_at_utc": amended_available.isoformat().replace(
            "+00:00", "Z"
        ),
        "forecast_revision": amendment_revision,
        "taf_fields": {
            "tx_c": 24.0,
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
        },
    }
    amend_receipt = ingest_forward_taf_inbox(conn, {
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": [{
            **amendment_identity,
            "content_sha256": content_sha256(amendment_identity),
        }],
    })
    assert amend_receipt["wrote_count"] == 1
    amendment_capture_sha = conn.execute(
        "SELECT content_sha256 FROM calibration_forward_taf_capture "
        "WHERE revision_id='taf-amd-2026-06-21'"
    ).fetchone()[0]
    quarantined_join = build_taf_native_exact_join(
        conn,
        station_id="TEST",
        season_bin="JJA",
        lead_bucket=candidate["lead_bucket"],
        run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
        settlement_definition="official_daily_tmax_c",
        settlement_timezone="Etc/UTC",
        ensemble_system_ids=candidate["selected_system_ids"],
        independence_family_ids=candidate["independence_family_ids"],
        family_weight_caps=candidate["family_weight_caps"],
        bucket_intervals={
            "19°C or below": [None, 19.5],
            "20°C": [19.5, 20.5],
            "21°C": [20.5, 21.5],
            "22°C or higher": [21.5, None],
        },
    )
    assert quarantined_join["sample_count"] == 0
    assert quarantined_join["observational_forward_event_count"] == 8
    assert quarantined_join["excluded_correction_sample_count"] == 1
    assert quarantined_join["excluded_correction_samples"] == [{
        "station_id": "TEST",
        "event_date": "2026-06-21",
        "reason": "forward_taf_correction_requires_manual_review",
        "corrections": [{
            "revision_kind": "AMD",
            "revision_id": "taf-amd-2026-06-21",
            "revision_sequence": 2,
            "provider_available_at_utc": "2026-06-20T20:00:00Z",
            "content_sha256": amendment_capture_sha,
        }],
    }]
    intervals = {
        "19°C or below": [None, 19.5],
        "20°C": [19.5, 20.5],
        "21°C": [20.5, 21.5],
        "22°C or higher": [21.5, None],
    }
    systems = []
    for system_id, (family_id, member_count) in SYSTEMS.items():
        members = [
            {
                "ensemble_system_id": system_id,
                "member_id": (
                    "control"
                    if index == 0
                    else f"perturbed_{index:02d}"
                ),
                "member_role": (
                    "CONTROL" if index == 0 else "PERTURBED"
                ),
                "raw_tmax_c": 20.0 + (-0.2, 0.0, 0.2)[index],
            }
            for index in range(member_count)
        ]
        systems.append({
            "ensemble_system_id": system_id,
            "independence_family_id": family_id,
            "member_count": member_count,
            "members": members,
        })
    stable = {
        "schema": "open_meteo_native_ensemble_forward_packet_v1",
        "station_id": "TEST",
        "event_date": "2026-07-01",
        "settlement_timezone": "Etc/UTC",
        "aggregator_snapshot_at_utc": "2026-07-01T12:00:00Z",
        "evidence_role": "observational_candidate_not_oos",
        "oos_event_temporal_scope": "observational_candidate_not_oos",
        "strict_oos_sample_eligible": False,
        "can_substitute_for_formal_promotion": False,
        "bucket_intervals": intervals,
        "systems": systems,
    }
    forward = {
        **stable,
        "source_authority": _aggregator_source_authority(),
        "snapshot_content_sha256": content_sha256(stable),
    }
    compiled = compile_native_ensemble_p0(
        conn,
        forward_packet=forward,
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    forward_persisted = persist_native_ensemble_forward_prediction(
        conn,
        compiled,
    )
    assert forward_persisted["state"] == "COMMITTED"
    assert forward_persisted["wrote_count"] == 1
    assert persist_native_ensemble_forward_prediction(
        conn,
        compiled,
    )["state"] == "EXISTING_FIRST_FORWARD_PREDICTION"
    envelope = build_probability_envelope(compiled["packet"])
    assert envelope.stage == "P0"
    assert envelope.lineage["probability_pathway"] == (
        "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
    )
    assert envelope.lineage["native_authority_scope"] == (
        "PRODUCTION_NO_LIVE_ONLY"
    )
    assert len(json.loads(
        envelope.lineage["independence_family_ids_json"]
    )) == 2
    assert sum(envelope.probabilities.values()) == 1.0
    fingerprint_sha = envelope.lineage[
        "taf_nwp_live_compatibility_fingerprint_sha256"
    ]
    artifact_identity = {
        "schema": TAF_ARTIFACT_SCHEMA,
        "artifact_id": "taf-test-native-v1",
        "activation_id": "taf-test-native-activation-v1",
        "station_id": "TEST",
        "season_bin": "JJA",
        "lead_bucket": "d0",
        "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
        "settlement_definition": "official_daily_tmax_c",
        "settlement_timezone": "Etc/UTC",
        "method_version": TAF_METHOD_VERSION,
        "trained_through_utc": "2026-06-30T00:00:00Z",
        "sample_n": 40,
        "effective_sample_n": 30.0,
        "oos_metrics": {
            "brier_score": 0.12,
            "nll": 0.4,
            "crps": 0.6,
        },
        "tx_bias_c": 0.0,
        "taf_nwp_delta_coefficient": 0.6,
        "tx_residual_scale_c": 0.8,
        "physical_feature_effect_c": {
            "cloud_cover_fraction": -0.5,
            "precipitation_probability": -0.5,
            "wind_speed_normalized": 0.0,
        },
        "wind_speed_cap_mps": 20.0,
        "confirmation_tolerance_c": 0.75,
        "max_confirmation_strength": 0.25,
        "max_correction_strength": 0.20,
        "max_probability_mass_transfer": 0.10,
        "max_center_displacement_buckets": 1,
        "likelihood_ratio_floor": 0.25,
        "likelihood_ratio_ceiling": 4.0,
        "nwp_combination_artifact_sha256": "9" * 64,
        "nwp_combination_artifact_kind": (
            TAF_NWP_CROSSFIT_ARTIFACT_KIND
        ),
        "nwp_live_compatibility_contract_version": (
            TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        ),
        "nwp_live_compatibility_fingerprint_sha256": fingerprint_sha,
        "nwp_live_compatibility_evidence_sha256": "8" * 64,
        "nwp_live_compatibility_validated": True,
        "oos_taf_nwp_residual_correlation": 0.25,
        "correlation_discount_method": TAF_CORRELATION_METHOD,
        "correlation_discount": 0.75,
        "joint_physical_oos_validated": True,
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "activated": True,
    }
    taf_artifact = TafRevisionArtifact.from_mapping({
        **artifact_identity,
        "content_sha256": content_sha256(artifact_identity),
    })
    revision = ForecastRevision.from_mapping({
        "revision_id": "taf-test-1",
        "station_id": "TEST",
        "event_date": "2026-07-01",
        "revision_kind": "TAF",
        "sequence": 1,
        "issued_at_utc": "2026-07-01T12:10:00Z",
        "received_at_utc": "2026-07-01T12:20:00Z",
        "valid_from_utc": "2026-07-01T12:00:00Z",
        "valid_until_utc": "2026-07-02T18:00:00Z",
        "source_object_sha256": "7" * 64,
        "supersedes_revision_id": None,
    })
    proof = compile_taf_revision_proof(
        envelope,
        p0_root=envelope,
        forecast_revision=revision,
        artifact=taf_artifact,
        bucket_centers_c={
            "19°C or below": 19.0,
            "20°C": 20.0,
            "21°C": 21.0,
            "22°C or higher": 22.0,
        },
        taf_fields={
            "tx_c": 20.0,
            "cloud_cover_fraction": 0.2,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
        },
        decision_at_utc="2026-07-01T13:00:01Z",
    )
    p0m = materialize_p0m(
        envelope,
        p0_root=envelope,
        taf_revision_artifact=taf_artifact,
        taf_revision_proof=proof,
        decision_at_utc="2026-07-01T13:00:01Z",
        fresh_until_utc="2026-07-01T19:00:00Z",
        bridge_sha256=envelope.bridge_sha256,
    )
    assert p0m.stage == "P0-M"
    assert p0m.lineage["p0_root_checkpoint_id"] == envelope.checkpoint_id
    assert_formal_probability_model_authority(
        [envelope, p0m],
        probability=p0m,
    )
    registry_rows = [
        {
            "station_id": "TEST",
            "event_date": "2026-07-01",
            "settlement_timezone": "Etc/UTC",
            "settlement_unit": "C",
            "outcome": bucket,
            "registry_json": json.dumps({"upper": upper}),
        }
        for bucket, upper in (
            ("19°C or below", 19),
            ("20°C", 20),
            ("21°C", 21),
            ("22°C or higher", None),
        )
    ]
    p1_packets = compile_station_p1_packets(
        predecessor=p0m,
        prediction_root=p0m,
        observations=[{
            "observation_id": "metar-test-1",
            "station_id": "TEST",
                "event_date_local": "2026-07-01",
            "reported_temperature_c": 20.0,
                "valid_at_utc": "2026-07-01T00:10:00Z",
                "canonical_committed_at_utc": "2026-07-01T00:10:01Z",
            "raw_sha256": "6" * 64,
            "source_lane": "AWC",
            "report_type": "METAR",
            "evidence_role": "FORMAL",
            "qc_status": "ACCEPTED",
            "correction_suspected": False,
        }],
        registry_rows=registry_rows,
            compilation_at_utc="2026-07-01T00:10:02Z",
    )
    assert len(p1_packets) == 1
    p1 = build_probability_envelope(
        materialization_packet(p1_packets[0])
    )
    assert p1.stage == "P1"
    assert p1.lineage["p0_root_checkpoint_id"] == envelope.checkpoint_id
    assert p1.lineage["prediction_root_checkpoint_id"] == (
        p0m.checkpoint_id
    )
    assert_formal_probability_model_authority(
        [envelope, p0m, p1],
        probability=p1,
    )
    regime = build_regime_artifact(p1)
    assert regime.station_id == "TEST"
    facts = target_atomic_facts_from_cycle({
        "probability_envelope": p1.to_dict(),
    })
    assert facts["observation"]["observation_id"] == "metar-test-1"
    assert facts["bound"]["bound_id"] == p1.lineage["hard_bound_id"]

    older_packet = json.loads(json.dumps(compiled["packet"]))
    older_packet["decision_at_utc"] = "2026-07-01T12:59:59Z"
    older_packet["native_probability_envelope"][
        "decision_at_utc"
    ] = "2026-07-01T12:59:59Z"
    older_native = older_packet["native_probability_envelope"]
    older_native["posterior_sha256"] = content_sha256({
        "station_id": older_native["station_id"],
        "event_date": older_native["event_date"],
        "decision_at_utc": older_native["decision_at_utc"],
        "probabilities": older_native["probabilities"],
        "native_probability_evidence_sha256": older_native[
            "lineage"
        ]["native_probability_evidence_sha256"],
    })
    inbox_identity = {
        "schema": "bayes_basket_target_p0_inbox_v1",
        "generated_at_utc": "2026-07-01T13:00:00Z",
        "packets": [
            {
                "packet": older_packet,
                "packet_content_sha256": content_sha256(older_packet),
            },
            {
                "packet": compiled["packet"],
                "packet_content_sha256": compiled[
                    "packet_content_sha256"
                ],
            },
        ],
    }
    live_inbox = {
        **inbox_identity,
        "content_sha256": content_sha256(inbox_identity),
    }
    fingerprint = json.loads(
        envelope.lineage["taf_nwp_live_compatibility_json"]
    )
    samples = []
    for index in range(24):
        sample_day = date(2026, 7, 3) + timedelta(days=index)
        event_date = sample_day.isoformat()
        decision = datetime(
            sample_day.year,
            sample_day.month,
            sample_day.day,
            tzinfo=timezone.utc,
        )
        prediction_identity = {
            "station_id": "TEST",
            "event_date": event_date,
            "strict_oos": True,
            "sequence": index,
        }
        samples.append({
            "station_id": "TEST",
            "event_date": event_date,
            "settlement_timezone": "Etc/UTC",
            "calibration_scope": {
                "season_bin": "JJA",
                "lead_bucket": "d0",
                "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                "settlement_definition": "official_daily_tmax_c",
            },
            "native_p0": {
                "probability_pathway": (
                    "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
                ),
                "prediction_oos": True,
                "trained_through_event_date": "2026-06-29",
                "decision_at_utc": decision.isoformat().replace(
                    "+00:00", "Z"
                ),
                "ensemble_system_ids": fingerprint[
                    "ensemble_system_ids"
                ],
                "independence_family_ids": fingerprint[
                    "independence_family_ids"
                ],
                "family_weight_caps": fingerprint[
                    "family_weight_caps"
                ],
                "probabilities": {
                    "19°C or below": 0.05,
                    "20°C": 0.10,
                    "21°C": 0.80,
                    "22°C or higher": 0.05,
                },
                "bucket_centers_c": {
                    "19°C or below": 19.0,
                    "20°C": 20.0,
                    "21°C": 21.0,
                    "22°C or higher": 22.0,
                },
                "prediction_content_sha256": content_sha256(
                    prediction_identity
                ),
            },
            "taf": {
                "available_at_utc": (
                    decision + timedelta(hours=1)
                ).isoformat().replace("+00:00", "Z"),
                "tx_c": 20.0,
                "cloud_cover_fraction": 0.0,
                "precipitation_probability": 0.0,
                "wind_speed_mps": 2.0,
            },
            "truth": {"tmax_c": 20.0},
            "settlement_window": {
                "end_utc": (
                    decision + timedelta(days=1)
                ).isoformat().replace("+00:00", "Z"),
            },
        })
    exact_identity = {
        "schema": EXACT_JOIN_SCHEMA,
        "source_class": "strict_oos_native_p0_taf_truth_exact_join",
        "nwp_crossfit_prediction_artifact_sha256": "9" * 64,
        "sample_count": len(samples),
        "samples": samples,
    }
    native_taf_report = evaluate_taf_native_ensemble_oos(
        exact_join={
            **exact_identity,
            "content_sha256": content_sha256(exact_identity),
        },
        live_p0_inbox=live_inbox,
        taf_artifact_raw=taf_artifact.to_dict(),
        station_id="TEST",
        season_bin="JJA",
        lead_bucket="d0",
        run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
    )
    assert native_taf_report["state"] == "PASS"
    assert native_taf_report["validated"] is True
    bound_taf = bind_taf_artifact_to_live_nwp_compatibility(
        taf_artifact=taf_artifact,
        live_fingerprint=native_taf_report["live_fingerprint"],
        transport_oos_evidence=native_taf_report["evidence"],
    )
    assert bound_taf.nwp_live_compatibility_fingerprint_sha256 == (
        fingerprint["fingerprint_sha256"]
    )


def test_republisher_oos_persists_unknown_cycle_mapping_at_every_cross_layer() -> None:
    from bayes_basket_target.native_ensemble_oos import _selection_identity

    conn = sqlite3.connect(":memory:")
    _insert_fixture(
        conn,
        cycle_by_system={
            "ECMWF_IFS_TEST": "18Z",
            "ECMWF_AIFS_TEST": "18Z",
            "NOAA_GEFS_TEST": "00Z",
        },
    )
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    assert len(report["cross_candidates"]) == 1
    candidate = report["cross_candidates"][0]
    assert candidate["run_cycle"] == "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
    assert candidate["run_cycle_scope"] == "SAME_NATIVE_CYCLE"
    assert candidate["run_cycle_by_system"] == {
        system_id: "AGGREGATOR_UNKNOWN_NATIVE_CYCLE"
        for system_id in sorted(SYSTEMS)
    }
    assert candidate["run_cycle_by_system_sha256"] == content_sha256(
        candidate["run_cycle_by_system"]
    )
    assert candidate["p0_exact_match_ready"] is True
    assert candidate["p0_exact_match_blocker"] is None
    selection_identity = _selection_identity(candidate)
    assert (
        "run_cycle_by_system_sha256="
        + candidate["run_cycle_by_system_sha256"]
        in selection_identity["method_version"]
    )
    assert (
        "run_cycle_by_system="
        + json.dumps(
            candidate["run_cycle_by_system"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        in selection_identity["method_version"]
    )
    receipt = persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    assert receipt["state"] == "COMMITTED"
    expected_json = json.dumps(
        candidate["run_cycle_by_system"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_sha = candidate["run_cycle_by_system_sha256"]
    for table in (
        "calibration_cross_native_ensemble_selection_evidence",
        "calibration_cross_native_ensemble_artifact",
        "calibration_cross_native_ensemble_oos_prediction",
    ):
        rows = conn.execute(
            f"SELECT DISTINCT run_cycle,run_cycle_by_system_json,"
            f"run_cycle_by_system_sha256 FROM {table}"
        ).fetchall()
        assert rows == [
            ("AGGREGATOR_UNKNOWN_NATIVE_CYCLE", expected_json, expected_sha)
        ]
    cross_activation = conn.execute(
        "SELECT run_cycle,run_cycle_by_system_json,"
        "run_cycle_by_system_sha256 "
        "FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE'"
    ).fetchone()
    assert cross_activation == (
        "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
        expected_json,
        expected_sha,
    )
    system_activations = conn.execute(
        "SELECT ensemble_system_id_or_star,run_cycle,"
        "run_cycle_by_system_json,run_cycle_by_system_sha256 "
        "FROM calibration_native_ensemble_activation "
        "WHERE artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
        "ORDER BY ensemble_system_id_or_star"
    ).fetchall()
    assert len(system_activations) == 3
    for system_id, run_cycle, mapping_json, mapping_sha in (
        system_activations
    ):
        singleton = {system_id: run_cycle}
        assert mapping_json == json.dumps(
            singleton,
            sort_keys=True,
            separators=(",", ":"),
        )
        assert mapping_sha == content_sha256(singleton)


def test_cross_cycle_identity_rejects_same_system_multiple_cycles() -> None:
    from bayes_basket_target.native_ensemble_oos import (
        _cycle_mapping_for_rows,
    )

    with pytest.raises(
        ContractError,
        match="native_ensemble_oos_system_cycle_conflict",
    ):
        _cycle_mapping_for_rows([
            ("ECMWF_IFS_TEST", "18Z", {}),
            ("ECMWF_IFS_TEST", "00Z", {}),
            ("NOAA_GEFS_TEST", "18Z", {}),
        ])


def test_native_cycle_mapping_legacy_migration_is_additive_and_idempotent(
) -> None:
    mapping_column_ddl = (
        "  run_cycle_by_system_json TEXT,\n"
        "  run_cycle_by_system_sha256 TEXT CHECK(\n"
        "    run_cycle_by_system_sha256 IS NULL OR\n"
        "    length(run_cycle_by_system_sha256)=64\n"
        "  ),\n"
    )
    assert TARGET_SSOT_DDL.count(mapping_column_ddl) == 4
    legacy_ddl = TARGET_SSOT_DDL.replace(mapping_column_ddl, "")
    legacy = sqlite3.connect(":memory:")
    legacy.executescript(legacy_ddl)

    same_cycle = sqlite3.connect(":memory:")
    _insert_fixture(same_cycle)
    same_report = build_native_ensemble_oos(
        same_cycle,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    persist_native_ensemble_oos(
        same_cycle,
        same_report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )

    mixed = sqlite3.connect(":memory:")
    _insert_fixture(
        mixed,
        cycle_by_system={
            "ECMWF_IFS_TEST": "18Z",
            "ECMWF_AIFS_TEST": "00Z",
            "NOAA_GEFS_TEST": "18Z",
        },
        lead_bucket="d2",
    )
    mixed_report = build_native_ensemble_oos(
        mixed,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
        system_mask=("ECMWF_IFS_TEST", "NOAA_GEFS_TEST"),
    )
    persist_native_ensemble_oos(
        mixed,
        mixed_report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )

    migration_tables = (
        "calibration_cross_native_ensemble_selection_evidence",
        "calibration_cross_native_ensemble_artifact",
        "calibration_cross_native_ensemble_oos_prediction",
        "calibration_native_ensemble_activation",
    )
    for source in (same_cycle, mixed):
        for table in migration_tables:
            legacy_columns = [
                str(row[1])
                for row in legacy.execute(
                    f"PRAGMA table_info({table})"
                )
            ]
            source_columns = {
                str(row[1])
                for row in source.execute(
                    f"PRAGMA table_info({table})"
                )
            }
            assert set(legacy_columns).issubset(source_columns)
            rows = source.execute(
                f"SELECT {','.join(legacy_columns)} FROM {table}"
            ).fetchall()
            legacy.executemany(
                f"INSERT OR IGNORE INTO {table}({','.join(legacy_columns)}) "
                f"VALUES({','.join('?' for _ in legacy_columns)})",
                rows,
            )
    legacy.commit()

    before = native_ensemble_foundation_schema_status(legacy)
    assert before["ok"] is False
    assert set(before["missing_run_cycle_mapping_columns"]) == set(
        migration_tables
    )
    first = apply_native_ensemble_foundation_schema(legacy)
    assert first["run_cycle_mapping_migration"]["columns_added"] == 8
    assert (
        first["run_cycle_mapping_migration"][
            "same_cycle_rows_backfilled"
        ]
        > 0
    )
    assert first["run_cycle_mapping_migration"][
        "legacy_mixed_rows_left_unset"
    ] == 0
    assert native_ensemble_foundation_schema_status(legacy)["ok"] is True
    for table in migration_tables:
        same_rows = legacy.execute(
            f"SELECT run_cycle_by_system_json,"
            f"run_cycle_by_system_sha256 FROM {table} "
            "WHERE run_cycle<>'MIXED_NATIVE_CYCLES'"
        ).fetchall()
        assert same_rows
        assert all(
            mapping_json is not None and mapping_sha is not None
            for mapping_json, mapping_sha in same_rows
        )
        mixed_rows = legacy.execute(
            f"SELECT run_cycle_by_system_json,"
            f"run_cycle_by_system_sha256 FROM {table} "
            "WHERE run_cycle='MIXED_NATIVE_CYCLES'"
        ).fetchall()
        assert mixed_rows == []

    second = apply_native_ensemble_foundation_schema(legacy)
    assert second["run_cycle_mapping_migration"] == {
        "columns_added": 0,
        "same_cycle_rows_backfilled": 0,
        "legacy_mixed_rows_left_unset": first[
            "run_cycle_mapping_migration"
        ]["legacy_mixed_rows_left_unset"],
    }


def test_native_oos_excludes_an_open_future_station_day_even_with_early_truth() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)

    # Deliberately corrupt the retained truth clock for a still-open UTC
    # station day.  The loader must still reject the row on the independent
    # settlement-close boundary, rather than trusting a prematurely visible
    # truth timestamp as an OOS sample.
    preclose_truth = {
        "station_id": "TEST",
        "local_date": "2026-06-28",
        "official_tmax_c": 26.0,
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "revision_no": 1,
        "published_at_utc": "2026-06-28T01:00:00Z",
        "received_at_utc": "2026-06-28T01:00:00Z",
        "canonical_committed_at_utc": "2026-06-28T01:00:00Z",
        "timestamps_complete": 1,
        "qc_status": "ACCEPTED",
        "source_role": "TEST",
        "source_payload_sha256": content_sha256({"day": "2026-06-28", "revision": 1}),
        "observation_ids_json": "[]",
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
    }
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "truth-preclose-20260628",
            *preclose_truth.values(),
            content_sha256(preclose_truth),
        ),
    )

    cases = _load_cases(
        conn,
        station_id="TEST",
        as_of_utc="2026-06-28T12:00:00Z",
    )
    flattened = [case for rows in cases.values() for case in rows]

    assert flattened
    assert all(case["historical_closed_day"] is True for case in flattened)
    assert {case["event_date"] for case in flattened}.isdisjoint(
        {"2026-06-28"}
    )
    assert "2026-06-27" in {case["event_date"] for case in flattened}


def test_no_live_bootstrap_threshold_is_explicitly_non_formal() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)

    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
        min_train_days=NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS,
        min_holdout_days=NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS,
    )

    assert report["threshold_profile"] == (
        NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
    )
    assert report["min_train_days"] == 2
    assert report["min_holdout_days"] == 1
    assert report["formal_promotion_min_train_days"] == 20
    assert report["formal_promotion_min_holdout_days"] == 8
    assert report["formal_p0_eligible"] is False
    assert all(
        candidate["authority_scope"] == "PRODUCTION_NO_LIVE_ONLY"
        and candidate["formal_p0_eligible"] is False
        and candidate["threshold_profile"]
        == NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
        for candidate in report["cross_candidates"]
    )
    persisted = persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    assert persisted["counts"]["cross_artifacts"] == 1
    method_version = conn.execute(
        "SELECT method_version FROM "
        "calibration_cross_native_ensemble_selection_evidence"
    ).fetchone()[0]
    reliability = json.loads(conn.execute(
        "SELECT reliability_parameters_json FROM "
        "calibration_native_ensemble_system_artifact LIMIT 1"
    ).fetchone()[0])
    assert method_version.endswith(
        NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
    )
    assert reliability["oos_threshold_profile"] == (
        NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
    )
    assert reliability["formal_promotion_min_train_days"] == 20
    assert reliability["formal_promotion_min_holdout_days"] == 8
    assert reliability["formal_p0_eligible"] is False


def test_open_meteo_fallback_p0_uses_republisher_authority_lane() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    first = persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    activation_count = conn.execute(
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation"
    ).fetchone()[0]
    second = persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:01:00Z",
    )
    assert first["counts"]["activations"] == 4
    assert second["counts"]["activations"] == 0
    assert second["receipts"][0]["reused_existing"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation"
    ).fetchone()[0] == activation_count
    day = date(2026, 7, 1)
    forward_packet = _native_p0_forward_packet(
        event_date=day.isoformat(),
        captured_at_utc="2026-07-01T12:00:00Z",
    )

    compiled = compile_native_ensemble_p0(
        conn,
        forward_packet=forward_packet,
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    lineage = compiled["packet"]["native_probability_envelope"]["lineage"]
    assert lineage["native_transport_provider"] == "OPEN_METEO"
    assert lineage["transport_lineage_class"] == "aggregator_republisher"
    assert lineage["native_arrival_index_eligible"] == "false"
    assert lineage["native_formal_p0_eligible"] == "false"
    assert lineage["republisher_fallback_p0_eligible"] == "true"


def test_open_meteo_fallback_p0_capture_freshness_is_bounded() -> None:
    """An eligible historical contract does not let an old OM packet revive.

    The fallback retains its strict closed-AWC activation proof, but its
    forward receipt is still a time-bounded P0 input.  Recompiling the exact
    same snapshot at the TTL boundary must fail closed.
    """

    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    forward_packet = _native_p0_forward_packet(
        event_date="2026-07-01",
        captured_at_utc="2026-07-01T12:00:00Z",
    )

    fresh = compile_native_ensemble_p0(
        conn,
        forward_packet=forward_packet,
        decision_at_utc="2026-07-01T12:00:59Z",
        fresh_seconds=60,
    )
    assert fresh["packet"]["native_probability_envelope"][
        "fresh_until_utc"
    ] == "2026-07-01T12:01:00Z"
    with pytest.raises(
        ContractError,
        match="native_ensemble_snapshot_stale",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=forward_packet,
            decision_at_utc="2026-07-01T12:01:00Z",
            fresh_seconds=60,
        )


def test_p0_router_retries_governed_open_meteo_after_stale_native() -> None:
    """Real compiler loop: stale official → OM; stale both → no P0.

    The official fixture needs only a valid immutable receipt because the
    capture-age gate must fire before any later model-field decoding.  The OM
    packet, on the other hand, exercises the real closed-AWC strict-OOS
    activation and republisher lineage checks before it is accepted.
    """

    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    scope = ("TEST", "2026-07-01")

    def official_packet(received_at_utc: str) -> dict[str, object]:
        identity = {
            "schema": OFFICIAL_NATIVE_FORWARD_PACKET_SCHEMA,
            "station_id": scope[0],
            "event_date": scope[1],
            "received_at_utc": received_at_utc,
        }
        return {
            **identity,
            "snapshot_content_sha256": content_sha256(identity),
        }

    stale_official = official_packet("2026-07-01T12:00:00Z")
    fresh_republisher = _native_p0_forward_packet(
        event_date=scope[1],
        captured_at_utc="2026-07-01T17:00:00Z",
    )
    compiled, failures, source_class = (
        _compile_native_p0_with_republisher_fallback(
            conn,
            scope=scope,
            preferred_packet=stale_official,
            official_packet=stale_official,
            republisher_packet=fresh_republisher,
            decision_at_utc="2026-07-01T18:00:00Z",
        )
    )

    assert source_class == "aggregator_republisher"
    assert compiled is not None
    lineage = compiled["packet"]["native_probability_envelope"]["lineage"]
    assert lineage["transport_lineage_class"] == "aggregator_republisher"
    assert lineage["native_arrival_index_eligible"] == "false"
    assert failures[0]["source_class"] == "official_native"
    assert "native_ensemble_snapshot_stale" in failures[0]["reason"]

    stale_republisher = _native_p0_forward_packet(
        event_date=scope[1],
        captured_at_utc="2026-07-01T12:00:00Z",
    )
    compiled, failures, source_class = (
        _compile_native_p0_with_republisher_fallback(
            conn,
            scope=scope,
            preferred_packet=stale_official,
            official_packet=stale_official,
            republisher_packet=stale_republisher,
            decision_at_utc="2026-07-01T18:00:00Z",
        )
    )

    assert compiled is None
    assert source_class == ""
    assert [failure["source_class"] for failure in failures] == [
        "official_native",
        "aggregator_republisher",
    ]
    assert all(
        "native_ensemble_snapshot_stale" in failure["reason"]
        for failure in failures
    )


def test_open_meteo_fallback_p0_requires_strict_oos_proof() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    candidate = report["cross_candidates"][0]
    for evaluation in candidate["system_evaluations"].values():
        evaluation["transport_provider_id"] = "NOT_OPEN_METEO"
    report_identity = {
        key: value for key, value in report.items() if key != "content_sha256"
    }
    report = {**report_identity, "content_sha256": content_sha256(report_identity)}
    persist_native_ensemble_oos(conn, report, activated_at_utc="2026-07-01T00:00:00Z")
    forward_packet = _native_p0_forward_packet(
        event_date="2026-07-01",
        captured_at_utc="2026-07-01T12:00:00Z",
    )

    with pytest.raises(
        ContractError,
        match="aggregator_fallback_strict_oos_proof_missing",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=forward_packet,
            decision_at_utc="2026-07-01T13:00:00Z",
        )


def test_native_authority_p1_reaches_delta_decision_and_training_fact(
    tmp_path,
    monkeypatch,
) -> None:
    from tests import test_bayes_basket_target_production_chain_e2e as e2e

    original = e2e._inputs()
    envelopes, priors, artifacts, activations, market, cash_policy = original
    system_ids = ["ECMWF_IFS_TEST", "NOAA_GEFS_TEST"]
    family_ids = ["ECMWF", "NOAA_GEFS"]
    system_families = {
        "ECMWF_IFS_TEST": "ECMWF",
        "NOAA_GEFS_TEST": "NOAA_GEFS",
    }
    system_weights = {
        "ECMWF_IFS_TEST": 0.45,
        "NOAA_GEFS_TEST": 0.55,
    }
    family_caps = {"ECMWF": 0.45, "NOAA_GEFS": 1.0}
    system_artifacts = {
        "ECMWF_IFS_TEST": "1" * 64,
        "NOAA_GEFS_TEST": "2" * 64,
    }
    system_activations = {
        "ECMWF_IFS_TEST": "3" * 64,
        "NOAA_GEFS_TEST": "4" * 64,
    }
    fingerprint_identity = {
        "schema": TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "station_id": "ZUUU",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "NATIVE_TEST",
        "settlement_definition": "official_daily_tmax_c",
        "available_ensemble_system_set_hash": content_sha256(system_ids),
        "ensemble_system_ids": system_ids,
        "independence_family_ids": family_ids,
        "system_family_ids": system_families,
        "resolved_system_weights": system_weights,
        "resolved_family_weights": {
            "ECMWF": 0.45,
            "NOAA_GEFS": 0.55,
        },
        "family_weight_caps": family_caps,
        "cross_ensemble_artifact_sha256": "5" * 64,
        "cross_selection_evidence_sha256": "6" * 64,
        "cross_activation_sha256": "7" * 64,
        "system_calibration_artifacts": {
            system_id: {
                "artifact_content_sha256": system_artifacts[system_id],
                "activation_content_sha256": system_activations[system_id],
            }
            for system_id in system_ids
        },
    }
    fingerprint = {
        **fingerprint_identity,
        "fingerprint_sha256": content_sha256(fingerprint_identity),
    }
    p0_raw = envelopes[0].to_dict()
    p0_raw["lineage"] = {
        **p0_raw["lineage"],
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "p0_probability_pathway": P0_PROBABILITY_PATHWAY,
        "native_authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_definition": "official_daily_tmax_c",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "NATIVE_TEST",
        "ensemble_system_ids_json": json.dumps(
            system_ids, separators=(",", ":")
        ),
        "independence_family_ids_json": json.dumps(
            family_ids, separators=(",", ":")
        ),
        "system_family_ids_json": json.dumps(
            system_families, sort_keys=True, separators=(",", ":")
        ),
        "system_weights_json": json.dumps(
            system_weights, sort_keys=True, separators=(",", ":")
        ),
        "family_weight_caps_json": json.dumps(
            family_caps, sort_keys=True, separators=(",", ":")
        ),
        "system_artifact_shas_json": json.dumps(
            system_artifacts, sort_keys=True, separators=(",", ":")
        ),
        "system_activation_shas_json": json.dumps(
            system_activations, sort_keys=True, separators=(",", ":")
        ),
        "native_cross_ensemble_artifact_sha256": "5" * 64,
        "native_selection_evidence_sha256": "6" * 64,
        "native_cross_activation_sha256": "7" * 64,
        "native_probability_evidence_sha256": "8" * 64,
        "taf_nwp_live_compatibility_fingerprint_sha256": (
            fingerprint["fingerprint_sha256"]
        ),
        "taf_nwp_live_compatibility_json": json.dumps(
            fingerprint, sort_keys=True, separators=(",", ":")
        ),
    }
    native_p0 = ProbabilityEnvelope.from_mapping(p0_raw)
    p0m_raw = envelopes[1].to_dict()
    p0m_raw["lineage"] = {
        **p0m_raw["lineage"],
        "p0_root_checkpoint_id": native_p0.checkpoint_id,
        "p0_root_content_sha256": native_p0.content_sha256,
    }
    native_p0m = ProbabilityEnvelope.from_mapping(p0m_raw)
    p1_raw = envelopes[2].to_dict()
    p1_raw["lineage"] = {
        **p1_raw["lineage"],
        "p0_root_checkpoint_id": native_p0.checkpoint_id,
        "p0_root_content_sha256": native_p0.content_sha256,
        "prediction_root_checkpoint_id": native_p0m.checkpoint_id,
        "prediction_root_content_sha256": native_p0m.content_sha256,
    }
    native_p1 = ProbabilityEnvelope.from_mapping(p1_raw)
    assert_formal_probability_model_authority(
        [native_p0, native_p0m, native_p1],
        probability=native_p1,
    )
    monkeypatch.setattr(
        e2e,
        "_inputs",
        lambda: (
            [native_p0, native_p0m, native_p1],
            priors,
            artifacts,
            activations,
            market,
            cash_policy,
        ),
    )
    e2e.test_p1_to_plan_to_multileg_atomic_ledger_is_deterministic_and_no_live(
        tmp_path
    )
