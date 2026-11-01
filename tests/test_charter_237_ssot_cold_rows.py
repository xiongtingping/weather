"""Charter 237: the SSOT's aged history rows move to the cold tier, verified,
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
import inspect
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
    conn.executescript(ssot.REPUBLISHER_ENSEMBLE_DDL)
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
            json.dumps({"compiled_plan": {
                "pid": pid, "production_plan_sha256": _sha("prod" + pid), "pad": "x" * width,
            }}),
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


def _live_decision(conn, did: str, station: str, day: str, created: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,station_id,event_date,"
        "settlement_timezone,strategy_lane,decided_state,will_order,reason_codes_json,"
        "details_json,decision_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, "c_" + did, "plan_" + did, station, day, "UTC", "basket",
         "submitted", 1, "[]", json.dumps(details), _sha(did), created),
    )
    columns = [str(r[1]) for r in conn.execute("PRAGMA table_info(basket_target_order_intents)")]
    values = {
        "order_intent_id": "i_" + did, "decision_id": did, "cycle_id": "c_" + did,
        "bucket": "20°C", "token_id": "tok", "side": "YES", "order_action": "BUY",
        "limit_price": 0.4, "requested_size": 5.0, "requested_notional_usdc": 2.0,
        "state": "filled", "live_submit_authorized": 1, "idempotency_key": "k_" + did,
        "intent_json": "{}", "intent_sha256": _sha("i" + did), "created_at": created,
    }
    listed = [c for c in columns if c in values]
    conn.execute(
        f"INSERT INTO basket_target_order_intents({','.join(listed)}) VALUES({','.join('?' * len(listed))})",
        tuple(values[c] for c in listed),
    )


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
        ("old_lfpg_a", "LFPG", "2026-09-09", "2026-09-07T10:00:00.000Z"),
        ("old_lfpg_b", "LFPG", "2026-09-09", "2026-09-07T12:00:00.000Z"),
    ]
    for pid, station, day, created in rows:
        _plan(conn, pid, station, day, created)
    conn.execute(
        "INSERT INTO basket_p0_preselection_blob VALUES(?,?,?)",
        (_sha("blob"), '{"beta":1}', "2026-09-08T10:00:00Z"),
    )
    # EGLC 09-12 carried real money, and its decision names the plan it acted
    # on (committed in the same transaction: the same created_at).
    _live_decision(conn, "d_live", "EGLC", "2026-09-12", "2026-09-10T10:00:00.000Z",
                   {"production_plan_sha256": _sha("prodold_live_day")})
    # KLGA 09-10 carried money from a lane that compiles no plan: nothing named.
    _live_decision(conn, "d_unnamed", "KLGA", "2026-09-10", "2026-09-08T10:00:00.000Z", {})
    # LFPG 09-09 names a plan no row carries at that instant: the day stays whole.
    _live_decision(conn, "d_lost", "LFPG", "2026-09-09", "2026-09-07T12:00:00.000Z",
                   {"production_plan_sha256": _sha("not_stored")})
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
    assert summary["kept"] == {
        "latest_0:station_id": 3, "live_decision_plan": 1,
        "live_decision_plan_unresolved_station_day": 2,
    }
    after = _table_rows(store, PLAN)
    stayed = {row[1] for row in after.values()}
    assert stayed == {
        "latest_klga_before_cutoff", "old_live_day", "latest_eglc_before_cutoff",
        "latest_zuuu_old_forever", "hot_klga", "hot_eglc", "old_lfpg_a", "old_lfpg_b",
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
    assert plans["table_rows"] == 11
    # Nine rows of ~3 KB each: the whole-table estimate is the rows' payload.
    assert 11 * 3000 < plans["table_estimated_bytes"] < 11 * 4000
    assert 0 < plans["estimated_bytes"] < plans["table_estimated_bytes"]
    off._print_summary(receipt)
    printed = capsys.readouterr().out
    assert "预演（只读，一行不动）" in printed and "可搬 3 行" in printed


def test_the_receipt_says_whether_the_writers_can_open(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The writers' guard reads the store's live pages (2026-10-06): moving
    rows never shrinks the file, but the pages it frees reopen a store whose
    file sits on the ceiling."""

    from bayes_basket_target import resource_bounds

    assert off.MAX_TARGET_DB_BYTES == resource_bounds.MAX_TARGET_DB_BYTES
    _plans_fixture(store)
    size = store.stat().st_size
    monkeypatch.setattr(off, "MAX_TARGET_DB_BYTES", size - 1)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    assert receipt["state"] == "applied" and receipt["moved_rows"] == 3
    assert receipt["ceiling_before"]["writers_open"] is False
    after = receipt["ceiling_after"]
    assert after["file_bytes"] == size
    assert receipt["store_after"]["free_page_bytes"] > 0
    assert after["live_bytes"] == size - receipt["store_after"]["free_page_bytes"]
    assert after["writers_open"] is True
    assert after["writable_bytes"] == receipt["store_after"]["free_page_bytes"]
    off._print_summary(receipt)
    assert "写者可开" in capsys.readouterr().out
    off._print_summary({**receipt, "ceiling_after": {**after, "writers_open": False}})
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
    store: Path, cold: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run from an unpacked copy of the branch, the script still keeps the
    daily job's ledger, holds, receipt and lock -- beside the SSOT."""

    # ``main`` takes the wall clock; the fixture's rows are dated for AS_OF,
    # and from 2026-10-04 on the cut-off had walked past two more of them.
    monkeypatch.setattr(off, "_utc_now", lambda: AS_OF)
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
    assert len(_table_rows(store, PLAN)) == 8
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


def _evaluation(
    conn, eid: str, key: dict, at: str, *, days: list[str] = (), profile: str = "P",
    membership: str | None = None,
) -> None:
    scores = [
        {"event_date": d, "learned": {"brier": 0.5, "crps": 0.4, "nll": 1.0 + i / 10},
         "climate": {"brier": 0.6, "crps": 0.5, "nll": 1.5}}
        for i, d in enumerate(days)
    ]
    metrics = {"equal_weight_diagnostic": {"scheme_daily_scores": scores}, "gate": {"membership_mode": membership}}
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


def test_rows_whose_days_a_newer_row_gives_leave_from_inside_the_window(
    store: Path, cold: Path, tmp_path: Path
) -> None:
    """2026-10-13 (「桥接+精细保留」): inside the window, a row whose scored
    days newer rows of its gate key all give is never read again and leaves;
    the P0 readers' latest two and the gate's answer from now on are what
    they were."""

    key = _oos_key("KLGA", ["DWD_ICON", "ECMWF_IFS"])
    conn = sqlite3.connect(store)
    # Newest first: h5/h4 are the latest two; h3 gives 09-10 nobody newer
    # gives; h2 and h1 give only days h3..h5 give; h0 is a rollback row the
    # gate skips (no days for it) -- it leaves too.
    rows = (
        ("h0", "2026-09-12T01:00:00Z", ["2026-09-09"]),
        ("h1", "2026-09-14T01:00:00Z", ["2026-09-11", "2026-09-12"]),
        ("h2", "2026-09-16T01:00:00Z", ["2026-09-12", "2026-09-13"]),
        ("h3", "2026-09-18T01:00:00Z", ["2026-09-10", "2026-09-11", "2026-09-12"]),
        ("h4", "2026-09-22T01:00:00Z", ["2026-09-13"]),
        ("h5", "2026-09-26T01:00:00Z", ["2026-09-13"]),
    )
    for eid, at, days in rows:
        _evaluation(conn, eid, key, at, days=days, membership="contest" if eid == "h0" else None)
    conn.commit()
    gate_args = dict(
        station_id="KLGA", season_bin="PEAK", lead_bucket="h24_48", run_cycle="00Z",
        run_cycle_by_system_json=key["run_cycle_by_system_json"],
        run_cycle_by_system_sha256=key["run_cycle_by_system_sha256"],
        settlement_definition="WU_DAILY_HIGH", system_ids=["DWD_ICON", "ECMWF_IFS"],
        threshold_profile="P", as_of_utc="2026-09-27T06:00:00Z",
    )
    gate_before = native_ensemble_oos._stable_pool_gate_history(conn, **gate_args)
    p0_before = _p0_reader(conn, key, "2026-09-27T06:00:00Z")
    conn.close()

    receipt = _run(store, cold, tmp_path, apply=True, tables=[OOS])
    table = receipt["tables"][0]
    moved = {row[1] for row in _table_rows(_cold_file(cold, OOS), OOS).values()}
    assert moved == {"h0", "h1", "h2"}
    assert table["superseded_rows"] == 3

    conn = sqlite3.connect(store)
    assert _p0_reader(conn, key, "2026-09-27T06:00:00Z") == p0_before
    assert native_ensemble_oos._stable_pool_gate_history(conn, **gate_args) == gate_before
    conn.close()


def test_the_superseded_rule_groups_by_the_gates_own_key_and_reads() -> None:
    rule = off.POLICY_BY_TABLE[OOS].move_superseded
    assert rule is not None
    assert rule.key == tuple(native_ensemble_oos.STABLE_POOL_GATE_HISTORY_KEY)
    assert rule.row_days is native_ensemble_oos.stable_pool_gate_row_days
    source = inspect.getsource(native_ensemble_oos._stable_pool_gate_history)
    assert "STABLE_POOL_GATE_SCORES_SQL" in source and "STABLE_POOL_GATE_MEMBERSHIP_SQL" in source
    assert "stable_pool_gate_row_days" in source
    for column in rule.key:
        assert f"{column}=?" in source


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
    assert len(_table_rows(store, PLAN)) == 11


def test_an_unreadable_watchdog_leaves_the_plans_alone(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plans_fixture(store)

    def broken(_conn, *, stage):
        raise sqlite3.OperationalError("no such table")

    monkeypatch.setattr(serving_arithmetic, "watchdog", broken)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    assert receipt["tables"][0]["state"] == "refused"
    assert len(_table_rows(store, PLAN)) == 11


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
    assert len(_table_rows(store, PLAN)) == 8
    outcome = off.restore_table(
        ssot_path=store, cold_root=cold, table=PLAN, since="2026-09-01",
        holds_path=tmp_path / "holds.json", sleep=lambda _s: None,
    )
    assert outcome["restored_rows"] == 3
    assert _table_rows(store, PLAN) == before
    receipt = _run(store, cold, tmp_path, apply=True, tables=[PLAN])
    assert receipt["tables"][0]["candidate_rows"] == 0
    # The hold keeps every old row dated on or after it, whatever else would.
    assert receipt["tables"][0]["kept"] == {"hold": 9}
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


def test_the_lane_runs_every_three_hours_on_its_own_clock() -> None:
    """User 2026-11-01 「Back to every 3h」: once a day moved 0.06 GiB against
    GiB-a-day growth; the file-retention job still runs no pass of its own."""

    import plistlib

    job = (ROOT / "scripts" / "official_nwp_raw_retention_daily.sh").read_text(encoding="utf-8")
    assert "scripts/ssot_cold_offload.py" not in job
    with (ROOT / "launchd" / "com.weather.ssot_cold_offload.plist").open("rb") as handle:
        plist = plistlib.load(handle)
    assert "StartCalendarInterval" not in plist
    assert plist["StartInterval"] == 10800
    sweep = (ROOT / "scripts" / "ssot_cold_offload_sweep.sh").read_text(encoding="utf-8")
    assert "scripts/ssot_cold_offload.py --apply" in sweep
    assert "SWEEP_BUDGET_SEC:-2400}" in sweep


def test_charter_237_names_the_lane() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    article = next((line for line in charter.splitlines() if line.startswith("237. ")), "")
    widened = next((line for line in charter.splitlines() if line.startswith("238. ")), "")
    assert "ssot_cold_offload" in article
    for policy in off.POLICIES:
        # Charter 238 added the market snapshots to the same lane.
        assert policy.table in article or policy.table in widened


# --------------------------------------------------------------------------
# Training facts (2026-10-09 amendment): 47 days, foreign-key and live keeps.
# --------------------------------------------------------------------------


FACT = "basket_policy_training_fact"


def _fact(conn, fid: str, station: str, day: str, *, decision: str | None = None) -> None:
    columns = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({FACT})")]
    values: dict[str, object] = {}
    for column in columns:
        if column.endswith("sha256"):
            values[column] = _sha(column + fid)
        elif column in {"source_sequence", "intent_count"}:
            values[column] = 0
        elif column.endswith("_json"):
            values[column] = "{}"
        else:
            values[column] = f"{column}-{fid}"
    values.update({
        "fact_id": fid, "station_id": station, "event_date": day, "stage": "P0",
        "decision_at_utc": f"{day}T00:00:00Z", "created_at_utc": f"{day}T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_FORWARD", "decision_id": decision or f"d-{fid}",
    })
    conn.execute(
        f"INSERT INTO {FACT}({','.join(values)}) VALUES({','.join('?' * len(values))})",
        tuple(values.values()),
    )


def _label(conn, fid: str) -> None:
    conn.execute(
        "INSERT INTO basket_policy_training_label(label_id,fact_id,truth_id,truth_revision_no,"
        "truth_sha256,official_tmax_c,winning_bucket,settlement_closed_at_utc,labeled_at_utc,"
        "outcome_json,authority_scope,supersedes_label_id,content_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"l-{fid}", fid, f"t-{fid}", 0, _sha("t" + fid), 20.0, "20°C", "2026-08-10T00:00:00Z",
         "2026-08-10T00:00:00Z", "{}", "NO_LIVE_REPLAY_CANDIDATE", None, _sha("l" + fid)),
    )


def _facts_fixture(store: Path) -> None:
    """AS_OF 09-27: the facts' window (30 days) ends 08-28."""

    conn = sqlite3.connect(store)
    _fact(conn, "d2fact_old", "KLGA", "2026-08-01")
    _label(conn, "d2fact_old")
    _fact(conn, "fact_legacy", "KLGA", "2026-08-02")            # never labelled: not a d2fact
    _fact(conn, "d2fact_unlabelled", "KLGA", "2026-08-03")      # the label lane still owes it
    _fact(conn, "d2fact_live", "EGLC", "2026-08-04", decision="d_live")
    _label(conn, "d2fact_live")
    _fact(conn, "d2fact_superseded", "ZUUU", "2026-08-05")
    _label(conn, "d2fact_superseded")
    _fact(conn, "d2fact_hot", "KLGA", "2026-09-01")
    _label(conn, "d2fact_hot")
    _live_decision(conn, "d_live", "EGLC", "2026-08-04", "2026-08-03T00:00:00Z", {})
    columns = [str(r[1]) for r in conn.execute("PRAGMA table_info(basket_target_prepared_supersession_receipts)")]
    values = {c: (_sha(c) if c.endswith("sha256") else f"{c}-x") for c in columns}
    values.update({"old_training_fact_id": "d2fact_superseded", "released_amount_usdc": 0.0,
                   "reason": "authority_input_superseded"})
    conn.execute(
        "INSERT INTO basket_target_prepared_supersession_receipts"
        f"({','.join(values)}) VALUES({','.join('?' * len(values))})", tuple(values.values()),
    )
    conn.commit()
    conn.close()


def test_old_facts_move_and_the_named_ones_stay(store: Path, cold: Path, tmp_path: Path) -> None:
    _facts_fixture(store)
    conn = sqlite3.connect(store)
    policy = off.POLICY_BY_TABLE[FACT]
    assert off.eligibility(conn, policy) == []
    # Without the declared keep, a table other tables point at stays refused.
    bare = off.TablePolicy(table=FACT, time_column="event_date", hot_days=policy.hot_days, readers="t")
    assert off.eligibility(conn, bare) == ["foreign_key_children:basket_target_prepared_supersession_receipts"]
    conn.close()

    receipt = _run(store, cold, tmp_path, apply=True, tables=[FACT])

    summary = receipt["tables"][0]
    assert summary["cutoff_before"] == "2026-08-28"
    assert summary["kept"] == {
        "named:fk:basket_target_prepared_supersession_receipts.old_training_fact_id": 1,
        "named:live_decision": 1,
        "named:unlabelled_d2fact": 1,
    }
    stayed = {row[1] for row in _table_rows(store, FACT).values()}
    assert stayed == {"d2fact_unlabelled", "d2fact_live", "d2fact_superseded", "d2fact_hot"}
    landed = {row[1] for row in _table_rows(_cold_file(cold, FACT), FACT).values()}
    assert landed == {"d2fact_old", "fact_legacy"}


def test_the_fact_window_is_tied_to_its_readers() -> None:
    from bayes_basket_target import posterior_store, realisation_store, valuation_store

    reconcile = (ROOT / "scripts" / "settlement_truth_reconciliation.py").read_text(encoding="utf-8")
    assert re.search(r"^REPAIR_MAX_DAYS = (\d+)$", reconcile, re.M).group(1) == str(off.RECONCILIATION_MAX_DAYS)
    policy = off.POLICY_BY_TABLE[FACT]
    for window in (off.RECONCILIATION_MAX_DAYS, valuation_store.WINDOW_DAYS,
                   realisation_store.WINDOW_DAYS, posterior_store.WINDOW_DAYS):
        assert policy.hot_days > window
    assert policy.keep_foreign_key_refs
    label_lane = (ROOT / "bayes_basket_target" / "delta_v2_settlement_label.py").read_text(encoding="utf-8")
    assert "AND f.fact_id LIKE 'd2fact_%'" in label_lane


# --------------------------------------------------------------------------
# Native member history (2026-10-09 amendment): the fit's window, kept writers.
# --------------------------------------------------------------------------


MEMBERS = "calibration_native_ensemble_historical_member_run"


def _member(conn, rid: str, system: str, station: str, day: str, member: str = "1") -> None:
    conn.execute(
        f"INSERT INTO {MEMBERS}(historical_member_run_id,ensemble_system_id,station_id,target_local_date,"
        "archive_lead_bucket,run_cycle,member_id,member_role,raw_tmax_c,settlement_definition,"
        "extraction_version,provider_id,transport_provider_id,native_run_init_utc,offline_oos_eligible,"
        "archive_query_at_utc,source_url,raw_object_ref,source_object_sha256,qc_status,authority_scope,"
        "holdout_eligible,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, system, station, day, "h24_48", "00Z", member, "PERTURBED", 20.0, "def", "v1", "p",
         "ECMWF_OPEN_DATA_S3", f"{day}T00:00:00Z", 1, f"{day}T06:00:00Z", "u", "r", _sha("s" + rid),
         "ACCEPTED", "CALIBRATION_TRAIN_ONLY", 0, _sha(rid)),
    )


def test_members_past_the_fit_window_move_and_the_reprojected_systems_stay(
    store: Path, cold: Path, tmp_path: Path
) -> None:
    """AS_OF 09-27: the window ends 06-29."""

    conn = sqlite3.connect(store)
    _member(conn, "ifs_old_1", "ECMWF_IFS_ENS_OPEN_PF50", "KLGA", "2026-05-01", "1")
    _member(conn, "ifs_old_2", "ECMWF_IFS_ENS_OPEN_PF50", "KLGA", "2026-05-01", "2")
    _member(conn, "ifs_older", "ECMWF_IFS_ENS_OPEN_PF50", "KLGA", "2026-01-10")
    _member(conn, "gefs_newest_old", "NOAA_GEFS_05", "KLGA", "2026-04-01")
    _member(conn, "metar_old", "METAR_PHYSICAL_PATH_TMAX", "KLGA", "2026-05-01")
    _member(conn, "ifs_hot", "ECMWF_IFS_ENS_OPEN_PF50", "EGLC", "2026-09-01")
    conn.commit()
    policy = off.POLICY_BY_TABLE[MEMBERS]
    assert off.eligibility(conn, policy) == []
    conn.close()

    receipt = _run(store, cold, tmp_path, apply=True, tables=[MEMBERS])

    summary = receipt["tables"][0]
    assert summary["cutoff_before"] == "2026-06-29"
    # The newest old row of each system at each station stays (one of the
    # two 05-01 IFS members; the GEFS one); METAR's re-projected row is not
    # a candidate at all.
    assert summary["kept"] == {"latest_0:ensemble_system_id+station_id": 2}
    stayed = {row[1] for row in _table_rows(store, MEMBERS).values()}
    assert {"metar_old", "ifs_hot", "gefs_newest_old"} <= stayed
    assert "ifs_older" not in stayed and len(stayed & {"ifs_old_1", "ifs_old_2"}) == 1
    landed = {row[1] for row in _table_rows(_cold_file(cold, MEMBERS), MEMBERS).values()}
    assert "ifs_older" in landed and len(landed) == 2


def test_the_member_window_is_tied_to_the_fit_and_the_backfills_respect_it() -> None:
    from datetime import date

    from bayes_basket_target import metar_physical_tmax, native_ensemble_oos

    policy = off.POLICY_BY_TABLE[MEMBERS]
    assert policy.hot_days == (native_ensemble_oos.ROLLING_TRAIN_WINDOW_DAYS
                               + native_ensemble_oos.DEFAULT_MIN_HOLDOUT_DAYS
                               + off.MEMBER_RUN_LAG_MARGIN_DAYS)
    fit = (ROOT / "bayes_basket_target" / "native_ensemble_oos.py").read_text(encoding="utf-8")
    assert "- timedelta(days=ROLLING_TRAIN_WINDOW_DAYS)" in fit
    assert metar_physical_tmax.SYSTEM_ID in off.MEMBER_RUN_STAYING_SYSTEMS
    with pytest.raises(off.OffloadRefused, match="backfill_older_than_hot_window"):
        off.assert_within_hot_window(MEMBERS, "2026-05-01", today=date(2026, 9, 27))
    off.assert_within_hot_window(MEMBERS, "2026-07-01", today=date(2026, 9, 27))
    for name in ("run_native_history_backfill.py", "backfill_aifs_ens_history_from_research.py"):
        tree = ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "assert_within_hot_window"
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == MEMBERS
        ]
        assert calls, name


# --------------------------------------------------------------------------
# 2026-10-12 amendment (user ruling 「改」): biggest payload first, tiny-row
# tables capped per run, so one hour's budget is never spent where it frees
# the least and the tables at the end of the registry get their turn.
# --------------------------------------------------------------------------


def _regimes_and_plans(store: Path) -> None:
    conn = sqlite3.connect(store)
    for day in ("2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13"):
        _regime(conn, f"reg_{day}", day)
    conn.commit()
    conn.close()
    _plans_fixture(store)


def test_tables_move_biggest_estimated_payload_first(store: Path, cold: Path, tmp_path: Path) -> None:
    _regimes_and_plans(store)
    # Selected regime first; its rows are a few hundred bytes, the plans ~3 KB.
    receipt = _run(store, cold, tmp_path, apply=True, tables=[REGIME, PLAN])
    assert receipt["state"] == "applied"
    by_table = {row["table"]: row for row in receipt["tables"]}
    assert by_table[PLAN]["estimated_bytes"] > by_table[REGIME]["estimated_bytes"]
    # The receipt still lists the tables as selected; the moves went biggest first.
    assert [row["table"] for row in receipt["tables"]] == [REGIME, PLAN]
    assert receipt["move_order"] == [PLAN, REGIME]
    assert [m["table"] for m in receipt["moves"]] == [PLAN, REGIME]
    assert all(m["state"] == "moved" for m in receipt["moves"])


def test_a_budget_that_runs_out_now_starves_the_smallest_table_not_the_last_one(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _regimes_and_plans(store)
    # The budget expires after the first table has moved, whatever the clock says.
    expired = {"after_moves": 1}
    real = off.MoveBudget.expired

    def expired_after_first(self) -> bool:
        return expired["moved"] >= expired["after_moves"] or real(self)

    expired["moved"] = 0
    real_move = off.move_table

    def counting_move(**kwargs):
        outcome = real_move(**kwargs)
        expired["moved"] += 1
        return outcome

    monkeypatch.setattr(off.MoveBudget, "expired", expired_after_first)
    monkeypatch.setattr(off, "move_table", counting_move)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[REGIME, PLAN])
    assert receipt["state"] == "partial"
    states = {m["table"]: m["state"] for m in receipt["moves"]}
    assert states == {PLAN: "moved", REGIME: "not_started_budget"}


def test_a_capped_table_stops_at_its_cap_and_the_rest_of_the_run_goes_on(
    store: Path, cold: Path, tmp_path: Path
) -> None:
    import dataclasses

    _regimes_and_plans(store)
    capped = dataclasses.replace(off.POLICY_BY_TABLE[REGIME], max_batches_per_run=2)
    policies = [capped, off.POLICY_BY_TABLE[PLAN]]
    before = _table_rows(store, REGIME)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[REGIME, PLAN],
                   policies=policies, batch_rows=1)
    assert receipt["state"] == "partial"
    states = {m["table"]: m for m in receipt["moves"]}
    assert states[REGIME]["state"] == "partial_batch_cap"
    assert states[REGIME]["batches"] == 2 and states[REGIME]["moved_rows"] == 2
    assert states[PLAN]["state"] == "moved"
    assert len(_table_rows(store, REGIME)) == len(before) - 2
    # The cap is per run: the next run carries on from where it stopped.
    again = _run(store, cold, tmp_path, apply=True, tables=[REGIME], policies=[capped], batch_rows=1)
    moved_twice = {m["table"]: m for m in again["moves"]}[REGIME]
    assert moved_twice["moved_rows"] >= 1
    # The cap travels with the policy into the cold ledger beside each batch.
    ledger = sqlite3.connect(_cold_file(cold, REGIME)).execute(
        f"SELECT policy_json FROM {off.COLD_BATCH_TABLE} LIMIT 1").fetchone()[0]
    assert json.loads(ledger)["max_batches_per_run"] == 2


MEMBER_TABLES = (
    "calibration_native_ensemble_historical_member_run",
    "calibration_republisher_ensemble_historical_member_run",
    "calibration_republisher_ensemble_member_run",
)


def test_the_member_run_tables_are_the_only_capped_ones() -> None:
    for table in MEMBER_TABLES:
        assert off.POLICY_BY_TABLE[table].max_batches_per_run == off.MEMBER_RUN_MAX_BATCHES_PER_RUN == 100
    assert all(
        policy.max_batches_per_run is None
        for policy in off.POLICIES if policy.table not in MEMBER_TABLES
    )


def test_the_republisher_members_take_the_native_window_and_the_archive_respects_it() -> None:
    """User 2026-11-01 「Add rules」: both republisher member tables leave past the
    native member window, which also covers the newest 64 truths the worker hashes."""

    from bayes_basket_target import production_worker

    for table in MEMBER_TABLES[1:]:
        policy = off.POLICY_BY_TABLE[table]
        assert policy.hot_days == off.MEMBER_RUN_HOT_DAYS
        assert policy.keep_latest[0].key == ("ensemble_system_id", "station_id")
    assert off.MEMBER_RUN_HOT_DAYS > production_worker.MAX_NATIVE_ENSEMBLE_TRUTH_CASES_PER_STATION
    tree = ast.parse(
        (ROOT / "scripts" / "capture_open_meteo_historical_ensemble_archive.py").read_text(encoding="utf-8")
    )
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "assert_within_hot_window"
        and isinstance(node.args[0], ast.Constant) and node.args[0].value == MEMBER_TABLES[1]
    ]
    assert calls


def test_the_row_days_cache_plans_the_same_rows_and_reads_the_scores_once(
    store: Path, tmp_path: Path
) -> None:
    """2026-10-15: the superseded rule's per-row days are cached by rowid
    (146 s of a 185 s plan was re-parsing every evaluation's scores); a plan
    with the cache names the rows a plan without it names, and the second
    cached plan reads no score JSON at all."""

    key = _oos_key("KLGA", ["DWD_ICON", "ECMWF_IFS"])
    conn = sqlite3.connect(store)
    for eid, at, days in (
        ("h1", "2026-09-14T01:00:00Z", ["2026-09-11", "2026-09-12"]),
        ("h2", "2026-09-16T01:00:00Z", ["2026-09-12", "2026-09-13"]),
        ("h3", "2026-09-18T01:00:00Z", ["2026-09-10", "2026-09-11", "2026-09-12"]),
        ("h4", "2026-09-22T01:00:00Z", ["2026-09-13"]),
        ("h5", "2026-09-26T01:00:00Z", ["2026-09-13"]),
    ):
        _evaluation(conn, eid, key, at, days=days)
    conn.commit()
    policy = off.POLICY_BY_TABLE[OOS]
    plain = off.plan_table(conn, policy, as_of=AS_OF, holds={})
    cache = tmp_path / "row_days.sqlite"
    first = off.plan_table(conn, policy, as_of=AS_OF, holds={}, row_days_cache=cache)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    second = off.plan_table(conn, policy, as_of=AS_OF, holds={}, row_days_cache=cache)
    conn.set_trace_callback(None)
    assert list(first.candidates) == list(plain.candidates) == list(second.candidates)
    assert first.superseded_rows == plain.superseded_rows == second.superseded_rows > 0
    rule = policy.move_superseded
    assert not [s for s in statements if rule.day_sql[0].split("(")[0] in s and "json" in s.lower()]
    conn.close()


def test_a_batch_held_past_the_lock_target_halves_the_next(
    store: Path, cold: Path, tmp_path: Path, monkeypatch
) -> None:
    """2026-10-16, a machine swapping 3.5 GiB: 1,000-row batches held the
    writer 33-90 s each.  The batch follows the lock, down to a floor."""

    _regimes_and_plans(store)
    monkeypatch.setattr(off, "LOCK_TARGET_SEC", -1.0)  # every hold is long
    monkeypatch.setattr(off, "MIN_BATCH_ROWS", 1)
    receipt = _run(store, cold, tmp_path, apply=True, tables=[REGIME], batch_rows=4)
    move = {m["table"]: m for m in receipt["moves"]}[REGIME]
    assert move["state"] == "moved" and move["batches"] >= 1
    assert move["min_batch_rows"] == 2  # the next batch after a long hold: 4 -> 2


def test_planning_takes_half_the_budget_and_the_rest_go_first_next_run(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-27: on a swapping host the plans alone took the whole 2,400 s
    (1.39 GiB planned, 0.01 GiB moved) and the store reached its ceiling.
    Past its planning share a run moves what it planned; the tables it did
    not reach are planned first next run."""

    first, second = off.POLICIES[0], off.POLICIES[1]
    planned: list[str] = []
    moved: list[str] = []

    def plan(_conn, policy, **_kwargs):
        planned.append(policy.table)
        return off.TablePlan(policy=policy, cutoff="2026-01-01", candidates=[1], estimated_bytes=1)

    def move(*, plan, **_kwargs):
        moved.append(plan.policy.table)
        return {"table": plan.policy.table, "state": "moved", "moved_rows": 1, "moved_bytes": 1}

    monkeypatch.setattr(off, "plan_table", plan)
    monkeypatch.setattr(off, "move_table", move)
    monkeypatch.setattr(off, "PLAN_BUDGET_SHARE", 0.0)
    receipt = _run(store, cold, tmp_path, apply=True, budget_seconds=100.0,
                   policies=(first, second))
    assert planned == [first.table] and moved == [first.table]
    states = {row["table"]: row["state"] for row in receipt["tables"]}
    assert states == {first.table: "planned", second.table: "not_planned_budget"}
    planned.clear()
    moved.clear()
    _run(store, cold, tmp_path, apply=True, budget_seconds=100.0,
         policies=(first, second), plan_first=[second.table])
    assert planned == [second.table] and moved == [second.table]


def test_the_moves_keep_their_share_however_long_planning_took(
    store: Path, cold: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-11-01 (user 「Dig into it」): the 13:56 run planned 3.4 GiB in its
    first half plus the WAL waits before each table and moved nothing -- the
    budget had run out before the first batch.  The moves now get their share
    from the end of planning, and the receipt says where the time went."""

    first = off.POLICIES[0]
    clock = [1000.0]
    monkeypatch.setattr(off.time, "monotonic", lambda: clock[0])
    seen: dict[str, float] = {}

    def plan(_conn, policy, **_kwargs):
        clock[0] += 150.0          # planning alone overran the whole 100 s budget
        return off.TablePlan(policy=policy, cutoff="2026-01-01", candidates=[1], estimated_bytes=1)

    def move(*, plan, budget, **_kwargs):
        seen["left"] = budget.seconds_left(10_000.0)
        return {"table": plan.policy.table, "state": "moved", "moved_rows": 1, "moved_bytes": 1}

    monkeypatch.setattr(off, "plan_table", plan)
    monkeypatch.setattr(off, "move_table", move)
    receipt = _run(store, cold, tmp_path, apply=True, budget_seconds=100.0, policies=(first,))
    assert seen["left"] == pytest.approx(100.0 * (1.0 - off.PLAN_BUDGET_SHARE))
    (row,) = receipt["tables"]
    assert row["plan_seconds"] == 150.0 and row["wal_wait_seconds"] == 0.0
    assert receipt["planning_seconds"] >= 150.0
    assert receipt["moves"][0]["wal_wait_seconds"] == 0.0
