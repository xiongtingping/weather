from __future__ import annotations

import json
import sqlite3

from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.materialization import insert_probability_envelope
from bayes_basket_target.regime_materialization import materialize_resident_regimes
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts.audit_bayes_basket_target_regime_resident import (
    MAX_RECEIPT_BYTES,
    audit_regime_resident,
)


def _envelope(*, station_id="RKPK", stage="P0", checkpoint_id=None):
    timezone_name = (
        "Asia/Seoul" if station_id == "RKPK" else "America/New_York"
    )
    return ProbabilityEnvelope.from_mapping(
        {
            "stage": stage,
            "checkpoint_id": checkpoint_id or f"{station_id}-{stage}-1",
            "station_id": station_id,
            "event_date": "2026-07-27",
            "settlement_timezone": timezone_name,
            "decision_at_utc": "2026-07-26T16:00:00Z",
            "fresh_until_utc": "2026-07-27T15:00:00Z",
            "bucket_ordinals": {"36": 36, "37": 37, "38": 38},
            "probabilities": {"36": 0.2, "37": 0.5, "38": 0.3},
            "posterior_version": "strict-oos-v1",
            "posterior_sha256": "2" * 64,
            "bridge_sha256": "3" * 64,
            "lineage": {
                "settlement_timezone": timezone_name,
                "model_envelope_lower_bucket": "35",
                "model_envelope_upper_bucket": "39",
                "model_envelope_span_c": "4",
            },
        }
    )


def test_audit_passes_exact_resident_derivation_and_is_bounded_read_only():
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        envelope = _envelope()
        insert_probability_envelope(
            conn,
            envelope,
            created_at_utc="2026-07-26T16:00:00Z",
        )
        materialize_resident_regimes(
            conn,
            envelopes=[envelope],
            registry_timezones={
                ("RKPK", "2026-07-27"): "Asia/Seoul"
            },
            evaluation_at_utc="2026-07-26T18:00:00Z",
            created_at_utc="2026-07-26T18:00:00Z",
        )
        before = conn.total_changes
        receipt = audit_regime_resident(
            conn,
            evaluation_at_utc="2026-07-26T18:00:00Z",
            registry_timezones={
                ("RKPK", "2026-07-27"): "Asia/Seoul"
            },
        )
        assert receipt["status"] == "PASS"
        assert receipt["station_count"] == 1
        assert receipt["selections"][0]["stage"] == "P0"
        assert conn.total_changes == before
        assert len(
            json.dumps(receipt, sort_keys=True).encode("utf-8")
        ) < MAX_RECEIPT_BYTES


def test_audit_blocks_missing_regime_wrong_timezone_and_stale_high_stage():
    for mode in ("missing_regime", "wrong_timezone", "stale_high"):
        with sqlite3.connect(":memory:") as conn:
            apply_production_ssot_schema(conn)
            p0 = _envelope()
            insert_probability_envelope(
                conn,
                p0,
                created_at_utc="2026-07-26T16:00:00Z",
            )
            if mode == "stale_high":
                stale = ProbabilityEnvelope.from_mapping(
                    {
                        **_envelope(
                            stage="P0-M",
                            checkpoint_id="RKPK-P0-M-stale",
                        ).to_dict(),
                        "decision_at_utc": "2026-07-26T17:00:00Z",
                        "fresh_until_utc": "2026-07-26T17:30:00Z",
                    }
                )
                insert_probability_envelope(
                    conn,
                    stale,
                    created_at_utc="2026-07-26T17:00:00Z",
                )
            receipt = audit_regime_resident(
                conn,
                evaluation_at_utc="2026-07-26T18:00:00Z",
                registry_timezones={
                    ("RKPK", "2026-07-27"): (
                        "America/New_York"
                        if mode == "wrong_timezone"
                        else "Asia/Seoul"
                    )
                },
            )
            assert receipt["status"] == "BLOCKED"
            joined = "|".join(receipt["blockers"])
            expected = {
                "missing_regime": "regime_ssot_not_unique",
                "wrong_timezone": "settlement_timezone_mismatch",
                "stale_high": "latest_stage_stale_fail_closed",
            }[mode]
            assert expected in joined


def test_audit_low_high_low_replay_is_deterministic():
    snapshots = []
    for include_p0m in (False, True, False, True, True):
        with sqlite3.connect(":memory:") as conn:
            apply_production_ssot_schema(conn)
            p0 = _envelope()
            rows = [p0]
            for envelope in rows:
                insert_probability_envelope(
                    conn,
                    envelope,
                    created_at_utc="2026-07-26T16:00:00Z",
                )
            if include_p0m:
                p0m = _envelope(stage="P0-M")
                insert_probability_envelope(
                    conn,
                    p0m,
                    created_at_utc="2026-07-26T16:00:00Z",
                )
                rows.append(p0m)
            materialize_resident_regimes(
                conn,
                envelopes=rows,
                registry_timezones={
                    ("RKPK", "2026-07-27"): "Asia/Seoul"
                },
                evaluation_at_utc="2026-07-26T18:00:00Z",
                created_at_utc="2026-07-26T18:00:00Z",
            )
            receipt = audit_regime_resident(
                conn,
                evaluation_at_utc="2026-07-26T18:00:00Z",
                registry_timezones={
                    ("RKPK", "2026-07-27"): "Asia/Seoul"
                },
            )
            snapshots.append(
                (
                    receipt["status"],
                    receipt["selections"][0]["stage"],
                    receipt["selections"][0]["regime_sha256"],
                )
            )
    assert snapshots[0] == snapshots[2]
    assert snapshots[1] == snapshots[3] == snapshots[4]
