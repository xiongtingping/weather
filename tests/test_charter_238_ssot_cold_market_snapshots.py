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
            sid, "m", station, day, "UTC", sequence, f"{day}T00:00:0{sequence}.000Z", f"qv-{sid}",
            f"{day}T00:00:0{sequence}.000Z", f"{day}T00:05:00Z", _sha("fresh"), "OFFICIAL_CLOB_WS", "iv", "ov",
            1.0, json.dumps([{"bucket": "20°C", "pad": "x" * 500}]), "{}", _sha(sid), "COMMITTED",
            f"{day}T00:00:00Z",
        ),
    )


def _inventory(conn, table: str, sid: str, station: str, day: str, version: str) -> None:
    conn.execute(
        f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sid, station, day, version, f"{day}T00:00:00Z", 1, "{}", _sha(sid), "COMMITTED", f"{day}T00:00:00Z"),
    )


def _vector(conn, n: int, station: str, day: str, *, ws: str, instant: str, inventory: str, order_state: str) -> None:
    """One executed order's version vector: the hashes of the rows it was bound to."""

    cycle = f"cyc_{n}"
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (cycle, f"plan_{n}", station, day, "UTC", "basket", _sha(f"plan{n}"), 1.0, "settled",
         f"{day}T00:00:00Z", f"{day}T00:00:00Z"),
    )
    columns = {
        "order_intent_id": f"i_{n}", "cycle_id": cycle, "decision_id": f"d_{n}",
        "vector_policy": "EXECUTION", "target_price_id": "tp", "quote_observed_at": instant,
        "quote_expires_at": f"{day}T00:05:00Z", "probability_sha256": _sha("p"),
        "official_ws_quote_sha256": ws, "inventory_sha256": inventory, "order_state_sha256": order_state,
        "station_policy_sha256": _sha("sp"), "cash_policy_decision_sha256": _sha("cp"),
        "strict_delta_artifact_sha256": _sha("sd"), "vector_json": "{}", "vector_sha256": _sha(f"v{n}"),
        "created_at": f"{day}T00:00:00Z",
    }
    conn.execute(
        f"INSERT INTO basket_target_execution_version_vectors({','.join(columns)})"
        f" VALUES({','.join('?' * len(columns))})", tuple(columns.values()),
    )


BOOK_OLD, BOOK_HOT = "2026-08-10", "2026-09-10"     # the book window is 30 days: cutoff 08-28
STATE_OLD, STATE_HOT = "2026-09-10", "2026-09-22"   # the state window is 7 days: cutoff 09-20


def _fixture(store: Path) -> None:
    conn = sqlite3.connect(store)
    # KLGA: three books; only the highest sequence must stay.
    for seq in (0, 1, 2):
        _book(conn, f"ws_klga_{seq}", "KLGA", BOOK_OLD, seq)
    # EGLC: an order executed against book 0 -- it stays, book 1 moves
    # unless it is the newest of its day, so give the day a third book.
    for seq in (0, 1, 2):
        _book(conn, f"ws_eglc_{seq}", "EGLC", BOOK_OLD, seq)
    # ZUUU: an order names a book that is not there -- the whole day stays.
    for seq in (0, 1):
        _book(conn, f"ws_zuuu_{seq}", "ZUUU", BOOK_OLD, seq)
    # Inside the 30-day window: stays (the dashboard's valuation population).
    for seq in (0, 1):
        _book(conn, f"ws_hot_{seq}", "KLGA", BOOK_HOT, seq)
    for table in (INVENTORY, ORDER_STATE):
        for n in (0, 1, 2):
            _inventory(conn, table, f"{table[:6]}_klga_{n}", "KLGA", STATE_OLD, f"v{n}")
            _inventory(conn, table, f"{table[:6]}_eglc_{n}", "EGLC", STATE_OLD, f"v{n}")
        _inventory(conn, table, f"{table[:6]}_hot", "KLGA", STATE_HOT, "v0")
    # The vector spells the instant with microseconds, the book with millis.
    _vector(conn, 1, "EGLC", BOOK_OLD, ws=_sha("ws_eglc_0"), instant=f"{BOOK_OLD}T00:00:00.000000Z",
            inventory=_sha("basket_eglc_0"), order_state=_sha("basket_eglc_0"))
    _vector(conn, 2, "EGLC", STATE_OLD, ws=_sha("absent"), instant=f"{STATE_OLD}T00:00:00.000000Z",
            inventory=_sha(f"{INVENTORY[:6]}_eglc_0"), order_state=_sha(f"{ORDER_STATE[:6]}_eglc_0"))
    _vector(conn, 3, "ZUUU", BOOK_OLD, ws=_sha("not_written"), instant=f"{BOOK_OLD}T00:00:00.000000Z",
            inventory=_sha("x"), order_state=_sha("x"))
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
    receipt = _run(store, cold, tmp_path, apply=True, tables=list(NEW_TABLES))
    assert _ids(store, WS) == {
        "ws_klga_2",                  # newest sequence of its station-day
        "ws_eglc_0",                  # an executed order's book, found across spellings
        "ws_eglc_2",                  # newest of its day
        "ws_zuuu_0", "ws_zuuu_1",     # a named book missing: the whole day stays
        "ws_hot_0", "ws_hot_1",       # inside the window
    }
    for table in (INVENTORY, ORDER_STATE):
        prefix = table[:6]
        assert _ids(store, table) == {
            f"{prefix}_klga_2", f"{prefix}_eglc_2",   # newest of each day
            f"{prefix}_eglc_0",                       # named by an executed order
            f"{prefix}_hot",
        }
    kept = {plan["table"]: plan["kept"] for plan in receipt["tables"]}
    assert kept[WS].get("execution_vector") == 1
    assert kept[WS].get("execution_vector_unresolved_station_day") == 2
    cold_conn = off.open_cold_table(WS, root=cold)
    try:
        assert {r[0] for r in cold_conn.execute(f"SELECT snapshot_id FROM {WS}")} == {
            "ws_klga_0", "ws_klga_1", "ws_eglc_1",
        }
    finally:
        cold_conn.close()


def test_the_next_sequence_is_what_it_was(store: Path, cold: Path, tmp_path: Path) -> None:
    _fixture(store)
    query = f"SELECT COALESCE(MAX(sequence),-1)+1 FROM {WS} WHERE station_id=? AND event_date=?"
    conn = sqlite3.connect(store)
    before = conn.execute(query, ("KLGA", BOOK_OLD)).fetchone()[0]
    conn.close()
    _run(store, cold, tmp_path, apply=True, tables=[WS])
    conn = sqlite3.connect(store)
    after = conn.execute(query, ("KLGA", BOOK_OLD)).fetchone()[0]
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
            f"SELECT COUNT(*) FROM {WS} WHERE station_id='KLGA' AND event_date='{BOOK_OLD}'"
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
        assert reader.execute(f"SELECT COUNT(*) FROM {WS}").fetchone()[0] == 10
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


def test_the_windows_are_tied_to_their_readers() -> None:
    from bayes_basket_target import valuation_store

    for table in NEW_TABLES:
        policy = off.POLICY_BY_TABLE[table]
        assert policy.time_column == "event_date"
        assert policy.hot_days > off.PAPER_LEDGER_EVENT_DAYS
        assert policy.keep_station_days is None
        assert policy.keep_vector_refs is not None
        assert policy.keep_latest[0].key == ("station_id", "event_date")
    # The dashboard's charter-EV page joins the valuation population's facts
    # to their books over WINDOW_DAYS; the posterior page over 14 days.
    assert off.POLICY_BY_TABLE[WS].hot_days > valuation_store.WINDOW_DAYS
    dashboard = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "realisation(\n            conn, since_event_date=(date.today() - timedelta(days=14))" in dashboard
    assert off.POLICY_BY_TABLE[WS].hot_days > 14
    runtime = (ROOT / "bayes_basket_target" / "execution_transition_runtime.py").read_text(encoding="utf-8")
    for table, column in ((WS, "official_ws_quote_sha256"), (INVENTORY, "inventory_sha256"),
                          (ORDER_STATE, "order_state_sha256")):
        assert off.POLICY_BY_TABLE[table].keep_vector_refs.vector_column == column
        assert f'"SELECT * FROM {table} WHERE content_sha256=?"' in runtime
        assert f'vector["{column}"]' in runtime
    from bayes_basket_target import certain_win_recycle

    assert certain_win_recycle.MAX_EVENT_AGE_DAYS < off.POLICY_BY_TABLE[INVENTORY].hot_days


def test_charter_238_names_the_tables() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    article = next((line for line in charter.splitlines() if line.startswith("238. ")), "")
    assert "attach_cold_history" in article
    for table in NEW_TABLES:
        assert table in article
