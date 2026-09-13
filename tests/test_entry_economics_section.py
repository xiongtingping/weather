"""The nine numbers a bet is made of, and the three ways they are averaged.

The section exists because each of these was already on the board without
the number that gives it a meaning beside it: a 41.4% hit rate is a rout
against a 41.6-cent basket and a licence to print against a 30-cent one.
Putting them together makes two things breakable, so both are pinned here.

**The pair must measure the same set of buckets.**  ``claimed_win_
probability`` is the served probability summed over the buckets the chain
ended up HOLDING, so it divides by ``hit`` and by nothing else.  The
ledger also carries ``intended_hit`` -- the truth landing in what the
objective asked for, including legs the market never sold -- and it is the
larger number (60.7% against 39.8% measured on 2026-09-08).  Swapping it
in would lift the realisation ratio to about 1.0 and read as a model that
is exactly calibrated, when what changed was the denominator's set.

**The share column has to close.**  Weighting each chain by the basket
shares it bought makes cost-per-share equal total money over total shares
and realised-EV-per-share equal total P&L over total shares, so their
ratio IS the book's return.  A rounding shortcut or a dropped row shows up
here as a column that no longer reconciles with the equity curve.

Plus the convexity trap the section warns about in prose: the plain
per-chain mean of (1-c)/c is dominated by a handful of few-cent baskets
(14.7x against a book whose money bought 1.53x), so the tile must print
the odds implied by the average cost, never that mean.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

DDL = (
    "CREATE TABLE paper_trade_settlement ("
    " settlement_id TEXT PRIMARY KEY, position_id TEXT NOT NULL,"
    " station_id TEXT NOT NULL, event_date TEXT NOT NULL, stage TEXT NOT NULL,"
    " settled_tmax_c REAL NOT NULL, settled_bucket TEXT NOT NULL,"
    " hit INTEGER NOT NULL, stake_usdc REAL NOT NULL,"
    " payout_usdc REAL NOT NULL, pnl_usdc REAL NOT NULL,"
    " claimed_win_probability REAL NOT NULL, center_error_c REAL,"
    " bucket_distance INTEGER, settled_at_utc TEXT NOT NULL,"
    " truth_revision_no INTEGER, truth_content_sha256 TEXT NOT NULL DEFAULT '',"
    " basket_all_in_cost REAL, intended_hit INTEGER)"
)


def _book(rows: list[tuple]) -> sqlite3.Connection:
    """A book of (stage, claim, hit, intended_hit, cost, shares).

    Stake is derived from cost and shares rather than given, so every row
    is internally consistent: a hit pays one per share, a miss pays zero.
    """

    conn = sqlite3.connect(":memory:")
    conn.execute(DDL)
    for i, (stage, claim, hit, intended, cost, shares) in enumerate(rows):
        stake = cost * shares
        payout = shares if hit else 0.0
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"s{i}", f"p{i}", f"ST{i}", f"2026-09-{i + 1:02d}", stage,
             30.0, "30°C", int(hit), stake, payout, payout - stake,
             claim, 0.0, 0, "2026-09-10T00:00:00Z", 1, "", cost,
             int(intended)),
        )
    conn.commit()
    return conn


def test_claim_pairs_with_the_held_basket_not_the_chosen_one():
    # Every chain claims 50% and holds a basket that hits half the time,
    # while the objective's chosen basket would have hit every time.  The
    # realisation ratio is 1.0 only if the denominator is the held set.
    conn = _book([
        ("P0", 0.5, 1, 1, 0.4, 10.0),
        ("P0", 0.5, 0, 1, 0.4, 10.0),
        ("P0", 0.5, 1, 1, 0.4, 10.0),
        ("P0", 0.5, 0, 1, 0.4, 10.0),
    ])
    got = C.collect_entry_economics(conn)
    chain = got["weightings"]["chain"]
    assert chain["claimed_coverage"] == pytest.approx(0.5)
    assert chain["realised_coverage"] == pytest.approx(0.5)
    assert chain["realisation_ratio"] == pytest.approx(1.0)
    # The chosen-basket number is published, and it is a different set.
    assert got["intended_coverage"] == pytest.approx(1.0)
    assert got["intended_coverage"] != chain["realised_coverage"]


def test_share_column_reconciles_with_the_book_return():
    # Uneven sizes on purpose: a big cheap winner and a small dear loser
    # pull the three weightings apart, which is the case the column has to
    # survive.
    conn = _book([
        ("P0", 0.30, 1, 1, 0.20, 100.0),
        ("P0", 0.80, 0, 0, 0.75, 4.0),
        ("P1", 0.55, 0, 1, 0.50, 20.0),
        ("P1", 0.60, 1, 1, 0.55, 8.0),
    ])
    got = C.collect_entry_economics(conn)
    share = got["weightings"]["share"]
    assert share["cost_per_share"] == pytest.approx(
        got["stake_usdc"] / got["shares"]
    )
    assert share["ev_per_share_realised"] == pytest.approx(
        got["pnl_usdc"] / got["shares"]
    )
    # The closure the section leads with: EV per share over cost per share
    # is the realised return on the money, to the last place.
    assert (
        share["ev_per_share_realised"] / share["cost_per_share"]
        == pytest.approx(got["roi"])
    )
    # And the three weightings really do disagree here, so the test is not
    # passing on a book where they collapse to one number.
    claims = {
        round(got["weightings"][key]["claimed_coverage"], 6)
        for key in ("chain", "stake", "share")
    }
    assert len(claims) == 3


def test_break_even_rate_is_the_cost_under_every_weighting():
    conn = _book([
        ("P0", 0.45, 1, 1, 0.35, 12.0),
        ("P0", 0.45, 0, 0, 0.62, 3.0),
        ("P1", 0.70, 0, 1, 0.51, 40.0),
    ])
    got = C.collect_entry_economics(conn)
    for key in ("chain", "stake", "share"):
        block = got["weightings"][key]
        assert block["breakeven_hit_rate"] == pytest.approx(
            block["cost_per_share"]
        )
        assert block["net_odds"] == pytest.approx(
            (1.0 - block["cost_per_share"]) / block["cost_per_share"]
        )
        assert block["margin_over_breakeven"] == pytest.approx(
            block["realised_coverage"] - block["breakeven_hit_rate"]
        )


def test_published_odds_are_not_the_mean_of_per_chain_odds():
    # One three-cent basket among four.  Its own odds are 32x and the
    # plain mean of the four is over eight, while the money in the book
    # bought about 1.2x.  The tile must print the second kind.
    conn = _book([
        ("P0", 0.05, 0, 0, 0.03, 10.0),
        ("P0", 0.60, 1, 1, 0.55, 10.0),
        ("P0", 0.60, 0, 0, 0.55, 10.0),
        ("P0", 0.60, 1, 1, 0.55, 10.0),
    ])
    got = C.collect_entry_economics(conn)
    chain = got["weightings"]["chain"]
    assert got["net_odds_chain_naive"] > 8.0
    assert chain["net_odds"] < 2.0
    assert got["net_odds_max"] == pytest.approx((1 - 0.03) / 0.03)
    assert got["cheap_chains"] == 1


def test_hit_and_profit_are_counted_apart():
    # A basket that cost more than the 1.00 it can win.  It hits and still
    # loses, which is the day the two rates come apart -- the section
    # prints both precisely so that day is visible.
    conn = _book([
        ("P0", 0.99, 1, 1, 1.10, 5.0),
        ("P0", 0.40, 1, 1, 0.30, 5.0),
        ("P0", 0.40, 0, 0, 0.30, 5.0),
    ])
    got = C.collect_entry_economics(conn)
    chain = got["weightings"]["chain"]
    assert chain["realised_coverage"] == pytest.approx(2 / 3)
    assert chain["profitable_rate"] == pytest.approx(1 / 3)
    assert got["hit_not_profitable"] == 1
    assert got["profitable_not_hit"] == 0


def test_rows_without_a_basket_cost_are_counted_not_dropped_silently():
    conn = _book([("P0", 0.5, 1, 1, 0.4, 10.0)])
    conn.execute(
        "INSERT INTO paper_trade_settlement VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sx", "px", "STX", "2026-09-20", "P0", 30.0, "30°C", 1,
         4.0, 10.0, 6.0, 0.5, 0.0, 0, "2026-09-10T00:00:00Z", 1, "",
         None, 1),
    )
    conn.commit()
    got = C.collect_entry_economics(conn)
    assert got["settled_rows"] == 2
    assert got["chains"] == 1
    assert got["cost_missing"] == 1


def test_sub_cent_baskets_are_excluded_on_the_ledgers_own_rule():
    """The floor is the board's, and it is imported, not restated.

    A basket costing under a cent is a minimum-tick offer with nothing
    bid against it; the ledger calls such a chain not credible and every
    other section on the page drops it.  This section shipped without the
    floor on 2026-09-10 and was the only one counting 249 chains while
    the daily table counted 239 -- and seven sub-cent misses worth 0.25
    USDC of stake moved the per-chain averages further than any of the
    real chains did (cost 0.428 -> 0.416, odds 1.34x -> 1.40x, plain
    per-chain odds mean 5.9x -> 14.7x).
    """

    conn = _book([
        ("P0", 0.42, 1, 1, 0.40, 10.0),
        ("P0", 0.42, 0, 0, 0.40, 10.0),
        ("P0", 0.21, 0, 0, 0.003, 5.0),   # dust: 332x odds, 1.5 cents at risk
        ("P0", 0.19, 0, 0, 0.004, 5.0),
    ])
    got = C.collect_entry_economics(conn)
    assert got["settled_rows"] == 4
    assert got["chains"] == 2
    assert got["dust_excluded"] == 2
    assert got["dust_excluded_stake_usdc"] == pytest.approx(0.035)
    # Without the floor these two would drag the mean price from 0.40 to
    # about 0.20 and double the odds.
    assert got["weightings"]["chain"]["cost_per_share"] == pytest.approx(0.40)
    assert got["weightings"]["chain"]["net_odds"] == pytest.approx(1.5)


def test_dust_floor_is_the_ledgers_and_not_a_local_copy():
    from paper_trading_ledger import DUST_COST_FLOOR as LEDGER_FLOOR

    assert C.DUST_COST_FLOOR is LEDGER_FLOOR
    conn = _book([("P0", 0.5, 1, 1, 0.4, 10.0)])
    assert C.collect_entry_economics(conn)["dust_cost_floor"] == LEDGER_FLOOR


def test_odds_never_come_from_averaging_per_row_odds():
    """Convexity, pinned in both modules that aggregate odds.

    ``(1-c)/c`` is convex, so the mean of it is set by whichever basket
    was cheapest rather than by the money at stake.  On this book the
    per-row mean was 14.7x against a book whose money bought 1.53x, and
    one day of the daily table printed 32.8x.
    """

    import selection_market_dashboard as SM

    costs = [0.02, 0.60, 0.60, 0.60]
    naive = sum((1 - c) / c for c in costs) / len(costs)
    for helper in (C._odds_from_mean_cost, SM._odds_from_mean_cost):
        got = helper(costs)
        mean_cost = sum(costs) / len(costs)
        assert got == pytest.approx((1 - mean_cost) / mean_cost)
        assert got < naive / 4
    assert C._odds_from_mean_cost([]) is None
    assert C._odds_from_mean_cost([0.0, None]) is None

    source = (ROOT / "scripts" / "selection_market_dashboard.py").read_text()
    page = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    for text in (source, page):
        assert 'sum(odds) / len(odds)' not in text
        assert '_mean([r["odds"]' not in text


def test_the_portfolio_card_no_longer_prints_the_same_numbers():
    """One quantity, one place on the page.

    The portfolio card carried its own 平均赔率 / 保本胜率 / 平均成本
    tiles, and they disagreed with this section on screen (1.53x against
    1.40x, 0.592 against 0.416) because they were a different weighting
    wearing the same label.  Worse, the card's own arithmetic did not
    close: it asserted that a 0.592 ticket gives 1.53x odds, and
    (1-0.592)/0.592 is 0.69.
    """

    page = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    assert '${tile("保本胜率"' not in page
    assert '${tile("平均赔率' not in page
    assert '<b>赔率这一组读法：</b>' not in page
    # The correction stays on the page where the wrong number was.
    assert 'href="#sec-entry"' in page


def test_empty_book_says_so_instead_of_publishing_zeroes():
    # The ledger is rebuilt in place -- cleared, then re-walked for about
    # forty minutes -- and a render landing inside that window used to
    # publish a page of zeroes as if measured (2026-09-08).
    conn = sqlite3.connect(":memory:")
    conn.execute(DDL)
    got = C.collect_entry_economics(conn)
    assert got["available"] is False
    assert got["chains"] == 0
    assert "重建" in got["reason"]


def test_stake_weighted_column_agrees_with_the_portfolio_tiles():
    """One quantity, one value, however many sections print it.

    ``collect_risk_metrics`` already publishes the stake-weighted cost,
    odds, break-even rate and realised win rate.  This section computes
    them again from the same table, so the two are pinned together: a
    change to either definition has to move both or fail here.
    """

    conn = _book([
        ("P0", 0.30, 1, 1, 0.20, 100.0),
        ("P0", 0.80, 0, 0, 0.75, 4.0),
        ("P1", 0.55, 0, 1, 0.50, 20.0),
        ("P1", 0.60, 1, 1, 0.55, 8.0),
        ("P0", 0.44, 1, 1, 0.41, 15.0),
    ])
    entry = C.collect_entry_economics(conn)["weightings"]["stake"]
    risk = C.collect_risk_metrics(conn)
    assert risk["available"] is True
    assert entry["cost_per_share"] == pytest.approx(risk["cost_mean"])
    assert entry["breakeven_hit_rate"] == pytest.approx(
        risk["breakeven_win_rate"]
    )
    assert entry["realised_coverage"] == pytest.approx(
        risk["realised_win_rate_weighted"]
    )
    # The odds are the one pair that is allowed to differ, and the reason
    # is the convexity above: the portfolio tile averages each chain's own
    # (1-c)/c weighted by stake, this section inverts the average cost.
    # Pinned as an inequality so nobody "reconciles" them by accident.
    assert risk["odds_mean"] != pytest.approx(entry["net_odds"])


def test_section_is_rendered_and_reachable():
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    # Since 2026-09-13 the section is a pane of the money board, under the
    # same anchor; tests/test_dashboard_money_board.py renders the board.
    assert '["entry", "入场的九个数", () => entryEconomics(), "sec-entry"]' in source
    assert 'sec(moneyBoard, "资金看板", "sec-money")' in source
    assert '"entry_economics": _cached_section(' in source


def test_the_three_verdict_pairs_are_exact_identities():
    """What the strip at the top of the section rests on (2026-09-13).

    Nine tiles in a grid read as nine verdicts and they are not: only
    three PAIRS decide whether a bet earned or lost, and each pair is an
    exact identity on the ledger rather than a rule of thumb.  Pinned
    here because the strip prints the subtraction already done -- if any
    of the three stopped closing, the page would be asserting a verdict
    its own tiles no longer support, and nothing else would notice.

      ⑨实际 = ②×命中实收 − ⑧            share weighting, to the last place
      ⑨声称 = ① − ④                      every weighting (a mean is linear)
      盈亏   = 链数 × (③·⑥ + (1−③)·⑦)   chain weighting, in USDC

    The third one never touches cost or coverage, so it is arithmetically
    independent of the first two: a book the page has the sign wrong on
    shows up as two rows disagreeing rather than three agreeing quietly.
    """

    conn = _book([
        ("P0", 0.30, 1, 1, 0.20, 100.0),
        ("P0", 0.80, 0, 0, 0.75, 4.0),
        ("P1", 0.55, 0, 1, 0.50, 20.0),
        ("P1", 0.60, 1, 1, 0.55, 8.0),
        ("P0", 0.44, 1, 1, 0.41, 15.0),
    ])
    got = C.collect_entry_economics(conn)
    share, chain = got["weightings"]["share"], got["weightings"]["chain"]

    # Row one: what a share took out, against what it cost.
    assert share["ev_per_share_realised"] == pytest.approx(
        share["realised_coverage"] * got["hit_pays_per_share"]
        - share["cost_per_share"]
    )
    # Row two: the same subtraction with the claim in place of the truth,
    # under every weighting the page offers -- the tile prints all three.
    for block in got["weightings"].values():
        assert block["ev_per_share_claimed"] == pytest.approx(
            block["claimed_coverage"] - block["cost_per_share"]
        )
    # Row three: the USDC cross-check, which touches neither cost nor
    # coverage and still has to land on the same P&L.
    assert got["chains"] * (
        chain["profitable_rate"] * got["mean_win_usdc"]
        + (1 - chain["profitable_rate"]) * got["mean_loss_usdc"]
    ) == pytest.approx(got["pnl_usdc"])
    # And its inside-out form, the one the section prints as "盈亏比":
    # ③·⑥ > (1-③)·|⑦|  is  ⑥/|⑦| > (1-③)/③.
    payoff = got["mean_win_usdc"] / -got["mean_loss_usdc"]
    needed = (1 - chain["profitable_rate"]) / chain["profitable_rate"]
    assert (payoff > needed) is (got["pnl_usdc"] > 0)


def test_the_plain_language_paragraph_stays_in_one_weighting():
    """One column per sentence, and only the realised figure scales.

    The 白话版 led with the per-chain cost and break-even rate and then
    subtracted the PER-CHAIN claimed EV from the PER-SHARE realised one,
    printing the difference of two different books as "差".  It then said
    that difference times the share count was the P&L, which it never
    was: the quantity that multiplies out to the book's money is the
    realised per-share figure itself (0.9c x 789 shares = +6.96 U on
    2026-09-13), and the claimed-minus-realised gap multiplies out to
    nothing at all.
    """

    page = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    assert "(C.ev_per_share_claimed||0)-(S.ev_per_share_realised||0)" not in page
    assert "这一笔差价乘上" not in page


# ---------------------------------------------------------------- as drawn
#
# User ruling, 2026-09-13: 不要给我解释，在数据看板上用直观的数据对比得出结论即可.
# The section is a verdict, comparison bars that each end in a result, and
# the nine values -- no paragraphs.  Rendered from the collector's own
# output, so the verdicts are checked against a real book, not a fixture
# shaped to agree with them.

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

SOURCE = C.HTML_TEMPLATE


def _function(name: str) -> str:
    start = SOURCE.index(f"function {name}(")
    depth = 0
    for index in range(SOURCE.index("{", start), len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
        elif SOURCE[index] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start:index + 1]
    raise AssertionError(f"{name}() has unbalanced braces")


def _draw(entry: dict) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI images all carry node
        pytest.skip("node is required to run the board's renderer")
    script = (
        "const D = " + json.dumps({"entry_economics": entry}, ensure_ascii=False) + ";\n"
        "const h = (s) => String(s == null ? '' : s)"
        ".replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');\n"
        + _function("entryEconomics") + "\nconsole.log(entryEconomics());\n"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _card(html: str, title: str) -> str:
    start = html.index(f"<b>{title}</b>")
    end = html.find('<div class="card ee-cmp">', start)
    return html[start:end if end > 0 else html.index('<div class="ee-nine">', start)]


def _pill(fragment: str) -> tuple[str, str]:
    tone, word = re.search(r'<span class="pill (\w+)[^"]*">([^<]*)</span>', fragment).groups()
    return tone, word


WINNING = [
    ("P0", 0.30, 1, 1, 0.20, 100.0),
    ("P0", 0.80, 0, 0, 0.75, 4.0),
    ("P1", 0.55, 0, 1, 0.50, 20.0),
    ("P1", 0.60, 1, 1, 0.55, 8.0),
    ("P0", 0.44, 1, 1, 0.41, 15.0),
]
LOSING = [
    ("P0", 0.50, 0, 1, 0.45, 20.0),
    ("P0", 0.50, 0, 0, 0.45, 20.0),
    ("P0", 0.50, 1, 1, 0.45, 5.0),
]


def test_no_paragraph_of_explanation_is_drawn():
    html = _draw(C.collect_entry_economics(_book(WINNING)))

    assert 'class="why"' not in html
    assert '<p class="sub">' not in html
    assert "<details" not in html
    for prose in ("白话版", "怎么读", "为什么", "容易读错", "三个口径"):
        assert prose not in html


def test_the_verdict_follows_the_money_both_ways():
    for rows, word, tone in ((WINNING, "赚", "green"), (LOSING, "亏", "red")):
        entry = C.collect_entry_economics(_book(rows))
        html = _draw(entry)
        top = html[html.index('class="card ee-top"'):html.index('<div class="ee-cmps">')]

        assert _pill(top) == (tone, word)
        assert _pill(_card(html, "赚的钱 vs 亏的钱")) == (tone, word)
        assert (entry["pnl_usdc"] >= 0) is (word == "赚")


def test_gains_minus_losses_is_the_books_pnl():
    entry = C.collect_entry_economics(_book(WINNING))
    card = _card(_draw(entry), "赚的钱 vs 亏的钱")

    gains, losses = (float(v) for v in re.findall(r'class="ee-v">([\d.]+) U<', card))
    assert gains - losses == pytest.approx(entry["pnl_usdc"], abs=0.011)
    assert f'赢 {entry["win_count"]} 条' in card and f'亏 {entry["loss_count"]} 条' in card


def test_bars_share_one_scale_so_the_longer_bar_is_the_larger_number():
    html = _draw(C.collect_entry_economics(_book(LOSING)))
    card = _card(html, "赚的钱 vs 亏的钱")

    widths = [float(w) for w in re.findall(r'style="width:([\d.]+)%"', card)]
    values = [float(v) for v in re.findall(r'class="ee-v">([\d.]+) U<', card)]
    assert max(widths) == pytest.approx(100.0)
    assert widths[0] / widths[1] == pytest.approx(values[0] / values[1], rel=0.01)


def test_hit_card_compares_realised_coverage_with_the_break_even_line():
    entry = C.collect_entry_economics(_book(LOSING))
    share = entry["weightings"]["share"]
    card = _card(_draw(entry), "实际命中 vs 保本线")

    below = share["realised_coverage"] < share["breakeven_hit_rate"]
    assert _pill(card) == (("red", "低于保本线") if below else ("green", "盖过保本线"))


def test_a_losing_stage_is_named_with_its_loss():
    entry = C.collect_entry_economics(_book(WINNING))
    stages = {s["stage"]: s for s in entry["by_stage"]}
    card = _card(_draw(entry), "分阶段收益率")

    losers = [name for name, s in stages.items() if s["pnl_usdc"] < 0]
    winners = [name for name, s in stages.items() if s["pnl_usdc"] >= 0]
    if losers and winners:
        assert _pill(card) == ("amber", "赚在 " + "、".join(winners))
        for name in losers:
            assert f"亏在 {name}" in card or f"· {name}" in card
    for name, s in stages.items():
        assert f"{name} · {s['chains']} 链" in card


def test_all_nine_numbers_are_on_the_board():
    html = _draw(C.collect_entry_economics(_book(WINNING)))
    cells = re.findall(r'<div class="ee-n"><span class="muted">([^<]+)</span>', html)

    assert [c[0] for c in cells] == list("①②③④⑤⑥⑦⑧⑨")
