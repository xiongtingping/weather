from __future__ import annotations

import sqlite3

from scripts.rebuild_taf_exact_join_quarantine_evidence import (
    _forward_revision_chain_audit,
    _revision_quarantine_receipt,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_forward_taf_capture ("
        "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
        "revision_kind TEXT,revision_id TEXT,revision_sequence INTEGER,"
        "provider_available_at_utc TEXT,issued_at_utc TEXT,received_at_utc TEXT,"
        "valid_from_utc TEXT,valid_until_utc TEXT,source_object_sha256 TEXT,"
        "source_capture_sha256 TEXT,content_sha256 TEXT,source_class TEXT,"
        "authority_scope TEXT)"
    )
    return conn


def test_receipt_exports_persisted_amd_cor_pointers_without_claiming_raw_bytes() -> None:
    conn = _db()
    conn.executemany(
        "INSERT INTO calibration_forward_taf_capture VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "MMMX",
                "2026-07-30",
                "America/Mexico_City",
                "COR",
                "corr-1",
                2,
                "2026-07-30T19:51:03Z",
                "2026-07-30T19:50:00Z",
                "2026-07-30T19:51:03Z",
                "2026-07-30T19:50:00Z",
                "2026-07-31T00:00:00Z",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "official_forward_native_receipt",
                "PRODUCTION_NO_LIVE_ONLY",
            ),
            (
                "ZUUU",
                "2026-07-31",
                "Asia/Shanghai",
                "AMD",
                "amd-1",
                2,
                "2026-07-30T20:50:13Z",
                "2026-07-30T20:49:00Z",
                "2026-07-30T20:50:13Z",
                "2026-07-30T20:49:00Z",
                "2026-08-01T00:00:00Z",
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "official_forward_native_receipt",
                "PRODUCTION_NO_LIVE_ONLY",
            ),
            # A non-production correction must not leak into the formal
            # quarantine receipt.
            (
                "TEST",
                "2026-07-30",
                "Etc/UTC",
                "COR",
                "ignored",
                2,
                "2026-07-30T00:00:00Z",
                "2026-07-30T00:00:00Z",
                "2026-07-30T00:00:00Z",
                "2026-07-30T00:00:00Z",
                "2026-07-31T00:00:00Z",
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "official_forward_native_receipt",
                "CALIBRATION_TRAIN_ONLY",
            ),
        ],
    )
    receipt = _revision_quarantine_receipt(conn, p0_stations={"MMMX"})
    assert receipt["correction_count"] == 2
    assert receipt["station_day_count"] == 2
    by_day = {(row["station_id"], row["event_date"]): row for row in receipt["revision_days"]}
    assert by_day[("MMMX", "2026-07-30")]["current_p0_scope_present"] is True
    assert by_day[("ZUUU", "2026-07-31")]["current_p0_scope_present"] is False
    assert by_day[("ZUUU", "2026-07-31")]["awc_truth_closed"] is False
    assert by_day[("ZUUU", "2026-07-31")]["corrections"][0]["raw_object_status"] == (
        "pointer_only_not_retained"
    )
    assert by_day[("MMMX", "2026-07-30")]["corrections"][0]["quarantine_reason"] == (
        "forward_taf_correction_requires_manual_review"
    )
    assert receipt["forward_revision_chain_audit"]["boundary"]["writes_database"] is False
    conn.close()


def test_duplicate_forward_sequence_is_a_pointer_only_fail_closed_chain() -> None:
    conn = _db()
    base = (
        "LEMD",
        "2026-07-31",
        "Europe/Madrid",
    )
    conn.executemany(
        "INSERT INTO calibration_forward_taf_capture VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                *base,
                "TAF",
                "root-taf",
                1,
                "2026-07-30T17:25:14Z",
                "2026-07-30T17:00:00Z",
                "2026-07-30T17:30:00Z",
                "2026-07-30T17:00:00Z",
                "2026-07-31T18:00:00Z",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "official_forward_native_receipt",
                "PRODUCTION_NO_LIVE_ONLY",
            ),
            (
                *base,
                "AMD",
                "amd-2a",
                2,
                "2026-07-31T02:43:07Z",
                "2026-07-31T02:41:00Z",
                "2026-07-31T02:49:38Z",
                "2026-07-31T02:41:00Z",
                "2026-07-31T18:00:00Z",
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "official_forward_native_receipt",
                "PRODUCTION_NO_LIVE_ONLY",
            ),
            (
                *base,
                "TAF",
                "taf-2b",
                2,
                "2026-07-31T05:25:39Z",
                "2026-07-31T05:00:00Z",
                "2026-07-31T05:39:31Z",
                "2026-07-31T05:00:00Z",
                "2026-07-31T18:00:00Z",
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "official_forward_native_receipt",
                "PRODUCTION_NO_LIVE_ONLY",
            ),
        ],
    )

    audit = _forward_revision_chain_audit(conn)
    row = audit["station_days"][0]

    assert audit["state"] == "AMBIGUOUS_CHAINS_FAIL_CLOSED"
    assert row["state"] == "AMBIGUOUS_FAIL_CLOSED"
    assert row["duplicate_revision_sequences"] == [2]
    assert row["blockers"] == ["forward_revision_sequence_duplicate"]
    assert [item["revision_id"] for item in row["revisions"]] == [
        "root-taf",
        "amd-2a",
        "taf-2b",
    ]
    assert audit["authority"]["duplicate_sequence_policy"] == (
        "fail_closed_no_sequence_inference"
    )
    conn.close()
