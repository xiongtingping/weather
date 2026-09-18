"""Two scope refusals from 2026-09-18, one test each side of the hop.

1. ``probability_p1_source_lane_mismatch`` on US station-days.  An AWC
   station-day's P1 may be refreshed on an intra-hour ASOS carrier and then
   names that lane (``MADIS_HFMETAR``, report ``ASOS_HF``).  The P1 writer,
   the SSOT loader and the worker admitted it; the runtime optimizer's own
   gate compared lanes for equality, so whenever the newest P1 was a carrier
   refresh the whole scope was refused until the next METAR P1 superseded it
   (10:52Z cycle: 7 of the 10 US station-days).
2. ``production_inventory_version_drift`` on station-days two writers stamp
   at one instant.  Resident ingress re-rendered the quote's
   ``.089Z`` as ``.089000Z`` on the inventory and order-state rows; the
   loader orders each table by the string, so the quote table's newest row
   came from one writer and the inventory table's from the other.
"""

from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, ProbabilityEnvelope
from bayes_basket_target.metar_path_state import (
    SOFT_PATH_SOURCE_LANES,
    admissible_p1_source_lanes,
)
from bayes_basket_target.production_io import compile_production_cycle_from_ssot
from bayes_basket_target.resident_market_ingress import _timestamp as ingress_timestamp
from bayes_basket_target.runtime import _assert_current_p1_source_lane
from tests.test_bayes_basket_target_production_io import (
    _cash_authority,
    _envelope,
    _insert_probability_row,
    _seed,
    _seed_delta_v2,
    _seed_target_registry_lane,
    _stage_envelope,
)


def _p1(lane: str) -> ProbabilityEnvelope:
    base = _stage_envelope(
        _envelope(), stage="P1", checkpoint_id="p1-lane",
        decision_at="2026-07-23T05:00:01Z", fresh_until="2026-07-23T06:00:00Z",
    )
    return ProbabilityEnvelope.from_mapping({
        **base.to_dict(),
        "lineage": {**dict(base.lineage), "source_lane": lane, "prediction_root_stage": "P0"},
    })


def test_the_admissible_lanes_are_one_rule() -> None:
    assert admissible_p1_source_lanes("AWC") == {"awc"} | SOFT_PATH_SOURCE_LANES
    assert admissible_p1_source_lanes("awc") == {"awc", "asos_1min", "madis_hfmetar"}
    assert admissible_p1_source_lanes("amsc") == {"amsc"}


@pytest.mark.parametrize(("registry", "lane", "admitted"), [
    ("awc", "AWC", True),
    ("awc", "MADIS_HFMETAR", True),
    ("awc", "ASOS_1MIN", True),
    ("awc", "AMSC", False),
    ("amsc", "AMSC", True),
    ("amsc", "MADIS_HFMETAR", False),
    ("amsc", "AWC", False),
])
def test_the_runtime_gate_admits_what_the_loader_admits(registry: str, lane: str, admitted: bool) -> None:
    if admitted:
        _assert_current_p1_source_lane(_p1(lane), expected_source_lane=registry)
    else:
        with pytest.raises(ContractError, match="probability_p1_source_lane_mismatch"):
            _assert_current_p1_source_lane(_p1(lane), expected_source_lane=registry)


def _compile_outcome(registry: str, lane: str) -> str:
    with sqlite3.connect(":memory:") as conn:
        _seed(conn, cash_promoted=False)
        _seed_delta_v2(conn)
        _seed_target_registry_lane(conn, source_lane=registry)
        _insert_probability_row(conn, _p1(lane), created_at="2026-07-23T05:00:01Z")
        try:
            compile_production_cycle_from_ssot(
                conn, station_id="ZUUU", event_date="2026-07-23",
                evaluation_at_utc="2026-07-23T05:00:04Z", season_bin="summer",
                maturity_bin="d1", liquidity_bin="liquid", strategy_version="target-v1",
                policy_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
                base_policy={"clip_max_notional_usdc": 3.0},
                cash_phase_authority_manifest=_cash_authority(
                    conn, authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
                ),
            )
        except ContractError as exc:
            return str(exc)
        return "compiled"


def test_a_carrier_refreshed_p1_compiles_exactly_as_far_as_a_metar_p1() -> None:
    """Across the loader and the runtime: a US station-day whose newest P1 is
    a carrier refresh goes wherever the same P1 on the registry's own lane
    goes, and never stops on the lane."""

    carrier = _compile_outcome("awc", "MADIS_HFMETAR")
    assert carrier != "probability_p1_source_lane_mismatch"
    assert carrier == _compile_outcome("awc", "AWC")
    # An AMSC station-day still refuses a carrier P1.
    assert _compile_outcome("amsc", "MADIS_HFMETAR") == "probability_p1_source_lane_mismatch"


@pytest.mark.parametrize("stamp", [
    "2026-09-18T10:56:54.089Z",
    "2026-09-18T10:56:54Z",
    "2026-09-18T10:56:54.000Z",
    "2026-09-18T10:56:54.089123Z",
])
def test_ingress_keeps_a_utc_stamp_exactly_as_the_quote_carries_it(stamp: str) -> None:
    assert ingress_timestamp(stamp) == stamp


def test_ingress_renders_only_other_offsets() -> None:
    assert ingress_timestamp("2026-09-18T19:56:54.5+09:00") == "2026-09-18T10:56:54.500000Z"
    with pytest.raises(ContractError):
        ingress_timestamp("2026-09-18T10:56:54")


def test_two_generations_at_one_instant_pair_up_in_every_table() -> None:
    """The loader's own ordering, on the rows the 10:56Z cycle wrote for
    EDDM 09-19: P0 preselection's pair first, the ingress pair after it, all
    at one instant.  With one stamp form the newest quote and the newest
    inventory are the same writer's; with the ``.089000Z`` form they were not."""

    from bayes_basket_target.production_io import _latest_snapshot_row

    def newest(stamp_ingress: str) -> tuple[str, str]:
        conn = sqlite3.connect(":memory:")
        for table in ("basket_official_ws_snapshot", "basket_confirmed_inventory_snapshot"):
            conn.execute(f"CREATE TABLE {table}(station_id TEXT, event_date TEXT, state TEXT, observed_at TEXT, inventory_version TEXT)")
            conn.execute(f"INSERT INTO {table} VALUES('EDDM','2026-09-19','COMMITTED','2026-09-18T10:56:54.089Z','p0_preselection_ledger_c7cd')")
            ingress_stamp = stamp_ingress if table == "basket_confirmed_inventory_snapshot" else "2026-09-18T10:56:54.089Z"
            conn.execute(f"INSERT INTO {table} VALUES('EDDM','2026-09-19','COMMITTED',?,'invrec_ingress_7609')", (ingress_stamp,))
        at = "2026-09-18T10:57:40Z"
        quote = _latest_snapshot_row(conn, "basket_official_ws_snapshot", "EDDM", "2026-09-19", at)
        inventory = _latest_snapshot_row(conn, "basket_confirmed_inventory_snapshot", "EDDM", "2026-09-19", at)
        return str(quote["inventory_version"]), str(inventory["inventory_version"])

    q, i = newest(ingress_timestamp("2026-09-18T10:56:54.089Z"))
    assert q == i == "invrec_ingress_7609"
    # What the old rendering produced: the pair split across writers.
    q_old, i_old = newest("2026-09-18T10:56:54.089000Z")
    assert q_old != i_old
