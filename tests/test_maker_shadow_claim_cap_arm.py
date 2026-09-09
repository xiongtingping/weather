"""The arm that prices off the claim instead of off the book.

Every other rule in the probe answers "where is the other side resting?".
None answers "what is this bucket worth?" -- and neither does production:
charter 68's gate knows a ceiling (``claim x lambda(a)``, the claim after the
fraction history says arrives) and that ceiling has never reached the price.
``delta_v2_runtime`` says so at the maker branch: "the rung is the price.
The reservation solve rides along for the audit."

The floor under the gate's own check is zero -- ``minimum_expected_edge_per
_share`` is 0.0 -- so a leg priced a thousandth under its ceiling passes,
and lambda is fitted on tens to hundreds of seats per band, which puts a
thousandth well inside its error bar.  These arms measure what happens if
the ceiling prices the order instead of auditing it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "maker_shadow_probe", ROOT / "scripts" / "maker_shadow_probe.py"
)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# One band each side of 1.0 on the shipped P0 shape, so the lookup is
# exercised rather than assumed.  Values are the table's own form: (upper
# bound, lambda), read as "the first bound the agreement falls below".
TABLES = {"P0": ((0.7, 0.40), (1.0, 1.00), (float("inf"), 1.20))}
QUOTE = {"bid": 0.30, "ask": 0.40, "tick": 0.01,
         "queue_ahead": None, "queue_known": False}


def _price(rule, probability, quote=None):
    return probe._limit_price(
        rule, quote or QUOTE, probability=probability, lambda_tables=TABLES,
    )


def test_both_arms_are_declared_beside_the_rules_they_answer() -> None:
    assert "claim_cap" in probe.PRICE_RULES
    assert "claim_cap@0.8" in probe.PRICE_RULES
    assert "bid+1t" in probe.PRICE_RULES, "the ladder twin stays"
    assert probe.CLAIM_CAP_MARGIN == {"claim_cap": 0.0, "claim_cap@0.8": 0.2}
    # A shadow order carries no stage, so the table it prices from is pinned
    # rather than inferred.
    assert probe.CLAIM_CAP_STAGE == "P0"


def test_a_rung_already_under_the_ceiling_is_posted_unchanged() -> None:
    """The arm is its own control on every leg the ceiling does not bind.

    Claim 0.32 at the 0.31 rung is a = 0.969, lambda 1.00, ceiling 0.320 --
    above the rung, so the rung stands and this leg is identical to bid+1t.
    """
    assert _price("claim_cap", 0.32) == probe._limit_price("bid+1t", QUOTE)


def test_a_rung_over_the_ceiling_is_cut_to_it() -> None:
    """And this is the only place the arms can differ.

    Claim 0.55 at the 0.31 rung is a = 0.564, lambda 0.40, ceiling 0.220 --
    the rung is 9 cents dearer than what history says the claim delivers.
    """
    capped = _price("claim_cap", 0.55)
    assert capped is not None
    assert capped < probe._limit_price("bid+1t", QUOTE)
    assert abs(capped - 0.22) < 1e-9
    # The margin arm keeps a fifth of the ceiling back.
    assert abs(_price("claim_cap@0.8", 0.55) - 0.17) < 1e-9


def test_the_ceiling_is_floored_to_the_tick() -> None:
    """A price off the venue's grid is not a price it would accept."""
    for probability in (0.55, 0.43, 0.37, 0.21):
        for rule in ("claim_cap", "claim_cap@0.8"):
            price = _price(rule, probability)
            if price is None:
                continue
            assert abs(price / QUOTE["tick"] - round(price / QUOTE["tick"])) < 1e-6


def test_no_claim_means_no_order_rather_than_a_book_price() -> None:
    """Falling back to the rung would silently re-run bid+1t under a new
    name, and the arm would look like it had measured something."""
    assert _price("claim_cap", None) is None
    assert _price("claim_cap", 0.0) is None
    one_sided = dict(QUOTE, bid=0.0)
    assert probe._limit_price(
        "claim_cap", one_sided, probability=0.5, lambda_tables=TABLES
    ) is None


def test_the_chasing_lane_never_reprices_a_claim_capped_order() -> None:
    """Chasing re-derives from the bid, which is the thing these arms are
    deliberately not anchored to."""
    assert not (set(probe.CHASING_RULES) & set(probe.CLAIM_CAP_MARGIN))
    assert not (set(probe.BASE_RULE) & set(probe.CLAIM_CAP_MARGIN))


def _shadow_db():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    probe.ensure_schema(conn)
    return conn


def _order(conn, rule, price, *, bucket="30°C", state="OPEN", oid=None):
    conn.execute(
        "INSERT INTO maker_shadow_order("
        " order_id, station_id, event_date, bucket, price_rule,"
        " horizon_minutes, placed_at_utc, book_observed_at_utc,"
        " expires_at_utc, lead_hours, limit_price, reference_bid,"
        " reference_ask, reference_tick, queue_ahead_shares, queue_known,"
        " model_probability, state, content_sha256)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid or f"o-{rule}-{bucket}", "ZSQD", "2026-09-09", bucket, rule, 60,
         "2026-09-09T00:00:00Z", "2026-09-09T00:00:00Z",
         "2026-09-09T01:00:00Z", 24.0, price, 0.30, 0.40, 0.01, None, 0,
         0.55, state, "d" * 64),
    )
    return oid or f"o-{rule}-{bucket}"


def test_the_reading_pairs_leg_by_leg_and_never_pools() -> None:
    """The arm drops legs whose ceiling falls under the one-cent floor, so
    its surviving population runs at a higher claim than bid+1t's; comparing
    the two means would read that selection as a result."""
    conn = _shadow_db()
    _order(conn, "claim_cap", 0.22, bucket="30°C")
    _order(conn, "bid+1t", 0.31, bucket="30°C")
    # An unpaired bid+1t leg: the arm refused this one, and it must not
    # enter the comparison on either side.
    _order(conn, "bid+1t", 0.05, bucket="31°C")
    line = probe.claim_cap_reading(conn)
    assert "paired 1," in line
    assert "ceiling bound 1 (100%)" in line
    assert "+0.0900" in line
    conn.close()


def test_the_reading_says_so_when_nothing_has_settled() -> None:
    conn = _shadow_db()
    assert "no paired legs yet" in probe.claim_cap_reading(conn)
    _order(conn, "claim_cap", 0.22)
    _order(conn, "bid+1t", 0.31)
    assert "no leg settled on both rules yet" in probe.claim_cap_reading(conn)
    conn.close()


def test_the_reading_carries_both_numbers_that_decide_the_arm() -> None:
    """Fill rate says whether the ceiling priced us out; pnl per filled share
    says whether the legs it did buy were worth more.  One without the other
    cannot judge this arm."""
    conn = _shadow_db()
    cap = _order(conn, "claim_cap", 0.22, state="FILLED")
    rung = _order(conn, "bid+1t", 0.31, state="FILLED")
    for oid, price, pnl in ((cap, 0.22, 0.78), (rung, 0.31, 0.69)):
        conn.execute(
            "INSERT INTO maker_shadow_settlement("
            " order_id, station_id, event_date, settled_bucket, hit,"
            " limit_price, pnl_per_share, settled_at_utc)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (oid, "ZSQD", "2026-09-09", "30°C", 1, price, pnl,
             "2026-09-09T12:00:00Z"),
        )
    line = probe.claim_cap_reading(conn)
    assert "filled 1 vs 1" in line
    assert "settled both 1" in line
    assert "+0.7800 vs +0.6900" in line
    conn.close()
