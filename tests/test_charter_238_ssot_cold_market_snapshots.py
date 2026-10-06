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


BOOK_OLD = "2026-08-10"                              # older than every window
BOOK_HOT = "2026-09-10"   # outside the book window (7 days, cutoff 09-20), inside its readers' 30
STATE_OLD, STATE_HOT = "2026-09-10", "2026-09-22"   # the state window is 7 days: cutoff 09-20


def _readers_of_hot_books(conn) -> None:
    """The two named readers, reduced to the columns their keep queries read."""

    conn.execute("DROP TABLE IF EXISTS basket_policy_training_fact")
    conn.execute("CREATE TABLE basket_policy_training_fact(fact_id TEXT, event_date TEXT, ws_snapshot_id TEXT)")
    conn.execute("INSERT INTO basket_policy_training_fact VALUES('f0', ?, 'ws_hot_0')", (BOOK_HOT,))
    conn.execute("DROP TABLE IF EXISTS basket_probability_envelope")
    conn.execute(
        "CREATE TABLE basket_probability_envelope(envelope_id TEXT, station_id TEXT, event_date TEXT,"
        " state TEXT, stage TEXT, decision_at_utc TEXT, fresh_until_utc TEXT)"
    )
    conn.execute(
        "CREATE INDEX basket_probability_envelope_publisher_scope_idx ON basket_probability_envelope("
        "state,event_date,station_id,decision_at_utc,stage)"
    )
    conn.executemany(
        "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?,?)",
        [
            ("e1", "KLGA", BOOK_HOT, "COMMITTED", "P0", f"{BOOK_HOT}T00:00:01.500Z", ""),
            ("e2", "KLGA", BOOK_HOT, "COMMITTED", "P1", f"{BOOK_HOT}T00:00:02.500Z", ""),
        ],
    )


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
    # Outside the 7-day window but inside its readers' 30 days: a book stays
    # when a training fact names it (charter-EV / posterior pages) or when it
    # is the newest book at or before a committed P0 envelope (the board's
    # centre/shape reading); ws_hot_3 is the newest of its day; ws_hot_2 moves.
    for seq in (0, 1, 2, 3):
        _book(conn, f"ws_hot_{seq}", "KLGA", BOOK_HOT, seq)
    _readers_of_hot_books(conn)
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
        "ws_hot_0",                   # named by a training fact
        "ws_hot_1",                   # the book at or before a P0 envelope
        "ws_hot_3",                   # newest sequence of its station-day
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
            "ws_klga_0", "ws_klga_1", "ws_eglc_1", "ws_hot_2",
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
        assert reader.execute(f"SELECT COUNT(*) FROM {WS}").fetchone()[0] == 12
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
    named = {rule.name: rule for rule in off.POLICY_BY_TABLE[WS].keep_named}
    assert named["training_fact_book"].window_days > valuation_store.WINDOW_DAYS
    assert named["p0_envelope_book"].window_days > valuation_store.WINDOW_DAYS
    # The realisation reader moved to the backend board-readings job on
    # 2026-10-02 (the board only reads its file); same 14 days.
    readings = (ROOT / "bayes_basket_target" / "board_readings.py").read_text(encoding="utf-8")
    assert "since_event_date=(_now().date() - timedelta(days=14))" in readings
    assert named["training_fact_book"].window_days > 14
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


# --------------------------------------------------------------------------
# The probability envelope (2026-10-01, 「查发布链」).
# --------------------------------------------------------------------------

ENVELOPE = "basket_probability_envelope"
ENVELOPE_WHOLE_HISTORY_READERS = (
    "scripts/measure_climate_blend.py",
    "scripts/audit_p1_hourly_curve_receipts_45station.py",
    "scripts/edge_attribution.py",
    "scripts/replay_p0_member_own_lead.py",
    "scripts/replay_p0_stable_pool.py",
)


def _scheduled_scripts() -> set[str]:
    """Every script a schedule runs: shell sweeps, the daemon and worker, LaunchAgents."""

    import re
    import subprocess

    texts = [p.read_text(encoding="utf-8", errors="ignore") for p in (ROOT / "scripts").glob("*.sh")]
    for name in ("scripts/weather_basket_yes_daemon.py", "scripts/weather_maintenance_worker.py",
                 "bayes_basket_target/production_worker.py"):
        texts.append((ROOT / name).read_text(encoding="utf-8"))
    agents = Path.home() / "Library" / "LaunchAgents"
    for plist in agents.glob("com.weather.*.plist") if agents.is_dir() else ():
        texts.append(subprocess.run(["plutil", "-p", str(plist)], capture_output=True, text=True).stdout)
    names = {m for text in texts for m in re.findall(r"([a-z_0-9]+\.py)", text)}
    # The dashboard imports these helpers into its own scheduled render.
    names |= {"live_book.py", "live_selection.py"}
    return {name for name in names if (ROOT / "scripts" / name).is_file()}


def test_no_scheduled_job_reads_the_cold_tier() -> None:
    """The external volume may never be a dependency of anything that runs on its own."""

    offenders = sorted(
        name for name in _scheduled_scripts() - {"ssot_cold_offload.py", "calibration_dashboard.py"}
        if "attach_cold_history" in (ROOT / "scripts" / name).read_text(encoding="utf-8")
        or "open_cold_table" in (ROOT / "scripts" / name).read_text(encoding="utf-8")
    )
    assert offenders == []
    # The dashboard (not a production consumer) may recompute through the
    # cold tier (user ruling 2026-10-01 「在外置硬盘」), only in _history_conn,
    # and an unreadable volume there is a section not ready.
    dashboard = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(dashboard)
    callers = {
        fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in {"attach_cold_history", "open_cold_table"}
    }
    assert callers == {"_history_conn"}
    helper = dashboard[dashboard.index("def _history_conn("):]
    helper = helper[:helper.index("\ndef ")]
    assert "raise SectionNotReady" in helper
    for name in ENVELOPE_WHOLE_HISTORY_READERS + WHOLE_HISTORY_READERS:
        assert Path(name).name not in _scheduled_scripts(), name


def test_every_envelope_history_reader_attaches_the_cold_rows() -> None:
    for name in ENVELOPE_WHOLE_HISTORY_READERS:
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        tables = {
            elt.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "attach_cold_history"
            for arg in node.args[1:2] for elt in getattr(arg, "elts", [])
        }
        assert ENVELOPE in tables, name


def test_the_envelope_window_and_floors_are_tied_to_their_readers() -> None:
    import re

    policy = off.POLICY_BY_TABLE[ENVELOPE]
    gate = (ROOT / "scripts" / "fit_deterministic_feature_gate.py").read_text(encoding="utf-8")
    assert re.search(r"^WINDOW_DAYS = (\d+)$", gate, re.M).group(1) == str(off.FEATURE_GATE_WINDOW_DAYS)
    sweep = (ROOT / "scripts" / "p1_likelihood_refit_sweep.sh").read_text(encoding="utf-8")
    assert f"timedelta(days={off.P1_LIKELIHOOD_WINDOW_DAYS})" in sweep
    assert policy.hot_days > off.FEATURE_GATE_WINDOW_DAYS and policy.hot_days > off.P1_LIKELIHOOD_WINDOW_DAYS
    assert {f.name for f in policy.floors} == {
        "calibration_evidence_anchor", "taf_case_cache", "metar_trajectory_training",
    }
    # The dashboard's fixed-start sections recompute through the cold tier
    # instead of holding rows back (「在外置硬盘」).
    assert off.POLICY_BY_TABLE[WS].floors == ()
    assert policy.keep_station_days is not None and policy.keep_station_days.live_only


def test_an_unanchored_calibration_lane_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bayes_basket_target import center_width_store
    from bayes_basket_target import serving_payload_cache as payloads

    monkeypatch.setattr(payloads, "CACHE_PATH", tmp_path / "payloads.db")
    monkeypatch.setattr(payloads, "_CONNECTIONS", {})
    monkeypatch.setattr(center_width_store, "evidence_anchor", lambda _conn, *, stage: None)
    with pytest.raises(off.OffloadRefused):
        off.evidence_anchor_floor(sqlite3.connect(":memory:"))
    anchors = {"P0": "2026-09-14T05:52:13Z", "P0-M": "2026-09-18T19:10:09Z", "P1": "2026-09-18T19:10:09Z"}
    monkeypatch.setattr(center_width_store, "evidence_anchor", lambda _conn, *, stage: anchors[stage])
    # No pass yet: everything from the anchor, less the margin.
    assert off.evidence_anchor_floor(sqlite3.connect(":memory:")) == "2026-09-12"
    # Every lane passed over its anchor: the floor follows the frozen days.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    lanes = [("center_width.serving_cells_by_day", "P0")] + [
        ("band_reliability.serving_lane_by_station_day", s) for s in ("P0", "P0-M", "P1")
    ]
    for sampler, stage in lanes[:-1]:
        payloads.record_pass(sampler, stage=stage, pre_band=True, min_decision_at_utc=anchors[stage], now=now,
                             cache_path=payloads.CACHE_PATH)
    assert off.evidence_anchor_floor(sqlite3.connect(":memory:")) == "2026-09-16"   # P1 still from its anchor
    payloads.record_pass(lanes[-1][0], stage="P1", pre_band=False, min_decision_at_utc=anchors["P1"], now=now,
                         cache_path=payloads.CACHE_PATH)
    from datetime import date, timedelta

    expected = (date.fromisoformat(payloads.frozen_through(now)) + timedelta(days=1)).isoformat()
    assert off.evidence_anchor_floor(sqlite3.connect(":memory:")) == expected


def test_the_taf_floor_is_the_first_day_its_cache_has_not_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import fit_taf_likelihood as taf

    path = tmp_path / "cases.db"
    monkeypatch.setattr(taf, "CASE_CACHE_PATH", path)
    assert off.taf_case_cache_floor(sqlite3.connect(":memory:")) == taf.DEFAULT_SINCE
    cache = taf.open_case_cache(path)
    for day in taf._days(taf.DEFAULT_SINCE, "2026-08-20"):
        cache.execute("INSERT INTO frozen_date VALUES(?,0,0,?,?)", (day, taf.CASE_CACHE_SCHEMA, "x"))
    cache.close()
    assert off.taf_case_cache_floor(sqlite3.connect(":memory:")) == "2026-08-21"


def test_an_unlabelled_fact_keeps_its_envelope(store: Path, cold: Path, tmp_path: Path) -> None:
    conn = sqlite3.connect(store)
    columns = [r[1] for r in conn.execute(f"PRAGMA table_info({ENVELOPE})")]
    for n in range(3):
        values = {c: f"{c}-{n}" for c in columns}
        values.update({
            "envelope_id": f"env_{n}", "stage": "P0", "checkpoint_id": f"cp_{n}", "station_id": "KLGA",
            "event_date": "2026-06-01", "decision_at_utc": f"2026-05-31T0{n}:00:00Z",
            "state": "COMMITTED", "content_sha256": _sha(f"e{n}"), "posterior_sha256": _sha("p"),
            "bridge_sha256": _sha("b"), "lineage_json": "{}", "probabilities_json": "{}",
            "bucket_ordinals_json": "{}",
        })
        conn.execute(
            f"INSERT INTO {ENVELOPE}({','.join(values)}) VALUES({','.join('?' * len(values))})",
            tuple(values.values()),
        )
    fact_columns = [r[1] for r in conn.execute("PRAGMA table_info(basket_policy_training_fact)")]
    conn.execute("CREATE TEMP TABLE _f AS SELECT * FROM basket_policy_training_fact WHERE 0")
    conn.commit()
    conn.close()
    policy = off.POLICY_BY_TABLE[ENVELOPE]
    reduced = off.TablePolicy(
        table=ENVELOPE, time_column="event_date", hot_days=policy.hot_days, readers="test",
        keep_latest=policy.keep_latest,
        keep_named=(off.KeepNamed(name="unlabelled_fact", column="envelope_id",
                                  sql="SELECT 'env_0'", why="test"),),
    )
    conn = sqlite3.connect(store)
    plan = off.plan_table(conn, reduced, as_of=AS_OF, holds={}, station_days=lambda _live: set())
    conn.close()
    assert plan.kept.get("named:unlabelled_fact") == 1
    assert plan.kept.get("latest_0:station_id+stage") == 1
    assert len(plan.candidates) == 1
    assert fact_columns   # the production schema carries the table the real query reads
