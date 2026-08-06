from __future__ import annotations

from pathlib import Path
import json
import sqlite3

import pytest

from bayes_basket_target.canonical_expected_scope_manifest import validate_scope_basis
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.canonical_ssot_v12_migration import (
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.pr11_taf_p0m_basis import (
    EXPECTED_STATION_COUNT,
    PR11TafP0mBasisError,
    REQUIRED_CANONICAL_FACT_TYPES,
    build_pr11_taf_p0m_artifacts,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest


AS_OF = "2026-08-04T12:00:00Z"
ROOT = Path(__file__).resolve().parents[1]
TAF_GAP_EVIDENCE = (
    ROOT
    / "output/audits/taf_p0m_exact_join_45station_20260802_current_p0_inbox.json"
)
PR10_SELECTION_TERMINAL = (
    ROOT
    / "output/audits/pr10_five_model_selection_terminal_20260805T120000Z_v2.json"
)
PR10_P0_BUNDLE = (
    ROOT / "output/audits/pr10_reserved_p0_real_clock_20260805T043225Z_v2.json"
)
PR10_P0_AS_OF = "2026-08-05T12:00:00Z"


def _v12_db(tmp_path: Path) -> Path:
    path = tmp_path / "pr11.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(TARGET_TABLE_DDL)
        for ddl in TARGET_TRIGGER_DDL:
            conn.execute(ddl)
    return path


def test_v12_without_roots_has_45_station_terminals_and_empty_typed_basis(
    tmp_path: Path,
) -> None:
    path = _v12_db(tmp_path)
    before = path.stat()
    expected, terminals, receipt = build_pr11_taf_p0m_artifacts(
        path, as_of_utc=AS_OF
    )
    after = path.stat()

    assert EXPECTED_STATION_COUNT == 45
    assert expected["scope_type"] == "TAF_P0M"
    assert expected["expected_keys"] == []
    assert len(expected["unresolved_input_receipts"]) == 45
    assert terminals["terminal_scope_count"] == 45
    assert receipt["terminal_scope_count"] == 45
    assert receipt["typed_expected_key_count"] == 0
    assert receipt["unresolved_input_receipt_count"] == 45
    assert receipt["state"] == receipt["release_state"] == "BLOCKED"
    assert receipt["blockers"] == ["ACTIVATION_ROOT_MISSING"]
    assert receipt["canonical_p0_activation_root_count"] == 0
    assert receipt["canonical_official_taf_revision_count"] == 0
    assert receipt["canonical_taf_p0m_exact_join_count"] == 0
    assert receipt["terminal_basis_content_sha256"] == terminals["content_sha256"]
    assert receipt["expected_basis_content_sha256"] == expected["content_sha256"]
    assert receipt["writes_database"] is False
    assert receipt["writes_canonical_fact"] is False
    assert receipt["writes_probability_envelope"] is False
    assert receipt["publishes_pr04_pointer"] is False
    assert receipt["starts_services"] is False
    assert receipt["releases_live_pause"] is False
    assert receipt["calls_order_gateway"] is False
    assert receipt["orders_submitted"] == 0
    validate_scope_basis(expected)
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_station_terminal_set_is_exact_frozen_registry(tmp_path: Path) -> None:
    expected, terminals, _receipt = build_pr11_taf_p0m_artifacts(
        _v12_db(tmp_path), as_of_utc=AS_OF
    )
    rows = terminals["terminal_scopes"]
    registry = coordinate_registry_manifest()
    assert [row["station_id"] for row in rows] == registry["station_ids"]
    assert len({row["content_sha256"] for row in rows}) == 45
    assert set(expected["unresolved_input_receipts"]) == {
        row["content_sha256"] for row in rows
    }
    assert all(row["blockers"] == ["ACTIVATION_ROOT_MISSING"] for row in rows)
    assert all(row["typed_scope_emitted"] is False for row in rows)
    assert all(
        row["canonical_registry_sha256"] == registry["registry_sha256"]
        for row in rows
    )
    assert all(
        row["required_canonical_fact_types"] == list(REQUIRED_CANONICAL_FACT_TYPES)
        for row in rows
    )
    assert all(
        row["unproven_artifact_key_fields"]
        == ["settlement_timezone", "season", "lead_bucket", "run_cycle"]
        for row in rows
    )


def test_legacy_taf_and_p0m_tables_do_not_create_typed_keys(tmp_path: Path) -> None:
    path = _v12_db(tmp_path)
    baseline = build_pr11_taf_p0m_artifacts(path, as_of_utc=AS_OF)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE basket_probability_envelope(value TEXT)")
        conn.execute("CREATE TABLE calibration_historical_taf_capture(value TEXT)")
        conn.execute("INSERT INTO basket_probability_envelope VALUES('legacy-p0m')")
        conn.execute("INSERT INTO calibration_historical_taf_capture VALUES('24 rows')")
    current = build_pr11_taf_p0m_artifacts(path, as_of_utc=AS_OF)
    assert current[0]["expected_keys"] == baseline[0]["expected_keys"] == []
    assert current[1]["terminal_scopes"] == baseline[1]["terminal_scopes"]
    assert current[2]["typed_expected_key_count"] == 0


def test_verified_legacy_audit_is_gap_evidence_not_canonical_input(
    tmp_path: Path,
) -> None:
    path = _v12_db(tmp_path)
    before = path.stat()
    expected, terminals, receipt = build_pr11_taf_p0m_artifacts(
        path, as_of_utc=AS_OF, taf_gap_evidence_path=TAF_GAP_EVIDENCE
    )
    after = path.stat()
    assert expected["expected_keys"] == []
    assert receipt["blockers"] == ["ACTIVATION_ROOT_MISSING"]
    assert receipt["legacy_exact_join_sample_count_total"] == 38
    assert receipt["legacy_exact_join_max_sample_count"] == 9
    assert receipt["legacy_exact_join_qualified_scope_count_at_24"] == 0
    rows = {row["station_id"]: row for row in terminals["terminal_scopes"]}
    assert rows["FACT"]["legacy_exact_join_sample_count"] == 9
    assert rows["KBKF"]["legacy_exact_join_sample_count"] == 8
    assert rows["KDAL"]["legacy_exact_join_sample_count"] == 0
    assert rows["KDAL"]["legacy_exact_join_diagnostic_codes"] == [
        "current_p0_scope_missing",
        "exact_join_artifact_missing",
    ]
    assert all(row["legacy_taf_audit_used_as_canonical_input"] is False for row in rows.values())
    assert all(row["typed_scope_emitted"] is False for row in rows.values())
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_non_v12_store_is_not_silently_reinterpreted(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    with sqlite3.connect(path):
        pass
    with pytest.raises(PR11TafP0mBasisError, match="canonical_v12_required"):
        build_pr11_taf_p0m_artifacts(path, as_of_utc=AS_OF)


def test_artifacts_are_deterministic(tmp_path: Path) -> None:
    path = _v12_db(tmp_path)
    assert build_pr11_taf_p0m_artifacts(path, as_of_utc=AS_OF) == (
        build_pr11_taf_p0m_artifacts(path, as_of_utc=AS_OF)
    )


def test_pr10_selection_terminal_binds_only_covered_stations(
    tmp_path: Path,
) -> None:
    _expected, terminals, receipt = build_pr11_taf_p0m_artifacts(
        _v12_db(tmp_path),
        as_of_utc="2026-08-05T12:00:00Z",
        pr10_selection_terminal_path=PR10_SELECTION_TERMINAL,
    )
    assert receipt["blockers"] == [
        "ACTIVATION_ROOT_MISSING",
        "UPSTREAM_PR10_SELECTION_TERMINAL_BLOCKED",
    ]
    assert receipt["upstream_pr10_bound_station_ids"] == ["CYYZ", "NZWN"]
    rows = {row["station_id"]: row for row in terminals["terminal_scopes"]}
    assert len(rows["CYYZ"]["upstream_pr10_station_terminal_content_sha256s"]) == 2
    assert len(rows["NZWN"]["upstream_pr10_station_terminal_content_sha256s"]) == 2
    assert rows["KATL"]["upstream_pr10_station_terminal_content_sha256s"] == []
    assert rows["KATL"]["upstream_pr10_blockers"] == []


def test_pr10_selection_terminal_hash_tamper_fails_closed(
    tmp_path: Path,
) -> None:
    tampered = json.loads(PR10_SELECTION_TERMINAL.read_text(encoding="utf-8"))
    tampered["terminal_count"] = 3
    source = tmp_path / "tampered-pr10-terminal.json"
    source.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(PR11TafP0mBasisError, match="terminal_hash_mismatch"):
        build_pr11_taf_p0m_artifacts(
            _v12_db(tmp_path),
            as_of_utc="2026-08-05T12:00:00Z",
            pr10_selection_terminal_path=source,
        )


def test_pr10_selection_terminal_after_pr11_asof_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(PR11TafP0mBasisError, match="terminal_semantics_invalid"):
        build_pr11_taf_p0m_artifacts(
            _v12_db(tmp_path),
            as_of_utc=AS_OF,
            pr10_selection_terminal_path=PR10_SELECTION_TERMINAL,
        )


def test_pr10_p0_bundle_emits_only_root_proven_taf_keys(tmp_path: Path) -> None:
    path = _v12_db(tmp_path)
    before = path.stat()
    expected, terminals, receipt = build_pr11_taf_p0m_artifacts(
        path,
        as_of_utc=PR10_P0_AS_OF,
        taf_gap_evidence_path=TAF_GAP_EVIDENCE,
        pr10_p0_bundle_path=PR10_P0_BUNDLE,
    )
    after = path.stat()
    assert receipt["canonical_p0_activation_root_count"] == 7
    assert receipt["canonical_p0_activation_root_mapper_ready"] is True
    assert receipt["canonical_taf_p0m_exact_join_count"] == 0
    assert receipt["state"] == receipt["release_state"] == "BLOCKED"
    assert receipt["typed_expected_key_count"] == 7
    keys = {
        (
            item["natural_key"]["station_id"],
            item["natural_key"]["lead_bucket"],
            item["natural_key"]["run_cycle"],
        )
        for item in expected["expected_keys"]
    }
    assert keys == {
        ("OEJN", "d2", "PREVIOUS_DAY2"),
        ("RJTT", "d1", "PREVIOUS_DAY1"),
        ("RJTT", "d2", "PREVIOUS_DAY2"),
        ("RKSI", "d1", "PREVIOUS_DAY1"),
        ("SAEZ", "d1", "PREVIOUS_DAY1"),
        ("SAEZ", "d2", "PREVIOUS_DAY2"),
        ("WSSS", "d1", "PREVIOUS_DAY1"),
    }
    rows = {row["station_id"]: row for row in terminals["terminal_scopes"]}
    for station in {"OEJN", "RJTT", "RKSI", "SAEZ", "WSSS"}:
        assert rows[station]["blockers"] == ["OOS_SAMPLE_SHORT"]
        assert rows[station]["typed_scope_emitted"] is True
        assert rows[station]["canonical_p0_activation_root_mapper_ready"] is True
    assert len(rows["RJTT"]["canonical_p0_activation_root_content_sha256s"]) == 2
    assert len(rows["SAEZ"]["canonical_p0_activation_root_content_sha256s"]) == 2
    assert rows["KATL"]["blockers"] == ["ACTIVATION_ROOT_MISSING"]
    validate_scope_basis(expected)
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_pr10_p0_bundle_hash_tamper_fails_closed(tmp_path: Path) -> None:
    tampered = json.loads(PR10_P0_BUNDLE.read_text(encoding="utf-8"))
    tampered["p0_root_count"] = 8
    source = tmp_path / "tampered-pr10-p0.json"
    source.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(PR11TafP0mBasisError, match="p0_bundle_hash_mismatch"):
        build_pr11_taf_p0m_artifacts(
            _v12_db(tmp_path), as_of_utc=PR10_P0_AS_OF,
            pr10_p0_bundle_path=source,
        )


def test_pr10_p0_bundle_after_pr11_asof_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PR11TafP0mBasisError, match="p0_bundle_semantics_invalid"):
        build_pr11_taf_p0m_artifacts(
            _v12_db(tmp_path), as_of_utc="2026-08-05T04:32:24Z",
            pr10_p0_bundle_path=PR10_P0_BUNDLE,
        )


@pytest.mark.parametrize(
    "parent_field",
    ["selection_content_sha256", "calibrated_distribution_content_sha256"],
)
def test_pr10_p0_bundle_root_parent_references_fail_closed(
    tmp_path: Path, parent_field: str,
) -> None:
    tampered = json.loads(PR10_P0_BUNDLE.read_text(encoding="utf-8"))
    contract = tampered["activation_contracts"][0]
    fact = contract["activation_root_facts"][0]
    fact[parent_field] = "0" * 64
    fact["content_sha256"] = content_sha256({
        key: value for key, value in fact.items() if key != "content_sha256"
    })
    contract["content_sha256"] = content_sha256({
        key: value for key, value in contract.items() if key != "content_sha256"
    })
    tampered["content_sha256"] = content_sha256({
        key: value for key, value in tampered.items() if key != "content_sha256"
    })
    source = tmp_path / f"tampered-{parent_field}.json"
    source.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(PR11TafP0mBasisError, match="p0_bundle_parent_missing"):
        build_pr11_taf_p0m_artifacts(
            _v12_db(tmp_path), as_of_utc=PR10_P0_AS_OF,
            pr10_p0_bundle_path=source,
        )
