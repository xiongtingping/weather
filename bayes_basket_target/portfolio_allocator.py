"""Split one bankroll across stations, charging for shared weather.

Today every station plans against the whole pool: the per-station budget and
the pool balance are both 20 USDC, so forty-five baskets each size themselves
as though they owned all of it.  Nothing over-commits only because almost
nothing trades.

This allocator makes the split explicit.  Each station reports what a dollar
buys it -- expected PnL per dollar, and the standard deviation of the
settled-outcome curve per dollar -- and the allocator maximizes

    sum_i e_i w_i  -  [ sum_b ( sum_{i in b} s_i w_i )^2 ] / (2 W)

over quotas ``w``, subject to the portfolio risk budget and each station's
hard cap.

The bracket is the point.  Variance adds across blocks but *sums inside* one
before squaring, so two cities under the same air mass are charged as one
position rather than two half-sized ones.  That is deliberately conservative
and needs no estimated correlation matrix: with a month of history a 45x45
matrix would be mostly noise, while "these cities share weather" is knowable
from geography and stays true.

The objective is concave and the feasible set convex, so projected gradient
with a diminishing step converges, and the fixed iteration count keeps the
result deterministic -- the same evidence must always produce the same
quotas, because the quotas are content-addressed into a receipt.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .contracts import ContractError, content_sha256

SCHEMA = "basket_target_portfolio_allocation_v1"
BLOCKS_SCHEMA = "basket_target_station_regional_blocks_v1"
DEFAULT_BLOCKS_PATH = (
    Path(__file__).resolve().parent.parent
    / "config/trading/station_regional_blocks_v1.json"
)
# Fraction of the bankroll that may be at risk across all stations at once.
DEFAULT_PORTFOLIO_RISK_FRACTION = 0.35
# No single station may hold more than this share of the bankroll, whatever
# its measured edge -- a station whose edge is an artefact of a miscalibrated
# model must not be able to take the book with it.
DEFAULT_STATION_CAP_FRACTION = 0.25
_ITERATIONS = 500
_TOLERANCE = 1e-9

PORTFOLIO_ALLOCATION_DDL = """
CREATE TABLE IF NOT EXISTS basket_portfolio_allocation (
  allocation_id TEXT PRIMARY KEY,
  as_of_utc TEXT NOT NULL,
  bankroll_usdc REAL NOT NULL CHECK(bankroll_usdc>=0),
  risk_budget_usdc REAL NOT NULL CHECK(risk_budget_usdc>=0),
  station_count INTEGER NOT NULL CHECK(station_count>=0),
  allocated_usdc REAL NOT NULL CHECK(allocated_usdc>=0),
  portfolio_sigma_usdc REAL NOT NULL CHECK(portfolio_sigma_usdc>=0),
  naive_sigma_usdc REAL NOT NULL CHECK(naive_sigma_usdc>=0),
  quota_json TEXT NOT NULL,
  block_exposure_json TEXT NOT NULL,
  blocks_content_sha256 TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS basket_portfolio_allocation_as_of_idx
  ON basket_portfolio_allocation(as_of_utc);
CREATE TRIGGER IF NOT EXISTS basket_portfolio_allocation_no_update
  BEFORE UPDATE ON basket_portfolio_allocation
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_portfolio_allocation_no_delete
  BEFORE DELETE ON basket_portfolio_allocation
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


def load_regional_blocks(
    path: Path | str = DEFAULT_BLOCKS_PATH,
) -> tuple[dict[str, str], str]:
    """Return (station -> block) and the config's content address."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != BLOCKS_SCHEMA:
        raise ContractError("portfolio_blocks_schema_invalid")
    blocks = raw.get("blocks")
    if not isinstance(blocks, Mapping) or not blocks:
        raise ContractError("portfolio_blocks_empty")
    by_station: dict[str, str] = {}
    for block, members in blocks.items():
        if not isinstance(members, Sequence) or isinstance(members, str):
            raise ContractError("portfolio_blocks_member_list_invalid")
        for station in members:
            key = str(station).strip().upper()
            if key in by_station:
                # A station in two blocks would be charged twice for the same
                # weather and its quota would depend on iteration order.
                raise ContractError("portfolio_blocks_station_duplicated")
            by_station[key] = str(block)
    return by_station, content_sha256(raw)


def allocate_portfolio_quota(
    *,
    station_edges: Sequence[Mapping[str, Any]],
    blocks_by_station: Mapping[str, str],
    bankroll_usdc: float,
    risk_fraction: float = DEFAULT_PORTFOLIO_RISK_FRACTION,
    station_cap_fraction: float = DEFAULT_STATION_CAP_FRACTION,
    default_block: str = "UNBLOCKED_SINGLETON",
) -> dict[str, Any]:
    """Solve the quota split; pure, deterministic, no I/O.

    ``station_edges`` rows carry ``station_id``, ``edge_per_usdc`` and
    ``sigma_per_usdc``.  A station with no measurable edge is admitted with a
    zero quota rather than dropped, so the receipt still accounts for it.
    """

    bankroll = float(bankroll_usdc)
    if not math.isfinite(bankroll) or bankroll < 0.0:
        raise ContractError("portfolio_bankroll_invalid")
    if not (0.0 < risk_fraction <= 1.0):
        raise ContractError("portfolio_risk_fraction_invalid")
    if not (0.0 < station_cap_fraction <= 1.0):
        raise ContractError("portfolio_station_cap_fraction_invalid")

    stations: list[str] = []
    edge: list[float] = []
    sigma: list[float] = []
    block_of: list[str] = []
    for row in station_edges:
        station = str(row.get("station_id") or "").strip().upper()
        if not station:
            raise ContractError("portfolio_station_id_missing")
        if station in stations:
            raise ContractError("portfolio_station_duplicated")
        e = float(row.get("edge_per_usdc") or 0.0)
        s = float(row.get("sigma_per_usdc") or 0.0)
        if not math.isfinite(e) or not math.isfinite(s) or s < 0.0:
            raise ContractError("portfolio_station_edge_invalid")
        stations.append(station)
        edge.append(e)
        sigma.append(s)
        block_of.append(
            str(blocks_by_station.get(station) or f"{default_block}:{station}")
        )

    n = len(stations)
    risk_budget = bankroll * risk_fraction
    station_cap = bankroll * station_cap_fraction
    if n == 0 or bankroll <= 0.0:
        return _receipt(
            stations, [0.0] * n, edge, sigma, block_of,
            bankroll=bankroll, risk_budget=risk_budget,
            minimum=[0.0] * n,
        )

    members: dict[str, list[int]] = {}
    for index, block in enumerate(block_of):
        members.setdefault(block, []).append(index)

    def _project(raw: Sequence[float]) -> list[float]:
        clipped = [min(max(0.0, value), station_cap) for value in raw]
        total = sum(clipped)
        if total <= risk_budget or total <= 0.0:
            return clipped
        scale = risk_budget / total
        return [value * scale for value in clipped]

    minimum = [
        max(0.0, float(row.get("minimum_basket_usdc") or 0.0))
        for row in station_edges
    ]
    weights = [0.0] * n
    step = risk_budget / max(1.0, float(n))
    for iteration in range(_ITERATIONS):
        block_exposure = {
            block: sum(sigma[i] * weights[i] for i in idx)
            for block, idx in members.items()
        }
        gradient = [
            edge[i] - sigma[i] * block_exposure[block_of[i]] / bankroll
            for i in range(n)
        ]
        proposal = _project([
            weights[i] + step * gradient[i] for i in range(n)
        ])
        movement = max(
            (abs(a - b) for a, b in zip(proposal, weights)), default=0.0
        )
        weights = proposal
        if movement < _TOLERANCE * max(1.0, risk_budget):
            break
        step = (risk_budget / max(1.0, float(n))) / math.sqrt(1.0 + iteration)
    weights = _fund_whole_baskets(
        weights,
        minimum=minimum,
        risk_budget=risk_budget,
        station_cap=station_cap,
    )
    return _receipt(
        stations, weights, edge, sigma, block_of,
        bankroll=bankroll, risk_budget=risk_budget, minimum=minimum,
    )


def _fund_whole_baskets(
    weights: Sequence[float],
    *,
    minimum: Sequence[float],
    risk_budget: float,
    station_cap: float,
) -> list[float]:
    """Round the continuous split onto what a basket can actually be.

    A basket is atomic: every support bucket takes at least the minimum clip
    or the station holds.  So a quota below that minimum buys nothing at all
    -- capital parked there is not a small position, it is an idle one.  The
    continuous optimum is therefore spent in order of its own preference,
    each funded station raised to its whole-basket minimum, and the first
    station that no longer fits ends the list rather than being part-funded.
    """

    order = sorted(
        range(len(weights)), key=lambda i: (-weights[i], i)
    )
    funded = [0.0] * len(weights)
    remaining = risk_budget
    for index in order:
        need = max(minimum[index], 0.0)
        if need <= 0.0 or weights[index] <= 0.0:
            continue
        grant = min(max(weights[index], need), station_cap)
        if grant > remaining + _TOLERANCE:
            grant = remaining if remaining + _TOLERANCE >= need else 0.0
        if grant < need - _TOLERANCE:
            continue
        funded[index] = grant
        remaining -= grant
    return funded


def derive_minimum_bankroll(
    *,
    station_edges: Sequence[Mapping[str, Any]],
    risk_fraction: float = DEFAULT_PORTFOLIO_RISK_FRACTION,
) -> dict[str, Any]:
    """What the book must hold for the strategy to run as configured.

    Not a preference -- an arithmetic consequence.  A station can only act if
    it can fund one whole atomic basket, so covering every station at once
    costs the sum of their basket minima, and the risk fraction means the
    bankroll has to be larger than that sum by its reciprocal.

    Reported rather than enforced: the allocator funds whoever fits and names
    the rest, so an undersized book degrades to fewer stations instead of
    failing, and the number to top up to is always visible.
    """

    minima = [
        max(0.0, float(row.get("minimum_basket_usdc") or 0.0))
        for row in station_edges
    ]
    total_minimum = sum(minima)
    if not (0.0 < risk_fraction <= 1.0):
        raise ContractError("portfolio_risk_fraction_invalid")
    return {
        "schema": "basket_target_portfolio_minimum_bankroll_v1",
        "station_count": len(minima),
        "station_minimum_usdc_total": round(total_minimum, 6),
        "largest_station_minimum_usdc": round(max(minima, default=0.0), 6),
        "risk_fraction": risk_fraction,
        # Every station fundable at once.
        "minimum_bankroll_all_stations_usdc": round(
            total_minimum / risk_fraction, 6
        ),
        # The floor below which nothing can trade at all.
        "minimum_bankroll_any_station_usdc": round(
            max(minima, default=0.0) / risk_fraction, 6
        ),
    }


def fundable_station_count(
    *,
    bankroll_usdc: float,
    station_edges: Sequence[Mapping[str, Any]],
    risk_fraction: float = DEFAULT_PORTFOLIO_RISK_FRACTION,
) -> int:
    """How many whole baskets this bankroll covers, cheapest first."""

    budget = float(bankroll_usdc) * risk_fraction
    count = 0
    for need in sorted(
        max(0.0, float(row.get("minimum_basket_usdc") or 0.0))
        for row in station_edges
    ):
        if need <= 0.0 or need > budget + _TOLERANCE:
            break
        budget -= need
        count += 1
    return count


def _receipt(
    stations: Sequence[str],
    weights: Sequence[float],
    edge: Sequence[float],
    sigma: Sequence[float],
    block_of: Sequence[str],
    *,
    bankroll: float,
    risk_budget: float,
    minimum: Sequence[float] | None = None,
) -> dict[str, Any]:
    block_exposure: dict[str, float] = {}
    for index, block in enumerate(block_of):
        block_exposure[block] = (
            block_exposure.get(block, 0.0) + sigma[index] * weights[index]
        )
    portfolio_variance = sum(value ** 2 for value in block_exposure.values())
    # What the same quotas would look like if every station were independent:
    # the gap between the two is the diversification the blocks refuse to
    # credit, and is the number to watch when the block map is revisited.
    naive_variance = sum(
        (sigma[i] * weights[i]) ** 2 for i in range(len(stations))
    )
    quota = {
        station: round(float(weight), 6)
        for station, weight in zip(stations, weights)
    }
    funded = [i for i, w in enumerate(weights) if w > _TOLERANCE]
    return {
        "schema": SCHEMA,
        "bankroll_usdc": round(bankroll, 6),
        "risk_budget_usdc": round(risk_budget, 6),
        "station_count": len(stations),
        "funded_station_count": len(funded),
        "unfunded_station_ids": [
            stations[i] for i in range(len(stations))
            if weights[i] <= _TOLERANCE
        ],
        "station_minimum_usdc_total": round(
            sum(minimum or ()), 6
        ),
        "allocated_usdc": round(sum(weights), 6),
        "portfolio_sigma_usdc": round(portfolio_variance ** 0.5, 6),
        "naive_sigma_usdc": round(naive_variance ** 0.5, 6),
        "expected_pnl_usdc": round(
            sum(edge[i] * weights[i] for i in range(len(stations))), 6
        ),
        "quota_usdc_by_station": quota,
        "block_exposure_usdc": {
            block: round(value, 6)
            for block, value in sorted(block_exposure.items())
        },
    }


def persist_portfolio_allocation(
    conn: sqlite3.Connection,
    allocation: Mapping[str, Any],
    *,
    as_of_utc: str,
    blocks_content_sha256: str,
    created_at_utc: str,
) -> int:
    """Append one allocation receipt; identical evidence replays as a no-op."""

    conn.executescript(PORTFOLIO_ALLOCATION_DDL)
    identity = {
        key: allocation[key]
        for key in (
            "schema", "bankroll_usdc", "risk_budget_usdc", "station_count",
            "allocated_usdc", "portfolio_sigma_usdc", "naive_sigma_usdc",
            "quota_usdc_by_station", "block_exposure_usdc",
        )
    }
    identity["as_of_utc"] = str(as_of_utc)
    identity["blocks_content_sha256"] = str(blocks_content_sha256)
    sha = content_sha256(identity)
    conn.execute(
        "INSERT OR IGNORE INTO basket_portfolio_allocation("
        "allocation_id,as_of_utc,bankroll_usdc,risk_budget_usdc,"
        "station_count,allocated_usdc,portfolio_sigma_usdc,naive_sigma_usdc,"
        "quota_json,block_exposure_json,blocks_content_sha256,"
        "content_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "portalloc-" + sha[:32], str(as_of_utc),
            float(allocation["bankroll_usdc"]),
            float(allocation["risk_budget_usdc"]),
            int(allocation["station_count"]),
            float(allocation["allocated_usdc"]),
            float(allocation["portfolio_sigma_usdc"]),
            float(allocation["naive_sigma_usdc"]),
            json.dumps(
                allocation["quota_usdc_by_station"], sort_keys=True
            ),
            json.dumps(allocation["block_exposure_usdc"], sort_keys=True),
            str(blocks_content_sha256), sha, str(created_at_utc),
        ),
    )
    return int(conn.execute("SELECT changes()").fetchone()[0])
