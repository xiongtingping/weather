"""Adversarial storage contracts for the target forward-observation packet.

This sidecar test module is intentionally limited to storage boundaries.  It
does not touch the production line, gateway, services, or any other test.

Some tests are deliberately red against the current API.  They document the
required contract for the next implementation pass:

* output symlinks must fail closed before the target is overwritten;
* the writer must expose and enforce a target-runtime-root boundary;
* concurrent writers must merge/reconcile rather than lose a packet;
* a full packet must roll forward in bounded, auditable form rather than
  permanently blocking all later observations;
* all budgets must use UTF-8 bytes, not Python character counts.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

import bayes_basket_target.cash_phase_forward_observation as storage
from bayes_basket_target.cash_phase_forward_observation import (
    ForwardObservationError,
    append_settlement_labels,
    build_forward_observation_packet,
    write_forward_observation_packet,
)
from bayes_basket_target.contracts import canonical_json, content_sha256
from bayes_basket_target.forward_observation_store import (
    MAX_BATCH_ROWS,
    ForwardObservationStoreError,
    append_forward_settlement_labels,
    commit_forward_observation_batch,
    initialize_forward_observation_store,
    quarantine_forward_observation,
    repair_false_quarantine_batch,
    read_forward_store_state,
    read_forward_observation_packet,
    tombstone_forward_observations,
)
from bayes_basket_target.resource_bounds import ResourceBoundError
from bayes_basket_target.settlement_truth import (
    CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1,
)


def _inputs(decision_at: str = "2026-07-26T21:08:23.560Z") -> tuple[dict, dict]:
    binding = {
        "schema": "bayes_basket_target_forward_evidence_binding_v1",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": decision_at,
        "probability_stage": "P1",
        "probability_decision_at_utc": "2026-07-26T21:00:00Z",
        "probability_artifact_sha256": "3" * 64,
        "probability_lineage_sha256": "4" * 64,
        "ws_quote_observed_at_utc": "2026-07-26T21:08:20Z",
        "ws_quote_expires_at_utc": "2026-07-26T21:09:20Z",
        "ws_snapshot_sha256": "2" * 64,
        "ws_quote_count": 1,
        "inventory_sha256": "8" * 64,
        "order_state_sha256": "9" * 64,
        "cash_phase": "STANDARD",
        "cash_checkpoint_id": "cash-rkpk-1",
        "cash_observed_at_utc": "2026-07-26T21:08:20Z",
        "cash_decision_sha256": "5" * 64,
        "cash_policy_sha256": "6" * 64,
        "station_policy_sha256": "a" * 64,
        "delta_v2_artifact_sha256": "b" * 64,
        "delta_v2_evaluation_sha256": "e" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
    }
    binding["content_sha256"] = content_sha256(binding)
    cycle = {
        "schema": "bayes_basket_target_atomic_forward_cycle_receipt_v1",
        "cycle_id": "cycle-rkpk-1-" + decision_at.replace(":", ""),
        "decision_id": "decision-rkpk-1-" + decision_at.replace(":", ""),
        "plan_id": "plan-rkpk-1",
        "plan_sha256": "c" * 64,
        "decision_sha256": "d" * 64,
        "decision_at_utc": decision_at,
        "forward_evidence_binding": binding,
        "execution_mode": "production_no_live",
        "writes_target_ledger": True,
        "orders_submitted": 0,
        "places_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "authorizes_order": False,
        "evidence_only": True,
    }
    cycle["receipt_sha256"] = content_sha256(cycle)
    registry = {
        "rows": [{
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
        }]
    }
    return cycle, registry


def _packet(decision_at: str = "2026-07-26T21:08:23.560Z") -> dict:
    cycle, registry = _inputs(decision_at)
    return build_forward_observation_packet(
        cycle_receipt=cycle,
        registry=registry,
    )


def _rehash_row(row: dict) -> None:
    row["observation_sha256"] = content_sha256({
        key: value
        for key, value in row.items()
        if key != "observation_sha256"
    })


def _rehash_packet(packet: dict) -> None:
    packet["content_sha256"] = content_sha256({
        key: value
        for key, value in packet.items()
        if key != "content_sha256"
    })


def test_output_symlink_is_rejected_before_target_mutation(tmp_path: Path) -> None:
    """A path supplied as output must never be resolved through a symlink."""

    target = tmp_path / "real-target.json"
    target.write_bytes(b"sentinel")
    output = tmp_path / "forward-observations.json"
    output.symlink_to(target)

    with pytest.raises(ForwardObservationError, match="symlink"):
        write_forward_observation_packet(output, _packet())

    assert target.read_bytes() == b"sentinel"


def test_writer_enforces_allowed_target_runtime_root_and_rejects_escape(
    tmp_path: Path,
) -> None:
    """The storage API must make the allowed target runtime root explicit.

    The current API calls the boundary ``allowed_root``.  The test deliberately
    checks the API by name so a future rename cannot silently remove the gate.
    """

    parameters = inspect.signature(write_forward_observation_packet).parameters
    assert "allowed_root" in parameters, (
        "expected API missing: write_forward_observation_packet must accept "
        "allowed_root"
    )

    root = tmp_path / "target-runtime"
    inside = root / "forward-observations.json"
    outside = tmp_path / "escape.json"
    packet = _packet()

    write_forward_observation_packet(
        inside,
        packet,
        allowed_root=root,
    )
    with pytest.raises(ForwardObservationError, match="root|boundary|scope|path"):
        write_forward_observation_packet(
            outside,
            packet,
            allowed_root=root,
        )


def test_two_concurrent_writers_do_not_lose_distinct_observations(
    tmp_path: Path,
) -> None:
    """Two independent packets must reconcile to one auditable packet."""

    output = tmp_path / "forward-observations.json"
    packets = [
        _packet("2026-07-26T21:08:23.560Z"),
        _packet("2026-07-26T21:08:24.560Z"),
    ]
    barrier = Barrier(2)

    def write(packet: dict) -> bool:
        barrier.wait(timeout=5)
        return write_forward_observation_packet(output, packet)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, packet) for packet in packets]
        # Calling result() also makes a temp-file race or writer exception
        # fail this test instead of being silently discarded by the executor.
        [future.result(timeout=10) for future in futures]

    stored = json.loads(output.read_text(encoding="utf-8"))
    expected_keys = {
        row["observation_key"]
        for packet in packets
        for row in packet["observations"]
    }
    assert {
        row["observation_key"] for row in stored["observations"]
    } == expected_keys


def test_store_admits_more_than_legacy_packet_cap_without_history_loss(
    tmp_path: Path,
) -> None:
    """The resident store pages history; MAX_ROWS applies only to JSON export."""

    store_path = tmp_path / "forward-observations.sqlite3"
    template = _packet()["observations"][0]
    generation = 0
    total = storage.MAX_ROWS + 1
    records = []
    for index in range(total):
        row = deepcopy(template)
        event_date = date(2030, 1, 1) + timedelta(days=index)
        decision_id = f"decision-{index:05d}"
        row["event_date"] = event_date.isoformat()
        row["decision_id"] = decision_id
        row["cycle_id"] = f"cycle-{index:05d}"
        row["observation_key"] = (
            f"RKPK|{row['event_date']}|STANDARD|"
            f"2026-07-26T21:08:23.560Z"
        )
        _rehash_row(row)
        records.append({
            "observation": row,
            "decision_id": decision_id,
            "decision_sha256": f"{index % 16:x}" * 64,
            "source_rowid": index + 1,
        })
    for offset in range(0, len(records), MAX_BATCH_ROWS):
        batch = records[offset:offset + MAX_BATCH_ROWS]
        result = commit_forward_observation_batch(
            store_path,
            records=batch,
            expected_generation=generation,
            last_source_rowid=batch[-1]["source_rowid"],
            materialized_at_utc="2026-07-28T00:00:00Z",
            allowed_root=tmp_path,
        )
        generation = result["generation"]
    state = read_forward_store_state(store_path, allowed_root=tmp_path)
    assert state["observation_count"] == total
    assert state["last_source_rowid"] == total
    assert store_path.stat().st_size <= 64 * 1024 * 1024


def test_utf8_byte_budget_rejects_multibyte_payload_by_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A character-count budget must not permit a UTF-8 byte overflow."""

    packet = _packet()
    key = packet["observations"][0]["observation_key"]
    packet = append_settlement_labels(packet, [{
        "observation_key": key,
        "winning_bucket": "温" * 128,
        "labeled_at_utc": "2026-07-28T00:00:00Z",
        "settlement_authority": "market_registry_official_resolution",
        "official_settlement_sha256": "7" * 64,
        "settlement_timezone": "Asia/Seoul",
    }])
    serialized = (
        json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    character_count = len(serialized)
    byte_count = len(serialized.encode("utf-8"))
    assert byte_count > character_count

    # This limit would incorrectly pass a character-count implementation but
    # must fail the byte-count implementation.
    monkeypatch.setattr(storage, "MAX_OUTPUT_BYTES", character_count + 1)
    with pytest.raises(ResourceBoundError, match="output_bytes_exceeded"):
        write_forward_observation_packet(
            tmp_path / "utf8-budget.json",
            packet,
        )


def _seed_local_store(tmp_path: Path) -> tuple[Path, dict]:
    store_path = tmp_path / "target" / "forward-observations.sqlite3"
    store_path.parent.mkdir()
    row = _packet()["observations"][0]
    commit_forward_observation_batch(
        store_path,
        records=[
            {
                "observation": row,
                "decision_id": row["decision_id"],
                "decision_sha256": "d" * 64,
                "source_rowid": 1,
            }
        ],
        expected_generation=0,
        last_source_rowid=1,
        materialized_at_utc="2026-07-28T00:00:00Z",
        allowed_root=store_path.parent,
    )
    return store_path, row


def _source_with_label(row: dict) -> tuple[sqlite3.Connection, str, str]:
    source = sqlite3.connect(":memory:")
    source.executescript(
        """
        CREATE TABLE basket_target_market_registry(
          station_id TEXT,event_date TEXT,settlement_timezone TEXT
        );
        CREATE TABLE basket_policy_training_fact(
          fact_id TEXT,station_id TEXT,event_date TEXT,
          settlement_timezone TEXT,decision_id TEXT,decision_at_utc TEXT
        );
        CREATE TABLE basket_policy_training_label(
          label_id TEXT,fact_id TEXT,truth_id TEXT,truth_revision_no INTEGER,
          truth_sha256 TEXT,official_tmax_c REAL,winning_bucket TEXT,
          settlement_closed_at_utc TEXT,labeled_at_utc TEXT,outcome_json TEXT,
          authority_scope TEXT,supersedes_label_id TEXT,content_sha256 TEXT
        );
        CREATE TABLE calibration_station_daily_high_truth(
          truth_id TEXT,station_id TEXT,local_date TEXT,revision_no INTEGER,
          content_sha256 TEXT,authority_scope TEXT,qc_status TEXT,
          timestamps_complete INTEGER,
          source_lane TEXT NOT NULL DEFAULT 'AWC',
          source_role TEXT NOT NULL DEFAULT 'CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1'
        );
        """
    )
    source.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?)",
        ("RKPK", "2026-07-27", "Asia/Seoul"),
    )
    source.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?)",
        (
            "fact-1",
            "RKPK",
            "2026-07-27",
            "Asia/Seoul",
            row["decision_id"],
            "2026-07-26T21:08:23Z",
        ),
    )
    outcome = canonical_json(
        {
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "schema": "delta_basket_score_no_live_outcome_v2",
            "winning_bucket": "30",
        }
    )

    def add_label(revision: int, truth_id: str, supersedes: str | None) -> str:
        truth_sha = f"{revision:x}" * 64
        source.execute(
            "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                truth_id,
                "RKPK",
                "2026-07-27",
                revision,
                truth_sha,
                "FORMAL_NATIVE_ELIGIBLE",
                "ACCEPTED",
                1,
                "AWC",
                CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1,
            ),
        )
        identity = {
            "fact_id": "fact-1",
            "truth_id": truth_id,
            "truth_revision_no": revision,
            "truth_sha256": truth_sha,
            "official_tmax_c": 30.0,
            "winning_bucket": "30",
            "settlement_closed_at_utc": "2026-07-28T00:00:00Z",
            "labeled_at_utc": f"2026-07-28T0{revision}:00:00Z",
            "outcome_json": outcome,
            "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
            "supersedes_label_id": supersedes,
        }
        label_sha = content_sha256(
            {"schema": "delta_basket_score_training_label_v2", **identity}
        )
        label_id = f"d2label_{label_sha[:40]}"
        source.execute(
            "INSERT INTO basket_policy_training_label VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                label_id,
                "fact-1",
                truth_id,
                revision,
                truth_sha,
                30.0,
                "30",
                identity["settlement_closed_at_utc"],
                identity["labeled_at_utc"],
                outcome,
                identity["authority_scope"],
                supersedes,
                label_sha,
            ),
        )
        return label_id

    first_label_id = add_label(1, "truth-1", None)
    source.commit()
    return source, first_label_id, outcome


def test_initialize_store_is_atomic_idempotent_and_cas_bounded(tmp_path: Path) -> None:
    root = tmp_path / "target"
    path = root / "forward-observations.sqlite3"
    first = initialize_forward_observation_store(
        path,
        expected_generation=0,
        initialized_at_utc="2026-07-28T00:00:00Z",
        allowed_root=root,
    )
    second = initialize_forward_observation_store(
        path,
        expected_generation=0,
        initialized_at_utc="2026-07-28T00:01:00Z",
        allowed_root=root,
    )
    assert first["state"] == "initialized"
    assert second["state"] == "already_initialized"
    assert second["generation"] == 0
    with pytest.raises(Exception, match="generation_cas"):
        initialize_forward_observation_store(
            path,
            expected_generation=1,
            initialized_at_utc="2026-07-28T00:02:00Z",
            allowed_root=root,
        )


def test_initialize_store_adds_label_skip_columns_to_legacy_v2_table(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target"
    path = root / "forward-observations.sqlite3"
    root.mkdir()
    with sqlite3.connect(path) as legacy:
        legacy.execute(
            "CREATE TABLE forward_label_ingest_state("
            "source_label_rowid INTEGER PRIMARY KEY,"
            "source_label_id TEXT NOT NULL UNIQUE,"
            "source_label_content_sha256 TEXT NOT NULL,"
            "materialized_at_utc TEXT NOT NULL)"
        )

    result = initialize_forward_observation_store(
        path,
        expected_generation=0,
        initialized_at_utc="2026-07-28T00:00:00Z",
        allowed_root=root,
    )

    assert result["state"] == "initialized"
    with sqlite3.connect(path) as migrated:
        columns = {
            row[1]
            for row in migrated.execute(
                "PRAGMA table_info(forward_label_ingest_state)"
            )
        }
    assert {"state", "skip_reason"}.issubset(columns)
    assert read_forward_store_state(
        path, allowed_root=root
    )["skipped_ineligible_label_count"] == 0


def test_settlement_labels_exact_join_revision_idempotency_and_latest_export(
    tmp_path: Path,
) -> None:
    path, row = _seed_local_store(tmp_path)
    source, first_label_id, _ = _source_with_label(row)
    first = append_forward_settlement_labels(
        path,
        source,
        expected_generation=1,
        materialized_at_utc="2026-07-28T02:00:00Z",
        allowed_root=path.parent,
    )
    replay = append_forward_settlement_labels(
        path,
        source,
        expected_generation=2,
        materialized_at_utc="2026-07-28T02:01:00Z",
        allowed_root=path.parent,
    )
    assert first["inserted"] == 1
    assert replay["state"] == "already_current"
    assert replay["generation"] == 2
    exported = read_forward_observation_packet(path, allowed_root=path.parent)
    assert len(exported["settlement_labels"]) == 1
    assert exported["settlement_labels"][0]["source_label_id"] == first_label_id
    source.close()


@pytest.mark.parametrize(
    ("source_lane", "source_role"),
    [
        ("AMSC", CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1),
        ("AWC", "CANONICAL_AMSC_AWC_INGRESS"),
    ],
)
def test_settlement_label_rejects_noncanonical_truth_binding(
    tmp_path: Path,
    source_lane: str,
    source_role: str,
) -> None:
    path, row = _seed_local_store(tmp_path)
    source, _, _ = _source_with_label(row)
    source.execute(
        "UPDATE calibration_station_daily_high_truth "
        "SET source_lane=?,source_role=? WHERE truth_id='truth-1'",
        (source_lane, source_role),
    )
    source.commit()

    with pytest.raises(
        ForwardObservationStoreError,
        match="forward_store_label_truth_missing_or_ambiguous",
    ):
        append_forward_settlement_labels(
            path,
            source,
            expected_generation=1,
            materialized_at_utc="2026-07-28T02:00:00Z",
            allowed_root=path.parent,
        )

    assert read_forward_store_state(
        path, allowed_root=path.parent
    )["label_count"] == 0
    source.close()


def test_settlement_label_projection_skips_explicitly_ineligible_fact(
    tmp_path: Path,
) -> None:
    path, row = _seed_local_store(tmp_path)
    source, _, outcome = _source_with_label(row)
    source.executescript(
        """
        CREATE TABLE auto_trade_decisions(
          decision_id TEXT PRIMARY KEY,
          details_json TEXT NOT NULL
        );
        INSERT INTO auto_trade_decisions VALUES(
          'decision-ineligible',
          '{"forward_observation_status":"INELIGIBLE_DELTA_V2_NOT_AUTHORIZED"}'
        );
        INSERT INTO basket_policy_training_fact VALUES(
          'fact-ineligible','RKPK','2026-07-27','Asia/Seoul',
          'decision-ineligible','2026-07-26T21:09:00Z'
        );
        INSERT INTO calibration_station_daily_high_truth(
          truth_id,station_id,local_date,revision_no,content_sha256,
          authority_scope,qc_status,timestamps_complete
        ) VALUES(
          'truth-ineligible','RKPK','2026-07-27',1,
          '2222222222222222222222222222222222222222222222222222222222222222',
          'FORMAL_NATIVE_ELIGIBLE','ACCEPTED',1
        );
        """
    )
    identity = {
        "fact_id": "fact-ineligible",
        "truth_id": "truth-ineligible",
        "truth_revision_no": 1,
        "truth_sha256": "2" * 64,
        "official_tmax_c": 30.0,
        "winning_bucket": "30",
        "settlement_closed_at_utc": "2026-07-28T00:00:00Z",
        "labeled_at_utc": "2026-07-28T01:30:00Z",
        "outcome_json": outcome,
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
        "supersedes_label_id": None,
    }
    label_sha = content_sha256(
        {"schema": "delta_basket_score_training_label_v2", **identity}
    )
    source.execute(
        "INSERT INTO basket_policy_training_label "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"d2label_{label_sha[:40]}",
            identity["fact_id"],
            identity["truth_id"],
            identity["truth_revision_no"],
            identity["truth_sha256"],
            identity["official_tmax_c"],
            identity["winning_bucket"],
            identity["settlement_closed_at_utc"],
            identity["labeled_at_utc"],
            identity["outcome_json"],
            identity["authority_scope"],
            identity["supersedes_label_id"],
            label_sha,
        ),
    )
    source.commit()

    result = append_forward_settlement_labels(
        path,
        source,
        expected_generation=1,
        materialized_at_utc="2026-07-28T02:00:00Z",
        allowed_root=path.parent,
    )

    assert result["inserted"] == 1
    assert result["skipped_ineligible"] == 1
    state = read_forward_store_state(path, allowed_root=path.parent)
    assert state["label_count"] == 1
    assert state["skipped_ineligible_label_count"] == 1
    assert state["permanent_skip_reasons"] == {
        "INELIGIBLE_DELTA_V2_NOT_AUTHORIZED": 1
    }
    # A permanent skip is settled in the ledger, not kept as a row
    # (2026-10-15: one row per such label filled the store's ceiling).
    with sqlite3.connect(path) as stored:
        assert stored.execute(
            "SELECT COUNT(*) FROM forward_label_ingest_state "
            "WHERE state='SKIPPED_INELIGIBLE'"
        ).fetchone()[0] == 0
    assert state["last_source_label_rowid"] == 2

    # The forward lane ingests only decisions written ELIGIBLE, fixed when
    # the decision is committed, so an ineligible decision's observation
    # never arrives in production.  Were one written anyway, the settled
    # label is not read again: the cursor is past it.
    late_observation = deepcopy(row)
    late_observation.update(
        {
            "decision_id": "decision-ineligible",
            "cycle_id": "cycle-ineligible",
            "decision_at_utc": "2026-07-26T21:09:23.560Z",
            "observation_key": (
                "RKPK|2026-07-27|STANDARD|2026-07-26T21:09:23.560Z"
            ),
        }
    )
    _rehash_row(late_observation)
    observation_commit = commit_forward_observation_batch(
        path,
        records=[
            {
                "observation": late_observation,
                "decision_id": "decision-ineligible",
                "decision_sha256": "e" * 64,
                "source_rowid": 2,
            }
        ],
        expected_generation=2,
        last_source_rowid=2,
        materialized_at_utc="2026-07-28T02:30:00Z",
        allowed_root=path.parent,
    )
    assert observation_commit["generation"] == 3
    replay = append_forward_settlement_labels(
        path,
        source,
        expected_generation=3,
        materialized_at_utc="2026-07-28T02:31:00Z",
        allowed_root=path.parent,
    )
    assert replay["state"] == "already_current"
    assert replay["source_rows"] == 0
    assert replay["generation"] == 3
    source.close()


def _add_eligible_label(
    source: sqlite3.Connection, outcome: str, *, decision_id: str, source_rowid: int,
    suffix: str,
) -> None:
    """A settled label whose decision the source calls ELIGIBLE, at a chosen
    rowid of the decision table."""

    source.execute(
        "CREATE TABLE IF NOT EXISTS auto_trade_decisions("
        " decision_id TEXT PRIMARY KEY, details_json TEXT NOT NULL)"
    )
    source.execute(
        "INSERT INTO auto_trade_decisions(rowid,decision_id,details_json) VALUES(?,?,?)",
        (source_rowid, decision_id, '{"forward_observation_status":"ELIGIBLE"}'),
    )
    source.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?)",
        (f"fact-{suffix}", "RKPK", "2026-07-27", "Asia/Seoul", decision_id,
         "2026-07-26T21:09:00Z"),
    )
    truth_sha = "3" * 64
    source.execute(
        "INSERT INTO calibration_station_daily_high_truth(truth_id,station_id,"
        "local_date,revision_no,content_sha256,authority_scope,qc_status,"
        "timestamps_complete) VALUES(?,?,?,?,?,?,?,?)",
        (f"truth-{suffix}", "RKPK", "2026-07-27", 1, truth_sha,
         "FORMAL_NATIVE_ELIGIBLE", "ACCEPTED", 1),
    )
    identity = {
        "fact_id": f"fact-{suffix}", "truth_id": f"truth-{suffix}",
        "truth_revision_no": 1, "truth_sha256": truth_sha, "official_tmax_c": 30.0,
        "winning_bucket": "30", "settlement_closed_at_utc": "2026-07-28T00:00:00Z",
        "labeled_at_utc": "2026-07-28T01:30:00Z", "outcome_json": outcome,
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE", "supersedes_label_id": None,
    }
    label_sha = content_sha256(
        {"schema": "delta_basket_score_training_label_v2", **identity}
    )
    source.execute(
        "INSERT INTO basket_policy_training_label VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"d2label_{label_sha[:40]}", identity["fact_id"], identity["truth_id"], 1,
         truth_sha, 30.0, "30", identity["settlement_closed_at_utc"],
         identity["labeled_at_utc"], outcome, identity["authority_scope"], None,
         label_sha),
    )
    source.commit()


def test_a_label_whose_source_row_was_rewritten_after_ingest_is_skipped(
    tmp_path: Path,
) -> None:
    """2026-09-27: 170 decisions of 2026-08-01..08-30 were rewritten in place
    -- same source row, new decision id -- after the store had ingested the
    originals, and their labels held the cursor from 2026-08-31.  The store's
    own ingest record at that source row is the evidence; nothing is joined
    across the two ids."""

    path, row = _seed_local_store(tmp_path)  # ingests row["decision_id"] at rowid 1
    source, _, outcome = _source_with_label(row)
    _add_eligible_label(source, outcome, decision_id="decision-rewritten",
                        source_rowid=1, suffix="rewritten")

    result = append_forward_settlement_labels(
        path, source, expected_generation=1,
        materialized_at_utc="2026-07-28T02:00:00Z", allowed_root=path.parent,
    )

    assert result["skipped_ineligible"] == 1
    state = read_forward_store_state(path, allowed_root=path.parent)
    assert state["permanent_skip_reasons"] == {"FORWARD_SOURCE_ROW_REWRITTEN": 1}
    with sqlite3.connect(path) as stored:
        assert stored.execute(
            "SELECT COUNT(*) FROM forward_label_ingest_state "
            "WHERE state='SKIPPED_INELIGIBLE'"
        ).fetchone()[0] == 0


def test_an_eligible_decision_the_store_never_saw_still_fails_closed(
    tmp_path: Path,
) -> None:
    path, row = _seed_local_store(tmp_path)
    source, _, outcome = _source_with_label(row)
    # Its source row was never ingested at all: its observation may yet come.
    _add_eligible_label(source, outcome, decision_id="decision-unseen",
                        source_rowid=2, suffix="unseen")

    with pytest.raises(
        Exception, match="forward_store_label_exact_decision_join_missing_or_ambiguous"
    ):
        append_forward_settlement_labels(
            path, source, expected_generation=1,
            materialized_at_utc="2026-07-28T02:00:00Z", allowed_root=path.parent,
        )


def test_settlement_label_skips_quarantined_decision_and_promotes_after_repair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target" / "forward-observations.sqlite3"
    path.parent.mkdir()
    row = _packet()["observations"][0]
    reason = "cash_phase_forward_binding_scope_or_clock_mismatch"
    quarantine_forward_observation(
        path,
        decision_id=row["decision_id"],
        source_decision_sha256="d" * 64,
        source_rowid=1,
        error_code=reason,
        expected_generation=0,
        quarantined_at_utc="2026-07-28T00:00:00Z",
        allowed_root=path.parent,
    )
    source, label_id, _ = _source_with_label(row)
    # The source decision still claims ELIGIBLE; only the store's own ingest
    # verdict explains the missing observation.
    source.executescript(
        """
        CREATE TABLE auto_trade_decisions(
          decision_id TEXT PRIMARY KEY,
          details_json TEXT NOT NULL
        );
        """
    )
    source.execute(
        "INSERT INTO auto_trade_decisions VALUES(?,?)",
        (row["decision_id"], '{"forward_observation_status":"ELIGIBLE"}'),
    )
    source.commit()

    skipped = append_forward_settlement_labels(
        path,
        source,
        expected_generation=1,
        materialized_at_utc="2026-07-28T02:00:00Z",
        allowed_root=path.parent,
    )
    assert skipped["inserted"] == 0
    assert skipped["skipped_ineligible"] == 1
    with sqlite3.connect(path) as stored:
        assert stored.execute(
            "SELECT state,skip_reason FROM forward_label_ingest_state "
            "WHERE source_label_id=?",
            (label_id,),
        ).fetchone() == (
            "SKIPPED_INELIGIBLE",
            "FORWARD_OBSERVATION_QUARANTINED:" + reason,
        )

    repaired = repair_false_quarantine_batch(
        path,
        records=[
            {
                "observation": row,
                "decision_id": row["decision_id"],
                "decision_sha256": "d" * 64,
                "source_rowid": 1,
                "prior_error_code": reason,
            }
        ],
        expected_generation=2,
        repaired_at_utc="2026-07-28T02:30:00Z",
        allowed_root=path.parent,
    )
    assert repaired["state"] == "repaired"
    promoted = append_forward_settlement_labels(
        path,
        source,
        expected_generation=3,
        materialized_at_utc="2026-07-28T02:31:00Z",
        allowed_root=path.parent,
    )
    assert promoted["inserted"] == 1
    state = read_forward_store_state(path, allowed_root=path.parent)
    assert state["label_count"] == 1
    with sqlite3.connect(path) as stored:
        assert stored.execute(
            "SELECT prior_state,state FROM forward_label_ingest_transition "
            "WHERE source_label_id=?",
            (label_id,),
        ).fetchone() == ("SKIPPED_INELIGIBLE", "MATERIALIZED")
    source.close()


def test_settlement_label_keyset_pages_more_than_one_batch_without_stall(
    tmp_path: Path,
) -> None:
    path, row = _seed_local_store(tmp_path)
    source, previous_label_id, outcome = _source_with_label(row)
    base = datetime(2026, 7, 28, tzinfo=timezone.utc)
    for revision in range(2, 67):
        truth_id = f"truth-{revision}"
        truth_sha = f"{revision % 16:x}" * 64
        source.execute(
            "INSERT INTO calibration_station_daily_high_truth("
            "truth_id,station_id,local_date,revision_no,content_sha256,"
            "authority_scope,qc_status,timestamps_complete) VALUES(?,?,?,?,?,?,?,?)",
            (
                truth_id,
                "RKPK",
                "2026-07-27",
                revision,
                truth_sha,
                "FORMAL_NATIVE_ELIGIBLE",
                "ACCEPTED",
                1,
            ),
        )
        labeled_at = (
            base + timedelta(minutes=revision)
        ).isoformat().replace("+00:00", "Z")
        identity = {
            "fact_id": "fact-1",
            "truth_id": truth_id,
            "truth_revision_no": revision,
            "truth_sha256": truth_sha,
            "official_tmax_c": 30.0,
            "winning_bucket": "30",
            "settlement_closed_at_utc": "2026-07-28T00:00:00Z",
            "labeled_at_utc": labeled_at,
            "outcome_json": outcome,
            "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
            "supersedes_label_id": previous_label_id,
        }
        label_sha = content_sha256(
            {"schema": "delta_basket_score_training_label_v2", **identity}
        )
        previous_label_id = f"d2label_{label_sha[:40]}"
        source.execute(
            "INSERT INTO basket_policy_training_label "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                previous_label_id,
                "fact-1",
                truth_id,
                revision,
                truth_sha,
                30.0,
                "30",
                identity["settlement_closed_at_utc"],
                labeled_at,
                outcome,
                "FORMAL_NATIVE_ELIGIBLE",
                identity["supersedes_label_id"],
                label_sha,
            ),
        )
    source.commit()
    first = append_forward_settlement_labels(
        path,
        source,
        expected_generation=1,
        materialized_at_utc="2026-07-28T03:00:00Z",
        max_labels=64,
        allowed_root=path.parent,
    )
    second = append_forward_settlement_labels(
        path,
        source,
        expected_generation=2,
        materialized_at_utc="2026-07-28T03:01:00Z",
        max_labels=64,
        allowed_root=path.parent,
    )
    assert first["source_rows"] == 64
    assert first["more_pending"] is True
    assert second["source_rows"] == 2
    assert second["more_pending"] is False
    state = read_forward_store_state(path, allowed_root=path.parent)
    assert state["label_count"] == 66
    assert state["last_source_label_rowid"] == 66
    exported = read_forward_observation_packet(path, allowed_root=path.parent)
    assert exported["settlement_labels"][0]["revision"] == 66
    source.close()


def test_a_permanent_skip_below_a_new_high_row_is_not_revisited(
    tmp_path: Path,
) -> None:
    """A high-water cursor must still allow low→high→low replay."""

    path, row = _seed_local_store(tmp_path)
    source, _, outcome = _source_with_label(row)
    source.executescript(
        """
        CREATE TABLE auto_trade_decisions(
          decision_id TEXT PRIMARY KEY,
          details_json TEXT NOT NULL
        );
        INSERT INTO auto_trade_decisions VALUES(
          'decision-ineligible',
          '{"forward_observation_status":"INELIGIBLE_DELTA_V2_NOT_AUTHORIZED"}'
        );
        INSERT INTO basket_policy_training_fact VALUES(
          'fact-ineligible','RKPK','2026-07-27','Asia/Seoul',
          'decision-ineligible','2026-07-26T21:09:00Z'
        );
        INSERT INTO calibration_station_daily_high_truth(
          truth_id,station_id,local_date,revision_no,content_sha256,
          authority_scope,qc_status,timestamps_complete
        ) VALUES(
          'truth-ineligible','RKPK','2026-07-27',1,
          '2222222222222222222222222222222222222222222222222222222222222222',
          'FORMAL_NATIVE_ELIGIBLE','ACCEPTED',1
        );
        """
    )
    low_identity = {
        "fact_id": "fact-ineligible",
        "truth_id": "truth-ineligible",
        "truth_revision_no": 1,
        "truth_sha256": "2" * 64,
        "official_tmax_c": 30.0,
        "winning_bucket": "30",
        "settlement_closed_at_utc": "2026-07-28T00:00:00Z",
        "labeled_at_utc": "2026-07-28T01:30:00Z",
        "outcome_json": outcome,
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
        "supersedes_label_id": None,
    }
    low_sha = content_sha256(
        {"schema": "delta_basket_score_training_label_v2", **low_identity}
    )
    source.execute(
        "INSERT INTO basket_policy_training_label VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"d2label_{low_sha[:40]}",
            low_identity["fact_id"],
            low_identity["truth_id"],
            low_identity["truth_revision_no"],
            low_identity["truth_sha256"],
            low_identity["official_tmax_c"],
            low_identity["winning_bucket"],
            low_identity["settlement_closed_at_utc"],
            low_identity["labeled_at_utc"],
            low_identity["outcome_json"],
            low_identity["authority_scope"],
            low_identity["supersedes_label_id"],
            low_sha,
        ),
    )
    source.commit()

    first = append_forward_settlement_labels(
        path,
        source,
        expected_generation=1,
        materialized_at_utc="2026-07-28T02:00:00Z",
        allowed_root=path.parent,
    )
    assert first["skipped_ineligible"] == 1
    assert first["generation"] == 2

    # Add a new high row and its observation while the lower row is still
    # skipped.  The next one-row page must choose the new high row first.
    source.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?)",
        (
            "fact-high",
            "RKPK",
            "2026-07-27",
            "Asia/Seoul",
            "decision-high",
            "2026-07-26T21:10:00Z",
        ),
    )
    source.execute(
        "INSERT INTO calibration_station_daily_high_truth("
        "truth_id,station_id,local_date,revision_no,content_sha256,"
        "authority_scope,qc_status,timestamps_complete) VALUES(?,?,?,?,?,?,?,?)",
        (
            "truth-high",
            "RKPK",
            "2026-07-27",
            2,
            "3" * 64,
            "FORMAL_NATIVE_ELIGIBLE",
            "ACCEPTED",
            1,
        ),
    )
    high_identity = {
        "fact_id": "fact-high",
        "truth_id": "truth-high",
        "truth_revision_no": 2,
        "truth_sha256": "3" * 64,
        "official_tmax_c": 31.0,
        "winning_bucket": "31",
        "settlement_closed_at_utc": "2026-07-28T00:00:00Z",
        "labeled_at_utc": "2026-07-28T01:31:00Z",
        "outcome_json": outcome,
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
        "supersedes_label_id": None,
    }
    high_sha = content_sha256(
        {"schema": "delta_basket_score_training_label_v2", **high_identity}
    )
    source.execute(
        "INSERT INTO basket_policy_training_label VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"d2label_{high_sha[:40]}",
            high_identity["fact_id"],
            high_identity["truth_id"],
            high_identity["truth_revision_no"],
            high_identity["truth_sha256"],
            high_identity["official_tmax_c"],
            high_identity["winning_bucket"],
            high_identity["settlement_closed_at_utc"],
            high_identity["labeled_at_utc"],
            high_identity["outcome_json"],
            high_identity["authority_scope"],
            high_identity["supersedes_label_id"],
            high_sha,
        ),
    )
    source.commit()
    high_observation = deepcopy(row)
    high_observation.update(
        {
            "decision_id": "decision-high",
            "cycle_id": "cycle-high",
            "decision_at_utc": "2026-07-26T21:10:23.560Z",
            "observation_key": (
                "RKPK|2026-07-27|STANDARD|2026-07-26T21:10:23.560Z"
            ),
        }
    )
    _rehash_row(high_observation)
    high_commit = commit_forward_observation_batch(
        path,
        records=[
            {
                "observation": high_observation,
                "decision_id": "decision-high",
                "decision_sha256": "f" * 64,
                "source_rowid": 2,
            }
        ],
        expected_generation=2,
        last_source_rowid=2,
        materialized_at_utc="2026-07-28T02:10:00Z",
        allowed_root=path.parent,
    )
    assert high_commit["generation"] == 3

    high = append_forward_settlement_labels(
        path,
        source,
        expected_generation=3,
        materialized_at_utc="2026-07-28T02:11:00Z",
        max_labels=1,
        allowed_root=path.parent,
    )
    assert high["inserted"] == 1
    # The ineligible low row is settled, not a retry candidate: nothing waits.
    assert high["more_pending"] is False
    assert high["generation"] == 4

    # An observation for the ineligible decision (never written in
    # production, where the lane ingests only ELIGIBLE decisions) does not
    # bring the settled label back.
    low_observation = deepcopy(row)
    low_observation.update(
        {
            "decision_id": "decision-ineligible",
            "cycle_id": "cycle-ineligible",
            "decision_at_utc": "2026-07-26T21:11:23.560Z",
            "observation_key": (
                "RKPK|2026-07-27|STANDARD|2026-07-26T21:11:23.560Z"
            ),
        }
    )
    _rehash_row(low_observation)
    low_commit = commit_forward_observation_batch(
        path,
        records=[
            {
                "observation": low_observation,
                "decision_id": "decision-ineligible",
                "decision_sha256": "a" * 64,
                "source_rowid": 3,
            }
        ],
        expected_generation=4,
        last_source_rowid=3,
        materialized_at_utc="2026-07-28T02:20:00Z",
        allowed_root=path.parent,
    )
    assert low_commit["generation"] == 5
    replay = append_forward_settlement_labels(
        path,
        source,
        expected_generation=5,
        materialized_at_utc="2026-07-28T02:22:00Z",
        max_labels=1,
        allowed_root=path.parent,
    )
    assert replay["state"] == "already_current"
    assert replay["source_rows"] == 0
    assert replay["generation"] == 5
    state = read_forward_store_state(path, allowed_root=path.parent)
    assert state["label_count"] == 2
    assert state["skipped_ineligible_label_count"] == 1
    assert state["last_source_label_rowid"] == 3
    with sqlite3.connect(path) as stored:
        # Deleting kept rows outside the retention gate still fails closed.
        with pytest.raises(sqlite3.DatabaseError):
            stored.execute("DELETE FROM forward_label_ingest_state")
    source.close()


def test_quarantine_is_payload_free_cursor_advancing_and_conflict_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target" / "forward-observations.sqlite3"
    path.parent.mkdir()
    result = quarantine_forward_observation(
        path,
        decision_id="poison-1",
        source_decision_sha256="a" * 64,
        source_rowid=7,
        error_code="malformed_json",
        expected_generation=0,
        quarantined_at_utc="2026-07-28T00:00:00Z",
        allowed_root=path.parent,
    )
    assert result["state"] == "blocked_fail_closed"
    assert result["last_source_rowid"] == 7
    with sqlite3.connect(path) as connection:
        columns = {
            str(item[1])
            for item in connection.execute("PRAGMA table_info(forward_ingest_state)")
        }
        assert "observation_json" not in columns
        assert connection.execute(
            "SELECT COUNT(*) FROM forward_ingest_state WHERE state='QUARANTINED'"
        ).fetchone()[0] == 1
    with pytest.raises(Exception, match="quarantine_identity_conflict"):
        quarantine_forward_observation(
            path,
            decision_id="poison-1",
            source_decision_sha256="b" * 64,
            source_rowid=7,
            error_code="malformed_json",
            expected_generation=1,
            quarantined_at_utc="2026-07-28T00:01:00Z",
            allowed_root=path.parent,
        )


def test_false_quarantine_repair_is_append_only_exact_and_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target" / "forward-observations.sqlite3"
    path.parent.mkdir()
    row = _packet()["observations"][0]
    decision_sha = "a" * 64
    source_rowid = 7
    reason = "cash_phase_forward_binding_scope_or_clock_mismatch"
    quarantine_forward_observation(
        path,
        decision_id=row["decision_id"],
        source_decision_sha256=decision_sha,
        source_rowid=source_rowid,
        error_code=reason,
        expected_generation=0,
        quarantined_at_utc="2026-07-28T00:00:00Z",
        allowed_root=path.parent,
    )
    record = {
        "observation": row,
        "decision_id": row["decision_id"],
        "decision_sha256": decision_sha,
        "source_rowid": source_rowid,
        "prior_error_code": reason,
    }
    with pytest.raises(Exception, match="generation_cas"):
        repair_false_quarantine_batch(
            path,
            records=[record],
            expected_generation=0,
            repaired_at_utc="2026-07-28T00:05:00Z",
            allowed_root=path.parent,
        )
    repaired = repair_false_quarantine_batch(
        path,
        records=[record],
        expected_generation=1,
        repaired_at_utc="2026-07-28T00:05:00Z",
        allowed_root=path.parent,
    )
    assert repaired["state"] == "repaired"
    assert repaired["generation"] == 2
    assert repaired["last_source_rowid"] == source_rowid
    assert repaired["observation_count"] == 1
    assert repaired["quarantine_count"] == 0
    assert repaired["historical_quarantine_count"] == 1
    assert repaired["repaired_quarantine_count"] == 1

    replay = repair_false_quarantine_batch(
        path,
        records=[record],
        expected_generation=2,
        repaired_at_utc="2026-07-28T00:05:00Z",
        allowed_root=path.parent,
    )
    assert replay["state"] == "already_repaired"
    assert replay["inserted"] == 0
    assert replay["generation"] == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT state,error_code FROM forward_ingest_state "
            "WHERE decision_id=?",
            (row["decision_id"],),
        ).fetchone() == ("QUARANTINED", reason)
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            connection.execute(
                "UPDATE forward_ingest_repair SET prior_error_code='changed'"
            )


def test_legacy_store_state_is_readable_before_additive_repair_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target" / "forward-observations.sqlite3"
    path.parent.mkdir()
    quarantine_forward_observation(
        path,
        decision_id="legacy-poison",
        source_decision_sha256="a" * 64,
        source_rowid=7,
        error_code="malformed_json",
        expected_generation=0,
        quarantined_at_utc="2026-07-28T00:00:00Z",
        allowed_root=path.parent,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE forward_ingest_repair")
    state = read_forward_store_state(path, allowed_root=path.parent)
    assert state["generation"] == 1
    assert state["quarantine_count"] == 1
    assert state["historical_quarantine_count"] == 1
    assert state["repaired_quarantine_count"] == 0


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (
            "prior_error_code",
            "cash_phase_forward_probability_binding_drift",
            "reason_not_allowed",
        ),
        ("decision_sha256", "b" * 64, "identity_drift"),
        ("source_rowid", 8, "identity_drift"),
    ],
)
def test_false_quarantine_repair_rejects_reason_and_identity_drift_atomically(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    path = tmp_path / "target" / "forward-observations.sqlite3"
    path.parent.mkdir()
    row = _packet()["observations"][0]
    reason = "cash_phase_forward_binding_scope_or_clock_mismatch"
    quarantine_forward_observation(
        path,
        decision_id=row["decision_id"],
        source_decision_sha256="a" * 64,
        source_rowid=7,
        error_code=reason,
        expected_generation=0,
        quarantined_at_utc="2026-07-28T00:00:00Z",
        allowed_root=path.parent,
    )
    record = {
        "observation": row,
        "decision_id": row["decision_id"],
        "decision_sha256": "a" * 64,
        "source_rowid": 7,
        "prior_error_code": reason,
    }
    record[field] = value
    with pytest.raises(Exception, match=error):
        repair_false_quarantine_batch(
            path,
            records=[record],
            expected_generation=1,
            repaired_at_utc="2026-07-28T00:05:00Z",
            allowed_root=path.parent,
        )
    state = read_forward_store_state(path, allowed_root=path.parent)
    assert state["generation"] == 1
    assert state["observation_count"] == 0
    assert state["quarantine_count"] == 1
    assert state["repaired_quarantine_count"] == 0


def test_false_quarantine_repair_batch_rolls_back_valid_prefix_on_late_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "target" / "forward-observations.sqlite3"
    path.parent.mkdir()
    rows = [
        _packet("2026-07-26T21:08:23.560Z")["observations"][0],
        _packet("2026-07-26T21:08:33.560Z")["observations"][0],
    ]
    reason = "cash_phase_forward_binding_scope_or_clock_mismatch"
    generation = 0
    for source_rowid, row in enumerate(rows, start=7):
        result = quarantine_forward_observation(
            path,
            decision_id=row["decision_id"],
            source_decision_sha256=("a" if source_rowid == 7 else "b") * 64,
            source_rowid=source_rowid,
            error_code=reason,
            expected_generation=generation,
            quarantined_at_utc=f"2026-07-28T00:0{source_rowid - 7}:00Z",
            allowed_root=path.parent,
        )
        generation = result["generation"]
    records = [
        {
            "observation": rows[0],
            "decision_id": rows[0]["decision_id"],
            "decision_sha256": "a" * 64,
            "source_rowid": 7,
            "prior_error_code": reason,
        },
        {
            "observation": rows[1],
            "decision_id": rows[1]["decision_id"],
            "decision_sha256": "c" * 64,
            "source_rowid": 8,
            "prior_error_code": reason,
        },
    ]
    with pytest.raises(Exception, match="identity_drift"):
        repair_false_quarantine_batch(
            path,
            records=records,
            expected_generation=generation,
            repaired_at_utc="2026-07-28T00:05:00Z",
            allowed_root=path.parent,
        )
    state = read_forward_store_state(path, allowed_root=path.parent)
    assert state["generation"] == generation
    assert state["observation_count"] == 0
    assert state["quarantine_count"] == 2
    assert state["repaired_quarantine_count"] == 0


def test_retention_requires_explicit_proof_and_atomically_purges_with_receipt(
    tmp_path: Path,
) -> None:
    path, row = _seed_local_store(tmp_path)
    source, _, _ = _source_with_label(row)
    append_forward_settlement_labels(
        path,
        source,
        expected_generation=1,
        materialized_at_utc="2026-07-28T02:00:00Z",
        allowed_root=path.parent,
    )
    source.close()
    with pytest.raises(Exception, match="lineage_proof_(required|scope_mismatch)"):
        tombstone_forward_observations(
            path,
            lineage_proofs=[],
            observation_keys=[row["observation_key"]],
            retention_before_utc="2026-07-29T00:00:00Z",
            expected_generation=2,
            purged_at_utc="2026-07-29T01:00:00Z",
            allowed_root=path.parent,
        )
    scorecard_receipt = {
        "schema": "bayes_basket_target_forward_scorecard_retention_receipt_v1",
        "observation_key": row["observation_key"],
        "decision_id": row["decision_id"],
        "label_revision": 1,
        "complete": True,
    }
    scorecard_receipt["content_sha256"] = content_sha256(scorecard_receipt)
    attribution_receipt = {
        "schema": "bayes_basket_target_forward_attribution_retention_receipt_v1",
        "observation_key": row["observation_key"],
        "decision_id": row["decision_id"],
        "label_revision": 1,
        "complete": True,
    }
    attribution_receipt["content_sha256"] = content_sha256(
        attribution_receipt
    )
    proof = {
        "schema": "bayes_basket_target_forward_retention_lineage_proof_v1",
        "observation_key": row["observation_key"],
        "decision_id": row["decision_id"],
        "label_revision": 1,
        "retention_eligible": True,
        "scorecard_complete": True,
        "attribution_complete": True,
        "downstream_dependencies": [],
        "scorecard_receipt": scorecard_receipt,
        "attribution_receipt": attribution_receipt,
    }
    proof["content_sha256"] = content_sha256(proof)
    purged = tombstone_forward_observations(
        path,
        lineage_proofs=[proof],
        observation_keys=[row["observation_key"]],
        retention_before_utc="2026-07-29T00:00:00Z",
        expected_generation=2,
        purged_at_utc="2026-07-29T01:00:00Z",
        allowed_root=path.parent,
    )
    assert purged["tombstoned_count"] == 1
    assert len(purged["purge_receipts"]) == 1
    assert read_forward_observation_packet(path, allowed_root=path.parent)["observations"] == []
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            connection.execute("DELETE FROM forward_retention_tombstone")


def _legacy_skip_rows(path: Path, *, permanent: int, quarantined: int) -> None:
    """Rows as the lane wrote them before the ledger: one per skipped label."""

    with sqlite3.connect(path) as stored:
        rowid = 100
        for index in range(permanent):
            rowid += 1
            reason = (
                "FORWARD_SOURCE_ROW_REWRITTEN:decision-x" if index % 7 == 0
                else "INELIGIBLE_DELTA_V2_NOT_AUTHORIZED"
            )
            stored.execute(
                "INSERT INTO forward_label_ingest_state VALUES(?,?,?,?,?,?)",
                (rowid, f"d2label_p{index}", f"{index:064x}", "SKIPPED_INELIGIBLE",
                 reason, "2026-10-01T00:00:00Z"),
            )
        for index in range(quarantined):
            rowid += 1
            stored.execute(
                "INSERT INTO forward_label_ingest_state VALUES(?,?,?,?,?,?)",
                (rowid, f"d2label_q{index}", f"{index + 10**6:064x}",
                 "SKIPPED_INELIGIBLE", "FORWARD_OBSERVATION_QUARANTINED:x",
                 "2026-10-01T00:00:00Z"),
            )


def test_legacy_permanent_skip_rows_fold_into_the_ledger_and_the_file_shrinks(
    tmp_path: Path,
) -> None:
    """2026-10-15 17:07Z the store hit its 64 MiB ceiling: 147,815 of its
    149,907 label-ingest rows were permanent skips, 5-7k a day, kept for
    nothing.  Folding them keeps every count and the cursor."""

    from bayes_basket_target.forward_observation_store import (
        compact_permanent_label_skips,
    )

    path, _row = _seed_local_store(tmp_path)
    _legacy_skip_rows(path, permanent=3000, quarantined=5)
    before = read_forward_store_state(path, allowed_root=path.parent)
    assert before["skipped_ineligible_label_count"] == 3005
    assert before["last_source_label_rowid"] == 3105
    with sqlite3.connect(path) as stored:
        stored.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size_before = path.stat().st_size

    receipt = compact_permanent_label_skips(
        path, compacted_at_utc="2026-10-16T02:00:00Z", allowed_root=path.parent
    )
    assert receipt["state"] == "compacted" and receipt["deleted"] == 3000
    assert receipt["store_bytes_after"] < size_before
    after = read_forward_store_state(path, allowed_root=path.parent)
    for key in ("skipped_ineligible_label_count", "last_source_label_rowid",
                "label_count", "generation", "content_sha256"):
        assert after[key] == before[key], key
    assert after["permanent_skip_reasons"] == {
        "FORWARD_SOURCE_ROW_REWRITTEN": 429,
        "INELIGIBLE_DELTA_V2_NOT_AUTHORIZED": 2571,
    }
    with sqlite3.connect(path) as stored:
        assert stored.execute(
            "SELECT COUNT(*) FROM forward_label_ingest_state"
        ).fetchone()[0] == 5  # the quarantined, which may still be repaired
    again = compact_permanent_label_skips(
        path, compacted_at_utc="2026-10-16T02:05:00Z", allowed_root=path.parent
    )
    assert again["state"] == "already_compact"


def test_a_store_over_its_ceiling_with_legacy_skips_opens_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bayes_basket_target.forward_observation_store as store

    path, _row = _seed_local_store(tmp_path)
    _legacy_skip_rows(path, permanent=3000, quarantined=0)
    with sqlite3.connect(path) as stored:
        stored.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    over = path.stat().st_size
    monkeypatch.setattr(store, "MAX_STORE_BYTES", over - 1)
    state = read_forward_store_state(path, allowed_root=path.parent)
    assert state["skipped_ineligible_label_count"] == 3000
    assert path.stat().st_size < over
