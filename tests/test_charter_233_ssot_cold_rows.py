"""Charter 233: the SSOT's aged history rows move to the cold tier, verified,
and every production reader answers exactly as before.

User ruling 2026-09-27 (「优化ssot，移动部分不影响生产的数据至外置硬盘」).  What
these bind:

* every registered table is eligible on the production schema itself (rowid
  table, no FOREIGN KEY children, DELETE triggers that only refuse);
* a row leaves only when it is older than its table's window and no reader
  protection names it -- the newest rows of a latest-per-key reader's key,
  a live station-day, a floor the fingerprint audit still needs, a hold;
* what leaves is on the volume, read back identical in value and type,
  before the SSOT deletes it, and the SSOT's own guards and schema are never
  touched;
* the P0 and stable-pool readers of the OOS evaluations give the same answer
  after the move at every instant after the cutoff;
* an unusable volume moves nothing; a crash between copy and delete is
  finished by the next run; a row that changed after its copy stays;
  ``--restore`` puts rows back and holds them;
* production code never reaches for the cold rows, and each window is pinned
  to the reader constant it was derived from.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import (  # noqa: E402
    candidate_window_evidence,
    native_ensemble_oos,
    p0_preselection,
    portfolio_inventory,
    serving_arithmetic,
    shadow_objective,
    ssot,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from scripts import ssot_cold_offload as off  # noqa: E402

AS_OF = datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)
PLAN = "basket_p0_preselection_plan"
OOS = "calibration_cross_native_ensemble_oos_evaluation"
WINDOWS = "basket_candidate_window_evidence"
REGIME = "basket_regime_artifact"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _production_store(path: Path) -> None:
    """The production schema, plus the tables writers create on first use."""

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    ssot.apply_production_ssot_schema(conn)
    p0_preselection.ensure_preselection_blob_schema(conn)
    candidate_window_evidence.ensure_schema(conn)
    conn.executescript(shadow_objective.SHADOW_OBJECTIVE_DDL)
    conn.executescript(portfolio_inventory.PORTFOLIO_INVENTORY_DDL)
    serving_arithmetic.ensure_schema(conn)
    conn.commit()
    conn.close()


_REAL_WATCHDOG = serving_arithmetic.watchdog


@pytest.fixture(autouse=True)
def _quiet_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real watchdog fingerprints the serving code (seconds a call); the
    floor is exercised on it once, and stubbed to "nothing pending" elsewhere."""

    monkeypatch.setattr(
        serving_arithmetic, "watchdog",
        lambda _conn, *, stage: {"discrepancy": False, "acknowledged": None, "fingerprint_since_utc": None},
    )


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "ssot.db"
    _production_store(path)
    return path


@pytest.fixture()
def cold(tmp_path: Path) -> Path:
    root = tmp_path / "volume" / "weather_cold"
    root.mkdir(parents=True)
    return root


def _run(store: Path, cold: Path | None, tmp_path: Path, **kwargs):
    return off.run(
        ssot_path=store, cold_root=cold, as_of=kwargs.pop("as_of", AS_OF),
        local_reference=None, min_cold_free_bytes=0,
        manifest=tmp_path / "manifest.jsonl", holds_path=tmp_path / "holds.json",
        sleep=lambda _seconds: None, **kwargs,
    )


PLAN_COLUMNS = (
    "preselection_id", "station_id", "event_date", "checkpoint_id", "probability_stage",
    "decision_at_utc", "quote_observed_at_utc", "quote_manifest_sha256", "policy_activation_id",
    "policy_authority_scope", "plan_json", "plan_sha256", "state", "final_authority",
    "live_submit_authorized", "calls_order_gateway", "orders_submitted", "created_at",
)


def _plan(conn, pid: str, station: str, day: str, created: str, width: int = 3000) -> None:
    conn.execute(
        f"INSERT INTO {PLAN}({','.join(PLAN_COLUMNS)}) VALUES({','.join('?' * len(PLAN_COLUMNS))})",
        (
            pid, station, day, f"cp-{pid}", "P0", created, created, _sha("q" + pid), "act",
            "PRODUCTION_NO_LIVE_CANDIDATE",
            json.dumps({"compiled_plan": {"pid": pid, "pad": "x" * width}}),
            _sha("p" + pid), "PRELIMINARY_NO_LIVE", "P1", 0, 0, 0, created,
        ),
    )


def _table_rows(path: Path, table: str) -> dict[int, tuple]:
    conn = sqlite3.connect(path)
    try:
        return {int(r[0]): tuple(r) for r in conn.execute(f'SELECT rowid, * FROM "{table}"')}
    finally:
        conn.close()


def _cold_file(cold: Path, table: str) -> Path:
    return off.cold_path(cold, table)


# --------------------------------------------------------------------------
# Eligibility on the real schema.
# --------------------------------------------------------------------------


def test_every_registered_table_is_eligible_on_the_production_schema(store: Path) -> None:
    conn = sqlite3.connect(store)
    try:
        for policy in off.POLICIES:
            assert off.eligibility(conn, policy) == [], policy.table
    finally:
        conn.close()


def test_the_old_rows_are_found_without_reading_a_row_body(store: Path) -> None:
    """Each table's time column is behind a large JSON column in some rows;
    enumerating through the table walks every overflow chain (the dashboard's
    1,017-second read).  Every registered enumeration must be answered by an
    index alone."""

    conn = sqlite3.connect(store)
    try:
        for policy in off.POLICIES:
            extra = f" AND ({policy.candidate_where})" if policy.candidate_where else ""
            detail = " ".join(
                str(row[-1]) for row in conn.execute(
                    f'EXPLAIN QUERY PLAN SELECT rowid,"{policy.time_column}" FROM "{policy.table}"'
                    f' WHERE "{policy.time_column}" < ?{extra}', ("2026-09-01",),
                )
            )
            assert "COVERING INDEX" in detail, (policy.table, detail)
    finally:
        conn.close()


def test_a_table_other_tables_point_at_or_with_a_working_delete_trigger_is_refused(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.executescript(
        """
        CREATE TABLE parent_rows (id TEXT PRIMARY KEY, created_at TEXT);
        CREATE TABLE child_rows (id TEXT, parent_id TEXT REFERENCES parent_rows(id));
        CREATE TABLE logged_rows (id TEXT PRIMARY KEY, created_at TEXT);
        CREATE TABLE deletion_log (id TEXT);
        CREATE TRIGGER logged_rows_log BEFORE DELETE ON logged_rows
        BEGIN INSERT INTO deletion_log VALUES (OLD.id); END;
        CREATE TABLE narrow_rows (id TEXT PRIMARY KEY, created_at TEXT) WITHOUT ROWID;
        """
    )
    policy = lambda table: off.TablePolicy(table=table, time_column="created_at", hot_days=1, readers="")  # noqa: E731
    assert off.eligibility(conn, policy("parent_rows")) == ["foreign_key_children:child_rows"]
    assert off.eligibility(conn, policy("logged_rows")) == ["delete_trigger_not_a_guard:logged_rows_log"]
    assert off.eligibility(conn, policy("narrow_rows")) == ["without_rowid_table"]
    assert off.eligibility(conn, policy("absent_rows")) == ["table_missing"]
    conn.close()


def test_only_an_unconditional_refusal_counts_as_a_guard() -> None:
    assert off._is_append_only_guard(
        "CREATE TRIGGER t_no_delete BEFORE DELETE ON t BEGIN SELECT RAISE(ABORT,'append_only'); END"
    )
    assert not off._is_append_only_guard(
        "CREATE TRIGGER t_x BEFORE DELETE ON t WHEN OLD.a=1 BEGIN SELECT RAISE(ABORT,'no'); END"
    )
    assert not off._is_append_only_guard(
        "CREATE TRIGGER t_x BEFORE DELETE ON t BEGIN SELECT RAISE(ABORT,'no'); DELETE FROM u; END"
    )
    assert not off._is_append_only_guard(
        "CREATE TRIGGER t_x AFTER DELETE ON t BEGIN SELECT RAISE(ABORT,'no'); END"
    )


def test_a_value_that_changed_its_storage_class_is_not_the_same_row() -> None:
    rows = [(1, 1, "a", None, b"\x00")]
    assert off.same_rows(rows, [(1, 1, "a", None, b"\x00")])
    for changed in ((1, 1.0, "a", None, b"\x00"), (1, "1", "a", None, b"\x00"),
                    (1, 1, "a", "", b"\x00"), (1, 1, "a", None, "\x00")):
        assert not off.same_rows(rows, [changed])
        assert off.rows_digest([changed]) != off.rows_digest(rows)
    # A byte moved from one column into the next is a different row.
    assert off.rows_digest([(1, "ab", "c")]) != off.rows_digest([(1, "a", "bc")])
    assert not off.same_rows(rows, [])


# --------------------------------------------------------------------------
# The move.
# --------------------------------------------------------------------------


def _plans_fixture(store: Path) -> None:
    conn = sqlite3.connect(store)
    rows = [
        ("old_a", "KLGA", "2026-09-10", "2026-09-08T10:00:00.000Z"),
        ("old_b", "KLGA", "2026-09-11", "2026-09-09T10:00:00.000Z"),
        ("latest_klga_before_cutoff", "KLGA", "2026-09-13", "2026-09-11T10:00:00.000Z"),
        ("old_live_day", "EGLC", "2026-09-12", "2026-09-10T10:00:00.000Z"),
        ("latest_eglc_before_cutoff", "EGLC", "2026-09-13", "2026-09-11T11:00:00.000Z"),
        ("old_c", "ZUUU", "2026-09-10", "2026-09-08T11:00:00.000Z"),
        ("latest_zuuu_old_forever", "ZUUU", "2026-09-11", "2026-09-09T11:00:00.000Z"),
        ("hot_klga", "KLGA", "2026-09-28", "2026-09-26T10:00:00.000Z"),
        ("hot_eglc", "EGLC", "2026-09-28", "2026-09-26T11:00:00.000Z"),
    ]
    for pid, station, day, created in rows:
        _plan(conn, pid, station, day, created)
    conn.execute(
        "INSERT INTO basket_p0_preselection_blob VALUES(?,?,?)",
        (_sha("blob"), '{"beta":1}', "2026-09-08T10:00:00Z"),
    )
    # EGLC 09-12 carried real money.
    conn.execute(
        "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,event_date,"
        "settlement_timezone,strategy_lane,decided_state,will_order,reason_codes_json,"
        "details_json,decision_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("d_live", "c_live", "plan_x", "EGLC", "2026-09-12", "Europe/London", "basket",
         "submitted", 1, "[]", "{}", _sha("d"), "2026-09-10T10:00:00.000Z"),
    )
    columns = [str(r[1]) for r in conn.execute("PRAGMA table_info(basket_target_order_intents)")]
    values = {
        "order_intent_id": "i_live", "decision_id": "d_live", "cycle_id": "c_live",
        "bucket": "20°C", "token_id": "tok", "side": "YES", "order_action": "BUY",
        "limit_price": 0.4, "requested_size": 5.0, "requested_notional_usdc": 2.0,
        "state": "filled", "live_submit_authorized": 1, "idempotency_key": "k",
        "intent_json": "{}", "intent_sha256": _sha("i"), "created_at": "2026-09-10T10:01:00Z",
    }
    listed = [c for c in columns if c in values]
    conn.execute(
        f"INSERT INTO basket_target_order_intents({','.join(listed)}) VALUES({','.join('?' * len(listed))})",
        tuple(values[c] for c in listed),
    )
    conn.commit()
    conn.close()


def test_old_unprotected_plans_move_and_every_protected_one_stays(
    store: Path, cold: Path, tmp_path: Path
) -> None:
    _plans_fixture(store)
    before = _table_rows(store, PLAN)
    schema_version = sqlite3.connect(store).execute("PRAGMA schema_version").fetchone()[0]

    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])

    assert receipt["state"] == "applied"
    summary = receipt["tables"][0]
    assert summary["cutoff_before"] == "2026-09-20"
    assert summary["kept"] == {"latest_0:station_id": 3, "station_day": 1}
    after = _table_rows(store, PLAN)
    stayed = {row[1] for row in after.values()}
    assert stayed == {
        "latest_klga_before_cutoff", "old_live_day", "latest_eglc_before_cutoff",
        "latest_zuuu_old_forever", "hot_klga", "hot_eglc",
    }
    moved = {rowid: row for rowid, row in before.items() if rowid not in after}
    assert {row[1] for row in moved.values()} == {"old_a", "old_b", "old_c"}
    # On the volume, rowid and every value identical; the blobs copied beside them.
    landed = _table_rows(_cold_file(cold, PLAN), PLAN)
    assert landed == moved
    assert off.rows_digest(sorted(landed.values())) == off.rows_digest(sorted(moved.values()))
    blob = sqlite3.connect(_cold_file(cold, PLAN)).execute(
        "SELECT COUNT(*) FROM basket_p0_preselection_blob").fetchone()[0]
    assert blob == 1
    # The SSOT's schema never changed and its guard still refuses everyone else.
    conn = sqlite3.connect(store)
    assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema_version
    with pytest.raises(sqlite3.DatabaseError, match="append_only"):
        conn.execute(f"DELETE FROM {PLAN} WHERE preselection_id='hot_klga'")
    conn.close()
    # The archive is append-only too.
    with pytest.raises(sqlite3.DatabaseError, match="cold_append_only"):
        sqlite3.connect(_cold_file(cold, PLAN)).execute(f"DELETE FROM {PLAN}")
    lines = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text().splitlines()]
    assert sum(line["rows"] for line in lines) == 3
    assert all(line["production_dependency"] is False for line in lines)


def test_the_newest_plan_of_each_station_is_what_it_was(store: Path, cold: Path, tmp_path: Path) -> None:
    """``production_worker._station_edge_evidence`` reads MAX(rowid) per station."""

    _plans_fixture(store)
    query = (
        f"SELECT plan_json FROM {PLAN} WHERE rowid=("
        f"  SELECT MAX(rowid) FROM {PLAN} WHERE station_id=?)"
    )
    conn = sqlite3.connect(store)
    before = {s: conn.execute(query, (s,)).fetchone() for s in ("KLGA", "EGLC", "ZUUU")}
    conn.close()
    _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    conn = sqlite3.connect(store)
    assert {s: conn.execute(query, (s,)).fetchone() for s in ("KLGA", "EGLC", "ZUUU")} == before
    conn.close()


def test_a_dry_run_touches_nothing_and_says_how_big_each_table_is(
    store: Path, cold: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _plans_fixture(store)
    digest = hashlib.sha256(store.read_bytes()).hexdigest()
    receipt = _run(store, cold, tmp_path, apply=False)
    assert receipt["state"] == "dry_run"
    assert receipt["candidate_rows"] == 3
    assert hashlib.sha256(store.read_bytes()).hexdigest() == digest
    assert not (cold / off.COLD_SUBDIR).exists()
    plans = next(row for row in receipt["tables"] if row["table"] == PLAN)
    assert plans["table_rows"] == 9
    # Nine rows of ~3 KB each: the whole-table estimate is the rows' payload.
    assert 9 * 3000 < plans["table_estimated_bytes"] < 9 * 4000
    assert 0 < plans["estimated_bytes"] < plans["table_estimated_bytes"]
    off._print_summary(receipt)
    printed = capsys.readouterr().out
    assert "预演（只读，一行不动）" in printed and "可搬 3 行" in printed


def test_the_receipt_says_whether_the_writers_can_open(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The writers' guard reads the file's size (2026-09-27): moving rows frees
    pages but never shrinks a file that already sits on the ceiling."""

    from bayes_basket_target import resource_bounds

    assert off.MAX_TARGET_DB_BYTES == resource_bounds.MAX_TARGET_DB_BYTES
    _plans_fixture(store)
    size = store.stat().st_size
    monkeypatch.setattr(off, "MAX_TARGET_DB_BYTES", size - 1)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    assert receipt["state"] == "applied" and receipt["moved_rows"] == 3
    assert receipt["ceiling_before"]["writers_open"] is False
    assert receipt["ceiling_after"]["file_bytes"] == size
    assert receipt["ceiling_after"]["writers_open"] is False
    assert receipt["ceiling_after"]["writable_bytes"] == 0
    off._print_summary(receipt)
    assert "写者拒开" in capsys.readouterr().out

    monkeypatch.setattr(off, "MAX_TARGET_DB_BYTES", size + 10 * 4096)
    receipt = _run(store, cold, tmp_path, apply=False)
    pages, ceiling = receipt["store_before"], receipt["ceiling_before"]
    assert ceiling["writers_open"] is True
    assert ceiling["writable_bytes"] == (
        size + 10 * 4096 - pages["file_bytes_by_pages"] + pages["free_page_bytes"]
    )
    off._print_summary(receipt)
    assert "写者可开" in capsys.readouterr().out


def test_the_books_and_the_lock_live_beside_the_ssot(
    store: Path, cold: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run from an unpacked copy of the branch, the script still keeps the
    daily job's ledger, holds, receipt and lock -- beside the SSOT."""

    _plans_fixture(store)
    argv = ["--ssot", str(store), "--cold-tier-root", str(cold), "--cold-tier-min-free-gib", "0"]
    assert off.main(argv) == 0
    capsys.readouterr()
    runtime = store.parent / "runtime"
    receipt = json.loads((runtime / off.RECEIPT.name).read_text(encoding="utf-8"))
    assert receipt["mode"] == "dry_run" and receipt["candidate_rows"] == 3
    assert (runtime / off.LOCK.name).exists()
    assert off.RUNTIME == off.DEFAULT_SSOT.parent / off.RUNTIME.name


@pytest.mark.parametrize("unusable", ["missing", "same_device", "disabled"])
def test_an_unusable_cold_tier_moves_nothing(
    store: Path, tmp_path: Path, unusable: str
) -> None:
    _plans_fixture(store)
    before = _table_rows(store, PLAN)
    if unusable == "missing":
        receipt = _run(store, tmp_path / "unplugged", tmp_path, apply=True)
    elif unusable == "same_device":
        root = tmp_path / "looks_like_the_volume"
        root.mkdir()
        receipt = off.run(
            ssot_path=store, cold_root=root, apply=True, as_of=AS_OF, local_reference=tmp_path,
            min_cold_free_bytes=0, manifest=tmp_path / "m.jsonl", holds_path=tmp_path / "h.json",
            sleep=lambda _s: None,
        )
    else:
        receipt = _run(store, None, tmp_path, apply=True)
    assert receipt["state"] == f"skipped_cold_tier_{unusable}"
    assert _table_rows(store, PLAN) == before


def test_a_crash_after_the_copy_is_finished_by_the_next_run(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plans_fixture(store)
    before = _table_rows(store, PLAN)

    def crash(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error (simulated power loss)")

    real_wait = off._wait_for_wal
    monkeypatch.setattr(off, "_wait_for_wal", crash)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    assert receipt["state"] == "stopped"
    assert _table_rows(store, PLAN) == before          # nothing deleted
    assert len(_table_rows(_cold_file(cold, PLAN), PLAN)) == 3   # but copied
    off._print_summary(receipt)          # a stopped run still reads out
    monkeypatch.setattr(off, "_wait_for_wal", real_wait)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    assert receipt["state"] == "applied"
    assert len(_table_rows(store, PLAN)) == 6
    assert len(_table_rows(_cold_file(cold, PLAN), PLAN)) == 3   # once, not twice
    off._print_summary(receipt)


def _regime(conn, artifact_id: str, day: str, state: str = "COMMITTED") -> None:
    conn.execute(
        f"INSERT INTO {REGIME} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            artifact_id, "KLGA", day, "P0", f"cp-{artifact_id}", "America/New_York",
            _sha("prob" + artifact_id), f"{day}T00:00:00Z", f"{day}T00:00:00Z", f"{day}T23:00:00Z",
            '{"regime":"clear"}', _sha(artifact_id), state, f"{day}T00:00:00Z",
        ),
    )


def test_a_row_that_changed_after_its_copy_is_not_deleted(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(store)
    _regime(conn, "reg_old", "2026-09-10")
    _regime(conn, "reg_hot", "2026-09-26")
    conn.commit()
    conn.close()
    real_wait = off._wait_for_wal

    def supersede_then_wait(db, read, **kwargs):
        # The controlled transition the SSOT allows, between copy and delete.
        writer = sqlite3.connect(store)
        writer.execute(f"UPDATE {REGIME} SET state='SUPERSEDED' WHERE artifact_id='reg_old'")
        writer.commit()
        writer.close()
        return real_wait(db, read, **kwargs)

    monkeypatch.setattr(off, "_wait_for_wal", supersede_then_wait)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[REGIME])
    assert receipt["moves"][0]["reason"] == "ssot_rows_changed_since_copy"
    rows = {row[1]: row for row in _table_rows(store, REGIME).values()}
    assert rows["reg_old"][13] == "SUPERSEDED"   # still in the SSOT, as updated


def test_regime_rows_stay_for_the_paper_ledger_window(store: Path, cold: Path, tmp_path: Path) -> None:
    conn = sqlite3.connect(store)
    for day in ("2026-09-18", "2026-09-19", "2026-09-20", "2026-09-23", "2026-09-29"):
        _regime(conn, f"reg_{day}", day)
    conn.commit()
    conn.close()
    _run(store, cold, tmp_path, apply=True, tables=[REGIME])
    left = sorted(row[3] for row in _table_rows(store, REGIME).values())
    # today-7 = 09-20 onward stays; the ledger walks today-4 .. today-2.
    assert left == ["2026-09-20", "2026-09-23", "2026-09-29"]
    # The production schema gate reads the regime delete trigger's text.
    conn = sqlite3.connect(store)
    status = ssot.inspect_regime_superseded_schema(conn)
    conn.close()
    assert status["ok"] is True


def _window(conn, evidence_id: str, day: str, sampled: int) -> None:
    conn.execute(
        f"INSERT INTO {WINDOWS}(evidence_id,fact_id,decision_id,station_id,event_date,stage,"
        "decision_at_utc,production_decision_at_utc,lead_band,sampled,buckets_json,window_size,"
        "win_probability,all_in_price,admissible,seated,created_at_utc)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (evidence_id, "f-" + evidence_id, "d-" + evidence_id, "KLGA", day, "P0",
         f"{day}T01:00:00Z", f"{day}T01:00:00Z", "h24_48", sampled, '["20°C"]', 1, 0.3, 0.25, 1, 0,
         f"{day}T01:00:00Z"),
    )


def test_only_the_resampled_windows_leave(store: Path, cold: Path, tmp_path: Path) -> None:
    conn = sqlite3.connect(store)
    _window(conn, "sampled_old", "2026-09-10", 1)
    _window(conn, "resampled_old_1", "2026-09-10", 0)
    _window(conn, "resampled_old_2", "2026-09-11", 0)
    _window(conn, "resampled_hot", "2026-09-26", 0)
    _window(conn, "sampled_hot", "2026-09-26", 1)
    conn.commit()
    scored_before = conn.execute("SELECT * FROM basket_candidate_window_scored ORDER BY candidate_id").fetchall()
    conn.close()
    _run(store, cold, tmp_path, apply=True, tables=[WINDOWS])
    left = sorted(row[1] for row in _table_rows(store, WINDOWS).values())
    assert left == ["resampled_hot", "sampled_hot", "sampled_old"]
    conn = sqlite3.connect(store)
    assert conn.execute("SELECT * FROM basket_candidate_window_scored ORDER BY candidate_id").fetchall() == scored_before
    conn.close()


def test_a_long_held_write_lock_is_outwaited_and_other_errors_are_not(tmp_path: Path) -> None:
    db = tmp_path / "lock.db"
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("CREATE TABLE t(x)")
    holder.execute("BEGIN IMMEDIATE")
    waiter = sqlite3.connect(db, isolation_level=None, timeout=0.05)
    waits: list[float] = []

    def release(seconds: float) -> None:
        waits.append(seconds)
        holder.execute("COMMIT")

    off._begin_immediate(waiter, sleep=release)
    assert waits == [30.0]
    waiter.execute("ROLLBACK")
    waiter.execute("BEGIN")
    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        off._begin_immediate(waiter, sleep=release)
    assert waits == [30.0]


# --------------------------------------------------------------------------
# OOS evaluations: the P0 and stable-pool readers answer the same.
# --------------------------------------------------------------------------


def _oos_key(station: str, systems: list[str]) -> dict:
    mapping = {system: "00Z" for system in systems}
    mapping_json = native_ensemble_oos._json(mapping)
    return {
        "station_id": station, "season_bin": "PEAK", "lead_bucket": "h24_48", "run_cycle": "00Z",
        "run_cycle_by_system_json": mapping_json, "run_cycle_by_system_sha256": _sha(mapping_json),
        "settlement_definition": "WU_DAILY_HIGH", "ensemble_system_ids_json": native_ensemble_oos._json(sorted(systems)),
        "available_system_set_hash": content_sha256(sorted(systems)),
    }


def _evaluation(conn, eid: str, key: dict, at: str, *, days: list[str] = (), profile: str = "P") -> None:
    scores = [
        {"event_date": d, "learned": {"brier": 0.5, "crps": 0.4, "nll": 1.0 + i / 10},
         "climate": {"brier": 0.6, "crps": 0.5, "nll": 1.5}}
        for i, d in enumerate(days)
    ]
    metrics = {"equal_weight_diagnostic": {"scheme_daily_scores": scores}, "gate": {"membership_mode": None}}
    row = {
        "evaluation_id": eid, **key, "selection_evidence_sha256": _sha("sel" + eid),
        "oos_state": "PRODUCTION_NO_LIVE_OOS_PASS", "threshold_profile": profile,
        "min_train_days": 20, "min_holdout_days": 8, "train_end_date": "2026-08-01",
        "holdout_start_date": "2026-08-02", "holdout_end_date": "2026-08-09",
        "oos_metrics_json": json.dumps(metrics), "climatology_baseline_metrics_json": "{}",
        "evaluated_at_utc": at, "content_sha256": _sha("content" + eid),
    }
    conn.execute(
        f"INSERT INTO {OOS}({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values())
    )


def _p0_reader(conn, key: dict, decision_at: str) -> list[tuple]:
    """The query native_ensemble_p0 (and the republisher fallback) runs."""

    return conn.execute(
        f"SELECT oos_state,selection_evidence_sha256,evaluated_at_utc FROM {OOS} WHERE "
        + " AND ".join(f"{c}=?" for c in native_ensemble_oos._EVALUATION_READER_KEY)
        + " AND evaluated_at_utc<=? ORDER BY evaluated_at_utc DESC,evaluation_id DESC LIMIT 2",
        (*(key[c] for c in native_ensemble_oos._EVALUATION_READER_KEY), decision_at),
    ).fetchall()


def test_the_oos_readers_answer_the_same_at_every_instant_after_the_cutoff(
    store: Path, cold: Path, tmp_path: Path
) -> None:
    stable = _oos_key("KLGA", ["ECMWF_IFS", "NOAA_GEFS"])
    busy = _oos_key("KLGA", ["DWD_ICON", "ECMWF_IFS"])
    tied = _oos_key("ZUUU", ["ECMWF_IFS", "NOAA_GEFS"])
    conn = sqlite3.connect(store)
    # A scope charter 221 stopped rewriting: only old rows, forever.
    for eid, at in (("s1", "2026-08-20T01:00:00Z"), ("s2", "2026-08-25T01:00:00Z"), ("s3", "2026-08-28T01:00:00Z")):
        _evaluation(conn, eid, stable, at)
    for eid, at in (("b0", "2026-08-15T01:00:00Z"), ("b1", "2026-08-20T01:00:00Z"),
                    ("b2", "2026-08-22T01:00:00Z"), ("b3", "2026-09-10T01:00:00Z"),
                    ("b4", "2026-09-20T01:00:00Z")):
        _evaluation(conn, eid, busy, at, days=["2026-09-08", "2026-09-09"] if eid >= "b3" else [])
    # Two evaluations of one key in one scan: the readers refuse the tie.
    for eid, at in (("t0", "2026-08-20T01:00:00Z"), ("t1", "2026-08-28T01:00:00Z"), ("t2", "2026-08-28T01:00:00Z")):
        _evaluation(conn, eid, tied, at)
    conn.commit()
    instants = ["2026-09-04T00:00:00Z", "2026-09-10T12:00:00Z", "2026-09-27T06:00:00Z"]
    before = {(k["ensemble_system_ids_json"], k["station_id"], t): _p0_reader(conn, k, t)
              for k in (stable, busy, tied) for t in instants}
    gate_args = dict(
        station_id="KLGA", season_bin="PEAK", lead_bucket="h24_48", run_cycle="00Z",
        run_cycle_by_system_json=busy["run_cycle_by_system_json"],
        run_cycle_by_system_sha256=busy["run_cycle_by_system_sha256"],
        settlement_definition="WU_DAILY_HIGH", system_ids=["DWD_ICON", "ECMWF_IFS"],
        threshold_profile="P", as_of_utc="2026-09-27T06:00:00Z",
    )
    gate_before = native_ensemble_oos._stable_pool_gate_history(conn, **gate_args)
    assert gate_before  # the fixture reaches the gate's own window
    conn.close()

    receipt = _run(store, cold, tmp_path, apply=True, tables=[OOS])
    assert receipt["tables"][0]["cutoff_before"] == "2026-09-04"
    moved = {row[1] for row in _table_rows(_cold_file(cold, OOS), OOS).values()}
    # Per key: everything after the cutoff, plus the newest two before it.
    assert moved == {"s1", "b0", "t0"}

    conn = sqlite3.connect(store)
    after = {(k["ensemble_system_ids_json"], k["station_id"], t): _p0_reader(conn, k, t)
             for k in (stable, busy, tied) for t in instants}
    assert after == before
    assert native_ensemble_oos._stable_pool_gate_history(conn, **gate_args) == gate_before
    conn.close()


# --------------------------------------------------------------------------
# Floors and holds.
# --------------------------------------------------------------------------


def test_plans_around_an_unacknowledged_fingerprint_stay_for_the_audit(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plans_fixture(store)

    def watchdog(_conn, *, stage):
        if stage == "P0":
            return {"discrepancy": True, "acknowledged": None, "fingerprint_since_utc": "2026-09-09T08:00:00Z"}
        return {"discrepancy": False, "acknowledged": None, "fingerprint_since_utc": None}

    monkeypatch.setattr(serving_arithmetic, "watchdog", watchdog)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    summary = receipt["tables"][0]
    # 09-09 08:00 minus the audit's twelve hours is 09-08 20:00: that whole day stays.
    assert summary["floors"] == {"serving_arithmetic_audit": "2026-09-08"}
    assert summary["candidate_rows"] == 0
    assert len(_table_rows(store, PLAN)) == 9


def test_an_unreadable_watchdog_leaves_the_plans_alone(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plans_fixture(store)

    def broken(_conn, *, stage):
        raise sqlite3.OperationalError("no such table")

    monkeypatch.setattr(serving_arithmetic, "watchdog", broken)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    assert receipt["tables"][0]["state"] == "refused"
    assert len(_table_rows(store, PLAN)) == 9


def test_the_real_watchdog_reads_a_fresh_store_as_no_floor(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serving_arithmetic, "watchdog", _REAL_WATCHDOG)
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        assert off.serving_arithmetic_floor(conn) is None
    finally:
        conn.close()


def test_restore_puts_rows_back_verbatim_and_holds_them(
    store: Path, cold: Path, tmp_path: Path
) -> None:
    _plans_fixture(store)
    before = _table_rows(store, PLAN)
    _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    assert len(_table_rows(store, PLAN)) == 6
    outcome = off.restore_table(
        ssot_path=store, cold_root=cold, table=PLAN, since="2026-09-01",
        holds_path=tmp_path / "holds.json", sleep=lambda _s: None,
    )
    assert outcome["restored_rows"] == 3
    assert _table_rows(store, PLAN) == before
    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    assert receipt["tables"][0]["candidate_rows"] == 0
    # The hold keeps every old row dated on or after it, whatever else would.
    assert receipt["tables"][0]["kept"] == {"hold": 7}
    assert _table_rows(store, PLAN) == before


def test_an_unreadable_hold_stops_the_run(store: Path, cold: Path, tmp_path: Path) -> None:
    (tmp_path / "holds.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(off.OffloadRefused, match="holds_unreadable"):
        _run(store, cold, tmp_path, apply=True)


def test_a_cold_table_that_no_longer_matches_the_ssot_stops_that_table(
    store: Path, cold: Path, tmp_path: Path
) -> None:
    _plans_fixture(store)
    _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    conn = sqlite3.connect(store)
    conn.execute(f"ALTER TABLE {PLAN} ADD COLUMN later_column TEXT")
    conn.commit()
    _plan(conn, "old_after_alter", "KLGA", "2026-09-10", "2026-09-08T12:00:00.000Z")
    conn.commit()
    conn.close()
    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    assert receipt["moves"][0]["state"] == "stopped"
    assert "cold_schema_mismatch" in receipt["moves"][0]["reason"]
    assert "old_after_alter" in {row[1] for row in _table_rows(store, PLAN).values()}


def test_an_offline_reader_is_told_when_the_volume_is_gone(tmp_path: Path) -> None:
    link = tmp_path / "weather_cold"
    link.symlink_to(tmp_path / "unplugged_volume")
    from bayes_basket_target.offloaded_paths import OffloadedVolumeMissing

    with pytest.raises(OffloadedVolumeMissing):
        off.open_cold_table(PLAN, root=link)


def test_the_station_days_that_carried_money_come_from_live_intents(store: Path) -> None:
    _plans_fixture(store)
    conn = sqlite3.connect(store)
    try:
        assert off._intent_station_days(conn, live_only=True) == {("EGLC", "2026-09-12")}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# What production may not do, and what each window is tied to.
# --------------------------------------------------------------------------


_LIVE_ENTRY_SCRIPTS = (
    "scripts/weather_basket_yes_daemon.py",
    "scripts/weather_maintenance_worker.py",
    "scripts/capture_official_native_ensemble_forward.py",
)


def test_production_code_never_reaches_for_the_cold_rows() -> None:
    files = sorted((ROOT / "bayes_basket_target").rglob("*.py")) + [ROOT / name for name in _LIVE_ENTRY_SCRIPTS]
    offenders: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
                names += [a.name for a in node.names]
                if any("ssot_cold_offload" in str(name) for name in names):
                    offenders.append(f"{path.name}:import")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                off.COLD_SUBDIR in node.value or "ssot_cold_offload" in node.value
            ):
                offenders.append(f"{path.name}:literal")
    assert offenders == []


def test_each_window_is_tied_to_the_reader_it_protects() -> None:
    oos = off.POLICY_BY_TABLE[OOS]
    assert oos.hot_days > native_ensemble_oos.STABLE_POOL_GATE_LOOKBACK_DAYS
    assert tuple(oos.keep_latest[0].key) == tuple(native_ensemble_oos._EVALUATION_READER_KEY)
    assert oos.keep_latest[0].count >= 2   # the readers LIMIT 2 and refuse a tie
    p0_source = (ROOT / "bayes_basket_target" / "native_ensemble_p0.py").read_text(encoding="utf-8")
    assert p0_source.count("ORDER BY evaluated_at_utc DESC,evaluation_id DESC LIMIT 2") >= 2

    audit = (ROOT / "scripts" / "acknowledge_serving_arithmetic.py").read_text(encoding="utf-8")
    window = re.search(r"^SHAPE_SHIFT_WINDOW_HOURS = (\d+)$", audit, re.M)
    assert window and int(window.group(1)) == off.SERVING_AUDIT_WINDOW_HOURS

    worker = (ROOT / "bayes_basket_target" / "production_worker.py").read_text(encoding="utf-8")
    assert "SELECT MAX(rowid) FROM basket_p0_preselection_plan" in worker
    plan = off.POLICY_BY_TABLE[PLAN]
    assert plan.keep_latest[0].key == ("station_id",) and plan.keep_latest[0].order == ("rowid",)

    ledger = (ROOT / "scripts" / "paper_ledger_aligned.py").read_text(encoding="utf-8")
    lag = re.search(r"^DAILY_PASS_SETTLE_LAG_DAYS = (\d+)$", ledger, re.M)
    default_window = re.search(r'"--window-days", type=int, default=(\d+)', ledger)
    sweep = (ROOT / "scripts" / "paper_ledger_aligned_sweep.sh").read_text(encoding="utf-8")
    assert 'WINDOW_DAYS="${PAPER_LEDGER_WINDOW_DAYS:-3}"' in sweep
    assert lag and default_window
    assert off.PAPER_LEDGER_EVENT_DAYS == int(lag.group(1)) + int(default_window.group(1)) - 1
    assert off.POLICY_BY_TABLE[REGIME].hot_days > off.PAPER_LEDGER_EVENT_DAYS

    evidence = (ROOT / "bayes_basket_target" / "candidate_window_evidence.py").read_text(encoding="utf-8")
    assert "WHERE e.sampled = 1;" in evidence
    assert off.POLICY_BY_TABLE[WINDOWS].candidate_where == "sampled = 0"


def test_the_write_only_tables_have_no_reader() -> None:
    """Pinned so a reader added later has to come here and move the window."""

    for table, writer in (
        ("basket_shadow_objective_score", "shadow_objective.py"),
        ("basket_portfolio_inventory_snapshot", "portfolio_inventory.py"),
    ):
        readers = []
        for path in sorted((ROOT / "bayes_basket_target").rglob("*.py")) + sorted((ROOT / "scripts").glob("*.py")):
            if path.name in {writer, "ssot_cold_offload.py", "retire_ssot_class_a_pre_20260905.py"}:
                continue
            if table in path.read_text(encoding="utf-8"):
                readers.append(path.name)
        assert readers == [], table


def test_the_daily_job_runs_the_lane_after_the_file_lane() -> None:
    job = (ROOT / "scripts" / "official_nwp_raw_retention_daily.sh").read_text(encoding="utf-8")
    assert "scripts/ssot_cold_offload.py" in job
    assert job.index("scripts/cold_tier_offload.py") < job.index("scripts/ssot_cold_offload.py")
    assert "PM_SSOT_COLD_OFFLOAD_APPLY" in job


def test_charter_233_names_the_lane() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    article = next((line for line in charter.splitlines() if line.startswith("233. ")), "")
    assert "ssot_cold_offload" in article
    for policy in off.POLICIES:
        assert policy.table in article
