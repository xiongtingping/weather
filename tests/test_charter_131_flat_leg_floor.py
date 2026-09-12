"""Charter 131 (Q8): the leg floor is a flat three cents, read at the mid.

Charter 91 (2026-09-06) let the floor fall with belief -- 0.03 x min(1,
0.30/p) -- so ZGSZ 2026-09-06's confirmed 27°C at 2.3 cents could be bought.
User ruling 2026-09-12 ("0.03 地板：读中价") retired that: a rung the market
prices under three cents is not bought however much we believe in it, and
the reference price stays charter 103's mid.
"""
from __future__ import annotations

from bayes_basket_target import delta_v2_runtime as P
from bayes_basket_target.basket_kernel import delta_v2 as K
from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot
from bayes_basket_target.delta_v2_runtime import _support_windows


def test_the_floor_is_flat_whatever_the_belief() -> None:
    assert K.LEG_FLOOR_RULE_VERSION == "flat_mid_v2"
    assert not hasattr(K, "LEG_FLOOR_BELIEF_ANCHOR")
    for belief in (None, 0.0, 0.05, 0.30, 0.45, 0.615, 0.90):
        assert K.leg_floor_all_in_ask(0.03, belief) == 0.03
    # The ZGSZ rung: 2.3 cents is under the floor at 61.5% as at 5%.
    assert K.leg_below_floor(0.023, 0.615, 0.03) is True
    assert K.leg_below_floor(0.023, 0.05, 0.03) is True
    assert K.leg_below_floor(0.031, 0.05, 0.03) is False
    assert K.leg_below_floor(None, 0.05, 0.03) is False


def _snapshot(asks: dict[str, float], probabilities: dict[str, float]):
    ordered = sorted(asks, key=lambda b: int(b))
    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6", "snapshot_id": "charter-91", "sequence": 1,
        "observed_at": "2026-09-06T06:50:00Z", "market_id": "m", "station_id": "ZGSZ",
        "event_date": "2026-09-06", "quote_version": "q", "quote_observed_at": "2026-09-06T06:50:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "inventory_version": "i", "order_state_version": "o",
        "evidence": {
            "probability_stage": "P1", "posterior_version": "p1", "posterior_sha256": "a" * 64,
            "reported_high_bucket": "27", "reported_high_ordinal": 27,
            "reported_high_observation_id": "obs", "hard_bound_id": "hb", "reported_high_advanced": False,
        },
        "buckets": [
            {
                "bucket": b, "ordinal": int(b), "probability": probabilities[b],
                "bid_price": max(0.001, round(asks[b] - 0.001, 3)),
                "bid_levels": [{"price": max(0.001, round(asks[b] - 0.001, 3)), "shares": 10.0}],
                "ask_levels": [{"price": asks[b], "shares": 100.0}],
                "price_tick_size": 0.001, "maker_queue_ahead_shares": 2.0, "maker_queue_known": True,
            }
            for b in ordered
        ],
        "checkpoint_changed": True, "quote_changed": True, "fill_changed": True,
        "time_gate_changed": True, "evidence_fresh": True, "contract_matches": True,
        "order_state_known": True, "risk_inputs_complete": True, "time_left_minutes": 540.0,
    })


def test_the_confirmed_high_at_two_cents_is_refused_like_any_tail(monkeypatch) -> None:
    """ZGSZ 2026-09-06 at 14:50 local: the market asks 2.3 cents for 27°C
    against a 61.5% belief.  Charter 91 admitted it; charter 131 does not."""

    monkeypatch.setattr(P, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
    asks = {"27": 0.023, "28": 0.283, "29": 0.61, "30": 0.18, "31": 0.04, "32": 0.023, "33": 0.002}
    probabilities = {"27": 0.615, "28": 0.002, "29": 0.014, "30": 0.109, "31": 0.180, "32": 0.073, "33": 0.007}
    snapshot = _snapshot(asks, probabilities)
    windows, audit = _support_windows(
        snapshot=snapshot, desired={"28"}, probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=4, minimum_support_buckets=3, center_bucket="28",
        probability_stage="P1",
    )
    assert "27" in audit["leg_floor_excluded_buckets"], "61.5% at 2.3 cents is under the flat floor"
    assert "33" in audit["leg_floor_excluded_buckets"]
    assert "32" in audit["leg_floor_excluded_buckets"]
    assert audit["leg_floor_rule"] == "flat_mid_v2"
    assert "leg_floor_belief_anchor" not in audit
    assert audit["leg_floor_refused_window_count"] == 0


def test_the_ledger_reads_the_kernels_floor_and_hashes_the_rule() -> None:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import paper_trading_ledger as L  # noqa: PLC0415

    # Charter 103 moved the read point: both books ask the kernel the whole
    # question -- floor and the side of the book it is measured on.
    assert L.leg_below_floor is K.leg_below_floor
    assert L.LEG_FLOOR_RULE_VERSION == K.LEG_FLOOR_RULE_VERSION
    assert L.LEG_FLOOR_PRICE_BASIS == K.LEG_FLOOR_PRICE_BASIS
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")
    assert '"leg_floor_rule": LEG_FLOOR_RULE_VERSION,' in source
    assert '"leg_floor_price_basis": LEG_FLOOR_PRICE_BASIS,' in source
    assert "LEG_FLOOR_BELIEF_ANCHOR" not in source
    runtime = (root / "bayes_basket_target" / "delta_v2_runtime.py").read_text(encoding="utf-8")
    # No production site compares a quote to the bare constant any more.
    assert "< MINIMUM_LEG_ALL_IN_ASK" not in runtime
    assert ">= MINIMUM_LEG_ALL_IN_ASK" not in runtime
    # And no site in either book reaches past the shared read point.
    assert "leg_floor_all_in_ask(" not in source
    assert "leg_floor_all_in_ask(" not in runtime

def test_the_audits_ask_the_kernel_too_rather_than_the_bare_constant() -> None:
    """An audit that hand-copies the rule audits the rule it copied.

    ``test_paper_trading_ledger_rules`` held the book to
    ``quote * (1 + FEE_RATE) < MINIMUM_LEG_ALL_IN_ASK`` -- the flat floor
    this charter retired on 2026-09-06 -- and read it off ``prices_json``,
    which records the best ASK, while charter 103 measures the floor on
    the MID.  Both halves were wrong in the same direction, so the first
    seat where they mattered read as a production defect: KDAL 2026-09-09
    P0 sat two rungs wide with 102-103F outside it, asking exactly 0.03
    against a 0.03 floor.  Its bid was 0.02, its mid 0.025, and the live
    rule refused the rung correctly.  Two tests went red for three days
    over a seat that obeyed the rule.

    So the audits go through ``leg_below_floor`` like everything else.
    They cannot answer it from the position row -- the row has no bid --
    and they replay the entry cycle for the handful of seats narrower
    than the floor instead of guessing from the ask.
    """

    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    audit = (root / "tests" / "test_paper_trading_ledger_rules.py").read_text(
        encoding="utf-8"
    )
    assert "< MINIMUM_LEG_ALL_IN_ASK" not in audit
    assert ">= MINIMUM_LEG_ALL_IN_ASK" not in audit
    assert "leg_below_floor(" in audit
    # And the replay that gives it the other side of the book.
    assert "def _entry_book(" in audit


def test_the_ledgers_two_flat_comparisons_are_execution_not_admission() -> None:
    """Not every mention of the constant is the retired rule.

    Two sites in the ledger still compare a price to the flat 0.03, and
    both are execution guards rather than the admission floor: whether to
    CROSS a quote, not whether a rung may be in the basket.  A sub-floor
    ask is a one-sided tail offer, and reaching for it is what once
    manufactured +260% ROI out of tenth-of-a-cent lottery tickets.  Pinned
    here with their reason so the next sweep for the retired rule does not
    take them for it.
    """

    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert "float(cross_ask) >= MINIMUM_LEG_ALL_IN_ASK" in source
    assert "if take >= MINIMUM_LEG_ALL_IN_ASK:" in source
    # The admission question is never asked this way in either book.
    assert "< MINIMUM_LEG_ALL_IN_ASK" not in source
