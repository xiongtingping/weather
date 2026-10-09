"""Charter 258: the Proper 下注线 is never the benchmark cluster's counterparty.

User rulings 2026-10-09 「proper线增加一条规则：不做对标账号的对手盘」 and, told
that Proper YES rungs had been filled by the cluster's NO buys at the exact
complement of our price and size, 「先卖出，反向买no，和跟单线不冲突！可以重叠」.

Then 「买no线交给跟单线」 and 「proper线以对标账号为筛子，不做对标账号的对手盘！」:
the cluster is the lane's filter.  A bucket on which any of the eight wallets
has bought NO or sold YES (maker or taker, at an implied YES price inside
[0.02, 0.98]) is sealed: no plan, no further rung, a resting rung cancelled by
the resting-ladder pass.  ``sell`` also sells a YES the lane holds there; no
mode buys NO.  A failed read seals nothing and opens nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import benchmark_cluster as cluster  # noqa: E402
from bayes_basket_target import proper_bet_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from bayes_basket_target.execution_reconciliation import (  # noqa: E402
    record_order_event,
    resolve_cancelled_after_partial_fill,
)
from bayes_basket_target.resting_ladder_lifecycle import (  # noqa: E402
    BENCHMARK_SEAL_CANCEL_RULE_VERSION,
    resting_rung_keep_or_cancel,
)
import tests.test_charter_231_proper_bet_lane as base  # noqa: E402
from tests.test_charter_231_proper_bet_lane import (  # noqa: E402,F401 -- ``store`` is a fixture
    ADMIT,
    ED,
    NOW,
    _intents,
    _ladder,
    _seed,
    _snapshot,
    store,
)
from tests.test_charter_249_proper_market_mode_yes import LONG, V3, WARM  # noqa: E402
from tests.test_tiebreak_lane import ST  # noqa: E402

LEAD = cluster.BENCHMARK_CLUSTER_WALLETS[0]
#: The modal bucket of the fixture ladder (27°C, YES t27 / NO n27).
COND = "cond_27°C"


@pytest.fixture()
def warm(monkeypatch):
    monkeypatch.setattr(base, "MODEL", dict(WARM))


def _fill(asset: str, side: str, price: float, *, cond: str = COND, size: float = 16.0,
          ts: int = 1_790_000_000, wallet: str = LEAD) -> dict:
    return {"proxyWallet": wallet, "conditionId": cond, "asset": asset, "side": side, "price": price,
            "size": size, "timestamp": ts, "transactionHash": f"0x{ts}{asset}{side}"}


def _tape(*fills: dict, fail: str | None = None):
    """A data-API stand-in: each wallet sees its own fills on the markets asked."""

    calls: list = []

    def fetch(path, params):
        calls.append((path, dict(params)))
        if fail is not None and params["user"] == fail:
            raise RuntimeError("data_api_down")
        markets = set(str(params["market"]).split(","))
        return [f for f in fills if f["proxyWallet"] == params["user"] and f["conditionId"] in markets]

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _transport(sent: list):
    """Rungs rest; a FOK fills whole; a GTD sell takes the bid whole."""

    def transport(path, payload):
        sent.append((path, payload))
        if payload["order_type"] == "FOK" or payload["side"] == "SELL":
            making = payload["size"] if payload["side"] == "SELL" else round(payload["size"] * payload["price"], 2)
            taking = round(payload["size"] * payload["price"], 2) if payload["side"] == "SELL" else payload["size"]
            return {"ok": True, "orderID": f"0xfill{len(sent)}", "status": "matched",
                    "size_matched": payload["size"],
                    "exchange_response": {"makingAmount": making, "takingAmount": taking}}
        return {"ok": True, "accepted": True, "exchange_order_id": f"0xrest{len(sent)}", "status": "live"}

    return transport


def _wallet(shares: float):
    return lambda token_id: {"ok": True, "shares": shares}


def _run(conn, sent, *, fetch, now=NOW, wallet=None, **overrides):
    return lane.execute_proper_bets(
        conn, transport=_transport(sent), now_utc=now, live_submit_authorized=True,
        settings={**V3, **overrides}, admission=ADMIT, benchmark_fetch=fetch,
        position_reader=wallet or _wallet(0.0),
    )


def _fill_rung(conn, intent_id: str, shares: float, price: float) -> None:
    order_id = conn.execute(
        "SELECT exchange_order_id FROM basket_target_order_reconciliations WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()[0]
    record_order_event(conn, order_intent_id=intent_id, event={
        "event_type": "FILL" if shares >= 5.0 else "PARTIAL_FILL", "exchange_order_id": order_id,
        "observed_at": "2026-09-18T12:03:00.000Z",
        "matched_size": shares, "matched_notional_usdc": round(shares * price, 6),
        "event_payload": {"reason": "test"},
    })


def _seal(conn) -> dict | None:
    return lane._seal_row(conn, ST, ED, "27°C", "YES")


# ---------------------------------------------------------------------------
# What counts as the other side.


@pytest.mark.parametrize("asset,side,price,against", [
    ("n27", "BUY", 0.59, True),      # their NO buy is our YES bid's counterparty
    ("t27", "SELL", 0.41, True),     # a YES sale into the bid
    ("t27", "BUY", 0.41, False),     # on our side
    ("n27", "SELL", 0.59, False),    # selling NO is buying the bucket
    ("t27", "SELL", 0.998, False),   # the cluster's recycle of a settled winner
    ("n27", "BUY", 0.999, False),    # a sub-cent YES: not a view
    ("x99", "BUY", 0.50, False),     # another market's token
])
def test_an_opposite_fill_is_a_no_buy_or_a_yes_sale_inside_the_price_band(asset, side, price, against) -> None:
    fill = _fill(asset, side, price)
    assert cluster.is_opposite(fill, side="YES", yes_token="t27", no_token="n27") is against
    if asset in {"t27", "n27"} and 0.02 <= (price if asset == "t27" else 1 - price) <= 0.98:
        assert cluster.is_opposite(fill, side="NO", yes_token="t27", no_token="n27") is (not against)


def test_the_evidence_is_the_net_side_and_the_latest_opposite_fill() -> None:
    fills = [_fill("n27", "BUY", 0.60, ts=20, size=10), _fill("n27", "BUY", 0.59, ts=10, size=8),
             _fill("t27", "BUY", 0.4, ts=5, size=12)]
    evidence = cluster.opposite_evidence(fills, side="YES", yes_token="t27", no_token="n27")
    assert evidence["criterion"] == cluster.SEAL_CRITERION
    assert evidence["fill_ts"] == 20 and evidence["implied_price"] == pytest.approx(0.40)
    assert (evidence["opposite_shares"], evidence["same_side_shares"], evidence["opposite_fills"]) == (18.0, 12.0, 2)


def test_one_opposite_fill_inside_a_yes_accumulation_does_not_seal() -> None:
    """「改封桶口径」: EGLC 10-09 20°C -- one 31-share maker NO fill of the lead
    wallet, inside its own 183.7-share YES take of the same second, against
    1,044 shares the cluster stood YES on that bucket."""

    fills = [_fill("n27", "BUY", 0.44, ts=10, size=31.0)] + [
        _fill("t27", "BUY", 0.42, ts=10 + i, size=104.4) for i in range(10)]
    assert cluster.opposite_evidence(fills, side="YES", yes_token="t27", no_token="n27") is None
    assert cluster.side_tallies(fills, side="YES", yes_token="t27", no_token="n27") == {
        "opposite_shares": 31.0, "same_side_shares": 1044.0}
    # Selling the NO it held is the YES side too; a tie does not seal.
    tie = [_fill("n27", "BUY", 0.6, size=10), _fill("n27", "SELL", 0.6, size=10)]
    assert cluster.opposite_evidence(tie, side="YES", yes_token="t27", no_token="n27") is None


def test_one_wallet_failing_leaves_every_market_unreadable() -> None:
    read = cluster.read_cluster([COND], fetch=_tape(fail=cluster.BENCHMARK_CLUSTER_WALLETS[3]))
    assert read["unreadable"] == {COND} and read["errors"]


def test_a_read_the_offset_cap_cuts_short_is_unreadable() -> None:
    full = [_fill("t27", "BUY", 0.4, ts=i) for i in range(cluster.PAGE_SIZE)]
    read = cluster.read_cluster([COND], fetch=lambda path, params: full,
                                wallets=cluster.BENCHMARK_CLUSTER_WALLETS[:1])
    assert read["unreadable"] == {COND}
    assert any("benchmark_read_truncated" in e for e in read["errors"])


def test_the_cluster_is_one_list() -> None:
    assert strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS["source_users"] == list(
        strategy_lanes.BENCHMARK_CLUSTER_WALLETS)
    assert len(set(strategy_lanes.BENCHMARK_CLUSTER_WALLETS)) == 8
    assert strategy_lanes.PROPER_BET_DEFAULT_SETTINGS["benchmark_counterparty"] == "sell"
    with pytest.raises(ValueError, match="benchmark_counterparty_unknown"):
        lane.benchmark_mode({"benchmark_counterparty": "sometimes"})


# ---------------------------------------------------------------------------
# The seal.


def test_no_plan_on_a_bucket_the_cluster_has_bought_no_on(store, warm) -> None:
    _seed(store, fresh_until=LONG)
    sent: list = []
    receipt = _run(store, sent, fetch=_tape(_fill("n27", "BUY", 0.51)))
    assert receipt["planned"] == 0 and not sent
    assert receipt["waiting_reasons"] == {lane.BENCHMARK_OPPOSITE: 1}
    seal = _seal(store)
    assert seal["sealed_by"] == "plan" and seal["wallet"] == LEAD
    assert (seal["fill_token_side"], seal["fill_action"]) == ("NO", "BUY")
    assert seal["implied_price"] == pytest.approx(0.49)
    # Sealed for the station-day: the next pass reads nothing and still waits.
    tape = _tape()
    _snapshot(store, "snap_f2", "2026-09-18T12:03:00.000Z", _ladder(), 2)
    again = _run(store, sent, fetch=tape, now="2026-09-18T12:03:30.000Z")
    assert again["waiting_reasons"] == {lane.BENCHMARK_OPPOSITE: 1} and not sent
    assert not any(COND in params["market"] for _p, params in tape.calls)


def test_the_cluster_on_our_side_does_not_seal(store, warm) -> None:
    _seed(store, fresh_until=LONG)
    sent: list = []
    receipt = _run(store, sent, fetch=_tape(_fill("t27", "BUY", 0.49)))
    assert receipt["planned"] == 1 and len(sent) == 1 and _seal(store) is None


def test_an_unreadable_cluster_opens_nothing_and_seals_nothing(store, warm) -> None:
    _seed(store, fresh_until=LONG)
    sent: list = []
    receipt = _run(store, sent, fetch=_tape(fail=cluster.BENCHMARK_CLUSTER_WALLETS[5]))
    assert receipt["planned"] == 0 and not sent
    assert receipt["waiting_reasons"] == {lane.BENCHMARK_UNREADABLE: 1} and _seal(store) is None


def test_off_is_the_lane_before_the_ruling(store, warm) -> None:
    _seed(store, fresh_until=LONG)
    sent: list = []
    receipt = _run(store, sent, fetch=_tape(_fill("n27", "BUY", 0.51)), benchmark_counterparty="off")
    assert receipt["planned"] == 1 and len(sent) == 1 and _seal(store) is None


def test_a_seal_after_the_plan_stops_the_reposts_and_closes_the_plan(store, warm) -> None:
    _seed(store, fresh_until=LONG)
    sent: list = []
    _run(store, sent, fetch=_tape())
    assert len(sent) == 1
    rung = _intents(store)[0][0]
    base._end(store, rung)                                     # the rung expires unfilled
    later = "2026-09-18T12:20:00.000Z"
    _snapshot(store, "snap_f2", later, _ladder(), 2)
    receipt = _run(store, sent, fetch=_tape(_fill("t27", "SELL", 0.48)), now=later)
    assert receipt["rungs"] == 0 and len(sent) == 1
    state, reason = store.execute(
        f"SELECT plan_state, plan_state_reason FROM {lane.EVALUATION_TABLE}").fetchone()
    assert (state, reason) == (lane.PLAN_CLOSED, lane.BENCHMARK_OPPOSITE)
    assert _seal(store)["sealed_by"] == "rung"
    # Nothing was held, so there is nothing to sell.
    assert receipt["benchmark"]["sell_orders"] == []


def test_a_resting_proper_rung_on_a_sealed_bucket_is_cancelled(store, warm) -> None:
    _seed(store, fresh_until=LONG)
    sent: list = []
    _run(store, sent, fetch=_tape())
    rung = _intents(store)[0][0]
    later = "2026-09-18T12:10:00.000Z"
    _snapshot(store, "snap_f2", later, _ladder(), 2)
    _run(store, sent, fetch=_tape(_fill("n27", "BUY", 0.51)), now=later)
    intent = {"order_intent_id": rung, "station_id": ST, "event_date": ED, "bucket": "27°C", "side": "YES",
              "order_action": "BUY", "limit_price": 0.49, "posted_at_utc": NOW}
    seal = lane.benchmark_sealed_rung(store, intent)
    assert seal is not None and seal["wallet"] == LEAD
    verdict = resting_rung_keep_or_cancel(
        intent=intent, dead_buckets=(), running_high=None, load_book=lambda: None, as_of_utc=later,
        stale_book_seconds=120.0, benchmark_seal=seal,
    )
    assert verdict["action"] == "cancel" and verdict["reason"] == lane.BENCHMARK_OPPOSITE
    assert verdict["cancel_rule"] == BENCHMARK_SEAL_CANCEL_RULE_VERSION
    # Another lane's rung on the same bucket is not this ruling's.
    assert lane.benchmark_sealed_rung(store, {**intent, "order_intent_id": "tailshort_x"}) is None
    assert lane.benchmark_sealed_rung(store, {**intent, "order_intent_id": "proper_flipsell_x"}) is None


# ---------------------------------------------------------------------------
# What the lane holds on a sealed bucket: sold under ``sell``, never a NO buy.


def _held(store, sent, shares: float) -> str:
    """A plan whose rung filled ``shares`` YES at 0.49."""

    _seed(store, fresh_until=LONG)
    _run(store, sent, fetch=_tape())
    rung = _intents(store)[0][0]
    _fill_rung(store, rung, shares, 0.49)
    if shares < 5.0:
        # The venue cancels the rest of the rung, as the resting-ladder pass records it.
        resolve_cancelled_after_partial_fill(store, order_intent_id=rung, filled_size=shares,
                                             filled_notional_usdc=round(shares * 0.49, 6),
                                             resolved_at_utc="2026-09-18T12:04:00.000Z")
    return rung


def test_sell_mode_sells_the_held_yes_at_the_bid_and_never_buys_no(store, warm) -> None:
    sent: list = []
    _held(store, sent, 10.0)
    opposite = _tape(_fill("n27", "BUY", 0.52))
    t1 = "2026-09-18T12:10:00.000Z"
    _snapshot(store, "snap_f2", t1, _ladder(), 2)
    first = _run(store, sent, fetch=opposite, now=t1, wallet=_wallet(10.0), benchmark_counterparty="sell")
    (sell,) = first["benchmark"]["sell_orders"]
    assert sell["kind"] == "sell_yes" and sell["price"] == pytest.approx(0.48) and sell["shares"] == 10.0
    _path, payload = sent[-1]
    assert payload["side"] == "SELL" and payload["token_id"] == "t27" and payload["order_type"] == "GTD"
    assert payload["post_only"] is False
    assert _seal(store)["flip_state"] == "selling_yes"
    # Sold whole: done, and nothing on the NO token, ever (「买no线交给跟单线」).
    t2 = "2026-09-18T12:13:00.000Z"
    _snapshot(store, "snap_f3", t2, _ladder(), 3)
    second = _run(store, sent, fetch=opposite, now=t2, wallet=_wallet(0.0), benchmark_counterparty="sell")
    assert second["benchmark"]["sell_orders"] == []
    assert _seal(store)["flip_state"] == "done"
    assert not any(p["token_id"] == "n27" for _path, p in sent)
    assert not any(p.get("outcome_side") == "NO" for _path, p in sent)


def test_yes_under_the_venue_minimum_is_held(store, warm) -> None:
    sent: list = []
    _held(store, sent, 3.0)
    t1 = "2026-09-18T12:10:00.000Z"
    _snapshot(store, "snap_f2", t1, _ladder(), 2)
    receipt = _run(store, sent, fetch=_tape(_fill("n27", "BUY", 0.52)), now=t1, wallet=_wallet(3.0),
                   benchmark_counterparty="sell")
    assert receipt["benchmark"]["sell_orders"] == []
    assert _seal(store)["flip_state"] == "done:below_venue_minimum"
    assert len(sent) == 1


def test_no_sell_while_the_lanes_own_rung_is_out(store, warm) -> None:
    sent: list = []
    _seed(store, fresh_until=LONG)
    _run(store, sent, fetch=_tape())
    rung = _intents(store)[0][0]
    record_order_event(store, order_intent_id=rung, event={
        "event_type": "PARTIAL_FILL",
        "exchange_order_id": store.execute(
            "SELECT exchange_order_id FROM basket_target_order_reconciliations WHERE order_intent_id=?",
            (rung,)).fetchone()[0],
        "observed_at": "2026-09-18T12:03:00.000Z", "matched_size": 2.0, "matched_notional_usdc": 0.98,
        "event_payload": {"reason": "test"},
    })
    t1 = "2026-09-18T12:05:00.000Z"
    _snapshot(store, "snap_f2", t1, _ladder(), 2)
    receipt = _run(store, sent, fetch=_tape(_fill("n27", "BUY", 0.52)), now=t1, wallet=_wallet(2.0),
                   benchmark_counterparty="sell")
    assert receipt["benchmark"]["sell_orders"] == []
    assert _seal(store)["flip_state"] == "waiting:order_open"


def test_seal_mode_is_the_filter_alone(store, warm) -> None:
    """「proper线以对标账号为筛子」: sealed and its rungs stopped, the YES held."""

    sent: list = []
    _held(store, sent, 10.0)
    t1 = "2026-09-18T12:10:00.000Z"
    _snapshot(store, "snap_f2", t1, _ladder(), 2)
    receipt = _run(store, sent, fetch=_tape(_fill("n27", "BUY", 0.52)), now=t1, wallet=_wallet(10.0),
                   benchmark_counterparty="seal")
    assert receipt["benchmark"]["mode"] == "seal"
    assert receipt["benchmark"]["sell_orders"] == [] and _seal(store) is not None
    assert len(sent) == 1


def test_the_default_sells_every_yes_on_the_clusters_other_side(store, warm) -> None:
    """「是对标账号的对手盘的yes全部卖掉」."""

    sent: list = []
    _held(store, sent, 10.0)
    t1 = "2026-09-18T12:10:00.000Z"
    _snapshot(store, "snap_f2", t1, _ladder(), 2)
    receipt = _run(store, sent, fetch=_tape(_fill("n27", "BUY", 0.52)), now=t1, wallet=_wallet(10.0))
    assert receipt["benchmark"]["mode"] == "sell"
    (sell,) = receipt["benchmark"]["sell_orders"]
    assert sell["shares"] == 10.0 and sent[-1][1]["side"] == "SELL"


def test_a_paused_lane_still_sells_and_opens_nothing(store, warm) -> None:
    sent: list = []
    _held(store, sent, 10.0)
    t1 = "2026-09-18T12:10:00.000Z"
    _snapshot(store, "snap_f2", t1, _ladder(), 2)
    receipt = lane.execute_benchmark_sells(
        store, transport=base._resting_transport(sent), now_utc=t1, live_submit_authorized=True,
        settings={**V3, "enabled": False}, admission=ADMIT,
        benchmark_fetch=_tape(_fill("n27", "BUY", 0.52)), position_reader=_wallet(10.0),
    )
    assert receipt["state"] == "lane_paused_exits_only"
    (sell,) = receipt["benchmark"]["sell_orders"]
    assert sell["kind"] == "sell_yes" and sell["shares"] == 10.0
    assert [p["side"] for _path, p in sent] == ["BUY", "SELL"]        # the old rung, then the sale
    # Nothing on a clear bucket, and nothing at all under ``seal``.
    quiet = lane.execute_benchmark_sells(
        store, transport=base._resting_transport(sent), now_utc=t1, live_submit_authorized=True,
        settings={**V3, "benchmark_counterparty": "seal"}, admission=ADMIT,
        benchmark_fetch=_tape(), position_reader=_wallet(10.0),
    )
    assert quiet["benchmark"]["sell_orders"] == [] and len(sent) == 2


def test_no_sell_on_a_bucket_the_book_has_all_but_decided(store, warm) -> None:
    sent: list = []
    _held(store, sent, 10.0)
    t1 = "2026-09-18T12:10:00.000Z"
    decided = _ladder()
    decided[0] = base._row("27°C", 27, [(0.99, 200.0)], [(0.995, 200.0)], tick=0.001)
    _snapshot(store, "snap_f2", t1, decided, 2)
    receipt = _run(store, sent, fetch=_tape(_fill("n27", "BUY", 0.52)), now=t1, wallet=_wallet(10.0),
                   benchmark_counterparty="sell")
    assert receipt["benchmark"]["sell_orders"] == []
    assert _seal(store)["flip_state"] == "waiting:bucket_decided:0.990"


def test_the_charter_and_agents_carry_the_ruling() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = next(line for line in charter.splitlines() if line.startswith("258. "))
    assert "不做对标账号的对手盘" in clause and "买no线交给跟单线" in clause and "筛子" in clause
    assert "proper_bet_benchmark_seals" in clause and "benchmark_counterparty" in clause
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "宪章 258" in agents and "买 NO 归跟单线" in agents and "筛子" in agents


def _legacy_seal(conn) -> None:
    """A seal the first-fill criterion wrote before 「改封桶口径」."""

    lane.ensure_lane_schema(conn)
    conn.execute(
        f"INSERT INTO {lane.SEAL_TABLE}(station_id, event_date, bucket, side, yes_token, no_token, condition_id,"
        " sealed_at_utc, sealed_by, wallet, fill_ts, fill_tx, fill_token_side, fill_action, fill_price, fill_size,"
        " implied_price, opposite_fills, flip_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ST, ED, "27°C", "YES", "t27", "n27", COND, NOW, "holding", LEAD, 1, "0x1", "NO", "BUY", 0.44, 31.0,
         0.56, 1, "done"),
    )


def test_an_earlier_criterions_seal_not_borne_out_is_lifted_and_kept(store, warm) -> None:
    _seed(store, fresh_until=LONG)
    _legacy_seal(store)
    assert _seal(store) is None                    # not a seal under this criterion
    sent: list = []
    receipt = _run(store, sent, fetch=_tape(_fill("n27", "BUY", 0.44, size=31.0),
                                             _fill("t27", "BUY", 0.42, size=1044.0)))
    assert receipt["planned"] == 1 and len(sent) == 1
    row = store.execute(f"SELECT criterion, opposite_shares, same_side_shares, flip_state FROM {lane.SEAL_TABLE}").fetchone()
    assert row == (lane.SEAL_LIFTED, 31.0, 1044.0, "done")


def test_an_earlier_criterions_seal_borne_out_is_rewritten(store, warm) -> None:
    _seed(store, fresh_until=LONG)
    _legacy_seal(store)
    sent: list = []
    receipt = _run(store, sent, fetch=_tape(_fill("n27", "BUY", 0.52, size=40.0, ts=1_790_000_500),
                                             _fill("t27", "BUY", 0.48, size=10.0)))
    assert receipt["planned"] == 0 and not sent
    seal = _seal(store)
    assert seal["criterion"] == cluster.SEAL_CRITERION and seal["sealed_by"] == "plan"
    assert (seal["opposite_shares"], seal["same_side_shares"]) == (40.0, 10.0)
    assert seal["fill_ts"] == 1_790_000_500 and seal["flip_state"] == "done"
