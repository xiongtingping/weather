"""Every ladder on the board reads left to right, coldest rung first.

User ruling, 2026-09-12.  Nothing upstream promises ladder order, and one
lane actively breaks it: the live order panel groups legs off a query that
returns intents newest-first, so a three-leg EPWA basket printed
``19°C 20°C 18°C``.  Measured on the board's own payload the day of the
ruling, 55 of 130 live baskets were drawn out of temperature order -- and a
ladder drawn out of order reads as three unrelated buys rather than as one
basket, which is the exact misreading the grouping exists to prevent.

The order is asserted on rendered output, not on the source: a grep for
``rungSorted`` would pass on a page that sorted the wrong list, and it was
a call site -- not the comparator -- that was missing.

The comparator is label arithmetic, because a label is all the live panel
has: the intent row carries ``bucket`` and no rung index.  Four shapes are
minted -- ``18°C``, ``78-79°F``, ``17°C or below``, ``21°C or higher`` --
and a couple of older lanes write ``<=17°C`` / ``>=21°C``.  A range sorts
on its lower bound; an open end sorts half a rung outside the degree it
names, so the bottom bucket lands below its own number and the top one
above it, which is where they sit on the ladder.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

SOURCE = C.HTML_TEMPLATE

HELPERS_HEAD = "// Rungs read left to right, coldest first"
HELPERS_TAIL = "  .sort((x, y) => byRung(pick ? pick(x) : x, pick ? pick(y) : y));"


def _helpers() -> str:
    """The shared rung comparator, as it ships."""

    head = SOURCE.index(HELPERS_HEAD)
    tail = SOURCE.index(HELPERS_TAIL, head) + len(HELPERS_TAIL)
    return SOURCE[head:tail]


def _extract(name: str) -> str:
    """One top-level board function, by brace matching rather than copying."""

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


def _const(name: str) -> str:
    """One top-level ``const NAME={...}`` table, by brace matching."""

    start = SOURCE.index(f"const {name}=")
    depth = 0
    for index in range(SOURCE.index("{", start), len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
        elif SOURCE[index] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start:index + 1] + ";"
    raise AssertionError(f"{name} has unbalanced braces")


PRELUDE = (
    "const h = (s) => String(s == null ? '' : s);\n"
    "const shares = (v) => String(v);\n"
    "const hours = (v) => v == null ? '-' : String(v);\n"
    # The live-book column the roster draws beside the ladder.  Stubbed
    # rather than extracted: it reads a different book, and nothing it
    # prints is a rung.
    "const LIVE_BY_SD = {};\n"
    "function liveCell(){ return '<td></td>'; }\n"
)


def _run(payload: dict, body: str, call: str) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI images all carry node
        pytest.skip("node is required to run the board's renderer")
    script = (
        "const D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        + PRELUDE + _helpers() + "\n" + body + "\n"
        + f"console.log({call});\n"
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _order(html: str, labels: list[str]) -> list[str]:
    """The labels that made it into the output, in the order they were drawn."""

    seen = [(html.index(label), label) for label in labels if label in html]
    return [label for _, label in sorted(seen)]


def test_the_comparator_places_every_label_shape_this_book_mints() -> None:
    out = _run(
        {},
        "",
        "JSON.stringify([\n"
        "  rungSorted(['19°C','20°C','18°C']),\n"
        "  rungSorted(['78-79°F','80-81°F','76-77°F']),\n"
        # Lexical order puts 100-101 first; the ladder does not.
        "  rungSorted(['96-97°F','100-101°F','98-99°F']),\n"
        # The open ends sit outside the degree they name.
        "  rungSorted(['17°C','21°C or higher','17°C or below','18°C']),\n"
        "  rungSorted(['18°C','>=21°C','<=17°C','19°C']),\n"
        "  rungSorted(['-3°C','-5°C','0°C','2°C']),\n"
        # A label with no number in it sorts last and keeps its own order.
        "  rungSorted(['zzz','20°C','aaa','18°C']),\n"
        "  rungSorted(null),\n"
        "])",
    )
    assert json.loads(out) == [
        ["18°C", "19°C", "20°C"],
        ["76-77°F", "78-79°F", "80-81°F"],
        ["96-97°F", "98-99°F", "100-101°F"],
        ["17°C or below", "17°C", "18°C", "21°C or higher"],
        ["<=17°C", "18°C", "19°C", ">=21°C"],
        ["-5°C", "-3°C", "0°C", "2°C"],
        ["18°C", "20°C", "zzz", "aaa"],
        [],
    ]


def _live_payload(buckets: list[str]) -> dict:
    """One live basket whose legs arrive in the order the query returns them."""

    return {
        "live_readiness": {
            "available": True,
            "execution_mode": "production_live",
            "contracts": {"live_submit_authorized": True},
            "live_orders": {
                "intent_count": len(buckets),
                "rows": [
                    {
                        "order_intent_id": f"oi{index}",
                        "decision_id": "d1",
                        "bucket": bucket,
                        "limit_price": 0.2,
                        "requested_size": 5,
                        "requested_notional_usdc": 1.0,
                        "state": "pending",
                        "created_at": f"2026-09-13T0{index}:00:00Z",
                        "station_id": "EPWA",
                        "event_date": "2026-09-13",
                        "settled_bucket": "",
                        "reason": "live_intent_disarmed",
                    }
                    for index, bucket in enumerate(buckets)
                ],
                "plan_state": {},
                "book": {},
                "exchange": {},
                "pnl": {},
            },
            "open_orders": {"available": True, "count": 0},
        }
    }


def test_live_basket_legs_are_drawn_cold_to_warm() -> None:
    """The regression: legs arrive newest-intent-first, not ladder order."""

    scrambled = ["19°C", "20°C", "18°C"]
    html = _run(
        _live_payload(scrambled),
        _extract("liveOrderCard"),
        "liveOrderCard(D.live_readiness)",
    )
    assert _order(html, scrambled) == ["18°C", "19°C", "20°C"]


def test_live_basket_legs_sort_on_the_rung_not_the_string() -> None:
    """A Fahrenheit book steps two degrees, so its labels sort lexically wrong."""

    scrambled = ["100-101°F", "96-97°F", "98-99°F"]
    html = _run(
        _live_payload(scrambled),
        _extract("liveOrderCard"),
        "liveOrderCard(D.live_readiness)",
    )
    assert _order(html, scrambled) == ["96-97°F", "98-99°F", "100-101°F"]


def _roster_payload(buckets: list[str]) -> dict:
    """The live roster (2026-09-16): one station-day, one stage, scrambled."""

    return {
        "live_roster": {"2026-09-13": [{
            "station_id": "EPWA",
            "event_date": "2026-09-13",
            "station_label": "",
            "market_slug": "",
            "settled_bucket": None,
            "settled_source": None,
            "posted_legs": len(buckets),
            "filled_legs": len(buckets),
            "staked_usdc": 3.0,
            "payout_usdc": None,
            "pnl_usdc": None,
            "held_hit": None,
            "basket_hit": None,
            "stages": {"P0": {
                "baskets": 1,
                "posted_legs": len(buckets),
                "filled_legs": len(buckets),
                "stake_usdc": 3.0,
                "claimed_win_probability": 0.5,
                "basket_all_in_cost": 0.5,
                "claimed_ev_usdc": 0.0,
                "held_shares": {bucket: 5.0 for bucket in buckets},
                "held_buckets": sorted(buckets),
                "intended_buckets": sorted(buckets),
                "buckets": [
                    {"bucket": bucket, "posted_size": 5.0, "legs": 1,
                     "filled_size": 5.0, "filled_notional_usdc": 1.0,
                     "claimed_probability": 0.2}
                    for bucket in buckets
                ],
            }},
        }]},
    }


def test_the_posted_ladder_and_its_legs_are_drawn_cold_to_warm() -> None:
    """The roster's ladder and its per-leg table, both off scrambled input."""

    scrambled = ["20°C", "18°C", "19°C"]
    html = _run(_roster_payload(scrambled), _extract("roster"), "roster()")
    # The ladder line: "18+19+20", with the unit stripped as the cell does.
    ladder = html[:html.index("<details")]
    assert _order(ladder, ["18<", "19<", "20<"]) == ["18<", "19<", "20<"]
    # And the per-leg table under it, which reads the same list.
    table = html[html.index("<tbody>"):]
    assert _order(table, scrambled) == ["18°C", "19°C", "20°C"]


def test_convergence_candidate_buckets_are_drawn_cold_to_warm() -> None:
    scrambled = ["27°C", "25°C", "26°C"]
    html = _run(
        {"convergence_claim": {"examples": [{
            "station_id": "ZUUU", "event_date": "2026-09-13", "stage": "P0",
            "at": "2026-09-13T00:00", "buckets": scrambled, "claim": 0.4,
            "price": 0.3, "agreement": 0.5, "claim_rise": 0.01,
            "price_fall": 0.01, "lifted_edge": 0.02, "flips_gate": False,
        }]}},
        _extract("convergenceClaim"),
        "convergenceClaim()",
    )
    assert _order(html, scrambled) == ["25°C", "26°C", "27°C"]
