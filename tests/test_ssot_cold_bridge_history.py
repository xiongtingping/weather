"""Charter 237 amendment (2026-11-01, user 「Do bridge tables」): the bridge history.

The probability bridge history and its bucket probabilities had no rule and
only grew (4.6 GiB).  These tests pin what the window protects -- the rows
the samplers and the board's traded calibration walk, the ladder each live
decision's seat was priced on, each station's newest decision -- and that a
history row's bucket probabilities leave with it, come back with it, and stop
the table when they changed after their copy.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import serving_arithmetic, ssot  # noqa: E402
from scripts import ssot_cold_offload as off  # noqa: E402

AS_OF = datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)   # window: event dates before 08-11 leave
BRIDGE = "basket_probability_bridge_history"
BUCKETS = "basket_probability_bridge_bucket_probability"


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


def _unfloored() -> off.TablePolicy:
    """The registered policy without its run-time floor (the fixture has no anchor)."""

    return dataclasses.replace(off.POLICY_BY_TABLE[BRIDGE], floors=())


def _run(store: Path, cold: Path, tmp_path: Path, **kwargs):
    return off.run(
        ssot_path=store, cold_root=cold, as_of=AS_OF, local_reference=None, min_cold_free_bytes=0,
        manifest=tmp_path / "manifest.jsonl", holds_path=tmp_path / "holds.json",
        sleep=lambda _seconds: None, tables=[BRIDGE], policies=[_unfloored()], **kwargs,
    )


def _bridge(conn, hid: str, station: str, day: str, stage: str, decided: str, *, buckets: int = 3) -> None:
    conn.execute(
        f"INSERT INTO {BRIDGE}(history_id,station_id,event_date,settlement_timezone,stage,checkpoint_id,"
        "decision_at_utc,fresh_until_utc,envelope_content_sha256,bridge_sha256,bucket_map_sha256,"
        "bucket_intervals_json,projection_sha256,envelope_json,state,created_at_utc,content_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (hid, station, day, "UTC", stage, "cp_" + hid, decided, decided, _sha("env_" + hid), _sha("b"),
         _sha("m"), "[]", _sha("p"), "{}", "COMMITTED", decided, _sha(hid)),
    )
    for k in range(buckets):
        conn.execute(
            f"INSERT INTO {BUCKETS}(history_id,bucket,ordinal,lower_c,upper_c,probability,content_sha256)"
            " VALUES(?,?,?,?,?,?,?)",
            (hid, f"{20 + k}°C", k, 19.5 + k, 20.5 + k, 1.0 / buckets, _sha(f"{hid}:{k}")),
        )


def _live_decision(conn, did: str, station: str, day: str, binding: dict) -> None:
    conn.execute(
        "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,event_date,"
        "settlement_timezone,strategy_lane,decided_state,will_order,reason_codes_json,"
        "details_json,decision_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, "c_" + did, "plan_" + did, station, day, "UTC", "basket", "submitted", 1, "[]",
         json.dumps({"forward_evidence_binding": binding}), _sha(did), f"{day}T00:00:00Z"),
    )
    columns = [str(r[1]) for r in conn.execute("PRAGMA table_info(basket_target_order_intents)")]
    values = {
        "order_intent_id": "i_" + did, "decision_id": did, "cycle_id": "c_" + did, "bucket": "20°C",
        "token_id": "tok", "side": "YES", "order_action": "BUY", "limit_price": 0.4,
        "requested_size": 5.0, "requested_notional_usdc": 2.0, "state": "filled",
        "live_submit_authorized": 1, "idempotency_key": "k_" + did, "intent_json": "{}",
        "intent_sha256": _sha("i" + did), "created_at": f"{day}T00:00:00Z",
    }
    listed = [c for c in columns if c in values]
    conn.execute(
        f"INSERT INTO basket_target_order_intents({','.join(listed)}) VALUES({','.join('?' * len(listed))})",
        tuple(values[c] for c in listed),
    )


def _fixture(store: Path) -> None:
    conn = sqlite3.connect(store)
    _bridge(conn, "klga_0731", "KLGA", "2026-07-31", "P1", "2026-07-31T01:00:00Z")
    _bridge(conn, "klga_0801", "KLGA", "2026-08-01", "P1", "2026-08-01T01:00:00Z")
    _bridge(conn, "klga_0802", "KLGA", "2026-08-02", "P1", "2026-08-02T01:00:00Z")
    _bridge(conn, "klga_0925", "KLGA", "2026-09-25", "P1", "2026-09-25T01:00:00Z")
    _bridge(conn, "eglc_named", "EGLC", "2026-08-02", "P1", "2026-08-01T10:00:00.123Z")
    _bridge(conn, "eglc_other", "EGLC", "2026-08-02", "P1", "2026-08-01T11:00:00Z")
    _bridge(conn, "eglc_newest", "EGLC", "2026-09-26", "P1", "2026-09-26T11:00:00Z")
    _bridge(conn, "zuuu_only", "ZUUU", "2026-07-30", "P0", "2026-07-28T05:00:00Z")
    # live_book's key: second, stage and the envelope hash the decision names.
    _live_decision(conn, "d_named", "EGLC", "2026-08-02", {
        "probability_stage": "P1", "probability_decision_at_utc": "2026-08-01T10:00:00.123000Z",
        "probability_artifact_sha256": _sha("env_eglc_named"),
    })
    conn.commit()
    conn.close()


def _ids(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {str(r[0]) for r in conn.execute(f"SELECT history_id FROM {table}")}
    finally:
        conn.close()


def test_the_bridge_is_eligible_with_its_children_and_planned_off_an_index(store: Path) -> None:
    conn = sqlite3.connect(store)
    try:
        assert off.eligibility(conn, off.POLICY_BY_TABLE[BRIDGE]) == []
        assert BUCKETS not in off.POLICY_BY_TABLE   # it leaves with its parent, never on its own
        (child,) = off.POLICY_BY_TABLE[BRIDGE].children
        assert (child.table, child.column, child.parent_column) == (BUCKETS, "history_id", "history_id")
    finally:
        conn.close()


def test_a_child_with_no_index_on_its_key_is_refused(store: Path) -> None:
    conn = sqlite3.connect(store)
    conn.execute("CREATE TABLE side(owner TEXT, v REAL)")
    policy = dataclasses.replace(
        _unfloored(), children=(off.ChildRows(table="side", column="owner", parent_column="history_id", why="t"),),
    )
    assert off.eligibility(conn, policy) == ["child_index_missing:side.owner"]
    conn.close()


def test_old_rows_move_with_their_buckets_and_the_protected_ones_stay(
    store: Path, cold: Path, tmp_path: Path
) -> None:
    _fixture(store)
    receipt = _run(store, cold, tmp_path, apply=True)

    (summary,) = receipt["tables"]
    assert summary["kept"] == {
        "live_decision_envelope": 1,          # EGLC's named ladder
        "latest_0:station_id+stage": 3,       # the newest old row of each station and stage
    }
    gone = {"klga_0731", "klga_0801"}
    assert _ids(store, BRIDGE) == {
        "klga_0802", "klga_0925", "eglc_named", "eglc_other", "eglc_newest", "zuuu_only",
    }
    assert _ids(store, BUCKETS) == _ids(store, BRIDGE)
    assert _ids(off.cold_path(cold, BRIDGE), BRIDGE) == gone
    assert _ids(off.cold_path(cold, BUCKETS), BUCKETS) == gone
    conn = sqlite3.connect(off.cold_path(cold, BUCKETS))
    assert conn.execute(f"SELECT count(*) FROM {BUCKETS}").fetchone()[0] == 6
    conn.close()
    (move,) = receipt["moves"]
    assert move["moved_rows"] == 2 and move["child_rows"] == {BUCKETS: 6}
    lines = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text().splitlines()]
    assert {line["table"] for line in lines} == {BRIDGE, BUCKETS}
    assert all(line["parent_table"] == BRIDGE for line in lines if line["table"] == BUCKETS)


def test_an_offline_reader_attaches_the_buckets_too(store: Path, cold: Path, tmp_path: Path) -> None:
    _fixture(store)
    _run(store, cold, tmp_path, apply=True)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    assert set(off.attach_cold_history(
        conn, (BRIDGE, BUCKETS), root=cold, manifest=tmp_path / "manifest.jsonl",
    )) == {BRIDGE, BUCKETS}
    assert conn.execute(
        f"SELECT count(*) FROM {BRIDGE} h JOIN {BUCKETS} b ON b.history_id=h.history_id"
    ).fetchone()[0] == 8 * 3
    conn.close()


def test_buckets_that_changed_after_their_copy_stop_the_table(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture(store)
    real_wait = off._wait_for_wal

    def add_a_bucket_then_wait(db, conn, **kwargs):
        writer = sqlite3.connect(store)
        writer.execute(
            f"INSERT INTO {BUCKETS}(history_id,bucket,ordinal,lower_c,upper_c,probability,content_sha256)"
            " VALUES('klga_0801','late',9,0,1,0.0,?)", (_sha("late"),),
        )
        writer.commit()
        writer.close()
        monkeypatch.setattr(off, "_wait_for_wal", real_wait)
        return real_wait(db, conn, **kwargs)

    monkeypatch.setattr(off, "_wait_for_wal", add_a_bucket_then_wait)
    receipt = _run(store, cold, tmp_path, apply=True)
    (move,) = receipt["moves"]
    assert move["state"] == "stopped"
    assert move["reason"] == f"ssot_child_rows_changed_since_copy:{BUCKETS}"
    # Nothing left the SSOT: the batch rolled back whole.
    assert {"klga_0731", "klga_0801"} <= _ids(store, BRIDGE)
    assert {"klga_0731", "klga_0801"} <= _ids(store, BUCKETS)


def test_restore_brings_the_buckets_back_with_their_rows(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture(store)
    conn = sqlite3.connect(store)
    before = {
        str(r[0]): tuple(r) for r in conn.execute(f"SELECT content_sha256, * FROM {BUCKETS}")
    }
    conn.close()
    _run(store, cold, tmp_path, apply=True)
    monkeypatch.setattr(off, "MANIFEST", tmp_path / "manifest.jsonl")
    result = off.restore_table(
        ssot_path=store, cold_root=cold, table=BRIDGE, since="2026-07-01",
        holds_path=tmp_path / "holds.json", sleep=lambda _s: None,
    )
    assert result["restored_rows"] == 2 and result["restored_child_rows"] == {BUCKETS: 6}
    conn = sqlite3.connect(store)
    after = {str(r[0]): tuple(r) for r in conn.execute(f"SELECT content_sha256, * FROM {BUCKETS}")}
    conn.close()
    assert after == before


def test_the_floor_is_the_samplers_anchor_and_the_traded_calibration_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bayes_basket_target import board_readings, center_width_store

    monkeypatch.setattr(center_width_store, "evidence_anchor", lambda _conn, *, stage: None)
    with pytest.raises(off.OffloadRefused):
        off.bridge_reader_floor(sqlite3.connect(":memory:"))
    anchors = {"P0": "2026-09-14T05:52:13Z", "P0-M": "2026-09-18T19:10:09Z", "P1": "2026-09-18T19:10:09Z"}
    monkeypatch.setattr(center_width_store, "evidence_anchor", lambda _conn, *, stage: anchors[stage])
    assert board_readings.TRADED_CALIBRATION_SINCE == "2026-09-06"
    assert off.bridge_reader_floor(sqlite3.connect(":memory:")) == "2026-09-06"
    monkeypatch.setattr(board_readings, "TRADED_CALIBRATION_SINCE", "2026-09-30")
    # The P0 anchor less the decision-to-event margin.
    assert off.bridge_reader_floor(sqlite3.connect(":memory:")) == "2026-09-12"
    assert {f.name for f in off.POLICY_BY_TABLE[BRIDGE].floors} == {"bridge_readers"}


def test_the_bridge_window_is_tied_to_its_readers() -> None:
    from bayes_basket_target import board_readings

    policy = off.POLICY_BY_TABLE[BRIDGE]
    assert policy.hot_days > board_readings.SEAT_LOOKBACK_DAYS
    assert policy.keep_decision_envelopes is not None
    assert policy.keep_decision_envelopes.hash_column == "envelope_content_sha256"
    live_book = (ROOT / "scripts" / "live_book.py").read_text(encoding="utf-8")
    assert "AND h.decision_at_utc=?" in live_book   # the seat's ladder is read at its own instant
    sampler = (ROOT / "bayes_basket_target" / "band_reliability_store.py").read_text(encoding="utf-8")
    assert "cached_history_envelope_payload" in sampler   # payloads cached, rows still walked
