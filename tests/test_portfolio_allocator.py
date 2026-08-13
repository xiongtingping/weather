"""Quota allocation: block correlation, whole baskets, and the capital floor."""

from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.portfolio_allocator import (
    allocate_portfolio_quota,
    derive_minimum_bankroll,
    fundable_station_count,
    load_regional_blocks,
    persist_portfolio_allocation,
)


def _edges(*specs):
    return [
        {
            "station_id": station,
            "edge_per_usdc": edge,
            "sigma_per_usdc": sigma,
            "minimum_basket_usdc": minimum,
        }
        for station, edge, sigma, minimum in specs
    ]


def test_shipped_blocks_cover_every_station_exactly_once() -> None:
    blocks, sha = load_regional_blocks()
    assert len(sha) == 64
    assert len(blocks) == 45
    assert blocks["ZUUU"] == blocks["ZUCK"]          # one air mass
    assert blocks["KLAX"] != blocks["KLGA"]          # opposite coasts


def test_shared_weather_is_charged_once_not_twice() -> None:
    """Two stations in a block must not look like diversification."""

    same_block = allocate_portfolio_quota(
        station_edges=_edges(("ZUUU", 0.5, 2.0, 1.0), ("ZUCK", 0.5, 2.0, 1.0)),
        blocks_by_station={"ZUUU": "CHINA_CENTRAL", "ZUCK": "CHINA_CENTRAL"},
        bankroll_usdc=100.0,
    )
    apart = allocate_portfolio_quota(
        station_edges=_edges(("ZUUU", 0.5, 2.0, 1.0), ("KLAX", 0.5, 2.0, 1.0)),
        blocks_by_station={"ZUUU": "CHINA_CENTRAL", "KLAX": "NA_WEST"},
        bankroll_usdc=100.0,
    )
    # Same evidence, same edge: the pair under one air mass is charged the
    # sum of its exposures before squaring, so it is allowed less capital.
    # Its total sigma ends up lower precisely because it holds less -- the
    # charge shows up against the independent reading of its own position,
    # not against the other allocation.
    assert same_block["allocated_usdc"] < apart["allocated_usdc"]
    # Independent blocks are exactly the naive case; a shared block is worse.
    assert apart["portfolio_sigma_usdc"] == pytest.approx(
        apart["naive_sigma_usdc"]
    )
    assert same_block["portfolio_sigma_usdc"] > same_block["naive_sigma_usdc"]


def test_quota_funds_whole_baskets_or_nothing() -> None:
    """A quota below a station's atomic minimum buys nothing, so is not given."""

    allocation = allocate_portfolio_quota(
        station_edges=_edges(
            ("AAAA", 0.9, 1.0, 4.0),
            ("BBBB", 0.8, 1.0, 4.0),
            ("CCCC", 0.7, 1.0, 4.0),
        ),
        blocks_by_station={},
        bankroll_usdc=30.0,   # risk budget 10.5: two whole baskets, not three
    )
    quotas = allocation["quota_usdc_by_station"]
    assert all(
        value == 0.0 or value >= 4.0 - 1e-9 for value in quotas.values()
    )
    assert allocation["funded_station_count"] == 2
    assert set(allocation["unfunded_station_ids"]) == {"CCCC"}
    assert allocation["allocated_usdc"] <= 10.5 + 1e-9


def test_minimum_bankroll_is_derived_not_assumed() -> None:
    edges = _edges(
        ("AAAA", 0.5, 1.0, 4.0),
        ("BBBB", 0.5, 1.0, 6.0),
    )
    minimum = derive_minimum_bankroll(station_edges=edges, risk_fraction=0.5)
    assert minimum["station_minimum_usdc_total"] == 10.0
    assert minimum["largest_station_minimum_usdc"] == 6.0
    # Covering everything at once needs the sum grossed up by the fraction.
    assert minimum["minimum_bankroll_all_stations_usdc"] == 20.0
    # Below this nothing can trade at all.
    assert minimum["minimum_bankroll_any_station_usdc"] == 12.0
    assert fundable_station_count(
        bankroll_usdc=20.0, station_edges=edges, risk_fraction=0.5
    ) == 2
    assert fundable_station_count(
        bankroll_usdc=12.0, station_edges=edges, risk_fraction=0.5
    ) == 1
    assert fundable_station_count(
        bankroll_usdc=4.0, station_edges=edges, risk_fraction=0.5
    ) == 0


def test_caps_refusals_and_determinism() -> None:
    edges = _edges(("AAAA", 5.0, 0.1, 1.0), ("BBBB", 0.1, 0.1, 1.0))
    allocation = allocate_portfolio_quota(
        station_edges=edges, blocks_by_station={},
        bankroll_usdc=100.0, station_cap_fraction=0.05,
    )
    # No station takes the book however good its measured edge looks.
    assert allocation["quota_usdc_by_station"]["AAAA"] <= 5.0 + 1e-9
    # Same evidence, same quotas -- the receipt is content-addressed.
    assert allocation == allocate_portfolio_quota(
        station_edges=edges, blocks_by_station={},
        bankroll_usdc=100.0, station_cap_fraction=0.05,
    )
    # An empty book allocates nothing rather than failing.
    assert allocate_portfolio_quota(
        station_edges=edges, blocks_by_station={}, bankroll_usdc=0.0,
    )["allocated_usdc"] == 0.0
    with pytest.raises(ContractError):
        allocate_portfolio_quota(
            station_edges=edges, blocks_by_station={}, bankroll_usdc=-1.0,
        )
    with pytest.raises(ContractError):
        allocate_portfolio_quota(
            station_edges=_edges(("AAAA", 0.0, -1.0, 1.0)),
            blocks_by_station={}, bankroll_usdc=10.0,
        )


def test_allocation_receipt_is_append_only_and_idempotent() -> None:
    allocation = allocate_portfolio_quota(
        station_edges=_edges(("AAAA", 0.5, 1.0, 1.0)),
        blocks_by_station={}, bankroll_usdc=50.0,
    )
    conn = sqlite3.connect(":memory:")
    assert persist_portfolio_allocation(
        conn, allocation, as_of_utc="2026-08-12T04:00:00Z",
        blocks_content_sha256="a" * 64, created_at_utc="2026-08-12T04:00:00Z",
    ) == 1
    assert persist_portfolio_allocation(
        conn, allocation, as_of_utc="2026-08-12T04:00:00Z",
        blocks_content_sha256="a" * 64, created_at_utc="2026-08-12T04:00:00Z",
    ) == 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE basket_portfolio_allocation SET bankroll_usdc=1"
        )
