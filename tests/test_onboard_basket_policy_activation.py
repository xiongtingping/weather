"""A station onboarded late gets the fleet's twelve basket policy activations.

Without them every compile of LLBG, LTFM and UUWW was refused on
production_basket_policy_artifacts_missing and no decision fact was written.
Only a uniform template is carried.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "onboard_basket_policy_activation",
    ROOT / "scripts" / "onboard_basket_policy_activation.py",
)
onboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(onboard)


def _rows(station: str, **overrides) -> list[dict]:
    rows = []
    for stage, token in (("P0", "p0"), ("P1", "p1")):
        for maturity in ("d0", "d1"):
            row = {
                "activation_id": f"target-v2-{station.lower()}-no-live-{token}-{maturity}-g5",
                "station_id": station,
                "stage": stage,
                "season_bin": "summer",
                "maturity_bin": maturity,
                "liquidity_bin": "liquid",
                "strategy_version": "target-v2",
                "global_prior_id": "target-v2-global-no-live-prior-g5",
                "target_artifact_id": None,
                "reserve_artifact_id": None,
                "cadence_artifact_id": None,
                "fallback_families_json": json.dumps(["target", "reserve", "cadence"]),
                "generation": 5,
                "valid_from_utc": "2026-08-23T00:00:00Z",
                "valid_until_utc": "2027-02-23T00:00:00Z",
            }
            row.update(overrides)
            rows.append(row)
    return rows


def test_each_template_row_is_issued_under_the_new_stations_name() -> None:
    packet = onboard.build_onboarding_packet(
        _rows("EGLC") + _rows("LTAC"), stations=["LLBG"], valid_from_utc="2026-10-04T16:00:00Z",
    )
    issued = packet["policy_activations"]
    assert sorted(row["activation_id"] for row in issued) == [
        "target-v2-llbg-no-live-p0-d0-g5", "target-v2-llbg-no-live-p0-d1-g5",
        "target-v2-llbg-no-live-p1-d0-g5", "target-v2-llbg-no-live-p1-d1-g5",
    ]
    assert {row["station_id"] for row in issued} == {"LLBG"}
    assert {row["valid_from_utc"] for row in issued} == {"2026-10-04T16:00:00Z"}
    assert {row["valid_until_utc"] for row in issued} == {"2027-02-23T00:00:00Z"}
    assert {row["promoted"] for row in issued} == {False}
    assert packet["policy_authority_scope"] == "PRODUCTION_NO_LIVE_CANDIDATE"


def test_a_fleet_that_is_not_uniform_is_refused() -> None:
    rows = _rows("EGLC") + _rows("LTAC", global_prior_id="something-else")
    with pytest.raises(SystemExit):
        onboard.build_onboarding_packet(rows, stations=["LLBG"], valid_from_utc="2026-10-04T16:00:00Z")


def test_a_station_that_already_has_activations_is_refused() -> None:
    with pytest.raises(SystemExit):
        onboard.build_onboarding_packet(
            _rows("EGLC"), stations=["EGLC"], valid_from_utc="2026-10-04T16:00:00Z",
        )
