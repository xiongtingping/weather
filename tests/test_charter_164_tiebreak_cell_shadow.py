"""Charter 164: the tiebreak cell's replay, and the hypotheses registered
against it (user ruling 2026-09-20 「落」).

What these bind is the discipline, not the numbers: the replay is the rule
replayed on first decisions at the station-day grain, a hypothesis is read
twice with the FORWARD window the one that decides, the intervals resample
event days rather than rows, and the registry only grows.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import tiebreak_cell_shadow as shadow  # noqa: E402


def _row(station: str, day: str, stage: str, *, hit: int, ask: float,
         move: float | None = None, decided: str | None = None) -> dict:
    return {
        "station_id": station, "event_date": day, "stage": stage,
        "decision_at_utc": decided or f"{day}T00:00:00.000Z",
        "rule_version": "tiebreak_model_mode_market_midband_spread_v2_1",
        "bucket": "20°C", "settled_bucket": "20°C" if hit else "21°C", "hit": hit,
        "market_share": 0.5, "model_share": 0.45, "market_mode_share": 0.5,
        "mode_distance": 0, "buckets_in_common": 11,
        "best_ask": ask, "best_bid": ask - 0.02, "spread": 0.02,
        "ask_depth_shares": 50.0, "market_move_on_model_mode": move,
        "prev_gap_minutes": None if move is None else 700.0,
        "prev_market_share": None, "prev_model_mode": None,
        "content_sha256": "c" * 64,
    }


def test_the_grain_is_a_station_day_and_the_earliest_firing_stage_wins() -> None:
    """A chain is a station-day.  Counting P0, P0-M and P1 of one day as
    three would weight a day that fires three times triple."""

    rows = [
        _row("AAAA", "2026-09-01", "P1", hit=1, ask=0.5, decided="2026-09-01T09:00:00.000Z"),
        _row("AAAA", "2026-09-01", "P0", hit=0, ask=0.5, decided="2026-09-01T01:00:00.000Z"),
        _row("AAAA", "2026-09-01", "P0-M", hit=1, ask=0.5, decided="2026-09-01T05:00:00.000Z"),
        _row("BBBB", "2026-09-01", "P0", hit=1, ask=0.5),
    ]
    got = shadow.one_per_station_day(rows)
    assert [(r["station_id"], r["stage"]) for r in got] == [("AAAA", "P0"), ("BBBB", "P0")]
    # A row with no ask was never buyable and is not a station-day either.
    noask = _row("CCCC", "2026-09-01", "P0", hit=1, ask=0.5)
    noask["best_ask"] = None
    assert not [r for r in shadow.one_per_station_day([noask])]


def test_the_interval_resamples_event_days_not_rows() -> None:
    """Stations of one day share their weather.  Resampling rows would call
    a day's worth of correlated outcomes independent evidence -- the reading
    that pooling stations manufactures.  Same rows, fewer days, wider."""

    # Twenty rows either way, and the same thirteen hits.  Spread over twenty
    # days they are twenty pieces of evidence; packed into three days where a
    # day's stations share their outcome, they are three.
    spread = [
        _row(f"S{i:03d}", f"2026-09-{1 + i:02d}", "P0", hit=int(i < 13), ask=0.5)
        for i in range(20)
    ]
    clustered = [
        _row(f"S{i:03d}", f"2026-09-{1 + (0 if i < 7 else 1 if i < 13 else 2):02d}",
             "P0", hit=int(i < 13), ask=0.5)
        for i in range(20)
    ]
    wide = shadow.day_clustered_interval(spread)
    narrow_days = shadow.day_clustered_interval(clustered)
    assert wide["event_days"] == 20 and narrow_days["event_days"] == 3
    assert wide["point"] == pytest.approx(narrow_days["point"])
    assert (wide["ci_hi"] - wide["ci_lo"]) < (narrow_days["ci_hi"] - narrow_days["ci_lo"])
    # Under three event days there is no interval at all, rather than a narrow one.
    assert shadow.day_clustered_interval(clustered[:2])["ci_lo"] is None


def test_a_hypothesis_is_read_twice_and_only_the_forward_window_can_decide() -> None:
    """The reading over the days that suggested a hypothesis is not evidence
    for it.  Registration day 2026-09-20: everything up to and including it
    is 'all', and only strictly later event days are 'forward'."""

    rows = (
        [_row(f"A{i:03d}", "2026-09-10", "P1", hit=1, ask=0.5) for i in range(4)]
        + [_row(f"B{i:03d}", "2026-09-10", "P0", hit=0, ask=0.5) for i in range(4)]
        + [_row(f"C{i:03d}", "2026-09-25", "P1", hit=1, ask=0.5) for i in range(3)]
        + [_row(f"D{i:03d}", "2026-09-25", "P0", hit=0, ask=0.5) for i in range(3)]
    )
    got = {(r["hypothesis_id"], r["reading_window"]): r for r in shadow.hypothesis_readings(rows)}
    gradient_all = got[("stage_gradient_v1", "all")]
    gradient_fwd = got[("stage_gradient_v1", "forward")]
    assert gradient_all["n_treatment"] == 7 and gradient_all["n_control"] == 7
    # Only 2026-09-25 is after the registration date.
    assert gradient_fwd["n_treatment"] == 3 and gradient_fwd["n_control"] == 3
    assert gradient_fwd["event_days"] == 1
    # Every registered hypothesis is read in both windows, always.
    assert {h["hypothesis_id"] for h in shadow.HYPOTHESES} == {
        r["hypothesis_id"] for r in shadow.hypothesis_readings(rows)
    }


def test_the_registry_carries_its_registration_day_and_what_it_read_then() -> None:
    """A hypothesis without the day it was written down, and without the
    number it was written from, cannot be told from one fitted afterwards."""

    for spec in shadow.HYPOTHESES:
        assert len(str(spec["registered_at_utc"])) >= 10
        assert str(spec["prediction"]).strip()
        assert str(spec["at_registration"]).strip()
    ids = [s["hypothesis_id"] for s in shadow.HYPOTHESES]
    assert len(ids) == len(set(ids))
    # The momentum split is registered as the CONTROL for the stage gradient,
    # not as a candidate: both live on P1, and if the stage is what pays this
    # one goes to zero while the gradient holds.
    control = next(s for s in shadow.HYPOTHESES if s["hypothesis_id"] == "p1_converging_v1")
    assert control["control"] is not None


def test_the_permutation_asks_about_the_split_not_about_the_arm() -> None:
    """Two different questions.  A subset of a profitable cell makes money by
    inheritance, so an interval on the arm says little about the split; the
    permutation holds each event day's own difficulty fixed and moves only
    the assignment.  A split that carries nothing lands near a half."""

    # Both arms present on every event day, and the labels carry nothing
    # about which arm a row is in: the split should land near a half.
    days = [f"2026-09-{1 + d:02d}" for d in range(6)]
    treat = [_row(f"T{d}{i}", day, "P1", hit=i % 2, ask=0.5)
             for d, day in enumerate(days) for i in range(4)]
    control = [_row(f"C{d}{i}", day, "P1", hit=i % 2, ask=0.5)
               for d, day in enumerate(days) for i in range(4)]
    p = shadow.permutation_p(treat, control, draws=400)
    assert p is not None and 0.05 < p < 0.95
    # No control arm, no question about a split.
    assert shadow.permutation_p(treat, []) is None
    # And arms that never share an event day: shuffling inside a day can move
    # nothing between them, so the test has no power and says None rather
    # than the 1.00 that reads as "certainly nothing".
    apart_a = [_row(f"A{i}", "2026-09-01", "P1", hit=i % 2, ask=0.5) for i in range(4)]
    apart_b = [_row(f"B{i}", "2026-09-02", "P0", hit=i % 2, ask=0.5) for i in range(4)]
    assert shadow.permutation_p(apart_a, apart_b, draws=50) is None


def test_the_refresh_is_idempotent_and_does_not_rewrite_settled_rows(tmp_path) -> None:
    """The replay is re-derived every day.  A station-day settles once, so a
    second pass must not append it, and must not restamp a row whose content
    has not changed -- a table that rewrites itself daily cannot be read for
    when something changed."""

    conn = sqlite3.connect(str(tmp_path / "shadow.db"))
    shadow.ensure_shadow_schema(conn, prefix="")
    rows = [_row("AAAA", "2026-09-01", "P0", hit=1, ask=0.5)]
    columns = [
        "station_id", "event_date", "stage", "decision_at_utc", "rule_version", "bucket",
        "settled_bucket", "hit", "market_share", "model_share", "market_mode_share",
        "mode_distance", "buckets_in_common", "best_ask", "best_bid", "spread",
        "ask_depth_shares", "market_move_on_model_mode", "prev_gap_minutes",
        "prev_market_share", "prev_model_mode", "content_sha256",
    ]
    stmt = (
        f"INSERT INTO {shadow.REPLAY_TABLE}({','.join(columns)},refreshed_at_utc)"
        f" VALUES({','.join('?' * (len(columns) + 1))})"
        " ON CONFLICT(station_id,event_date,stage) DO UPDATE SET"
        "  settled_bucket=excluded.settled_bucket, hit=excluded.hit,"
        "  content_sha256=excluded.content_sha256, refreshed_at_utc=excluded.refreshed_at_utc"
        f" WHERE {shadow.REPLAY_TABLE}.content_sha256<>excluded.content_sha256"
    )
    for stamp in ("2026-09-20T00:00:00.000Z", "2026-09-21T00:00:00.000Z"):
        conn.executemany(stmt, [tuple(r[c] for c in columns) + (stamp,) for r in rows])
    got = conn.execute(
        f"SELECT COUNT(*), MAX(refreshed_at_utc) FROM {shadow.REPLAY_TABLE}"
    ).fetchone()
    assert got == (1, "2026-09-20T00:00:00.000Z")
    conn.close()


def test_the_shadow_never_writes_to_the_store_it_reads(tmp_path) -> None:
    """A research artifact that can write the SSOT is a research artifact
    that can corrupt it.  The connection's ``main`` is read-only."""

    ssot = tmp_path / "ssot.db"
    sqlite3.connect(str(ssot)).close()
    conn = shadow.open_shadow_connection(
        writable=True, ssot_path=ssot, shadow_path=tmp_path / "shadow.db",
    )
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE main.should_not_exist(x INTEGER)")
        # and the shadow side is writable
        conn.execute(
            f"INSERT INTO {shadow.SHADOW_SCHEMA}.{shadow.HYPOTHESIS_TABLE}("
            " hypothesis_id,as_of_date,registered_at_utc,title,prediction,at_registration,"
            " reading_window,n_treatment,n_control,event_days,content_sha256,computed_at_utc)"
            " VALUES('h','2026-09-20','2026-09-20','t','p','r','all',1,0,1,?,'now')",
            ("d" * 64,),
        )
    finally:
        conn.close()


def test_every_hypothesis_declares_a_known_population() -> None:
    """The pool is part of the hypothesis (2026-09-20 second pass).  A stage
    contrast read on the slot grain and the same one read over every firing
    row are different questions, so the entry has to say which it asked."""

    for spec in shadow.HYPOTHESES:
        assert str(spec.get("pool") or "grain") in shadow.POOLS


def test_a_stage_contrast_on_the_slot_grain_carries_the_selection() -> None:
    """The slot reaches P1 only when nothing earlier fired.  So on a
    station-day where P0 fires too, the grain has no P1 row at all -- the two
    arms of a grain-pooled stage contrast are disjoint populations, and the
    unconditional version has to be read on the firing rows."""

    rows = [
        # Three station-days where P0 fires first and P1 fires later as well:
        # the grain keeps only the P0 row.
        *[r for i in range(3) for r in (
            _row(f"A{i:03d}", "2026-09-10", "P0", hit=0, ask=0.5,
                 decided="2026-09-10T01:00:00.000Z"),
            _row(f"A{i:03d}", "2026-09-10", "P1", hit=1, ask=0.5,
                 decided="2026-09-10T09:00:00.000Z"),
        )],
        # Three where only P1 fires.
        *[_row(f"B{i:03d}", "2026-09-11", "P1", hit=1, ask=0.5) for i in range(3)],
        *[_row(f"C{i:03d}", "2026-09-12", "P0", hit=0, ask=0.5) for i in range(3)],
    ]
    pools = shadow.pools_of(rows)
    assert len(pools["firings"]) == 12 and len(pools["grain"]) == 9
    assert [r["stage"] for r in pools["grain"]].count("P1") == 3
    assert [r["stage"] for r in pools["firings"]].count("P1") == 6
    got = {(r["hypothesis_id"], r["reading_window"]): r for r in shadow.hypothesis_readings(rows)}
    grain = got[("stage_gradient_v1", "all")]
    every = got[("stage_gradient_unconditional_v1", "all")]
    assert grain["n_treatment"] == 3 and every["n_treatment"] == 6
    assert grain["n_control"] == every["n_control"] == 6
    # Exactly one row of a station-day is the slot, and it is the earliest.
    slots = [r for r in pools["firings"] if r["is_slot"]]
    assert len(slots) == 9
    assert all(r["stage"] == "P0" for r in slots if r["station_id"].startswith("A"))


def test_the_contested_pool_pairs_the_slot_against_the_p1_it_passed_over() -> None:
    """Buying at P1 instead is only a choice on the station-days where both
    fired.  Those pair on the same weather; a day where only P1 fired is not
    evidence about the slot and stays out."""

    rows = [
        _row("AAAA", "2026-09-10", "P0-M", hit=0, ask=0.5,
             decided="2026-09-10T05:00:00.000Z"),
        _row("AAAA", "2026-09-10", "P1", hit=1, ask=0.5,
             decided="2026-09-10T09:00:00.000Z"),
        # P1 alone: no choice to price.
        _row("BBBB", "2026-09-10", "P1", hit=1, ask=0.5),
        # Two earlier stages and no P1: also no choice to price.
        _row("CCCC", "2026-09-10", "P0", hit=1, ask=0.5,
             decided="2026-09-10T01:00:00.000Z"),
        _row("CCCC", "2026-09-10", "P0-M", hit=1, ask=0.5,
             decided="2026-09-10T05:00:00.000Z"),
    ]
    pool = shadow.pools_of(rows)["contested_p1"]
    assert {(r["station_id"], r["stage"]) for r in pool} == {("AAAA", "P0-M"), ("AAAA", "P1")}
    spec = next(
        s for s in shadow.HYPOTHESES if s["hypothesis_id"] == "slot_to_p1_contested_v1"
    )
    treatment = [r for r in pool if spec["treatment"](r)]
    control = [r for r in pool if spec["control"](r)]
    assert [r["stage"] for r in treatment] == ["P1"]
    assert [r["stage"] for r in control] == ["P0-M"]
    # The arms never share a row: the slot of a contested day is never its P1.
    assert not {id(r) for r in treatment} & {id(r) for r in control}
