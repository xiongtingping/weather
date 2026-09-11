"""Lambda is fitted on one band vintage at a time.

The band table changes every claim, and the claim is the denominator of
``a = price / claim`` -- the axis lambda itself is keyed on.  Measured
2026-09-08 on 757 live candidate windows, moving P0 to the distance axis
shifted ``a`` on 100% of them and carried 6.1% across a band boundary into
a different lambda value.  A fit pooling both vintages fits one curve to two
different x axes.

The partition key is the AXIS, not the table.  Band tables are content
addressed and refitted daily, so the sha moves whenever a factor shifts in
its third decimal: P0's 662 scored windows carry 28 distinct shas, the
largest holding 61 -- fewer than the eight lambda bands need at twelve seats
each -- and P1's 77 carry 21 shas of which exactly one clears the threshold.
Partitioning on the sha would not make the fit purer, it would end it.  The
sha is kept for audit so drift inside an axis stays visible.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import paper_trading_ledger as ledger  # noqa: E402
from bayes_basket_target.effective_edge_store import _fit_axis  # noqa: E402


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    return conn


def _candidate(conn, cid, *, axis=None, sha=None, stage="P0", bucket="30°C"):
    conn.execute(
        "INSERT INTO paper_trade_candidate_window("
        " candidate_id, station_id, event_date, stage, decision_at_utc,"
        " buckets_json, window_size, win_probability, all_in_price,"
        " admissible, seated, band_axis, band_table_sha256, hit)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "ZSQD", "2026-09-05", stage, "2026-09-05T00:00:00Z",
         json.dumps([bucket]), 1, 0.4, 0.3, 1, 0, axis, sha, 1),
    )


def test_the_table_carries_the_partition_key_and_the_audit_column():
    conn = _db()
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(paper_trade_candidate_window)"
        )
    }
    assert {"band_axis", "band_table_sha256"} <= columns


def test_an_existing_table_is_widened_rather_than_left_behind():
    """CREATE TABLE IF NOT EXISTS will not add a column, and every row
    already recorded predates both of these."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trade_candidate_window("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT NOT NULL,"
        " event_date TEXT NOT NULL,"
        " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),"
        " decision_at_utc TEXT NOT NULL, buckets_json TEXT NOT NULL,"
        " window_size INTEGER NOT NULL, win_probability REAL NOT NULL,"
        " all_in_price REAL NOT NULL)"
    )
    ledger.ensure_schema(conn)
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(paper_trade_candidate_window)"
        )
    }
    assert {"band_axis", "band_table_sha256"} <= columns


def test_the_fit_reads_one_axis_and_counts_what_it_left_out():
    """Both halves matter.  Excluding the other vintage is the point; saying
    how much was excluded is what stops a halved sample looking like a
    sample that was always that size."""

    import inspect

    from bayes_basket_target import effective_edge_store as store

    # The selection moved into ``_candidate_population`` on 2026-09-09 so
    # the fit and the activation guard read one implementation; the tallies
    # still surface on the fit's own receipt.
    source = inspect.getsource(store._candidate_population)
    assert "band_axis IS ?" in source
    assert "band_axis IS NOT ?" in source
    assert '"excluded_other_band_axis"' in source
    assert '"band_axis": axis' in source
    fit_source = inspect.getsource(store.fit_lambda_table)
    assert "_candidate_population(" in fit_source
    assert 'population["excluded_other_band_axis"]' in fit_source


def test_an_unknown_vintage_is_excluded_rather_than_assumed_current():
    """Charter 104 forbids silently borrowing a basis for a claim; a
    partition key gets the same rule.  ``IS ?`` rather than ``= ?`` so the
    NULL case is explicit either way."""

    conn = _db()
    # Different buckets: the row's identity is (station, date, stage,
    # decision, buckets), so two rows on one decision have to differ there.
    _candidate(conn, "known", axis="claim", sha="a" * 64, bucket="30°C")
    _candidate(conn, "unknown", bucket="31°C")
    conn.commit()
    got = conn.execute(
        "SELECT COUNT(*) FROM paper_trade_candidate_window"
        " WHERE hit IS NOT NULL AND band_axis IS ?", ("claim",),
    ).fetchone()[0]
    left_out = conn.execute(
        "SELECT COUNT(*) FROM paper_trade_candidate_window"
        " WHERE hit IS NOT NULL AND band_axis IS NOT ?", ("claim",),
    ).fetchone()[0]
    assert (got, left_out) == (1, 1)


def test_the_axis_comes_from_what_the_stages_serve_not_from_the_caller():
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
        persist_and_activate_band_table,
    )

    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    # No table at all is not a vintage: partitioning on the store's
    # historical ``claim`` default would exclude every row in a fixture or a
    # replay extract that never had band lineage to record.
    assert _fit_axis(conn, ("P0",)) is None, "no table, no partition"
    persist_and_activate_band_table(
        conn, stage="P0", factors=[((0.0, 1.0), 1.1)],
        evidence={}, serving_day_count=20, axis="centre_distance",
    )
    assert _fit_axis(conn, ("P0",)) == "centre_distance"
    # Stages on two different axes cannot be pooled into one fit, and the
    # honest answer is no axis rather than one of them.
    persist_and_activate_band_table(
        conn, stage="P1", factors=[((0.0, 0.5), 0.9)],
        evidence={}, serving_day_count=20, axis="claim",
    )
    assert _fit_axis(conn, ("P0", "P1")) is None


def test_the_backfill_resolves_a_vintage_through_the_store_not_a_guess():
    """The receipt only started carrying ``axis`` on 2026-09-07, so every
    historical row resolves by sha through the band store.  A sha the store
    no longer holds stays NULL."""

    import inspect

    source = inspect.getsource(ledger._backfill_candidate_band_lineage)
    assert "calibration_band_reliability_table" in source
    assert "COALESCE(axis,'claim')" in source
    assert '"sha_not_in_store"' in source
    assert '"no_band_receipt"' in source
    # Called where its sibling is, so a migration cannot add the column
    # without filling it.
    schema = inspect.getsource(ledger.ensure_schema)
    assert "_backfill_candidate_band_lineage(conn)" in schema
    # And it runs inside that schema step, so a ledger-only database -- a
    # replay extract, a fixture -- must not be failed by it.  Both sources
    # absent means no vintage to resolve, not an error.
    assert "band_lineage_sources_absent" in source
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    assert ledger._backfill_candidate_band_lineage(conn)["skipped"] == (
        "band_lineage_sources_absent"
    )


# --- Charter 110 extended (2026-09-08): the claim transform is a second key --


def test_the_two_claim_paths_carry_different_fingerprints() -> None:
    """A centre-holding window and an off-centre one are not one vintage.

    They go through different tables -- charter 81's lift or the off-centre
    deflation -- so one fingerprint over the union would retire a row for a
    change to a table it never read.
    """
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )

    assert claim_transform_fingerprint(
        holds_centre=True
    ) != claim_transform_fingerprint(holds_centre=False)


def test_moving_the_off_centre_table_leaves_centre_holding_rows_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason the key is split, stated as a test.

    Measured 2026-09-08: all 1,493 candidate windows in the book verify
    against the centre-holding path and none against the off-centre one --
    charter 49's ladder floor has left the fit no off-centre rows at all.  A
    union key would therefore have taken the whole lambda sample to zero to
    record a change to a table none of those rows read.
    """
    import bayes_basket_target.basket_kernel.delta_v2 as kernel

    before_centre = kernel.claim_transform_fingerprint(holds_centre=True)
    before_off = kernel.claim_transform_fingerprint(holds_centre=False)
    monkeypatch.setattr(
        kernel,
        "OFF_CENTER_CLAIM_BANDS",
        ((0.05, 1.15), (0.10, 0.85), (0.20, 0.88), (1.01, 0.5)),
    )
    assert kernel.claim_transform_fingerprint(holds_centre=True) == before_centre
    assert kernel.claim_transform_fingerprint(holds_centre=False) != before_off


def test_moving_charter_81_retires_the_centre_holding_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And it still fails closed where it must: 1,321 of those rows read
    charter 81's lift, and moving it really does change their claims."""
    import bayes_basket_target.basket_kernel.delta_v2 as kernel

    before = kernel.claim_transform_fingerprint(holds_centre=True)
    monkeypatch.setattr(
        kernel, "CENTER_WINDOW_CLAIM_BANDS", ((0.35, 1.0), (1.01, 1.5)),
    )
    assert kernel.claim_transform_fingerprint(holds_centre=True) != before


def test_the_fit_excludes_rows_from_another_claim_vintage() -> None:
    """A row whose claim came from other tables is not evidence about a."""
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )
    from bayes_basket_target.effective_edge_store import fit_lambda_table

    conn = _db()
    current = claim_transform_fingerprint(holds_centre=True)
    for index in range(6):
        _candidate(
            conn, f"cand-current-{index}", bucket=f"3{index}°C",
        )
        conn.execute(
            "UPDATE paper_trade_candidate_window"
            " SET claim_transform_sha256=? WHERE candidate_id=?",
            (current, f"cand-current-{index}"),
        )
    for index in range(6):
        _candidate(
            conn, f"cand-stale-{index}", bucket=f"4{index}°C",
        )
        conn.execute(
            "UPDATE paper_trade_candidate_window"
            " SET claim_transform_sha256=? WHERE candidate_id=?",
            ("0" * 64, f"cand-stale-{index}"),
        )
    conn.execute(
        "INSERT INTO paper_trade_settlement("
        " settlement_id, position_id, station_id, event_date, stage,"
        " settled_tmax_c, settled_bucket, hit, stake_usdc, payout_usdc,"
        " pnl_usdc, claimed_win_probability, settled_at_utc)"
        " VALUES('s1','p1','ZSQD','2026-09-05','P0',30.0,'30°C',1,"
        " 1.0,2.0,1.0,0.5,'2026-09-05T12:00:00Z')"
    )
    fit = fit_lambda_table(conn, stage_scope="P0")
    assert fit["claim_transform_sha256"] is not None
    assert fit["excluded_other_claim_transform"] == 6
    conn.close()


# --- The band-axis replay: mis-keyed is not dead (2026-09-08) --------------


def _replay_row(conn, cid, *, source, axis="centre_distance", stage="P0",
                bucket="30°C", claim_sha="x" * 64, band_sha="b" * 64):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS paper_trade_candidate_window_replay ("
        " replay_id TEXT PRIMARY KEY, source_candidate_id TEXT NOT NULL,"
        " station_id TEXT NOT NULL, event_date TEXT NOT NULL,"
        " stage TEXT NOT NULL, decision_at_utc TEXT NOT NULL,"
        " buckets_json TEXT NOT NULL, window_size INTEGER NOT NULL,"
        " win_probability REAL NOT NULL, raw_win_probability REAL NOT NULL,"
        " all_in_price REAL NOT NULL, hit INTEGER, band_axis TEXT NOT NULL,"
        " band_table_sha256 TEXT NOT NULL,"
        " claim_transform_sha256 TEXT NOT NULL, source_band_axis TEXT,"
        " replayed_at_utc TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO paper_trade_candidate_window_replay VALUES"
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, source, "ZSQD", "2026-09-05", stage, "2026-09-05T00:00:00Z",
         json.dumps([bucket]), 1, 0.4, 0.42, 0.3, 1, axis, band_sha,
         claim_sha, "claim", "2026-09-08T00:00:00Z"),
    )


def _one_settlement(conn) -> None:
    conn.execute(
        "INSERT INTO paper_trade_settlement("
        " settlement_id, position_id, station_id, event_date, stage,"
        " settled_tmax_c, settled_bucket, hit, stake_usdc, payout_usdc,"
        " pnl_usdc, claimed_win_probability, settled_at_utc)"
        " VALUES('s1','p1','ZSQD','2026-09-05','P0',30.0,'30°C',1,"
        " 1.0,2.0,1.0,0.5,'2026-09-05T12:00:00Z')"
    )


def test_replayed_rows_are_off_unless_asked_for() -> None:
    """The default reads served rows only, exactly as before."""
    from bayes_basket_target.effective_edge_store import fit_lambda_table

    conn = _db()
    _one_settlement(conn)
    for index in range(4):
        _replay_row(conn, f"r-{index}", source=f"src-{index}",
                    bucket=f"3{index}°C")
    assert fit_lambda_table(conn, stage_scope="P0")["seat_count"] == 0
    conn.close()


def test_replayed_rows_fit_when_the_served_axis_has_none() -> None:
    """The case this lane exists for: P0 after its band table changed axis."""
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )
    from bayes_basket_target.effective_edge_store import fit_lambda_table

    conn = _db()
    _one_settlement(conn)
    current = claim_transform_fingerprint(holds_centre=True)
    for index in range(4):
        _replay_row(conn, f"r-{index}", source=f"src-{index}",
                    bucket=f"3{index}°C", claim_sha=current)
    fit = fit_lambda_table(conn, stage_scope="P0", include_replayed=True)
    assert fit["seat_count"] == 4
    assert fit["served_row_count"] == 0
    assert fit["replayed_row_count"] == 4
    conn.close()


def test_a_served_row_beats_its_own_replay() -> None:
    """One scope, one row: the replay stands in, it does not double-count."""
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )
    from bayes_basket_target.effective_edge_store import fit_lambda_table

    conn = _db()
    _one_settlement(conn)
    current = claim_transform_fingerprint(holds_centre=True)
    _candidate(conn, "served-1", bucket="30°C")
    conn.execute(
        "UPDATE paper_trade_candidate_window"
        " SET claim_transform_sha256=? WHERE candidate_id=?",
        (current, "served-1"),
    )
    _replay_row(conn, "r-1", source="served-1", bucket="30°C",
                claim_sha=current)
    fit = fit_lambda_table(conn, stage_scope="P0", include_replayed=True)
    assert fit["seat_count"] == 1
    assert fit["served_row_count"] == 1
    assert fit["replayed_row_count"] == 0
    conn.close()


def test_the_activation_guard_is_judged_on_the_population_it_was_fitted_on(
) -> None:
    """Narrowed by user ruling, 2026-09-09.

    This test used to assert the opposite -- that ``admitted_return`` reads
    the served table and nothing else, because whether a table earns its seat
    is a question about the book that was really bought.  That survives a
    band-axis change badly.  P0's served rows are all on the old axis (957 of
    them, every one excluded from the fit for that reason), so the guard was
    scoring a distance-axis table against claim-axis claims: an input it will
    never see again.  Measured that day, the same challenger reads -2.33% on
    served rows and +0.30% on the current replay generation, and the refusal
    reverses.

    What the ruling kept: the guard admits only REAL prices and REAL
    settlements, which the replay changes neither of.  What it gave up, said
    out loud: a table can now earn its seat on a claim nobody traded against.
    """
    import inspect

    from bayes_basket_target import effective_edge_store as store

    source = inspect.getsource(store.admitted_return)
    # One implementation with the fit, so the two populations cannot drift.
    assert "_candidate_population(" in source
    assert "include_replayed=include_replayed" in source
    # And the flag is the fit's own, not a default the guard picks.
    activate = inspect.getsource(store.persist_and_activate)
    assert 'scored_on_replay = bool(fit.get("include_replayed"))' in activate
    assert activate.count("include_replayed=scored_on_replay") == 2


def test_the_guard_and_the_fit_read_the_same_rows() -> None:
    """The narrowing, as behaviour rather than as source text."""
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )
    from bayes_basket_target.effective_edge_store import (
        _candidate_population, fit_lambda_table,
    )

    conn = _db()
    _one_settlement(conn)
    current = claim_transform_fingerprint(holds_centre=True)
    _activate_band_table(conn, "c" * 64)
    for index in range(4):
        _replay_row(conn, f"r-{index}", source=f"src-{index}",
                    bucket=f"3{index}°C", claim_sha=current, band_sha="c" * 64)
    fit = fit_lambda_table(conn, stage_scope="P0", include_replayed=True)
    judged = _candidate_population(
        conn, fit["table"]["stages"], fit["table"]["fit_floor_event_date"],
        include_replayed=True,
    )
    assert len(judged["rows"]) == fit["seat_count"] == 4
    # Off by default: a caller that does not ask still gets served rows only.
    plain = _candidate_population(
        conn, fit["table"]["stages"], fit["table"]["fit_floor_event_date"],
        include_replayed=False,
    )
    assert plain["replayed_row_count"] == 0
    conn.close()


def test_the_replay_lane_is_actually_asked_for_by_the_sweep() -> None:
    """A lane that exists and is never called is not a lane.

    Every behaviour below this line was tested from the day the replay lane
    landed -- off unless asked for, fitting when the served axis has none, a
    served row beating its own replay, the activation guard refusing to read
    it -- and the daily sweep asked for none of it.  Measured 2026-09-09:
    ``fit_lambda_table`` ran with ``include_replayed`` at its default, so
    841 recomputed rows sat unread while P0 fitted on ZERO served rows and
    served a table frozen at a vintage its own axis change had made
    unrefreshable.

    Both halves are asserted, because either alone is a different kind of
    stale.  Passing the flag without running the replay wires the daily fit
    to whatever snapshot somebody last made by hand -- the table found that
    day stopped at 2026-09-07.  Running the replay without the flag is what
    was already happening.
    """

    sweep = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "calibration_refit_sweep.sh"
    ).read_text(encoding="utf-8")

    assert "replay_candidate_windows_on_current_band.py" in sweep, (
        "the sweep must refresh the recomputed claims it is about to fit on"
    )
    refit = next(
        line for line in sweep.splitlines()
        if "refit_effective_edge_lambda.py" in line
    )
    assert "--include-replayed" in refit, refit
    # The replay has to run BEFORE the fit that reads it, or the fit reads
    # yesterday's recomputation every day.
    assert sweep.index("replay_candidate_windows_on_current_band.py") < (
        sweep.index("refit_effective_edge_lambda.py")
    )
    # And it may never be fatal: a replay that cannot run leaves the fit on
    # served rows, which is where it was before the lane existed.
    tail = sweep[sweep.index("replay_candidate_windows_on_current_band.py"):]
    assert "||" in tail[:400], "the replay must not fail the sweep"


def _seat(conn, *, stage, decided, claim, hit, stake, pnl, pid):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS paper_trade_position("
        " position_id TEXT PRIMARY KEY, stage TEXT, decision_at_utc TEXT,"
        " win_probability REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS paper_trade_settlement("
        " settlement_id TEXT PRIMARY KEY, position_id TEXT, hit INTEGER,"
        " stake_usdc REAL, pnl_usdc REAL)"
    )
    conn.execute(
        "INSERT INTO paper_trade_position VALUES(?,?,?,?)",
        (pid, stage, decided, claim),
    )
    conn.execute(
        "INSERT INTO paper_trade_settlement VALUES(?,?,?,?,?)",
        ("s-" + pid, pid, hit, stake, pnl),
    )


def test_a_table_is_scored_on_the_seats_it_was_serving_for() -> None:
    """The one reading a table cannot grade itself on.

    A table is fitted on a trailing window and then serves, so the seats
    decided WHILE it served are after its fit: it chose them, it did not
    learn from them.  Every other number here is in sample somewhere -- the
    bands are the trailing window by construction, and ``admitted_return``
    is a counterfactual over candidate windows that its own docstring calls
    a comparator rather than a book return.
    """

    from bayes_basket_target.effective_edge_store import ensure_schema
    from bayes_basket_target.forward_serving_score import (
        forward_serving_score,
    )

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO calibration_effective_edge_serving_interval VALUES"
        " ('P0','aaa','2026-09-01T00:00:00+00:00','2026-09-03T00:00:00+00:00'),"
        " ('P0','bbb','2026-09-03T00:00:00+00:00',NULL)"
    )
    # One seat under each table, and one decided before either served.
    _seat(conn, stage="P0", decided="2026-08-31T12:00:00+00:00",
          claim=0.9, hit=1, stake=10.0, pnl=1.0, pid="before")
    _seat(conn, stage="P0", decided="2026-09-02T12:00:00+00:00",
          claim=0.8, hit=0, stake=10.0, pnl=-10.0, pid="under-aaa")
    _seat(conn, stage="P0", decided="2026-09-04T12:00:00+00:00",
          claim=0.5, hit=1, stake=10.0, pnl=5.0, pid="under-bbb")
    # A P1 seat inside the same clock belongs to P1's intervals, not P0's.
    _seat(conn, stage="P1", decided="2026-09-04T13:00:00+00:00",
          claim=0.5, hit=1, stake=99.0, pnl=99.0, pid="other-stage")
    conn.commit()

    score = forward_serving_score(conn, stage_scope="P0")
    assert score["available"] is True
    assert [row["lambda_table_sha256"] for row in score["intervals"]] == [
        "aaa", "bbb"
    ]
    first, second = score["intervals"]
    # The seat decided before either table served belongs to neither.
    assert first["settled_seats"] == 1 and second["settled_seats"] == 1
    assert first["realized_over_claimed"] == pytest.approx(0.0)
    assert first["roi"] == pytest.approx(-1.0)
    assert second["realized_over_claimed"] == pytest.approx(2.0)
    assert second["roi"] == pytest.approx(0.5)
    # An open interval is still serving and still scored.
    assert first["still_serving"] is False
    assert second["still_serving"] is True


def test_a_table_that_has_settled_nothing_reports_no_record_not_a_zero() -> None:
    """Zero would read as a verdict.  A table nobody has settled under has
    no forward record at all, and saying so is the honest answer."""

    from bayes_basket_target.effective_edge_store import ensure_schema
    from bayes_basket_target.forward_serving_score import (
        forward_serving_score,
    )

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO calibration_effective_edge_serving_interval VALUES"
        " ('P0','fresh','2026-09-09T00:00:00+00:00',NULL)"
    )
    conn.commit()
    score = forward_serving_score(conn, stage_scope="P0")
    row = score["intervals"][0]
    assert row["settled_seats"] == 0
    assert row["realized_over_claimed"] is None
    assert row["roi"] is None
    assert score["scored_interval_count"] == 0


def test_activation_closes_the_outgoing_interval_and_opens_one() -> None:
    """``activated_at_utc`` said when a table started and never when one
    stopped, so there was no window to score anything over: 42 tables
    fitted by 2026-09-09, three carrying a stamp, all three stamped the
    same minute, no history behind them."""

    from bayes_basket_target.effective_edge_store import ensure_schema

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    # The shape the activation path writes: close the open one, open the new.
    conn.execute(
        "INSERT INTO calibration_effective_edge_serving_interval VALUES"
        " ('P0','old','2026-09-01T00:00:00+00:00',NULL)"
    )
    now = "2026-09-05T00:00:00+00:00"
    conn.execute(
        "UPDATE calibration_effective_edge_serving_interval"
        " SET served_until_utc=? WHERE stage_scope=? AND"
        " served_until_utc IS NULL",
        (now, "P0"),
    )
    conn.execute(
        "INSERT INTO calibration_effective_edge_serving_interval("
        " stage_scope, lambda_table_sha256, served_from_utc,"
        " served_until_utc) VALUES(?,?,?,NULL)",
        ("P0", "new", now),
    )
    # P1 activating must not close P0's interval.
    conn.execute(
        "INSERT INTO calibration_effective_edge_serving_interval VALUES"
        " ('P1','p1a','2026-09-06T00:00:00+00:00',NULL)"
    )
    conn.commit()
    rows = conn.execute(
        "SELECT stage_scope, lambda_table_sha256, served_until_utc"
        " FROM calibration_effective_edge_serving_interval"
        " ORDER BY stage_scope, served_from_utc"
    ).fetchall()
    assert rows == [
        ("P0", "old", now),
        ("P0", "new", None),
        ("P1", "p1a", None),
    ]


def _activate_band_table(conn, sha, *, stage="P0", axis="centre_distance"):
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
    )
    ensure_band_table_schema(conn)
    conn.execute(
        "INSERT INTO calibration_band_reliability_table("
        " band_table_sha256, factors_json, evidence_json, fitted_at_utc,"
        " serving_day_count, activated, activated_at_utc, stage, axis)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (sha, json.dumps([[[0.0, 1.0], 1.0]]), "{}",
         "2026-09-09T00:00:00Z", 30, 1, "2026-09-09T00:00:00Z", stage, axis),
    )


def test_the_replay_read_takes_one_band_generation() -> None:
    """A settled window re-derived onto three tables is still one window.

    The lane is keyed (source window, band table), so it grows a row every
    time the band table is refitted -- daily.  Measured 2026-09-09, the day
    after it shipped: 841 of P0's source windows carried three rows each and
    57 carried two, and the fit read all 2,637 of them, which is the same
    window voting three times with three different claims.
    """
    from bayes_basket_target.basket_kernel.delta_v2 import (
        claim_transform_fingerprint,
    )
    from bayes_basket_target.effective_edge_store import fit_lambda_table

    conn = _db()
    _one_settlement(conn)
    current = claim_transform_fingerprint(holds_centre=True)
    _activate_band_table(conn, "c" * 64)
    for index in range(4):
        for generation in ("a" * 64, "b" * 64, "c" * 64):
            _replay_row(
                conn, f"r-{generation[0]}-{index}", source=f"src-{index}",
                bucket=f"3{index}°C", claim_sha=current, band_sha=generation,
            )
    fit = fit_lambda_table(conn, stage_scope="P0", include_replayed=True)
    assert fit["replay_band_table_sha256"] == "c" * 64
    assert fit["replayed_row_count"] == 4, "one generation, not three"
    assert fit["seat_count"] == 4
    conn.close()


def test_a_table_fitted_on_another_band_axis_is_not_served() -> None:
    """``a = price / claim`` and the band axis moves every claim, so a table
    fitted on one axis describes a different quantity on the other.  The
    population was already partitioned on the axis (charter 110); the table
    that came out recorded none, so nothing downstream could tell.

    Three states, not two: a payload recording NO axis is carried as
    matching, because "did not record a vintage" is not "the vintage
    changed" -- treating it as one deadlocks the lane against its own
    activation gate, which is the 2026-09-08 judgement on the claim vintage.
    """

    import json
    import sqlite3

    from bayes_basket_target import effective_edge_store as store
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
    )

    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    ensure_band_table_schema(conn)

    def _band(axis: str) -> None:
        conn.execute(
            "DELETE FROM calibration_band_reliability_table WHERE stage='P0'"
        )
        conn.execute(
            "INSERT INTO calibration_band_reliability_table("
            " band_table_sha256, factors_json, evidence_json, fitted_at_utc,"
            " serving_day_count, activated, activated_at_utc, stage, axis)"
            " VALUES(?,?,?,?,?,1,?,?,?)",
            ("b" * 64, json.dumps([[[0.0, 1.0], 1.0]]), "{}",
             "2026-09-09T00:00:00Z", 30, "2026-09-09T00:00:00Z", "P0", axis),
        )

    def _table(sha: str, axis, lam: float) -> None:
        payload = {
            "schema": "effective_edge_lambda_table_v1",
            "stage_scope": "P0", "claim_scope": "window", "stages": ["P0"],
            "bands": [{"upper_bound": None, "lambda": lam}],
            "window_days": 28, "as_of_event_date": "2026-09-10",
            "fit_floor_event_date": "2026-08-13",
            "band_partition_cells": store.BAND_PARTITION_CELLS,
            "claim_transform_sha256": None,
        }
        if axis is not None:
            payload["band_axis"] = axis
        conn.execute(
            "UPDATE calibration_effective_edge_lambda SET activated=0"
            " WHERE stage_scope='P0'"
        )
        conn.execute(
            "INSERT INTO calibration_effective_edge_lambda("
            " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
            " window_days, seat_count, activated, activated_at_utc,"
            " stage_scope, claim_scope) VALUES(?,?,?,?,?,?,1,?,?,?)",
            (sha, json.dumps(payload), "{}", "2026-09-10T00:00:00Z", 28, 50,
             "2026-09-10T00:00:00Z", "P0", "window"),
        )
        conn.commit()

    _band("centre_distance")
    # Same axis: served.
    _table("a" * 64, "centre_distance", 0.31)
    assert store.load_active_lambda(conn, stage_scope="P0") == ((float("inf"), 0.31),)
    # Another axis: not served, and the incumbent falls back to the kernel
    # constant rather than to a table about a different quantity.
    _table("c" * 64, "claim", 0.31)
    assert store.load_active_lambda(conn, stage_scope="P0") is None
    assert store._incumbent_bands(conn, "P0", "window") == list(
        store._bootstrap_bands("P0", "window")
    )
    # No axis recorded: carried, not refused.
    _table("d" * 64, None, 0.29)
    assert store.load_active_lambda(conn, stage_scope="P0") == ((float("inf"), 0.29),)
    # No band table at all: no partition, so no mismatch is possible.
    conn.execute("DELETE FROM calibration_band_reliability_table")
    conn.commit()
    _table("e" * 64, "claim", 0.27)
    assert store.load_active_lambda(conn, stage_scope="P0") == ((float("inf"), 0.27),)


def test_the_fit_records_the_axis_it_was_drawn_on(tmp_path) -> None:
    """The payload must carry the axis, or the guard above has nothing to
    read and the next fit inherits across an axis move in silence."""

    import json
    import sqlite3

    from bayes_basket_target import effective_edge_store as store

    conn = sqlite3.connect(tmp_path / "e.db", isolation_level=None)
    conn.execute(
        "CREATE TABLE paper_trade_candidate_window ("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, buckets_json TEXT,"
        " win_probability REAL, all_in_price REAL, hit INTEGER)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_settlement ("
        " position_id TEXT, stage TEXT, event_date TEXT, hit INTEGER)"
    )
    for i in range(20):
        conn.execute(
            "INSERT INTO paper_trade_candidate_window VALUES(?,?,?,?,?,?,?,?,?)",
            (f"c{i}", f"S{i}", "2026-09-01", "P0", "2026-09-01T00:00:00Z",
             '["b"]', 0.60, 0.18, i % 2),
        )
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES(?,?,?,?)",
            (f"p{i}", "P0", "2026-09-01", i % 2),
        )
    fit = store.fit_lambda_table(conn, window_days=28)
    assert "band_axis" in fit["table"]
    assert fit["table"]["band_axis"] == fit["band_axis"]
    conn.close()


def test_the_axis_column_is_read_not_the_candidate_tables_column() -> None:
    """The lambda table's own migration decides whether the column is there.
    Keying off ``paper_trade_candidate_window``'s band_axis -- a different
    table on a different migration -- would be right only by coincidence."""

    import sqlite3

    from bayes_basket_target import effective_edge_store as store

    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    assert store._has_lambda_band_axis_column(conn)
    # No candidate table at all: the lambda column is still there and still
    # read.
    assert not store._has_band_axis_column(conn)
    columns = {
        str(r[1])
        for r in conn.execute(
            "PRAGMA table_info(calibration_effective_edge_lambda)"
        )
    }
    assert "band_axis" in columns
    # The journal deliberately does NOT gain the column: it records fits,
    # and the fit's axis travels on the table it points at.
    journal = {
        str(r[1])
        for r in conn.execute(
            "PRAGMA table_info(calibration_effective_edge_fit_journal)"
        )
    }
    assert "band_axis" not in journal


def test_the_axis_is_read_from_the_column_when_the_payload_has_none() -> None:
    """Every row that existed before 2026-09-11 records its axis ONLY in the
    column: ``lambda_table_sha256`` is ``content_sha256(payload)``, so the
    backfill could not touch the payload without breaking each row against
    its own hash.  A guard that reads only the payload therefore passes
    every one of them -- which is how the first version of this check
    shipped reading a different function's query and looked fine.
    """

    import json
    import sqlite3

    from bayes_basket_target import effective_edge_store as store
    from bayes_basket_target.band_reliability_store import (
        ensure_band_table_schema,
    )

    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    ensure_band_table_schema(conn)
    conn.execute(
        "INSERT INTO calibration_band_reliability_table("
        " band_table_sha256, factors_json, evidence_json, fitted_at_utc,"
        " serving_day_count, activated, activated_at_utc, stage, axis)"
        " VALUES(?,?,?,?,?,1,?,?,?)",
        ("b" * 64, json.dumps([[[0.0, 1.0], 1.0]]), "{}",
         "2026-09-11T00:00:00Z", 30, "2026-09-11T00:00:00Z", "P0-M",
         "centre_distance"),
    )
    payload = {
        "schema": "effective_edge_lambda_table_v1",
        "stage_scope": "P0-M", "claim_scope": "window", "stages": ["P0-M"],
        "bands": [{"upper_bound": None, "lambda": 0.31}],
        "window_days": 28, "as_of_event_date": "2026-09-10",
        "fit_floor_event_date": "2026-08-13",
        "band_partition_cells": store.BAND_PARTITION_CELLS,
        "claim_transform_sha256": None,
    }
    assert "band_axis" not in payload
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc, stage_scope,"
        " claim_scope, band_axis) VALUES(?,?,?,?,?,?,1,?,?,?,?)",
        ("a" * 64, json.dumps(payload), "{}", "2026-09-10T00:00:00Z", 28, 50,
         "2026-09-10T00:00:00Z", "P0-M", "window", "claim"),
    )
    conn.commit()
    # The column says claim, the stage serves centre_distance: no table.
    assert store.load_active_lambda(conn, stage_scope="P0-M") is None
    assert store._incumbent_bands(conn, "P0-M", "window") == list(
        store._bootstrap_bands("P0-M", "window")
    )
    # Same row, column agreeing with the served axis: served.
    conn.execute(
        "UPDATE calibration_effective_edge_lambda"
        " SET band_axis='centre_distance' WHERE lambda_table_sha256=?",
        ("a" * 64,),
    )
    conn.commit()
    assert store.load_active_lambda(conn, stage_scope="P0-M") == (
        (float("inf"), 0.31),
    )
