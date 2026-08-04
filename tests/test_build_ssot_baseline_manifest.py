from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.gap_ledger import BuilderScopeStatus, build_gap_ledger
from bayes_basket_target.station_registry import coordinate_registry_manifest
from scripts import build_ssot_baseline_manifest as baseline
from scripts import build_oos_atomic_snapshot_manifest as atomic_snapshot


def _write(path: Path, identity: dict) -> dict:
    value = {**identity, "content_sha256": content_sha256(identity)}
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return value


def _db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE canonical_ssot_fact (
            fact_id TEXT PRIMARY KEY,
            fact_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            fact_json TEXT NOT NULL,
            lineage_json TEXT NOT NULL,
            ingested_at_utc TEXT NOT NULL
        );
        """
    )
    activation = {
        "station_id": "ZUUU",
        "model_family": "global_deterministic",
        "lead_bucket": "d2",
        "run_cycle": "PREVIOUS_DAY2",
        "activation_generation": 1,
    }
    conn.execute(
        "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
        (
            "model:a" * 0 + "model:a",
            "model",
            "ZUUU|ECMWF|d2",
            "a" * 64,
            "{}",
            "{}",
            "2026-08-03T12:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
        (
            "activation:b",
            "activation",
            "ZUUU|global_deterministic|d2|PREVIOUS_DAY2|1",
            "b" * 64,
            json.dumps(activation, sort_keys=True),
            "{}",
            "2026-08-03T12:01:00Z",
        ),
    )
    conn.commit()
    conn.close()


def _inner_oos_snapshot(tmp_path: Path) -> Path:
    station_ids = sorted(
        str(item).strip().upper()
        for item in coordinate_registry_manifest()["station_ids"]
    )
    assert len(station_ids) == 45
    as_of_utc = "2026-08-03T12:10:00Z"
    p0 = tmp_path / "p0.json"
    p0_value = _write(p0, {
        "schema": "bayes_basket_target_p0_inbox_v1",
        "generated_at_utc": as_of_utc,
        "packets": [{"packet": {"station_id": station_id}} for station_id in station_ids],
    })
    taf = tmp_path / "taf.json"
    _write(taf, {
        "schema": "bayes_basket_target_taf_p0m_exact_join_45station_audit_v1",
        "input_artifacts": {"p0_inbox": {
            "content_sha256": p0_value["content_sha256"],
            "generated_at_utc": as_of_utc,
        }},
        "as_of_utc": as_of_utc,
        "entries": [{"station_id": station_id} for station_id in station_ids],
    })
    linkage = tmp_path / "linkage.json"
    _write(linkage, {
        "schema": "native_ensemble_activation_oos_linkage_audit_v1",
        "as_of_utc": as_of_utc,
        "active_exact_oos_consumable_activation_station_ids": station_ids,
    })
    pointer_identity = {
        "schema": "native_ensemble_artifact_pointer_v1",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "selection_evidence_sha256": "a" * 64,
        "cross_ensemble_artifact_id": "native-cross-artifact-test",
        "cross_artifact_content_sha256": "c" * 64,
        "activation_id": "native-activation-test",
        "activation_identity_sha256": "d" * 64,
        "generation": 1,
    }
    pointer = {**pointer_identity, "pointer_sha256": content_sha256(pointer_identity)}
    repair = tmp_path / "repair.json"
    _write(repair, {
        "schema": "native_ensemble_activation_repair_plan_v1",
        "as_of_utc": as_of_utc,
        "canonical_station_ids": station_ids,
        "artifact_pointer_count": len(station_ids),
        "candidate_plans": [{
            "action": "NOOP_ALREADY_CONSUMABLE",
            "candidate_state": "PRODUCTION_NO_LIVE_OOS_PASS",
            "selection_evidence_sha256": "a" * 64,
            "scope": {
                "station_id": station_id,
                "season_bin": "JJA",
                "lead_bucket": "d2",
                "run_cycle": "18Z",
                "run_cycle_by_system_json": "{\"X\":\"18Z\"}",
                "run_cycle_by_system_sha256": content_sha256({"X": "18Z"}),
            },
            "artifact_pointer": pointer,
            "expected_artifact": {
                "cross_ensemble_artifact_id": "native-cross-artifact-test",
                "cross_artifact_content_sha256": "c" * 64,
                "activation_id": "native-activation-test",
                "activation_identity_sha256": "d" * 64,
                "generation": 1,
            },
        } for station_id in station_ids],
    })
    result = atomic_snapshot.build(argparse.Namespace(
        p0_inbox=p0,
        taf_audit=taf,
        native_linkage=linkage,
        native_repair=repair,
        output=tmp_path / "atomic-output.json",
        as_of_utc=as_of_utc,
        max_retries=1,
    ))
    inner = tmp_path / "inner-oos-snapshot.json"
    inner.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    return inner


def _p1_pair(tmp_path: Path) -> tuple[Path, Path]:
    boundary = {
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "writes_canonical_database": False,
        "writes_target_database": False,
    }
    audit = tmp_path / "p1-audit.json"
    audit_value = _write(audit, {
        "schema": "bayes_basket_target_p1_trajectory_45station_audit_v1",
        "as_of_utc": "2026-08-03T12:10:00Z",
        "scope_count": 180,
        "boundary": boundary,
    })
    repair = tmp_path / "p1-repair.json"
    _write(repair, {
        "schema": "bayes_basket_target_p1_trajectory_repair_plan_v1",
        "as_of_utc": "2026-08-03T12:10:00Z",
        "scope_count": 180,
        "audit_content_sha256": audit_value["content_sha256"],
        "boundary": boundary,
    })
    return audit, repair


def _inputs(
    tmp_path: Path,
    *,
    inner_oos_snapshot: Path,
    consistent: bool = True,
) -> tuple[Path, Path, Path, Path, Path]:
    if not (tmp_path / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    db = tmp_path / "target.db"
    _db(db)
    ledger_path = tmp_path / "ledger.json"
    record = {
        "station_id": "ZUUU",
        "model_id": "ECMWF",
        "model_family": "global_deterministic",
        "run_cycle": "PREVIOUS_DAY2",
        "lead_bucket": "d2",
        "event_date": "2026-08-02",
        "evidence_role": "historical_strict_oos",
        "native_arrival_verified": True,
        "raw_receipt_hash_complete": True,
        "awc_truth_closed": True,
        "taf_revision_complete": True,
        "train_count": 20,
        "holdout_count": 8,
        "state": "ACTIVATION_READY",
        "blockers": [],
        "evidence": ["model:" + "a" * 64, "truth:" + "b" * 64],
    }
    ledger = build_gap_ledger(
        [{**record, "content_sha256": content_sha256(record)}],
        as_of_utc="2026-08-03T12:10:00Z",
    )
    ledger_path.write_text(json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8")
    ledger_file_sha = __import__("hashlib").sha256(ledger_path.read_bytes()).hexdigest()
    pointer_path = tmp_path / "pointer.json"
    _write(
        pointer_path,
        {
            "schema": "bayes_basket_target_canonical_gap_ledger_pointer_v1",
            "effective_as_of_utc": "2026-08-03T12:10:00Z",
            "ledger_path": str(ledger_path.resolve()),
            "ledger_content_sha256": ledger["content_sha256"] if consistent else "c" * 64,
            "ledger_file_sha256": ledger_file_sha,
            "record_count": 1,
            "inner_oos_snapshot_content_sha256": json.loads(
                inner_oos_snapshot.read_text(encoding="utf-8")
            )["content_sha256"],
            "production_no_live": True,
            "formal_p0_eligible": False,
            "formal_live_for_republisher": False,
            "future_data_counts_as_oos": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "writes_production_db": False,
        },
    )
    p1_audit, p1_repair = _p1_pair(tmp_path)
    return db, ledger_path, pointer_path, p1_audit, p1_repair


def _rewrite_pointer_for_ledger(pointer_path: Path, ledger_path: Path) -> None:
    value = json.loads(pointer_path.read_text(encoding="utf-8"))
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    value["ledger_content_sha256"] = ledger["content_sha256"]
    value["ledger_file_sha256"] = __import__("hashlib").sha256(
        ledger_path.read_bytes()
    ).hexdigest()
    value["record_count"] = len(ledger["records"])
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    value["content_sha256"] = content_sha256(identity)
    pointer_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def test_build_freezes_read_only_baseline_and_activation_generations(tmp_path: Path) -> None:
    inner = _inner_oos_snapshot(tmp_path)
    db, ledger, pointer, p1_audit, p1_repair = _inputs(
        tmp_path, inner_oos_snapshot=inner
    )
    result = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )
    assert result["state"] == "READY"
    assert result["blockers"] == []
    assert result["database"]["fact_type_counts"] == {"activation": 1, "model": 1}
    lane = result["database"]["activation_generation_summary"]["lanes"][0]
    assert lane["generation_set_station_counts"] == {"1": 1}
    assert result["writes_target_database"] is False
    assert result["orders_submitted"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 2


def test_build_marks_stale_pointer_blocked(tmp_path: Path) -> None:
    inner = _inner_oos_snapshot(tmp_path)
    db, ledger, pointer, p1_audit, p1_repair = _inputs(
        tmp_path, inner_oos_snapshot=inner, consistent=False
    )
    result = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )
    assert result["state"] == "BLOCKED"
    assert "LEDGER_POINTER_CONTENT_SHA256_MISMATCH" in result["blockers"]


def test_build_fails_closed_when_worktree_snapshot_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner = _inner_oos_snapshot(tmp_path)
    db, ledger, pointer, p1_audit, p1_repair = _inputs(
        tmp_path, inner_oos_snapshot=inner
    )
    monkeypatch.setattr(
        baseline,
        "_worktree_status",
        lambda _root: {
            "state": "UNAVAILABLE",
            "reason": "baseline_worktree_hunk_limit_exceeded",
        },
    )

    result = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )

    assert result["state"] == "BLOCKED"
    assert "WORKTREE_SNAPSHOT_UNAVAILABLE" in result["blockers"]
    assert result["worktree"]["state"] == "UNAVAILABLE"


def test_build_rejects_noncanonical_database(tmp_path: Path) -> None:
    inner = _inner_oos_snapshot(tmp_path)
    db = tmp_path / "bad.db"
    sqlite3.connect(db).close()
    ledger = tmp_path / "ledger.json"
    pointer = tmp_path / "pointer.json"
    _write(ledger, {"schema": "ledger", "records": []})
    _write(pointer, {"schema": "pointer", "record_count": 0})
    p1_audit, p1_repair = _p1_pair(tmp_path)
    with pytest.raises(baseline.BaselineError, match="canonical_ssot_fact_missing"):
        baseline.build(
            db_path=db,
            ledger_path=ledger,
            pointer_path=pointer,
            inner_oos_snapshot_path=inner,
            p1_audit_path=p1_audit,
            p1_repair_path=p1_repair,
            as_of_utc="2026-08-03T12:10:00Z",
            worktree_root=tmp_path,
        )


def test_build_blocks_inner_snapshot_after_outer_asof(tmp_path: Path) -> None:
    inner = _inner_oos_snapshot(tmp_path)
    db, ledger, pointer, p1_audit, p1_repair = _inputs(
        tmp_path, inner_oos_snapshot=inner
    )
    result = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:09:59Z",
        worktree_root=tmp_path,
    )
    assert result["state"] == "BLOCKED"
    assert "INNER_OOS_SNAPSHOT_AFTER_OUTER_AS_OF" in result["blockers"]


def test_build_tamper_block_restore_for_inner_taf_p0_binding(tmp_path: Path) -> None:
    inner = _inner_oos_snapshot(tmp_path)
    db, ledger, pointer, p1_audit, p1_repair = _inputs(
        tmp_path, inner_oos_snapshot=inner
    )
    original = inner.read_bytes()
    ready = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )
    tampered = json.loads(original)
    tampered["input_artifacts"]["taf_audit"]["p0_inbox_content_sha256"] = "f" * 64
    identity = {key: item for key, item in tampered.items() if key != "content_sha256"}
    tampered["content_sha256"] = content_sha256(identity)
    inner.write_text(json.dumps(tampered, sort_keys=True) + "\n", encoding="utf-8")
    blocked = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )
    inner.write_bytes(original)
    restored = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )
    assert ready["state"] == "READY"
    assert blocked["state"] == "BLOCKED"
    assert "INNER_OOS_SNAPSHOT_TAF_P0_BINDING_INVALID" in blocked["blockers"]
    assert restored["state"] == "READY"
    assert ready["verification_identity_sha256"] == restored["verification_identity_sha256"]
    assert blocked["verification_identity_sha256"] != ready["verification_identity_sha256"]


def test_build_blocks_ready_record_when_its_builder_scope_is_blocked(tmp_path: Path) -> None:
    inner = _inner_oos_snapshot(tmp_path)
    db, ledger_path, pointer, p1_audit, p1_repair = _inputs(
        tmp_path, inner_oos_snapshot=inner
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    status_identity = {
        "station_id": "ZUUU",
        "lead_bucket": "d2",
        "model_family": "global_deterministic",
        "builder_id": "d2.global_deterministic.historical_strict.v1",
        "state": "BLOCKED",
        "blockers": ["BUILD_ERROR:timezone"],
        "source_pointer": "/tmp/blocked.json",
    }
    status = BuilderScopeStatus.from_mapping({
        **status_identity,
        "content_sha256": content_sha256(status_identity),
    }).to_mapping()
    rebuilt = build_gap_ledger(
        ledger["records"],
        as_of_utc=ledger["as_of_utc"],
        source_pointers=ledger["source_pointers"],
        builder_scope_statuses=[status],
    )
    ledger_path.write_text(json.dumps(rebuilt, sort_keys=True) + "\n", encoding="utf-8")
    _rewrite_pointer_for_ledger(pointer, ledger_path)
    result = baseline.build(
        db_path=db,
        ledger_path=ledger_path,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )
    assert result["state"] == "BLOCKED"
    assert "LEDGER_BUILDER_SCOPE_BLOCKED" in result["blockers"]
    assert "LEDGER_BUILDER_BLOCKER_CONTRADICTS_RECORD_READY" in result["blockers"]


def test_build_uses_inner_cutoff_not_future_db_facts(tmp_path: Path) -> None:
    inner = _inner_oos_snapshot(tmp_path)
    db, ledger, pointer, p1_audit, p1_repair = _inputs(
        tmp_path, inner_oos_snapshot=inner
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            ("future", "model", "future", "c" * 64, "{}", "{}", "2026-08-03T12:10:01Z"),
        )
        conn.commit()
    result = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )
    assert result["state"] == "READY"
    assert result["database"]["fact_rows_after_cutoff"] == 1
    assert result["database"]["fact_type_counts"] == {"activation": 1, "model": 1}
    assert result["database"]["canonical_projection_row_count"] == 2
    assert len(result["database"]["canonical_projection_sha256"]) == 64


def test_build_blocks_unrelated_builder_scope_status(tmp_path: Path) -> None:
    inner = _inner_oos_snapshot(tmp_path)
    db, ledger_path, pointer, p1_audit, p1_repair = _inputs(
        tmp_path, inner_oos_snapshot=inner
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    status_identity = {
        "station_id": "KSEA",
        "lead_bucket": "d1",
        "model_family": "global_deterministic",
        "builder_id": "d1.global_deterministic.historical_strict.v1",
        "state": "BLOCKED",
        "blockers": ["BUILD_ERROR:truth"],
        "source_pointer": "/tmp/blocked-unrelated.json",
    }
    status = BuilderScopeStatus.from_mapping({
        **status_identity,
        "content_sha256": content_sha256(status_identity),
    }).to_mapping()
    rebuilt = build_gap_ledger(
        ledger["records"],
        as_of_utc=ledger["as_of_utc"],
        source_pointers=ledger["source_pointers"],
        builder_scope_statuses=[status],
    )
    ledger_path.write_text(json.dumps(rebuilt, sort_keys=True) + "\n", encoding="utf-8")
    _rewrite_pointer_for_ledger(pointer, ledger_path)

    result = baseline.build(
        db_path=db,
        ledger_path=ledger_path,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )

    assert result["state"] == "BLOCKED"
    assert "LEDGER_BUILDER_SCOPE_BLOCKED" in result["blockers"]
    assert "LEDGER_BUILDER_BLOCKER_CONTRADICTS_RECORD_READY" not in result["blockers"]


def test_build_p1_pair_tamper_block_restore(tmp_path: Path) -> None:
    inner = _inner_oos_snapshot(tmp_path)
    db, ledger, pointer, p1_audit, p1_repair = _inputs(
        tmp_path, inner_oos_snapshot=inner
    )
    original = p1_repair.read_bytes()
    ready = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )
    tampered = json.loads(original)
    tampered["audit_content_sha256"] = "f" * 64
    tampered_identity = {
        key: value for key, value in tampered.items() if key != "content_sha256"
    }
    tampered["content_sha256"] = content_sha256(tampered_identity)
    p1_repair.write_text(json.dumps(tampered, sort_keys=True) + "\n", encoding="utf-8")
    blocked = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )
    p1_repair.write_bytes(original)
    restored = baseline.build(
        db_path=db,
        ledger_path=ledger,
        pointer_path=pointer,
        inner_oos_snapshot_path=inner,
        p1_audit_path=p1_audit,
        p1_repair_path=p1_repair,
        as_of_utc="2026-08-03T12:10:00Z",
        worktree_root=tmp_path,
    )

    assert ready["state"] == "READY"
    assert blocked["state"] == "BLOCKED"
    assert "P1_EVIDENCE_PAIR_AUDIT_HASH_MISMATCH" in blocked["blockers"]
    assert restored["state"] == "READY"
    assert ready["verification_identity_sha256"] == restored["verification_identity_sha256"]


def test_worktree_snapshot_captures_preexisting_hunks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    tracked.write_text("first\nsecond\n", encoding="utf-8")

    result = baseline._worktree_status(repo)

    assert result["state"] == "CAPTURED"
    assert result["owner"] == "PREEXISTING_USER_WORKTREE"
    assert result["dirty_path_count"] == 1
    assert result["hunk_count"] >= 1
    assert len(result["paths_sha256"]) == 64
    assert result["paths"][0]["path"] == "tracked.txt"
    assert result["paths"][0]["owner"] == "PREEXISTING_USER_WORKTREE"
    assert {item.split(":", 1)[0] for item in result["paths"][0]["hunks"]} == {
        "index",
        "worktree",
    }
