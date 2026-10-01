"""Charter 238: the WS book and the inventory/order-state snapshots join the cold tier.

User ruling 2026-10-01 「扩」.  Measured that day, charter 237's seven tables
covered 45% of what the SSOT gains a day; the WS book alone was 26%.  These
tests pin what each new window protects, and that offline readers of the whole
history read the cold rows instead of a silently shorter table.
"""

from __future__ import annotations

import ast
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
from bayes_basket_target.offloaded_paths import OffloadedVolumeMissing  # noqa: E402
from scripts import ssot_cold_offload as off  # noqa: E402

AS_OF = datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)   # cutoff: event dates before 09-20
WS = "basket_official_ws_snapshot"
INVENTORY = "basket_confirmed_inventory_snapshot"
ORDER_STATE = "basket_order_state_snapshot"
NEW_TABLES = (WS, INVENTORY, ORDER_STATE)

#: The offline readers of the whole book history (charter 238).
WHOLE_HISTORY_READERS = (
    "scripts/probe_model_vs_market_logscore.py",
    "scripts/probe_price_move_information.py",
    "scripts/probe_center_vs_inputs.py",
    "scripts/measure_venue_price_calibration.py",
    "scripts/edge_attribution.py",
    "scripts/trace_tiebreak_entry_paths.py",
    "scripts/refit_posterior.py",
)


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


def _run(store: Path, cold: Path, tmp_path: Path, **kwargs):
    return off.run(
        ssot_path=store, cold_root=cold, as_of=AS_OF, local_reference=None, min_cold_free_bytes=0,
        manifest=tmp_path / "manifest.jsonl", holds_path=tmp_path / "holds.json",
        sleep=lambda _seconds: None, **kwargs,
    )


def _book(conn, sid: str, station: str, day: str, sequence: int) -> None:
    conn.execute(
        f"INSERT INTO {WS} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sid, "m", station, day, "UTC", sequence, f"{day}T00:00:0{sequence}Z", f"qv-{sid}",
            f"{day}T00:00:00Z", f"{day}T00:05:00Z", _sha("fresh"), "OFFICIAL_CLOB_WS", "iv", "ov",
            1.0, json.dumps([{"bucket": "20°C", "pad": "x" * 500}]), "{}", _sha(sid), "COMMITTED",
            f"{day}T00:00:00Z",
        ),
    )


def _inventory(conn, table: str, sid: str, station: str, day: str, version: str) -> None:
    conn.execute(
        f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sid, station, day, version, f"{day}T00:00:00Z", 1, "{}", _sha(sid), "COMMITTED", f"{day}T00:00:00Z"),
    )


def _live_intent(conn, station: str, day: str) -> None:
    conn.execute(
        "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,event_date,"
        "settlement_timezone,strategy_lane,decided_state,will_order,reason_codes_json,"
        "details_json,decision_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("d_live", "c_live", "plan_x", station, day, "UTC", "basket",
         "submitted", 1, "[]", "{}", _sha("d"), f"{day}T00:00:00Z"),
    )
    columns = [str(r[1]) for r in conn.execute("PRAGMA table_info(basket_target_order_intents)")]
    values = {
        "order_intent_id": "i_live", "decision_id": "d_live", "cycle_id": "c_live",
        "bucket": "20°C", "token_id": "tok", "side": "NO", "order_action": "BUY",
        "limit_price": 0.97, "requested_size": 10.0, "requested_notional_usdc": 9.7,
        "state": "filled", "live_submit_authorized": 1, "idempotency_key": "k",
        "intent_json": "{}", "intent_sha256": _sha("i"), "created_at": f"{day}T00:01:00Z",
    }
    listed = [c for c in columns if c in values]
    conn.execute(
        f"INSERT INTO basket_target_order_intents({','.join(listed)}) VALUES({','.join('?' * len(listed))})",
        tuple(values[c] for c in listed),
    )


def _fixture(store: Path) -> None:
    conn = sqlite3.connect(store)
    # KLGA 09-10: three books; only the highest sequence must stay.
    for seq in (0, 1, 2):
        _book(conn, f"ws_klga_10_{seq}", "KLGA", "2026-09-10", seq)
    # EGLC 09-11 carried real money: every book stays.
    for seq in (0, 1):
        _book(conn, f"ws_eglc_11_{seq}", "EGLC", "2026-09-11", seq)
    _live_intent(conn, "EGLC", "2026-09-11")
    # Inside the window (today-7 = 09-20 onward): stays.
    for seq in (0, 1):
        _book(conn, f"ws_klga_22_{seq}", "KLGA", "2026-09-22", seq)
    for table in (INVENTORY, ORDER_STATE):
        for n in (0, 1, 2):
            _inventory(conn, table, f"{table[:6]}_klga_10_{n}", "KLGA", "2026-09-10", f"v{n}")
        _inventory(conn, table, f"{table[:6]}_eglc_11", "EGLC", "2026-09-11", "v0")
        _inventory(conn, table, f"{table[:6]}_klga_22", "KLGA", "2026-09-22", "v0")
    conn.commit()
    conn.close()


def _ids(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {str(r[0]) for r in conn.execute(f'SELECT * FROM "{table}"')}
    finally:
        conn.close()


def test_the_new_tables_are_eligible_and_planned_off_an_index(store: Path) -> None:
    conn = sqlite3.connect(store)
    try:
        for table in NEW_TABLES:
            policy = off.POLICY_BY_TABLE[table]
            assert off.eligibility(conn, policy) == []
            detail = " ".join(
                str(row[3]) for row in conn.execute(
                    f'EXPLAIN QUERY PLAN SELECT rowid,"{policy.time_column}" FROM "{table}"'
                    f' WHERE "{policy.time_column}" < ?', ("2026-09-20",),
                )
            )
            assert "COVERING INDEX" in detail, (table, detail)
    finally:
        conn.close()


def test_old_books_move_and_the_protected_ones_stay(store: Path, cold: Path, tmp_path: Path) -> None:
    _fixture(store)
    _run(store, cold, tmp_path, apply=True, tables=list(NEW_TABLES))
    assert _ids(store, WS) == {
        "ws_klga_10_2",                     # newest sequence of its station-day
        "ws_eglc_11_0", "ws_eglc_11_1",     # real money
        "ws_klga_22_0", "ws_klga_22_1",     # inside the window
    }
    for table in (INVENTORY, ORDER_STATE):
        prefix = table[:6]
        assert _ids(store, table) == {f"{prefix}_klga_10_2", f"{prefix}_eglc_11", f"{prefix}_klga_22"}
    cold_conn = off.open_cold_table(WS, root=cold)
    try:
        assert {r[0] for r in cold_conn.execute(f"SELECT snapshot_id FROM {WS}")} == {"ws_klga_10_0", "ws_klga_10_1"}
    finally:
        cold_conn.close()


def test_the_next_sequence_is_what_it_was(store: Path, cold: Path, tmp_path: Path) -> None:
    _fixture(store)
    query = f"SELECT COALESCE(MAX(sequence),-1)+1 FROM {WS} WHERE station_id=? AND event_date=?"
    conn = sqlite3.connect(store)
    before = conn.execute(query, ("KLGA", "2026-09-10")).fetchone()[0]
    conn.close()
    _run(store, cold, tmp_path, apply=True, tables=[WS])
    conn = sqlite3.connect(store)
    after = conn.execute(query, ("KLGA", "2026-09-10")).fetchone()[0]
    conn.close()
    assert before == after == 3
    ingress = (ROOT / "bayes_basket_target" / "market_ingress.py").read_text(encoding="utf-8")
    assert query in ingress


def test_an_offline_reader_sees_the_whole_history(store: Path, cold: Path, tmp_path: Path) -> None:
    _fixture(store)
    conn = sqlite3.connect(store)
    everything = {tuple(r) for r in conn.execute(f"SELECT * FROM {WS}")}
    conn.close()
    _run(store, cold, tmp_path, apply=True, tables=[WS])
    reader = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        attached = off.attach_cold_history(reader, [WS], root=cold, manifest=tmp_path / "manifest.jsonl")
        reader.execute("PRAGMA query_only=1")
        assert set(attached) == {WS}
        assert {tuple(r) for r in reader.execute(f"SELECT * FROM {WS}")} == everything
        # The reader's own filters reach the cold rows too.
        assert reader.execute(
            f"SELECT COUNT(*) FROM {WS} WHERE station_id='KLGA' AND event_date='2026-09-10'"
        ).fetchone()[0] == 3
    finally:
        reader.close()


def test_a_row_in_both_places_is_read_once(store: Path, cold: Path, tmp_path: Path) -> None:
    _fixture(store)
    _run(store, cold, tmp_path, apply=True, tables=[WS])
    # A crash between copy and delete: put one moved row back in the SSOT.
    cold_conn = off.open_cold_table(WS, root=cold)
    row = cold_conn.execute(f"SELECT rowid, * FROM {WS} LIMIT 1").fetchone()
    cold_conn.close()
    conn = sqlite3.connect(store)
    conn.execute(f"INSERT INTO {WS}(rowid,{','.join(off._columns(conn, WS))}) VALUES({','.join('?' * len(row))})", row)
    conn.commit()
    conn.close()
    reader = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        off.attach_cold_history(reader, [WS], root=cold, manifest=tmp_path / "manifest.jsonl")
        assert reader.execute(f"SELECT COUNT(*) FROM {WS} WHERE snapshot_id=?", (row[1],)).fetchone()[0] == 1
    finally:
        reader.close()


def test_a_table_never_moved_reads_as_it_is(store: Path, cold: Path, tmp_path: Path) -> None:
    _fixture(store)
    reader = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        assert off.attach_cold_history(reader, [WS], root=cold, manifest=tmp_path / "none.jsonl") == {}
        assert reader.execute(f"SELECT COUNT(*) FROM {WS}").fetchone()[0] == 7
    finally:
        reader.close()


def test_moved_rows_on_a_missing_volume_fail_closed(store: Path, cold: Path, tmp_path: Path) -> None:
    _fixture(store)
    _run(store, cold, tmp_path, apply=True, tables=[WS])
    link = tmp_path / "unplugged"
    link.symlink_to(tmp_path / "nowhere")
    reader = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        with pytest.raises((OffloadedVolumeMissing, FileNotFoundError)):
            off.attach_cold_history(reader, [WS], root=link, manifest=tmp_path / "manifest.jsonl")
    finally:
        reader.close()


def test_every_whole_history_reader_attaches_the_cold_rows_before_reading() -> None:
    for name in WHOLE_HISTORY_READERS:
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "attach_cold_history"
        ]
        assert calls, name
        tables = {elt.value for call in calls for arg in call.args[1:2] for elt in getattr(arg, "elts", [])}
        assert WS in tables, name


def test_the_windows_are_tied_to_the_paper_ledger() -> None:
    for table in NEW_TABLES:
        policy = off.POLICY_BY_TABLE[table]
        assert policy.time_column == "event_date"
        assert policy.hot_days > off.PAPER_LEDGER_EVENT_DAYS
        assert policy.keep_station_days is not None and policy.keep_station_days.live_only
        assert policy.keep_latest[0].key == ("station_id", "event_date")
    from bayes_basket_target import certain_win_recycle

    assert certain_win_recycle.MAX_EVENT_AGE_DAYS < off.POLICY_BY_TABLE[INVENTORY].hot_days


def test_charter_238_names_the_tables() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    article = next((line for line in charter.splitlines() if line.startswith("238. ")), "")
    assert "attach_cold_history" in article
    for table in NEW_TABLES:
        assert table in article
