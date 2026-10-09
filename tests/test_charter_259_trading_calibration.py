"""Charter 259: two calibration layers.  Layer A (charters 211/240) judges the
whole served distribution; layer B -- this reading -- judges only the bucket
a lane actually bought: binary log loss and reliability of our served p
beside the market's mid on the same legs (user 2026-10-09 把「校准」明确拆成两层).

The reading is computed by the backend board-readings job and the board only
reads its file (user rulings 2026-10-02 「所有前端都改为只读」).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import board_readings as BR  # noqa: E402
from tests.test_tiebreak_lane import _insert, store  # noqa: E402,F401 -- ``store`` is a fixture

ST, ED, TZ = "ZSPD", "2026-09-20", "Asia/Shanghai"
AT = "2026-09-18T12:00:00Z"


def _served(conn, probs, *, stage="P0", decided="2026-09-18T11:30:00Z", hid="hist1"):
    _insert(conn, "basket_probability_bridge_history", {
        "history_id": hid, "station_id": ST, "event_date": ED, "settlement_timezone": TZ, "stage": stage,
        "checkpoint_id": f"cp_{hid}", "decision_at_utc": decided, "fresh_until_utc": "2026-09-19T00:00:00Z",
        "bucket_intervals_json": "{}", "envelope_json": "{}", "state": "COMMITTED", "created_at_utc": decided,
        "content_sha256": (hid + "c").ljust(64, "0"), "envelope_content_sha256": (hid + "e").ljust(64, "0"),
        "bridge_sha256": (hid + "b").ljust(64, "0"), "bucket_map_sha256": (hid + "m").ljust(64, "0"),
        "projection_sha256": (hid + "p").ljust(64, "0"),
    })
    for i, (bucket, p) in enumerate(probs.items()):
        _insert(conn, "basket_probability_bridge_bucket_probability", {
            "history_id": hid, "bucket": bucket, "ordinal": i, "lower_c": 27.0 + i - 0.5, "upper_c": 27.0 + i + 0.5,
            "probability": p, "content_sha256": (hid + bucket).encode().hex().ljust(64, "0")[:64],
        })


def _book(conn, quotes, *, observed="2026-09-18T11:50:00Z", snapshot_id="snap1"):
    buckets = [{"bucket": b, "ordinal": i, "bid_price": bid, "best_ask": ask, "ask_levels": [{"price": ask, "shares": 50}],
                "bid_levels": [{"price": bid, "shares": 50}]} for i, (b, (bid, ask)) in enumerate(quotes.items())]
    _insert(conn, "basket_official_ws_snapshot", {
        "snapshot_id": snapshot_id, "market_id": "m", "station_id": ST, "event_date": ED, "settlement_timezone": TZ,
        "sequence": 1, "observed_at": observed, "quote_version": "v", "quote_observed_at": observed,
        "quote_expires_at": "2026-09-18T13:00:00Z", "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(buckets), "state": "COMMITTED", "created_at": observed,
    })


def _leg(conn, intent_id, *, decision_id, bucket, side, price, size, filled, notional, created=AT):
    cycle = f"cyc_{intent_id}"
    _insert(conn, "basket_target_execution_cycles", {
        "cycle_id": cycle, "plan_id": f"plan_{intent_id}", "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "strategy_lane": "basket", "requested_reserve_usdc": 0.0, "state": "settled",
        "created_at": created, "updated_at": created,
    })
    _insert(conn, "basket_target_order_intents", {
        "order_intent_id": intent_id, "decision_id": decision_id, "cycle_id": cycle, "bucket": bucket,
        "token_id": f"{side.lower()}_{bucket}", "side": side, "order_action": "BUY", "limit_price": price,
        "requested_size": size, "requested_notional_usdc": round(price * size, 6),
        "state": "filled" if filled else "cancelled", "live_submit_authorized": 1, "idempotency_key": intent_id,
        "intent_json": "{}", "created_at": created,
    })
    _insert(conn, "basket_target_order_reconciliations", {
        "order_intent_id": intent_id, "state": "resolved_filled" if filled else "resolved_cancelled",
        "filled_size": filled, "filled_notional_usdc": notional, "updated_at": created,
    })


def _label(conn, bucket, *, station=ST, day=ED):
    # Production's label table is written by the settlement lane, not by the
    # target schema installer; its real columns, so ``knowable_labels`` reads it.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_station_day_settlement_label (station_id TEXT NOT NULL,"
        " event_date TEXT NOT NULL, truth_revision_no INTEGER NOT NULL, settled_bucket TEXT NOT NULL,"
        " settled_tmax_c REAL NOT NULL, truth_content_sha256 TEXT NOT NULL, known_at_utc TEXT NOT NULL,"
        " ladder_decision_at_utc TEXT NOT NULL, created_at_utc TEXT NOT NULL,"
        " PRIMARY KEY(station_id, event_date, truth_revision_no))"
    )
    _insert(conn, "basket_station_day_settlement_label", {
        "station_id": station, "event_date": day, "truth_revision_no": 1, "settled_bucket": bucket,
        "settled_tmax_c": 28.3, "known_at_utc": "2026-09-21T06:00:00Z", "ladder_decision_at_utc": AT,
        "created_at_utc": "2026-09-21T06:00:00Z",
    })


def _ll(p, hit):
    return -(hit * math.log(p) + (1 - hit) * math.log(1 - p))


def test_each_bought_leg_carries_our_p_the_markets_q_the_paid_price_and_the_outcome(store) -> None:  # noqa: F811
    """Three legs on one settled station-day: a filled YES on the bucket that
    landed, a filled NO on a bucket that did not (its p, q and win flip to
    the NO side), and a YES intent that never filled (in the intended
    population only).  Our p is the served vector at the intent's instant,
    floored as serving floors it; q is the two-sided mid normalised over the
    ladder."""

    _served(store, {"27°C": 0.2, "28°C": 0.5, "29°C": 0.3})
    _book(store, {"27°C": (0.18, 0.20), "28°C": (0.38, 0.42), "29°C": (0.28, 0.32)})
    _label(store, "28°C")
    _leg(store, "i_yes", decision_id="proper_bet_decision_ZSPD_2026-09-20", bucket="28°C", side="YES", price=0.40,
         size=10, filled=10, notional=4.0)
    _leg(store, "i_no", decision_id="tail_short_decision_ZSPD_2026-09-20", bucket="29°C", side="NO", price=0.70,
         size=5, filled=5, notional=3.5)
    _leg(store, "i_unfilled", decision_id="basket_plan_x", bucket="27°C", side="YES", price=0.19, size=10,
         filled=0, notional=0.0)
    legs, counts = BR.traded_calibration_legs(store)
    assert counts == {"intents": 3, "no_label": 0, "no_served_p": 0, "bucket_off_vector": 0, "no_market_mid": 0, "legs": 3}
    by_id = {l["order_intent_id"]: l for l in legs}
    yes = by_id["i_yes"]
    assert (yes["lane"], yes["side"], yes["stage"], yes["win"], yes["filled"]) == ("proper_bet", "YES", "P0", 1.0, True)
    assert yes["p"] == pytest.approx(0.5) and yes["q"] == pytest.approx(0.40 / 0.89) and yes["paid"] == pytest.approx(0.4)
    no = by_id["i_no"]
    assert (no["lane"], no["side"], no["win"]) == ("tail_short", "NO", 1.0), "29°C did not land, so the NO leg won"
    assert no["p"] == pytest.approx(0.7) and no["q"] == pytest.approx(1 - 0.30 / 0.89) and no["paid"] == pytest.approx(0.7)
    unfilled = by_id["i_unfilled"]
    assert (unfilled["lane"], unfilled["filled"], unfilled["paid"], unfilled["win"]) == ("basket", False, 0.19, 0.0)
    assert unfilled["hours"] is not None and unfilled["hours"] > 36


def test_the_group_scores_ours_market_and_paid_on_the_same_legs(monkeypatch) -> None:
    monkeypatch.setattr(BR, "TRADED_CALIBRATION_MIN_LEGS", 1)
    legs = [
        {"p": 0.6, "q": 0.5, "paid": 0.5, "win": 1.0, "event_date": "2026-09-20", "filled": True, "shares": 10.0},
        {"p": 0.3, "q": 0.4, "paid": 0.4, "win": 0.0, "event_date": "2026-09-21", "filled": True, "shares": 10.0},
        {"p": 0.8, "q": 0.6, "paid": 0.6, "win": 0.0, "event_date": "2026-09-22", "filled": False, "shares": 0.0},
    ]
    row = BR.traded_calibration_group(legs)
    assert (row["legs"], row["event_days"], row["filled_legs"]) == (3, 3, 2)
    assert row["logloss_ours"] == pytest.approx((_ll(0.6, 1) + _ll(0.3, 0) + _ll(0.8, 0)) / 3)
    assert row["logloss_market"] == pytest.approx((_ll(0.5, 1) + _ll(0.4, 0) + _ll(0.6, 0)) / 3)
    assert row["logloss_paid"] == pytest.approx(row["logloss_market"])
    assert row["logloss_diff"] == pytest.approx(row["logloss_ours"] - row["logloss_market"])
    assert isinstance(row["logloss_diff_ci"], list) and row["logloss_diff_ci"][0] <= row["logloss_diff_ci"][1]
    assert row["ours_closer_share"] == pytest.approx(2 / 3)
    assert row["usdc"] == pytest.approx(9.0) and row["return"] == pytest.approx(10.0 / 9.0 - 1.0)
    assert BR.traded_calibration_group(legs[:1]) is not None
    monkeypatch.setattr(BR, "TRADED_CALIBRATION_MIN_LEGS", 15)
    assert BR.traded_calibration_group(legs) is None, "a slice under the floor is not reported"


def test_reliability_bins_say_both_sides_on_the_same_legs() -> None:
    legs = [{"p": 0.05, "q": 0.5, "paid": 0.5, "win": 0.0}, {"p": 0.08, "q": 0.6, "paid": 0.6, "win": 1.0},
            {"p": 0.95, "q": 0.9, "paid": 0.9, "win": 1.0}]
    ours = BR.traded_calibration_reliability(legs, "p")
    assert [(b["bin"], b["legs"]) for b in ours] == [([0.0, 0.1], 2), ([0.9, 1.0], 1)]
    assert ours[0]["said_ours"] == pytest.approx(0.065) and ours[0]["said_market"] == pytest.approx(0.55)
    assert ours[0]["landed"] == pytest.approx(0.5)
    market = BR.traded_calibration_reliability(legs, "q")
    assert [(b["bin"], b["legs"]) for b in market] == [([0.5, 0.6], 1), ([0.6, 0.7], 1), ([0.9, 1.0], 1)]


def test_reliability_is_split_by_lane_on_each_lanes_own_legs() -> None:
    """User 2026-10-09 「可靠性图（已成交腿），按照车道区分」: each lane gets the
    same two diagrams on its own legs; no leg floor, a bin carries its count."""

    legs = [{"lane": "tiebreak", "p": 0.55, "q": 0.5, "paid": 0.5, "win": 1.0, "event_date": "2026-09-20"},
            {"lane": "tiebreak", "p": 0.52, "q": 0.45, "paid": 0.46, "win": 0.0, "event_date": "2026-09-21"},
            {"lane": "tail_short", "p": 0.97, "q": 0.93, "paid": 0.93, "win": 1.0, "event_date": "2026-09-20"}]
    by_lane = {r["lane"]: r for r in BR.traded_calibration_reliability_by_lane(legs)}
    assert set(by_lane) == {"tiebreak", "tail_short"}
    tb = by_lane["tiebreak"]
    assert (tb["legs"], tb["event_days"]) == (2, 2) and tb["landed"] == pytest.approx(0.5)
    assert tb["reliability_ours"] == BR.traded_calibration_reliability(legs[:2], "p")
    assert [(b["bin"], b["legs"]) for b in tb["reliability_market"]] == [([0.4, 0.5], 1), ([0.5, 0.6], 1)]
    ts = by_lane["tail_short"]
    assert [(b["bin"], b["legs"]) for b in ts["reliability_ours"]] == [([0.9, 1.0], 1)]


def test_every_lane_has_its_slice_row_while_other_groups_keep_the_floor() -> None:
    """User 2026-10-09 「为什么没有proper车道的数据，增加」: a lane under the
    leg floor still gets its row; the floor still holds for the other groups."""

    legs = [{"lane": "proper_bet", "side": "YES", "stage": "P0", "unit": "C", "hours": 40.0,
             "p": 0.45, "q": 0.40, "paid": 0.42, "win": float(i % 3 == 0), "event_date": f"2026-09-2{i % 3}",
             "filled": True, "shares": 2.0} for i in range(14)]
    rows = BR._traded_slices(legs)
    lane_rows = [r for r in rows if r["group"] == "车道"]
    assert [(r["slice"], r["legs"]) for r in lane_rows] == [("proper_bet", 14)]
    assert lane_rows[0]["logloss_diff_ci"] is not None, "three event days give the day-bootstrap interval"
    assert not [r for r in rows if r["group"] != "车道"], "14 legs stay under the floor for the other groups"


def test_the_reading_has_two_populations_and_a_stale_or_one_sided_book_drops_the_leg(store, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(BR, "TRADED_CALIBRATION_MIN_LEGS", 1)
    _served(store, {"27°C": 0.2, "28°C": 0.5, "29°C": 0.3})
    # A one-sided ladder (no bids) is not a market mid; a book older than the
    # window is not the book the leg was sent on.
    _book(store, {"27°C": (0.18, 0.20), "28°C": (0.38, 0.42), "29°C": (0.28, 0.32)}, observed="2026-09-18T10:00:00Z")
    _label(store, "28°C")
    _leg(store, "i1", decision_id="proper_bet_decision_ZSPD_2026-09-20", bucket="28°C", side="YES", price=0.40,
         size=10, filled=10, notional=4.0)
    legs, counts = BR.traded_calibration_legs(store)
    assert legs == [] and counts["no_market_mid"] == 1
    _book(store, {"27°C": (0.18, 0.20), "28°C": (0.38, 0.42), "29°C": (0.28, 0.32)}, snapshot_id="snap2")
    _leg(store, "i2", decision_id="basket_x", bucket="27°C", side="YES", price=0.19, size=10, filled=0, notional=0.0)
    value = BR.traded_calibration(store)
    assert value["rule"] == "traded_bucket_binary_logloss_v1"
    assert value["populations"]["filled"]["legs"] == 1 and value["populations"]["intended"]["legs"] == 2
    assert value["populations"]["intended"]["all"]["legs"] == 2
    assert [(r["lane"], r["legs"]) for r in value["populations"]["intended"]["reliability_by_lane"]] == [
        ("basket", 1), ("proper_bet", 1)]
    assert [r["lane"] for r in value["populations"]["filled"]["reliability_by_lane"]] == ["proper_bet"]
    assert {s["group"] for s in value["populations"]["intended"]["slices"]} >= {"车道", "方向", "阶段", "结算单位", "提前量"}
    assert len(value["recent"]) == 2 and value["recent"][0]["bucket"] in {"27°C", "28°C"}
    # A station-day without a standing label is not scored.
    _leg(store, "i3", decision_id="basket_y", bucket="29°C", side="YES", price=0.30, size=10, filled=10, notional=3.0,
         created="2026-09-18T12:01:00Z")
    store.execute("UPDATE basket_target_execution_cycles SET event_date='2026-09-21' WHERE cycle_id='cyc_i3'")
    _, counts = BR.traded_calibration_legs(store)
    assert counts["no_label"] == 1


def test_the_board_reads_the_file_and_scores_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path)
    assert C.collect_traded_calibration()["available"] is False
    BR.write_reading("traded_calibration", {"rule": "traded_bucket_binary_logloss_v1", "populations": {"filled": {"legs": 1}}},
                     code="c", build_sec=1.0)
    out = C.collect_traded_calibration()
    assert out["available"] is True and out["populations"]["filled"]["legs"] == 1 and out["computed_at_utc"].endswith("Z")


def test_the_wiring_and_the_charter() -> None:
    assert "traded_calibration" in BR.READING_NAMES and "traded_calibration" in BR._READINGS
    assert "traded_calibration" in BR.READING_MAX_AGE_SEC
    board = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "function tradedCalibration()" in board and '"traded_calibration": collect_traded_calibration()' in board
    assert '[tradedCalibration,"交易概率校准（第二层）"]' in board
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter[charter.index("\n259. ") + 1:]
    for phrase in ("两层", "Binary LogLoss", "LogScore", "可靠性", "不作训练目标", "traded_calibration"):
        assert phrase in clause, phrase


def test_the_leg_reads_the_most_informed_stage_not_the_newest(store) -> None:  # noqa: F811
    """2026-10-09: a same-day P0-M committed after the P1 is the staler belief
    (its upside mass is dead once the peak has passed); the reading takes P1
    when one stands, then P0-M, then P0."""

    _served(store, {"27°C": 0.2, "28°C": 0.5, "29°C": 0.3}, stage="P1", decided="2026-09-18T11:00:00Z", hid="h_p1")
    _served(store, {"27°C": 0.1, "28°C": 0.3, "29°C": 0.6}, stage="P0-M", decided="2026-09-18T11:40:00Z", hid="h_pm")
    stage, vector = BR._traded_served_vector(store, ST, ED, AT, {})
    assert stage == "P1" and vector["28°C"] == pytest.approx(0.5)
    stage, vector = BR._traded_served_vector(store, ST, ED, "2026-09-18T10:00:00Z", {})
    assert stage is None and vector is None, "no decision stood yet"


def test_every_bucket_of_the_ladder_is_one_row_with_our_p_the_markets_q_and_the_outcome(store) -> None:  # noqa: F811
    """User 2026-10-09 「校准每个桶的发生概率，用指标binary logloss」: one sample
    per settled station-day per hours-to-settlement band, at the first decision
    instant with a complete market reference; every bucket of the ladder is a
    row, its distance from the market's mode carried for the position slices."""

    _served(store, {"27°C": 0.2, "28°C": 0.5, "29°C": 0.3})  # decided 09-18 11:30Z, ~52h out
    _book(store, {"27°C": (0.18, 0.20), "28°C": (0.38, 0.42), "29°C": (0.28, 0.32)},
               observed="2026-09-18T11:20:00Z", snapshot_id="snapA")
    _label(store, "28°C")
    rows, counts = BR.bucket_calibration_rows(store)
    assert counts == {"station_days": 1, "samples": 1, "band_unpaired": 0, "ladder_mismatch": 0, "buckets": 3}
    by = {r["bucket"]: r for r in rows}
    assert {r["band"] for r in rows} == {"距结算 36–60h"} and {r["stage"] for r in rows} == {"P0"}
    assert by["28°C"]["win"] == 1.0 and by["27°C"]["win"] == 0.0
    assert by["28°C"]["p"] == pytest.approx(0.5) and by["28°C"]["q"] == pytest.approx(0.40 / 0.89)
    assert [by[b]["from_mode"] for b in ("27°C", "28°C", "29°C")] == [1, 0, 1]
    row = BR.bucket_calibration_group(rows)
    assert row["buckets"] == 3 and row["samples"] == 1
    assert row["logloss_ours"] == pytest.approx((_ll(0.2, 0) + _ll(0.5, 1) + _ll(0.3, 0)) / 3)
    assert row["logloss_diff"] == pytest.approx(row["logloss_ours"] - row["logloss_market"])


def test_a_band_without_a_fresh_complete_book_is_unpaired(store) -> None:  # noqa: F811
    _served(store, {"27°C": 0.2, "28°C": 0.5, "29°C": 0.3})
    # Older than the window before the instant: not the book the instant saw.
    _book(store, {"27°C": (0.18, 0.20), "28°C": (0.38, 0.42), "29°C": (0.28, 0.32)},
               observed="2026-09-18T10:30:00Z", snapshot_id="snapOld")
    _label(store, "28°C")
    rows, counts = BR.bucket_calibration_rows(store)
    assert rows == [] and counts["band_unpaired"] == 1


def test_the_bucket_reading_slices_and_bins() -> None:
    rows = [{"station_id": "S", "event_date": f"2026-09-2{d}", "band": "距结算 12–24h", "stage": "P1", "unit": "C",
             "from_mode": k, "p": p, "q": q, "win": w}
            for d in range(4) for k, p, q, w in ((0, 0.6, 0.5, 1.0), (1, 0.3, 0.3, 0.0), (3, 0.004, 0.02, 0.0))]
    value_bins = BR.bucket_calibration_reliability(rows, "p")
    assert [(b["bin"], b["buckets"]) for b in value_bins] == [([0.0, 0.01], 4), ([0.3, 0.4], 4), ([0.6, 0.7], 4)]
    group = BR.bucket_calibration_group(rows)
    assert group["event_days"] == 4 and isinstance(group["logloss_diff_ci"], list)
    assert BR.bucket_calibration_group([]) is None


def test_the_bucket_reading_is_wired_and_the_board_reads_its_file(tmp_path, monkeypatch) -> None:
    assert "bucket_calibration" in BR.READING_NAMES and "bucket_calibration" in BR._READINGS
    assert "bucket_calibration" in BR.READING_MAX_AGE_SEC
    monkeypatch.setattr(BR, "READINGS_DIR", tmp_path)
    assert C.collect_bucket_calibration()["available"] is False
    BR.write_reading("bucket_calibration", {"rule": "every_bucket_binary_logloss_v1", "all": {"buckets": 3}},
                     code="c", build_sec=1.0)
    assert C.collect_bucket_calibration()["all"]["buckets"] == 3
    board = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "function bucketCalibration()" in board and '"bucket_calibration": collect_bucket_calibration()' in board
    area = board[board.index('{id:"realisation"'):]
    area = area[:area.index("]},")]
    assert area.index("[bucketCalibration,") < area.index("[tradedCalibration,"), "the two samples sit before the drill-down"
    for phrase in ("校准每个桶的发生概率", "评价实际买入的桶"):
        assert phrase in board[board.index("function bucketCalibration()"):board.index("function tradedCalibration()")]


def test_the_board_renders_both_samples_side_by_side() -> None:
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    source = C.HTML_TEMPLATE
    start = source.index("function bucketCalibration(")
    depth, end = 0, None
    for index in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        if depth == 0:
            end = index + 1
            break
    group = {"buckets": 30, "samples": 10, "event_days": 4, "logloss_ours": 0.21, "logloss_market": 0.18,
             "logloss_diff": 0.03, "logloss_diff_ci": [0.01, 0.05], "brier_ours": 0.06, "brier_market": 0.05,
             "said_ours": 0.1, "said_market": 0.1, "landed": 0.1}
    payload = {
        "bucket_calibration": {"available": True, "since": "2026-09-06", "all": group,
                               "slices": [{"group": "桶位置", "slice": "市场众数桶", **group}],
                               "reliability_ours": [{"bin": [0.0, 0.01], "buckets": 3, "said_ours": 0.004,
                                                     "said_market": 0.01, "landed": 0.0}],
                               "reliability_market": [], "counts": {"station_days": 4}},
        "traded_calibration": {"populations": {"filled": {"all": {**group, "legs": 20, "logloss_paid": 0.19}}}},
    }
    script = ("const D = " + json.dumps(payload, ensure_ascii=False) + ";\nconst h = s => String(s == null ? '' : s);\n"
              + source[start:end] + "\nconsole.log(bucketCalibration());\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    html = result.stdout
    assert "<b>校准每个桶的发生概率</b>" in html and "30 桶 · 10 时点 · 4 日" in html
    assert "<b>评价实际买入的桶</b>" in html and "20 腿 · 4 日" in html and "0.1900" in html
    assert "+0.0300 [+0.0100, +0.0500]*" in html and "市场众数桶" in html
