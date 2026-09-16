"""Charter 140: the claim lanes fit on production's own evidence.

The lambda and beta lanes read every enumerated window -- refused ones too --
and until this charter the only table holding the whole enumeration was the
paper ledger's.  These pin the production path end to end: the compile
projects the enumeration into the decision audit, the atomic ledger appends it
beside the decision fact, the truth lane labels the day, and both fits read the
scored view.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from bayes_basket_target import atomic_ledger  # noqa: E402
from bayes_basket_target import beta_edge_store  # noqa: E402
from bayes_basket_target import candidate_window_evidence as E  # noqa: E402
from bayes_basket_target import effective_edge_store  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402

DAY = "2026-09-10"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE basket_probability_envelope (
          envelope_id TEXT PRIMARY KEY, decision_at_utc TEXT, lineage_json TEXT);
        CREATE TABLE calibration_station_daily_high_truth (
          station_id TEXT, local_date TEXT, revision_no INTEGER,
          official_tmax_c REAL, content_sha256 TEXT,
          canonical_committed_at_utc TEXT, qc_status TEXT);
        CREATE TABLE basket_probability_bridge_history (
          history_id TEXT, station_id TEXT, event_date TEXT, stage TEXT,
          state TEXT, decision_at_utc TEXT);
        CREATE TABLE basket_probability_bridge_bucket_probability (
          history_id TEXT, bucket TEXT, ordinal INTEGER, lower_c REAL,
          upper_c REAL, probability REAL);
        """
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES"
        " ('h1','KMIA',?,'P0','COMMITTED','2026-09-09T01:00:00Z')",
        (DAY,),
    )
    for ordinal, (bucket, low, high) in enumerate(
        [("29", None, 30.0), ("30", 30.0, 31.0), ("31", 31.0, 32.0),
         ("32", 32.0, None)]
    ):
        conn.execute(
            "INSERT INTO basket_probability_bridge_bucket_probability"
            " VALUES ('h1',?,?,?,?,0.25)",
            (bucket, ordinal, low, high),
        )
    return conn


def _window(buckets, *, price, claim, hours, admissible=True, seated=False):
    return {
        "buckets": buckets, "win_probability": claim,
        "raw_win_probability": claim, "all_in_price": price,
        "gate_agreement": price / claim, "admissible": admissible,
        "seated": seated, "holds_centre": True, "leg_claim_cost": None,
        "market_mid": price - 0.02, "hours_to_cutoff": hours,
    }


def _fact(conn, fact_id, envelope, decided, windows):
    conn.execute(
        "INSERT OR IGNORE INTO basket_probability_envelope VALUES (?,?,?)",
        (envelope, decided, json.dumps({})),
    )
    return {
        "fact_id": fact_id, "decision_id": "d-" + fact_id,
        "station_id": "KMIA", "event_date": DAY, "stage": "P0",
        "decision_at_utc": decided, "probability_envelope_id": envelope,
        "feature_json": json.dumps(
            {"plan": {"delta_v2_audit": {"candidate_windows": windows}}}
        ),
    }


def test_the_compile_projection_keeps_refused_windows_and_marks_the_seat():
    rows = E.candidate_windows_v1(
        [
            {"buckets": ["30", "31"], "win_probability": 0.6,
             "raw_win_probability": 0.55, "all_in_price": 0.5,
             "effective_edge_agreement": 0.83, "admissible": True,
             "center_gap_buckets": 0, "beta_market_mid": 0.48,
             "beta_hours_to_cutoff": 20.0},
            {"buckets": ["29"], "win_probability": 0.1, "all_in_price": 0.2,
             "admissible": False, "center_gap_buckets": 2},
            {"buckets": ["32"], "all_in_price": None},
        ],
        seated_buckets=["30", "31"],
    )
    assert [r["buckets"] for r in rows] == [["30", "31"], ["29"]]
    assert rows[0]["seated"] and not rows[1]["seated"]
    assert rows[1]["admissible"] is False and rows[1]["holds_centre"] is False
    assert rows[0]["market_mid"] == 0.48 and rows[0]["hours_to_cutoff"] == 20.0


def test_a_fact_appends_its_windows_and_only_the_first_decision_per_lead_band_samples():
    conn = _db()
    first = _fact(conn, "f1", "e1", "2026-09-09T01:00:00Z", [
        _window(["30", "31"], price=0.5, claim=0.6, hours=30.0, seated=True),
        _window(["29"], price=0.2, claim=0.1, hours=30.0, admissible=False),
    ])
    again = _fact(conn, "f2", "e2", "2026-09-09T02:00:00Z", [
        _window(["30", "31"], price=0.55, claim=0.6, hours=29.0),
    ])
    later = _fact(conn, "f3", "e3", "2026-09-10T01:00:00Z", [
        _window(["31"], price=0.4, claim=0.45, hours=10.0),
    ])
    assert E.record_fact_windows(conn, first) == 2
    assert E.record_fact_windows(conn, again) == 1
    assert E.record_fact_windows(conn, later) == 1
    sampled = dict(conn.execute(
        f"SELECT fact_id, MAX(sampled) FROM {E.EVIDENCE_TABLE} GROUP BY fact_id"
    ).fetchall())
    # f2 re-decided inside f1's lead band (24-36h); f3 is the first at 6-12h.
    assert sampled == {"f1": 1, "f2": 0, "f3": 1}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {E.EVIDENCE_TABLE}")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE {E.EVIDENCE_TABLE} SET sampled=0")


def test_the_truth_lane_labels_the_day_as_the_settler_would():
    from paper_trading_ledger import _settled_bucket as settler

    conn = _db()
    conn.executemany(
        "INSERT INTO calibration_station_daily_high_truth VALUES (?,?,?,?,?,?,?)",
        [
            ("KMIA", DAY, 0, 30.4, "sha0", "2026-09-11T04:00:00Z", "ACCEPTED"),
            ("KMIA", DAY, 1, 31.2, "sha1", "2026-09-12T04:00:00Z", "ACCEPTED"),
            ("KMIA", DAY, 2, 35.0, "sha2", "2026-09-13T04:00:00Z", "REJECTED"),
        ],
    )
    counts = E.refresh_settlement_labels(conn)
    assert counts["labelled"] == 1
    label = knowable_labels(conn)[("KMIA", DAY)]
    ladder = conn.execute(
        "SELECT bucket, ordinal, lower_c, upper_c, probability"
        " FROM basket_probability_bridge_bucket_probability ORDER BY ordinal"
    ).fetchall()
    # The standing ACCEPTED revision, placed by the settler's own rule, and
    # knowable when that revision was committed.
    assert label["bucket"] == settler(list(ladder), 31.2)[0] == "31"
    assert label["known_at"] == "2026-09-12T04:00:00Z"
    assert E.refresh_settlement_labels(conn)["labelled"] == 0


def _scored_book() -> sqlite3.Connection:
    conn = _db()
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES"
        " ('KMIA',?,0,31.2,'sha1','2026-09-11T04:00:00Z','ACCEPTED')",
        (DAY,),
    )
    E.refresh_settlement_labels(conn)
    E.record_fact_windows(conn, _fact(conn, "f1", "e1", "2026-09-09T01:00:00Z", [
        _window(["30", "31"], price=0.5, claim=0.6, hours=30.0, seated=True),
        _window(["29"], price=0.2, claim=0.1, hours=30.0, admissible=False),
    ]))
    E.record_fact_windows(conn, _fact(conn, "f2", "e2", "2026-09-09T02:00:00Z", [
        _window(["29", "30"], price=0.3, claim=0.3, hours=29.0),
    ]))
    conn.commit()
    return conn


def test_the_view_scores_sampled_rows_against_the_standing_label():
    conn = _scored_book()
    rows = conn.execute(
        f"SELECT buckets_json, hit, settled_bucket FROM {E.SCORED_VIEW}"
        " ORDER BY buckets_json"
    ).fetchall()
    assert rows == [('["29"]', 0, "31"), ('["30", "31"]', 1, "31")]


def test_both_fits_read_the_production_view():
    conn = _scored_book()
    assert effective_edge_store.CANDIDATE_SOURCE == E.SCORED_VIEW
    population = effective_edge_store._candidate_population(
        conn, ("P0",), "2026-09-01", include_replayed=True,
    )
    assert population["served_row_count"] == 2
    assert population["replayed_row_count"] == 0
    rows, meta = beta_edge_store.population(
        conn, stage_scope="P0", anchored=False, as_of=DAY,
        cutoff_utc="2026-09-20T00:00:00Z",
    )
    assert "error" not in meta, meta
    assert sorted(r["hit"] for r in rows) == [0.0, 1.0]


def test_a_failed_evidence_write_does_not_stop_the_decision():
    conn = _db()
    conn.execute("CREATE TABLE marker (x INTEGER)")
    conn.commit()
    # The decision's own write, still open when the evidence runs.
    conn.execute("INSERT INTO marker VALUES (1)")
    assert conn.in_transaction
    broken = {"fact_id": "f1", "feature_json": json.dumps(
        {"plan": {"delta_v2_audit": {"candidate_windows": [{"buckets": ["30"]}]}}}
    )}
    atomic_ledger._record_candidate_window_evidence(conn, broken)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 1


def test_the_compiled_plan_carries_the_whole_enumeration():
    from tests.test_bayes_basket_target_delta_v2_runtime import (
        _basket_policy,
        _delta_policy,
        _fill_resolver,
        _snapshot,
    )
    from bayes_basket_target.delta_v2_runtime import (
        compile_delta_v2_runtime_plan,
    )

    plan = compile_delta_v2_runtime_plan(
        snapshot=_snapshot(),
        basket_policy=_basket_policy(),
        delta_policy=_delta_policy(),
        fill_resolver=_fill_resolver,
        capital_available_usdc=5.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
    )
    windows = plan["delta_v2_audit"]["candidate_windows"]
    assert windows, "the audit must carry the enumeration"
    seated = (plan["basket_support_selection"].get("seated") or {}).get("buckets")
    assert sum(1 for w in windows if w["seated"]) == (1 if seated else 0)
    for key in ("buckets", "win_probability", "all_in_price", "admissible",
                "market_mid", "hours_to_cutoff"):
        assert key in windows[0]
