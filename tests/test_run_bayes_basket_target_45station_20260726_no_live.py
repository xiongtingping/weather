from __future__ import annotations

import builtins
import hashlib
import http.client
import json
import socket
import sqlite3
import urllib.request
from pathlib import Path

import pytest

from scripts import run_bayes_basket_target_45station_20260726_no_live as runner
from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.station_registry import coordinate_registry_manifest


# The fleet itself, not a copy of it: a hand-written tuple was a second
# place for the roster to be wrong, and it went stale the moment three
# stations were onboarded.
CANONICAL_STATIONS = tuple(
    sorted(coordinate_registry_manifest()["station_ids"])
)


def test_read_only_connection_is_wal_safe_and_query_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resident WAL view must be readable without allowing writes.

    ``immutable=1`` is unsafe here because it ignores the WAL sidecar.  Keep
    an uncheckpointed WAL row visible and verify the runner sees it through
    ``mode=ro`` while SQLite still rejects mutations.
    """
    target_db = tmp_path / "target.sqlite"
    writer = sqlite3.connect(target_db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        writer.execute("INSERT INTO marker(value) VALUES ('wal-visible')")
        writer.commit()
        monkeypatch.setattr(runner, "TARGET_DB", target_db)
        with runner._read_only_connection() as conn:
            assert conn.execute("SELECT value FROM marker").fetchone()[0] == "wal-visible"
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("CREATE TABLE forbidden(value TEXT)")
    finally:
        writer.close()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    station_ids: tuple[str, ...] = CANONICAL_STATIONS,
    adversarial_arrivals: bool = False,
) -> tuple[Path, Path]:
    output_dir = tmp_path / "isolated-output"
    target_db = tmp_path / "target.db"
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()

    with sqlite3.connect(target_db) as conn:
        conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        conn.execute("INSERT INTO marker VALUES ('read-only-target')")

    _write_json(
        input_dir / "contract.json",
        {
            "schema": "bayes_basket_target_model_run_contract_v1",
            "run_initialization_utc": runner.RUN_INITIALIZATION,
            # Retained contract is for the previous local date, not the target
            # settlement date.  It must not be repurposed by a current fetch.
            "target_local_date": "2026-07-25",
            "station_ids": list(station_ids),
            "contract_sha256": "c" * 64,
        },
    )

    arrival_paths: dict[str, Path] = {}
    decode_paths: dict[str, Path] = {}
    for model in runner.MODEL_IDS:
        arrival_path = input_dir / f"arrival-{model}.json"
        decode_path = input_dir / f"decode-{model}.json"
        arrival_paths[model] = arrival_path
        decode_paths[model] = decode_path
        arrivals: list[dict[str, object]] = []
        for station_id in station_ids:
            base = {
                "station_id": station_id,
                "model_id": model,
                "run_initialization_utc": runner.RUN_INITIALIZATION,
                # Every retained object is after the historical cutoff.
                "published_at_utc": "2026-07-25T00:05:00Z",
                "received_at_utc": "2026-07-25T00:06:00Z",
                "decision_at_utc": "2026-07-25T00:07:00Z",
                "target_local_date": "2026-07-25",
                "target_timezone": "America/New_York",
                "forecast_high_c": 38,
            }
            if adversarial_arrivals:
                # Duplicate plus low→high→low rewind.  The runner must remain
                # fail-closed even when hostile history is presented as input.
                arrivals.extend(
                    [
                        {**base, "forecast_high_c": 38},
                        {**base, "forecast_high_c": 41},
                        {**base, "forecast_high_c": 39},
                        {**base, "forecast_high_c": 38},
                    ]
                )
            else:
                arrivals.append(base)
        _write_json(arrival_path, {"arrivals": arrivals})
        _write_json(
            decode_path,
            {
                "curves": {
                    "curves": [
                        {
                            "station_id": station_id,
                            "model_id": model,
                            "event_date": "2026-07-25",
                            "target_local_date": "2026-07-25",
                            "target_timezone": "America/New_York",
                            "state": "COMPLETE",
                            "curve_sha256": hashlib.sha256(
                                f"{model}:{station_id}".encode()
                            ).hexdigest(),
                        }
                        for station_id in station_ids
                    ]
                }
            },
        )

    monkeypatch.setattr(runner, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(runner, "TARGET_DB", target_db)
    monkeypatch.setattr(runner, "CONTRACT_PATH", input_dir / "contract.json")
    monkeypatch.setattr(runner, "ARRIVAL_PATHS", arrival_paths)
    monkeypatch.setattr(runner, "DECODE_PATHS", decode_paths)
    return output_dir, target_db


def _network_and_gateway_tripwires(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unexpected network or gateway call")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(http.client, "HTTPConnection", forbidden)
    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)

    real_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        root = name.split(".", 1)[0]
        if root in {"requests", "httpx", "websocket", "clob", "gateway"}:
            raise AssertionError(f"unexpected runtime import: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def _assert_receipt_integrity(receipt_path: Path) -> dict[str, object]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert isinstance(receipt, dict)
    receipt_core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    expected_receipt_sha = hashlib.sha256(
        runner._canonical(receipt_core)
    ).hexdigest()
    assert receipt["receipt_sha256"] == expected_receipt_sha

    run_core = {
        "schema": receipt["schema"],
        "event_date": receipt["event_date"],
        "decision_cutoff_utc": receipt["decision_cutoff_utc"],
        "run_initialization_utc": receipt["run_initialization_utc"],
        "station_ids": receipt["station_ids"],
        "rows": receipt["rows"],
    }
    assert receipt["run_sha256"] == hashlib.sha256(
        runner._canonical(run_core)
    ).hexdigest()
    return receipt


def _assert_isolated_sqlite(sqlite_path: Path) -> None:
    with sqlite3.connect(sqlite_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {
            "run_metadata",
            "station_chain_status",
            "no_live_audit_decision",
        }
        assert conn.execute("SELECT COUNT(*) FROM station_chain_status").fetchone()[0] == expected_station_count()
        assert conn.execute("SELECT COUNT(*) FROM no_live_audit_decision").fetchone()[0] == expected_station_count()
        metadata = conn.execute(
            "SELECT station_count,execution_mode,live_submit_authorized,"
            "calls_order_gateway,orders_submitted FROM run_metadata"
        ).fetchone()
        assert tuple(metadata) == (expected_station_count(), "production_no_live", 0, 0, 0)


def _assert_baseline(receipt: dict[str, object], output_dir: Path, target_db: Path) -> None:
    assert receipt["schema"] == runner.SCHEMA
    assert receipt["event_date"] == "2026-07-26"
    assert receipt["decision_cutoff_utc"] == "2026-07-25T00:00:00Z"
    assert receipt["state"] == "FAIL_CLOSED"
    assert receipt["contract_scope"]["state"] == "RETAINED_SCOPE_MISMATCH"

    summary = receipt["summary"]
    assert summary["station_count"] == expected_station_count()
    assert summary["fail_closed_station_count"] == expected_station_count()
    assert summary["orders_submitted"] == 0
    assert summary["intents_written"] == 0
    assert summary["calls_order_gateway"] is False
    assert summary["writes_production_db"] is False
    assert summary["live_submit_authorized"] is False
    assert summary["bounded_fetch_attempted"] is False
    assert "cannot create valid prospective as-of evidence" in summary["bounded_fetch_skip_reason"]

    boundary = receipt["boundary"]
    assert boundary["execution_mode"] == "production_no_live"
    assert boundary["network_used"] is False
    assert boundary["calls_order_gateway"] is False
    assert boundary["orders_submitted"] == 0
    assert boundary["writes_production_db"] is False
    assert boundary["writes_real_ssot"] is False
    assert boundary["live_submit_authorized"] is False
    assert Path(boundary["isolated_sqlite"]).parent == output_dir

    rows = receipt["rows"]
    assert len(rows) == expected_station_count()
    for row in rows:
        assert row["final_state"] == "FAIL_CLOSED"
        assert row["stages"]["model_arrival_decode_exact_join"]["state"] == "BLOCKED"
        reasons = row["stages"]["model_arrival_decode_exact_join"]["reason_codes"]
        assert "three_model_exact_join_incomplete" in reasons
        assert any("arrival_not_visible_as_of_cutoff" in item for item in reasons)
        assert row["stages"]["no_live_decision_reconcile"]["orders_submitted"] == 0
        assert row["stages"]["no_live_decision_reconcile"]["intents_written"] == 0

    budget = receipt["resource_budget"]
    assert budget["observed_input_bytes"] <= budget["max_total_input_bytes"]
    assert budget["observed_json_nodes"] <= runner.MAX_JSON_NODES * len(receipt["input_artifacts"])
    assert budget["observed_max_json_depth"] <= runner.MAX_JSON_DEPTH
    assert budget["rss_bytes_before"] <= budget["max_rss_bytes"]
    assert budget["rss_bytes_after"] <= budget["max_rss_bytes"]
    assert budget["free_bytes_before"] >= budget["minimum_free_bytes"]
    assert budget["free_bytes_after"] >= budget["minimum_free_bytes"]
    assert sum(item["bytes"] for item in receipt["input_artifacts"]) == budget["observed_input_bytes"]
    assert all(item["bytes"] <= budget["max_input_file_bytes"] for item in receipt["input_artifacts"])
    output_files = [path for path in output_dir.iterdir() if path.is_file()]
    assert {path.name for path in output_files} == {"isolated_no_live.sqlite", "receipt.json"}
    assert all(path.stat().st_size <= budget["max_output_file_bytes"] for path in output_files)
    assert sum(path.stat().st_size for path in output_files) <= budget["max_total_output_bytes"]
    _assert_isolated_sqlite(output_dir / "isolated_no_live.sqlite")

    assert receipt["target_ssot_fingerprint_before"] == receipt["target_ssot_fingerprint_after"]
    assert target_db.is_file()


def test_canonical_45station_historical_cutoff_is_fail_closed_and_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir, target_db = _fixture(tmp_path, monkeypatch)
    _network_and_gateway_tripwires(monkeypatch)
    assert runner.main() == 0

    receipt = _assert_receipt_integrity(output_dir / "receipt.json")
    _assert_baseline(receipt, output_dir, target_db)


def test_receipt_tamper_is_detected_by_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir, _ = _fixture(tmp_path, monkeypatch)
    original_write = runner._write_receipt

    def tampering_write(path: Path, receipt: dict[str, object]) -> None:
        tampered = dict(receipt)
        tampered["state"] = "EXECUTABLE"
        original_write(path, tampered)

    monkeypatch.setattr(runner, "_write_receipt", tampering_write)
    assert runner.main() == 0
    with pytest.raises(AssertionError):
        _assert_receipt_integrity(output_dir / "receipt.json")


def test_target_ssot_fingerprint_drift_fails_closed_before_isolated_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir, _ = _fixture(tmp_path, monkeypatch)
    fingerprints = iter(
        [
            {"device": 1, "inode": 2, "size": 3, "mtime_ns": 4},
            {"device": 1, "inode": 2, "size": 3, "mtime_ns": 5},
        ]
    )
    monkeypatch.setattr(runner, "_db_fingerprint", lambda: next(fingerprints))

    with pytest.raises(runner.FailClosed, match="target_ssot_metadata_changed_during_read"):
        runner.main()
    assert output_dir.is_dir()
    assert list(output_dir.iterdir()) == []


def test_rss_over_budget_fails_closed_before_output_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_rss_bytes", lambda: runner.MAX_RSS_BYTES + 1)

    with pytest.raises(runner.FailClosed, match="rss_limit_exceeded"):
        runner.main()
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "count",
    [expected_station_count() - 1, expected_station_count() + 1],
)
def test_station_row_cap_rejects_underflow_and_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    station_ids = (
        tuple(CANONICAL_STATIONS[:count])
        if count < len(CANONICAL_STATIONS)
        else CANONICAL_STATIONS + ("XTRA",)
    )
    output_dir, _ = _fixture(tmp_path, monkeypatch, station_ids=station_ids)

    with pytest.raises(runner.FailClosed, match=f"canonical_station_count_not_{expected_station_count()}"):
        runner.main()
    assert list(output_dir.iterdir()) == []


def test_json_shape_accepts_exact_boundaries_and_rejects_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "MAX_JSON_DEPTH", 3)
    assert runner._json_shape({"a": {"b": 1}}) == (3, 3)
    with pytest.raises(runner.FailClosed, match="json_shape_limit_exceeded"):
        runner._json_shape({"a": {"b": {"c": 1}}})

    monkeypatch.setattr(runner, "MAX_JSON_NODES", 3)
    assert runner._json_shape({"a": 1, "b": 2}) == (3, 2)
    with pytest.raises(runner.FailClosed, match="json_shape_limit_exceeded"):
        runner._json_shape({"a": 1, "b": 2, "c": 3})


def test_duplicate_and_low_high_low_rewind_never_promote_historical_arrival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir, target_db = _fixture(
        tmp_path, monkeypatch, adversarial_arrivals=True
    )
    assert runner.main() == 0
    receipt = _assert_receipt_integrity(output_dir / "receipt.json")
    _assert_baseline(receipt, output_dir, target_db)

    for row in receipt["rows"]:
        for model in runner.MODEL_IDS:
            model_status = row["stages"]["model_arrival_decode_exact_join"]["models"][model]
            assert model_status["arrival_count"] == 4
            assert model_status["arrival_visible_count"] == 0
        assert row["stages"]["basket"]["target_inventory_materialized"] is False
        assert row["stages"]["basket"]["intents_generated"] == 0
