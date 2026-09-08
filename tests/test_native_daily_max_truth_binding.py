from __future__ import annotations

from copy import deepcopy
import json
import sqlite3

import pytest

from bayes_basket_target.awc_observation_contract import (
    AUTHORITY_SCOPE,
    COVERAGE_RECEIPT_SCHEMA,
)
from bayes_basket_target.canonical_ssot_v15_migration import (
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.model_product_identity import (
    build_registered_model_product_identity_receipt,
)
from bayes_basket_target.native_daily_max_manifest import (
    build_native_daily_max_manifest,
    expected_et_hourly_slots,
)
from bayes_basket_target.native_daily_max_truth_binding import (
    build_native_daily_max_truth_binding_bundle,
)
from bayes_basket_target.model_product_identity import (
    NATIVE_PRODUCT_IDS,
)

# This whole file exercises the native daily-max lane, and that lane cannot
# run: every fixture here builds a registered product identity receipt, and
# the receipt builder serves the native-decode ingress alone, which has no
# members.  See ``test_native_decode_ingress_is_unpopulated`` for the fact
# and the evidence -- both halves arrived in the 2026-08-06 bulk import
# naming products the registry has never held, and nothing in the resident
# cycle reaches them.
#
# Skipped at module level rather than deleted: the lane's contract tests are
# the specification of what it would have to satisfy if a native-decode
# product is ever registered, and the skip reason is what says why they are
# not running.  It goes away by itself the day that ingress gains a member.
if not NATIVE_PRODUCT_IDS:
    import pytest as _pytest

    _pytest.skip(
        "native decode ingress registers no product; see"
        " tests/test_native_decode_ingress_is_unpopulated.py",
        allow_module_level=True,
    )

from bayes_basket_target.truth_authority_supersession import (
    REVIEW_AUDIT_SCHEMA,
    build_canonical_truth_authority_supersession_contract,
)
from tests.pr12_authority_projection_test_support import authority_projection
from tests.test_awc_observation_contract import _bounds as _awc_bounds
from tests.test_awc_observation_contract import _revision as _awc_revision


def _seal(value: dict) -> dict:
    return {**value, "content_sha256": content_sha256(value)}


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
    conn.execute(TARGET_TABLE_DDL)
    for ddl in TARGET_TRIGGER_DDL:
        conn.execute(ddl)
    return conn


def _manifest(*, decision_at: str = "2026-08-01T01:02:00Z") -> dict:
    identity = build_registered_model_product_identity_receipt(
        product_id="DWD_ICON_GLOBAL", station_id="KATL", lead_bucket="d2",
        run_cycle="18Z", identity_source_receipt_sha256="a" * 64,
    )
    points = [
        {
            "valid_time_utc": slot,
            "forecast_temperature_c": 20.0 + index,
            "published_at_utc": "2026-07-25T01:00:00Z",
            "received_at_utc": "2026-07-25T01:01:00Z",
            "decision_at_utc": decision_at,
            "raw_object_sha256": f"{index + 1:064x}",
            "transport_receipt_sha256": f"{index + 101:064x}",
            "decoder_id": "test.icon.hourly.v1",
        }
        for index, slot in enumerate(expected_et_hourly_slots("2026-08-01"))
    ]
    return build_native_daily_max_manifest(
        identity_receipt=identity, event_date="2026-08-01",
        run_init_utc="2026-07-31T18:00:00Z", points=points,
    )


def _bounds(*, timezone: str = "America/New_York", version: int = 1,
            committed_at: str = "2026-07-26T04:05:00.000Z") -> dict:
    slots = expected_et_hourly_slots("2026-07-25")
    coverage = _seal({
        "schema": COVERAGE_RECEIPT_SCHEMA,
        "station_id": "KATL",
        "event_date": "2026-07-25",
        "settlement_timezone": timezone,
        "as_of_utc": committed_at,
        "expected_hourly_slots": slots,
        "observed_hourly_slots": slots,
        "expected_hourly_metar_count": len(slots),
        "observed_hourly_metar_count": len(slots),
        "coverage_complete": True,
        "source_evidence_receipt_sha256": "9" * 64,
    })
    return _seal({
        "station_id": "KATL",
        "event_date": "2026-07-25",
        "settlement_timezone": timezone,
        "bounds_version": version,
        "source_lane": "AWC",
        "authority_scope_projection_content_sha256": authority_projection()[
            "content_sha256"
        ],
        "day_start_utc": "2026-07-25T04:00:00.000Z",
        "day_end_utc": "2026-07-26T04:00:00.000Z",
        "running_max_c": 34.0,
        "max_observation_fact_sha256": "b" * 64,
        "observation_revision_vector_sha256": "c" * 64,
        "max_tie_vector_sha256": "d" * 64,
        "max_tie_break_rule": "EARLIEST_SEQUENCE_THEN_FACT_SHA",
        "coverage_receipt": coverage,
        "coverage_receipt_sha256": coverage["content_sha256"],
        "correction_state": "NO_CORRECTION",
        "halt_state": "CLEAR",
        "latest_review_receipt_sha256": None,
        "closed_day": True,
        "canonical_committed_at_utc": committed_at,
        "authority_scope": AUTHORITY_SCOPE,
    })


def _legacy(value: dict) -> dict:
    result = deepcopy(value)
    result.pop("authority_scope_projection_content_sha256", None)
    result.pop("content_sha256", None)
    return {**result, "content_sha256": content_sha256(result)}


def _insert_effective_truth(conn: sqlite3.Connection) -> dict:
    revision = _legacy(_awc_revision())
    bounds = _legacy(_awc_bounds(revision))
    conn.executemany(
        "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
        (
            (
                "legacy-revision", "truth_observation_revision", "legacy-revision",
                revision["content_sha256"], json.dumps(revision), "{}",
                revision["canonical_committed_at_utc"],
            ),
            (
                "legacy-bounds", "truth_observation_day_bounds", "legacy-bounds",
                bounds["content_sha256"], json.dumps(bounds), "{}",
                bounds["canonical_committed_at_utc"],
            ),
        ),
    )
    conn.commit()
    audit_identity = {
        "schema": REVIEW_AUDIT_SCHEMA,
        "as_of_utc": authority_projection()["as_of_utc"],
        "authority_scope_projection_content_sha256": authority_projection()[
            "content_sha256"
        ],
        "legacy_scope_count": 1,
        "review_scope_blocker_count": 0,
        "review_scope_blockers": [],
    }
    audit = {**audit_identity, "content_sha256": content_sha256(audit_identity)}
    contract = build_canonical_truth_authority_supersession_contract(
        conn, authority_scope_projection=authority_projection(),
        review_scope_audit=audit,
    )
    ingest_canonical_contract(conn, contract, commit=True)
    return contract["awc_observation_contract"]["day_bounds_facts"][0]


def test_missing_real_parent_shape_yields_one_terminal_without_writes() -> None:
    conn = _database()
    bundle = build_native_daily_max_truth_binding_bundle(
        conn, manifests=[_manifest()], authority_scope_projection=authority_projection(),
        as_of_utc="2026-08-05T12:00:00Z",
    )
    assert bundle["scope_count"] == 1
    assert bundle["binding_count"] == 0
    assert bundle["terminal_count"] == 1
    assert bundle["terminals"][0]["diagnostic"] == "TRUTH_AUTHORITY_SUPERSESSION_REQUIRED"
    assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0


def test_exact_closed_root_yields_unpublished_non_consumable_binding() -> None:
    conn = _database()
    bounds = _insert_effective_truth(conn)
    bundle = build_native_daily_max_truth_binding_bundle(
        conn, manifests=[_manifest()], authority_scope_projection=authority_projection(),
        as_of_utc="2026-08-05T12:00:00Z",
    )
    assert bundle["binding_count"] == 1
    assert bundle["terminal_count"] == 0
    assert bundle["bindings"][0]["truth_day_bounds_content_sha256"] == bounds["content_sha256"]
    assert bundle["bindings"][0]["state"] == "MATERIALIZED_UNPUBLISHED"
    assert bundle["bindings"][0]["consumable"] is False


def test_effective_root_resolver_returns_only_the_v2_head() -> None:
    conn = _database()
    _insert_effective_truth(conn)
    bundle = build_native_daily_max_truth_binding_bundle(
        conn, manifests=[_manifest()], authority_scope_projection=authority_projection(),
        as_of_utc="2026-08-05T12:00:00Z",
    )
    assert bundle["binding_count"] == 1


def test_wrong_timezone_is_not_cross_scope_fallback() -> None:
    conn = _database()
    _insert_effective_truth(conn)
    manifest = _manifest()
    manifest["settlement_timezone"] = "UTC"
    manifest["content_sha256"] = content_sha256({
        key: value for key, value in manifest.items() if key != "content_sha256"
    })
    with pytest.raises(ContractError, match="manifest_rebuild_mismatch"):
        build_native_daily_max_truth_binding_bundle(
            conn, manifests=[manifest], authority_scope_projection=authority_projection(),
            as_of_utc="2026-08-05T12:00:00Z",
        )


@pytest.mark.parametrize(
    ("manifest", "as_of", "match"),
    (
        (_manifest(), "2026-08-02T04:04:00Z", "PROJECTION_MISMATCH"),
        (_manifest(decision_at="2026-08-02T05:00:00Z"), "2026-08-05T12:00:00Z", "truth_root_not_strict_oos"),
    ),
)
def test_truth_after_asof_or_decision_after_day_end_fails_closed(
    manifest: dict, as_of: str, match: str,
) -> None:
    conn = _database()
    _insert_effective_truth(conn)
    with pytest.raises(ContractError, match=match):
        build_native_daily_max_truth_binding_bundle(
            conn, manifests=[manifest], authority_scope_projection=authority_projection(),
            as_of_utc=as_of,
        )


def test_manifest_identity_or_point_tamper_is_rejected() -> None:
    conn = _database()
    for field in ("model_id", "forecast_value_c"):
        tampered = deepcopy(_manifest())
        tampered[field] = "GFS" if field == "model_id" else 999.0
        tampered["content_sha256"] = content_sha256({
            key: value for key, value in tampered.items() if key != "content_sha256"
        })
        with pytest.raises(ContractError, match="manifest_rebuild_mismatch"):
            build_native_daily_max_truth_binding_bundle(
                conn, manifests=[tampered], authority_scope_projection=authority_projection(),
                as_of_utc="2026-08-05T12:00:00Z",
            )
