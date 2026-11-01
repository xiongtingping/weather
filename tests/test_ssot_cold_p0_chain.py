"""Charter 237 amendment (2026-11-01, user 「Do P0 tables」): the P0 certification chain.

The activations, their cross artifacts and the selection evidence had no rule
and grew ~0.5 GiB a day.  These tests pin what stays: every activation valid
at a decision being made (and a week past its end), the newest of each
generation key, the activation of every artifact a hot OOS prediction names,
and every evidence row a hot artifact names -- and that an artifact leaves
with its activation.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos, serving_arithmetic, ssot  # noqa: E402
from scripts import ssot_cold_offload as off  # noqa: E402

AS_OF = datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)   # activations ending before 09-20 leave
ACTIVATION = "calibration_native_ensemble_activation"
ARTIFACT = "calibration_cross_native_ensemble_artifact"
EVIDENCE = "calibration_cross_native_ensemble_selection_evidence"
PREDICTION = "calibration_cross_native_ensemble_oos_prediction"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _quiet_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        serving_arithmetic, "watchdog",
        lambda _conn, *, stage: {"discrepancy": False, "acknowledged": None, "fingerprint_since_utc": None},
    )


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "ssot.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    ssot.apply_production_ssot_schema(conn)
    serving_arithmetic.ensure_schema(conn)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def cold(tmp_path: Path) -> Path:
    root = tmp_path / "volume" / "weather_cold"
    root.mkdir(parents=True)
    return root


def _insert(conn, table: str, values: dict) -> None:
    conn.execute(
        f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' * len(values))})",
        tuple(values.values()),
    )


def _evidence(conn, eid: str, train_end: str) -> str:
    sha = _sha("ev_" + eid)
    _insert(conn, EVIDENCE, {
        "selection_evidence_id": eid, "station_id": "KLGA", "season_bin": "PEAK", "lead_bucket": "h24_48",
        "run_cycle": "00Z", "settlement_definition": "def", "method_version": "v", "candidate_system_ids_json": "[]",
        "selected_system_ids_json": "[]", "selected_system_count": 2, "independent_family_count": 2,
        "selected_weights_json": "{}", "family_weights_json": "{}", "family_weight_caps_json": "{}",
        "supported_exact_masks_json": "[]", "train_start_date": "2026-01-01", "train_end_date": train_end,
        "holdout_start_date": "2026-12-01", "holdout_end_date": "2026-12-08", "rolling_origin_fold_count": 1,
        "effective_sample_n": 10.0, "oos_metrics_json": "{}", "cross_system_residual_correlation_json": "{}",
        "deterministic_baseline_metrics_json": "{}", "content_sha256": sha,
    })
    return sha


def _artifact(conn, aid: str, evidence_sha: str, until: str) -> None:
    _insert(conn, ARTIFACT, {
        "cross_ensemble_artifact_id": aid, "station_id": "KLGA", "season_bin": "PEAK", "lead_bucket": "h24_48",
        "run_cycle": "00Z", "settlement_definition": "def", "ensemble_system_ids_json": "[]",
        "ensemble_system_count": 2, "independent_family_count": 2, "supported_exact_masks_json": "[]",
        "selection_evidence_sha256": evidence_sha, "combination_method": "m", "family_weight_caps_json": "{}",
        "probability_pathway": "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE",
        "valid_from_utc": "2026-07-01T00:00:00Z", "valid_until_utc": until, "content_sha256": _sha("art_" + aid),
    })


def _activation(conn, act: str, aid: str, generation: int, until: str, *, kind: str = "CROSS_NATIVE_ENSEMBLE",
                set_hash: str = "h1") -> None:
    _insert(conn, ACTIVATION, {
        "activation_id": act, "artifact_kind": kind, "artifact_id": aid, "station_id": "KLGA",
        "ensemble_system_id_or_star": "*", "season_bin": "PEAK", "lead_bucket": "h24_48", "run_cycle": "00Z",
        "settlement_definition": "def", "available_system_set_hash_or_star": set_hash, "generation": generation,
        "valid_from_utc": "2026-07-01T00:00:00Z", "valid_until_utc": until,
        "activated_at_utc": "2026-07-01T00:00:00Z", "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "content_sha256": _sha("act_" + act),
    })


def _prediction(conn, pid: str, aid: str) -> None:
    _insert(conn, PREDICTION, {
        "prediction_id": pid, "cross_ensemble_artifact_id": aid, "selection_evidence_sha256": _sha("x"),
        "station_id": "KLGA", "event_date": "2026-08-10", "settlement_timezone": "UTC", "season_bin": "PEAK",
        "lead_bucket": "h24_48", "run_cycle": "00Z", "settlement_definition": "def",
        "trained_through_event_date": "2026-08-01", "decision_at_utc": "2026-08-08T00:00:00Z",
        "ensemble_system_ids_json": "[]", "independence_family_ids_json": "[]", "system_family_ids_json": "[]",
        "system_weights_json": "{}", "family_weight_caps_json": "{}", "system_member_set_shas_json": "{}",
        "probabilities_json": "{}", "bucket_centers_c_json": "[]", "prediction_oos": 1,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY", "content_sha256": _sha("pred_" + pid),
    })


def _fixture(store: Path) -> None:
    conn = sqlite3.connect(store)
    shared = _evidence(conn, "ev_shared", "2026-07-10")
    for n, (until, gen) in enumerate((
        ("2026-08-01T00:00:00Z", 1),   # long expired: leaves
        ("2026-08-20T00:00:00Z", 2),   # expired: leaves
        ("2026-09-01T00:00:00Z", 3),   # expired, but an OOS prediction names its artifact
        ("2026-09-25T00:00:00Z", 4),   # expired inside the margin: stays
        ("2026-10-20T00:00:00Z", 5),   # valid: stays
    ), start=1):
        _artifact(conn, f"art_{n}", shared if n == 2 else _evidence(conn, f"ev_{n}", "2026-07-0" + str(n)),
                  until)
        _activation(conn, f"act_{n}", f"art_{n}", gen, until)
    # Shared evidence: art_2 leaves, art_6 (hot) still names it.
    _artifact(conn, "art_6", shared, "2026-10-21T00:00:00Z")
    _activation(conn, "act_6", "art_6", 6, "2026-10-21T00:00:00Z")
    # A scope that stopped minting: its newest generation stays however old.
    _artifact(conn, "art_7", _evidence(conn, "ev_7", "2026-07-07"), "2026-07-20T00:00:00Z")
    _activation(conn, "act_7", "art_7", 1, "2026-07-20T00:00:00Z", set_hash="h2")
    # A system calibration activation: no cross artifact behind it.
    _activation(conn, "sys_old", "sysart_1", 1, "2026-08-01T00:00:00Z", kind="NATIVE_ENSEMBLE_SYSTEM_CALIBRATION")
    _activation(conn, "sys_new", "sysart_2", 2, "2026-10-01T00:00:00Z", kind="NATIVE_ENSEMBLE_SYSTEM_CALIBRATION")
    _prediction(conn, "pred_3", "art_3")
    conn.commit()
    conn.close()


def _column(path: Path, table: str, column: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {str(r[0]) for r in conn.execute(f"SELECT {column} FROM {table}")}
    finally:
        conn.close()


def _run(store: Path, cold: Path, tmp_path: Path, tables: list[str], policies=None, **kwargs):
    return off.run(
        ssot_path=store, cold_root=cold, as_of=AS_OF, local_reference=None, min_cold_free_bytes=0,
        manifest=tmp_path / "manifest.jsonl", holds_path=tmp_path / "holds.json",
        sleep=lambda _seconds: None, tables=tables,
        policies=policies or [off.POLICY_BY_TABLE[t] for t in tables], **kwargs,
    )


def test_expired_activations_leave_with_their_artifacts_and_the_protected_ones_stay(
    store: Path, cold: Path, tmp_path: Path,
) -> None:
    _fixture(store)
    receipt = _run(store, cold, tmp_path, [ACTIVATION], apply=True)

    (summary,) = receipt["tables"]
    assert summary["cutoff_before"] == "2026-09-20"
    assert summary["kept"] == {
        "named:oos_prediction_artifact": 1,
        # The newest expired row of each key (act_7's stopped scope, sys_old).
        "latest_0:artifact_kind+station_id+ensemble_system_id_or_star+season_bin+lead_bucket+run_cycle"
        "+available_system_set_hash_or_star": 2,
    }
    assert _column(store, ACTIVATION, "activation_id") == {
        "act_3", "act_4", "act_5", "act_6", "act_7", "sys_old", "sys_new",
    }
    assert _column(off.cold_path(cold, ACTIVATION), ACTIVATION, "activation_id") == {"act_1", "act_2"}
    assert _column(store, ARTIFACT, "cross_ensemble_artifact_id") == {"art_3", "art_4", "art_5", "art_6", "art_7"}
    assert _column(off.cold_path(cold, ARTIFACT), ARTIFACT, "cross_ensemble_artifact_id") == {"art_1", "art_2"}
    (move,) = receipt["moves"]
    assert move["moved_rows"] == 2 and move["child_rows"] == {ARTIFACT: 2}


def test_evidence_leaves_only_when_no_hot_artifact_names_it(store: Path, cold: Path, tmp_path: Path) -> None:
    _fixture(store)
    _run(store, cold, tmp_path, [ACTIVATION], apply=True)
    receipt = _run(store, cold, tmp_path, [EVIDENCE], apply=True)

    (summary,) = receipt["tables"]
    # Every evidence row is older than the window; art_3..art_7 still name theirs.
    assert summary["kept"] == {"named:hot_artifact": 5}
    assert _column(off.cold_path(cold, EVIDENCE), EVIDENCE, "selection_evidence_id") == {"ev_1"}
    assert "ev_shared" in _column(store, EVIDENCE, "selection_evidence_id")   # art_6 names it


def test_an_artifact_a_prediction_still_names_stops_the_table_instead_of_deleting(
    store: Path, cold: Path, tmp_path: Path,
) -> None:
    _fixture(store)
    conn = sqlite3.connect(store)
    _prediction(conn, "pred_2", "art_2")
    conn.commit()
    conn.close()
    unguarded = dataclasses.replace(off.POLICY_BY_TABLE[ACTIVATION], keep_named=())
    receipt = _run(store, cold, tmp_path, [ACTIVATION], policies=[unguarded], apply=True)
    (move,) = receipt["moves"]
    assert move["state"] == "stopped"
    assert move["reason"] == f"ssot_child_named_by_foreign_key:{ARTIFACT}"
    # The batch holding art_2 rolled back whole.
    assert {"art_1", "art_2"} <= _column(store, ARTIFACT, "cross_ensemble_artifact_id")
    assert {"act_1", "act_2"} <= _column(store, ACTIVATION, "activation_id")


def test_the_windows_are_tied_to_the_validity_and_the_generation_key(store: Path) -> None:
    policy = off.POLICY_BY_TABLE[ACTIVATION]
    assert native_ensemble_oos.DEFAULT_VALID_DAYS == off.ACTIVATION_VALID_DAYS == 30
    assert policy.time_column == "valid_until_utc" and policy.hot_days == off.ACTIVATION_EXPIRED_MARGIN_DAYS
    conn = sqlite3.connect(store)
    try:
        unique = [
            [str(info[2]) for info in conn.execute(f"PRAGMA index_info('{name}')")]
            for (_seq, name, is_unique, *_rest) in conn.execute(f"PRAGMA index_list('{ACTIVATION}')")
            if is_unique and "generation" in [str(i[2]) for i in conn.execute(f"PRAGMA index_info('{name}')")]
        ]
    finally:
        conn.close()
    # The newest of the table's own generation key stays: every writer's
    # MAX(generation) scope is that key or coarser.
    assert unique == [list(policy.keep_latest[0].key) + ["generation"]]
    assert off.POLICY_BY_TABLE[EVIDENCE].hot_days > off.ACTIVATION_VALID_DAYS + off.ACTIVATION_EXPIRED_MARGIN_DAYS
    facts = (ROOT / "bayes_basket_target" / "native_ensemble_training_facts.py").read_text(encoding="utf-8")
    assert "AND a.artifact_id=p.cross_ensemble_artifact_id " in facts
    assert ARTIFACT not in off.POLICY_BY_TABLE   # it leaves with its activation, never on its own
