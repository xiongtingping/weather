"""Regression coverage for the ECMWF AIFS Open-Meteo ingress boundary.

The currently configured AIFS feeds are useful, explicitly-labelled
aggregator captures.  They must never be upgraded into native-arrival or
formal-P0 authority merely by crossing an internal hand-off boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_store import ingest_forward_packet
from bayes_basket_target.native_ensemble_store import republisher_product_identity
from bayes_basket_target.native_ensemble_oos import build_native_ensemble_oos
from bayes_basket_target.native_ensemble_p0 import (
    _require_aggregator_fallback_authority,
)
from bayes_basket_target.open_meteo_ensemble import (
    ENSEMBLE_SYSTEMS,
    capture_scope,
)
from bayes_basket_target.ssot import apply_target_ssot_schema


_ROOT = Path(__file__).resolve().parents[1]
_MODEL_POOL_PATH = (
    _ROOT / "config/bayes_basket_target/station_regional_model_pool_v1.json"
)
_AIFS = {
    "ECMWF_AIFS_ENS_GLOBAL": {
        "model_scope": "global",
        "api_model": "ecmwf_aifs025_ensemble",
        "pool": "global_aggregator_capture_models",
        "entry": "ECMWF_AIFS_GLOBAL",
    },
    "ECMWF_AIFS_ENS_EUROPE": {
        "model_scope": "regional",
        "api_model": "ecmwf_aifs_europe_ensemble",
        "pool": "regional_ensemble_aggregator_capture_models",
        "entry": "ECMWF_AIFS_EUROPE",
    },
}


def _model_pool() -> dict[str, object]:
    return json.loads(_MODEL_POOL_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("system_id", "expected"), _AIFS.items())
def test_aifs_catalog_registers_distinct_51_member_products(
    system_id: str,
    expected: dict[str, str],
) -> None:
    product = ENSEMBLE_SYSTEMS[system_id]

    assert product["model_scope"] == expected["model_scope"]
    assert product["api_model"] == expected["api_model"]
    assert product["expected_member_count"] == 51


@pytest.mark.parametrize(("system_id", "expected"), _AIFS.items())
def test_aifs_open_meteo_catalog_and_pool_keep_republisher_lineage(
    system_id: str,
    expected: dict[str, str],
) -> None:
    product = ENSEMBLE_SYSTEMS[system_id]
    pool = _model_pool()
    entry = pool[expected["pool"]][expected["entry"]]  # type: ignore[index]

    assert product["independence_family_id"] == "ECMWF"
    assert entry["system_id"] == system_id  # type: ignore[index]
    assert entry["model_parameter"] == product["api_model"]  # type: ignore[index]
    assert entry["source_class"] == "aggregator_republisher"  # type: ignore[index]
    assert entry["formal_p0_eligible"] is False  # type: ignore[index]
    assert entry["official_native_status"] == (  # type: ignore[index]
        "not_available_in_current_capture_chain"
    )
    assert product["candidate_only"] is True
    assert product["formal_p0_eligible"] is False
    assert product["native_arrival_index_eligible"] is False
    assert product["official_native_arrival_status"] == "MISSING_FAIL_CLOSED"


def _aifs_packet(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Create an Open-Meteo AIFS packet without network activity."""

    daily: dict[str, object] = {"time": ["2026-08-02"]}
    for system_id in _AIFS:
        product = ENSEMBLE_SYSTEMS[system_id]
        model = str(product["api_model"])
        daily[f"temperature_2m_max_{model}"] = [25.0]
        for index in range(1, 51):
            daily[f"temperature_2m_max_member{index:02d}_{model}"] = [
                25.0 + index / 100
            ]

    import bayes_basket_target.open_meteo_ensemble as ensemble

    monkeypatch.setattr(
        ensemble,
        "_bounded_get_json",
        lambda *_args, **_kwargs: ({"daily": daily}, "a" * 64),
    )
    return capture_scope(
        station_id="TEST",
        event_date="2026-08-02",
        latitude=1.0,
        longitude=2.0,
        settlement_timezone="Etc/UTC",
        registry_rows=[
            {
                "station_id": "TEST",
                "event_date": "2026-08-02",
                "settlement_unit": "C",
                "outcome": "24°C or below",
                "registry_json": json.dumps({"lower": None, "upper": 24}),
            },
            {
                "station_id": "TEST",
                "event_date": "2026-08-02",
                "settlement_unit": "C",
                "outcome": "25°C or higher",
                "registry_json": json.dumps({"lower": 25, "upper": None}),
            },
        ],
        captured_at_utc="2026-08-01T00:00:00Z",
    )


def _candidate_ingress_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ssot_schema(conn)
    station = {
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
        "INSERT INTO calibration_station_registry VALUES(?,?,?,?,?,?,?,?,?)",
        (*station.values(), content_sha256(station)),
    )
    conn.commit()
    return conn


def test_aifs_candidate_ingress_is_idempotent_and_never_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = _aifs_packet(monkeypatch)
    systems = {
        str(row["ensemble_system_id"]): row
        for row in packet["systems"]  # type: ignore[index]
    }
    assert set(systems) == set(_AIFS)
    for system_id in _AIFS:
        authority = systems[system_id]["candidate_authority"]
        assert authority == {
            "source_role": "aggregator_republisher",
            "candidate_only": True,
            "formal_p0_eligible": False,
            "native_arrival_index_eligible": False,
            "official_native_arrival_status": "MISSING_FAIL_CLOSED",
        }

    conn = _candidate_ingress_connection()
    first = ingest_forward_packet(conn, packet)
    second = ingest_forward_packet(conn, packet)
    assert first["counts"] == {
        "systems": 2,
        "member_runs": 102,
        "member_buckets": 2,
    }
    assert second["counts"] == {
        "systems": 0,
        "member_runs": 0,
        "member_buckets": 0,
    }
    rows = conn.execute(
        "SELECT source_role,authority_scope,count(*) "
        "FROM calibration_republisher_ensemble_member_bucket "
        "GROUP BY source_role,authority_scope"
    ).fetchall()
    assert rows == [("aggregator_republisher", "PRODUCTION_NO_LIVE_CANDIDATE", 2)]
    assert conn.execute(
        "SELECT count(*) FROM calibration_native_ensemble_system"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM calibration_republisher_ensemble_member_run "
        "WHERE transport_provider_id<>'OPEN_METEO'"
    ).fetchone()[0] == 0


def test_aifs_ingress_preserves_io_error_when_sqlite_drops_savepoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Savepoint cleanup must not replace a recoverable SQLite I/O error."""

    class TransactionAbortingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection
            self.failed = False

        def execute(self, statement: str, parameters=()):
            if (
                not self.failed
                and statement.startswith(
                    "INSERT INTO calibration_republisher_ensemble_product"
                )
            ):
                self.failed = True
                # Model SQLite's transaction-abort behavior: the savepoint
                # is gone before the caller's exception cleanup executes.
                self.connection.rollback()
                raise sqlite3.OperationalError("disk I/O error")
            return self.connection.execute(statement, parameters)

    conn = _candidate_ingress_connection()
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        ingest_forward_packet(
            TransactionAbortingConnection(conn),
            _aifs_packet(monkeypatch),
        )
    assert conn.in_transaction is False
    conn.close()


def test_republisher_ingress_reuses_immutable_legacy_product_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-contract-version product row must not block current packets."""

    packet = _aifs_packet(monkeypatch)
    conn = _candidate_ingress_connection()
    legacy = republisher_product_identity("ECMWF_AIFS_ENS_GLOBAL")
    legacy["publication_contract_version"] = (
        "native_timestamps_required_for_formal_v1"
    )
    # The historical row is append-only and its original hash is retained;
    # the ingress must validate semantic identity, never rewrite the row.
    legacy["content_sha256"] = "a" * 64
    conn.execute(
        "INSERT INTO calibration_republisher_ensemble_product "
        "(ensemble_system_id,canonical_name,independence_family_id,"
        "upstream_provider_id,transport_provider_id,source_role,"
        "expected_member_count,member_identity_contract_version,"
        "publication_contract_version,native_arrival_index_eligible,"
        "formal_p0_eligible,valid_from_utc,valid_until_utc,content_sha256) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(legacy[key] for key in (
            "ensemble_system_id",
            "canonical_name",
            "independence_family_id",
            "upstream_provider_id",
            "transport_provider_id",
            "source_role",
            "expected_member_count",
            "member_identity_contract_version",
            "publication_contract_version",
            "native_arrival_index_eligible",
            "formal_p0_eligible",
            "valid_from_utc",
            "valid_until_utc",
            "content_sha256",
        )),
    )
    conn.commit()

    result = ingest_forward_packet(conn, packet)
    assert result["state"] == "COMMITTED"
    assert result["counts"]["systems"] == 1
    assert conn.execute(
        "SELECT publication_contract_version,content_sha256 "
        "FROM calibration_republisher_ensemble_product "
        "WHERE ensemble_system_id='ECMWF_AIFS_ENS_GLOBAL'"
    ).fetchone() == (
        "native_timestamps_required_for_formal_v1",
        "a" * 64,
    )


def test_aifs_ingress_rejects_missing_per_system_negative_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = copy.deepcopy(_aifs_packet(monkeypatch))
    packet["systems"][0].pop("candidate_authority")  # type: ignore[index]
    packet["packet_content_sha256"] = content_sha256({
        key: value
        for key, value in packet.items()
        if key != "packet_content_sha256"
    })
    with pytest.raises(
        ContractError,
        match="native_ensemble_aifs_candidate_authority_invalid",
    ):
        ingest_forward_packet(_candidate_ingress_connection(), packet)


@pytest.mark.parametrize(
    ("packet_key", "bad_value"),
    [
        ("strict_oos_sample_eligible", True),
        ("can_substitute_for_formal_promotion", True),
    ],
)
def test_aifs_ingress_rejects_forward_packet_claiming_oos_or_promotion(
    monkeypatch: pytest.MonkeyPatch,
    packet_key: str,
    bad_value: object,
) -> None:
    packet = copy.deepcopy(_aifs_packet(monkeypatch))
    packet[packet_key] = bad_value
    packet["packet_content_sha256"] = content_sha256({
        key: value
        for key, value in packet.items()
        if key != "packet_content_sha256"
    })

    with pytest.raises(
        ContractError,
        match="native_ensemble_packet_observational_scope_invalid",
    ):
        ingest_forward_packet(_candidate_ingress_connection(), packet)


def test_aifs_open_meteo_authority_cannot_be_native_arrival_or_formal_p0() -> None:
    authority = {
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

    forward_packet = {
        "source_authority": authority,
        "evidence_role": "observational_candidate_not_oos",
        "oos_event_temporal_scope": "observational_candidate_not_oos",
        "strict_oos_sample_eligible": False,
        "can_substitute_for_formal_promotion": False,
    }
    _require_aggregator_fallback_authority(forward_packet)

    for forbidden_key, forbidden_value in (
        ("native_arrival_index_eligible", True),
        ("formal_p0_eligible", True),
    ):
        forged = {**authority, forbidden_key: forbidden_value}
        with pytest.raises(
            ContractError,
            match="FAIL_CLOSED_NO_P0:aggregator_fallback_authority_invalid",
        ):
            _require_aggregator_fallback_authority({
                **forward_packet,
                "source_authority": forged,
            })

    for forbidden_key, forbidden_value in (
        ("strict_oos_sample_eligible", True),
        ("can_substitute_for_formal_promotion", True),
    ):
        with pytest.raises(
            ContractError,
            match="FAIL_CLOSED_NO_P0:aggregator_fallback_authority_invalid",
        ):
            _require_aggregator_fallback_authority({
                **forward_packet,
                forbidden_key: forbidden_value,
            })


def test_aifs_republisher_oos_report_has_no_formal_native_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even an OOS report built with AIFS registered stays no-live only."""

    import sqlite3

    from bayes_basket_target import native_ensemble_oos as oos

    monkeypatch.setattr(
        oos,
        "native_ensemble_foundation_schema_status",
        lambda _conn: {"ok": True},
    )
    monkeypatch.setattr(
        oos,
        "_load_systems",
        lambda _conn: {
            system_id: {
                "ensemble_system_id": system_id,
                "independence_family_id": "ECMWF",
            }
            for system_id in _AIFS
        },
    )
    monkeypatch.setattr(oos, "_load_cases", lambda *_args, **_kwargs: {})

    report = build_native_ensemble_oos(
        sqlite3.connect(":memory:"),
        station_id="TEST",
        as_of_utc="2026-08-01T00:00:00Z",
    )

    assert report["formal_p0_eligible"] is False
    assert report["cross_candidates"] == []
    assert report["live_submit_authorized"] is False


def test_official_aifs_single_candidate_is_content_bound_and_oos_pending(
    tmp_path: Path,
) -> None:
    """The 45-station native decoder is the ingress source, never a duplicate.

    This guards the hand-off from the retained raw decoder to the model
    ingress audit.  A forged candidate must fail closed even when all aggregate
    counts look complete.
    """
    import scripts.audit_aifs_model_ingress as ingress

    stations = {f"T{index:02d}": "Etc/UTC" for index in range(45)}
    capture_identity = {
        "schema": "bayes_basket_target_aifs_forward_capture_scheduler_v1",
        "state": "CAPTURED_NATIVE_FORWARD_CANDIDATE",
        "provider_id": "ecmwf_aifs025_official",
        "product_id": "ECMWF_AIFS_SINGLE_0P25",
        "independence_family_id": "ECMWF",
        "blocked_count": 0,
        "captured_count": 0,
        "reused_count": 7,
        "station_timezones": stations,
        "fanout": {
            "expected_arrival_count": 179,
            "captured_count": 179,
            "blocked_count": 0,
        },
    }
    capture = {
        **capture_identity,
        "content_sha256": content_sha256(capture_identity),
    }
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(capture, sort_keys=True), encoding="utf-8")
    candidate_identity = {
        "schema": ingress.AIFS_SINGLE_CANDIDATE_SCHEMA,
        "state": "OFFICIAL_NATIVE_AIFS_SINGLE_45_STATION_CANDIDATE_COMPLETE",
        "provider_id": "ecmwf_aifs025_official",
        "product_id": "ECMWF_AIFS_SINGLE_0P25",
        "independence_family_id": "ECMWF",
        "station_timezones": stations,
        "capture_evidence": {
            "path": str(capture_path),
            "sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
            "content_sha256": capture["content_sha256"],
        },
        "checks": {
            "canonical_station_count": 45,
            "station_arrival_count": 179,
            "decoded_point_count": 179,
            "all_station_curves_complete": True,
            "all_raw_objects_official_native": True,
            "all_station_arrivals_prospective": True,
            "raw_objects_reused_without_download": True,
            "open_meteo_aifs_variants_not_upgraded": True,
        },
        "authority": {
            "source_role": "official_native",
            "native_arrival_index_eligible": True,
            "formal_p0_eligible": False,
            "p1_or_hard_no_eligible": False,
        },
        "curves": {"all_complete": True, "complete_curve_count": 45},
    }
    candidate = {
        **candidate_identity,
        "content_sha256": content_sha256(candidate_identity),
    }
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate, sort_keys=True), encoding="utf-8")

    status = ingress._official_single_status(capture_path, candidate_path)

    assert status["evidence_verified"] is True
    assert status["state"] == "OFFICIAL_NATIVE_FORWARD_CAPTURED_OOS_PENDING"
    assert status["decoded_point_count"] == 179
    candidate["authority"]["formal_p0_eligible"] = True
    candidate["content_sha256"] = content_sha256({
        key: value for key, value in candidate.items() if key != "content_sha256"
    })
    candidate_path.write_text(json.dumps(candidate, sort_keys=True), encoding="utf-8")

    assert ingress._official_single_status(capture_path, candidate_path)[
        "evidence_verified"
    ] is False
