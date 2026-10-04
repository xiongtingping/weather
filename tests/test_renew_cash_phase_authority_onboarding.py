"""A station onboarded after the cash-phase table was seeded gets the fleet's template.

LLBG, LTFM and UUWW joined the fleet on 2026-10-04 with no binding, and every
snapshot of theirs was refused on resident_cash_phase_authority_manifest_not_unique
(zero rows): three blocked scopes a cycle.  Only a uniform template may be
carried; anything else would be one station's policy chosen for another.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "renew_cash_phase_authority_window",
    ROOT / "scripts" / "renew_cash_phase_authority_window.py",
)
renew = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(renew)


def _binding(station: str, tz: str, **overrides) -> dict:
    row = {
        "policy_version": f"clip-candidate-{station}-heavy-open-v1@20260823",
        "station_id": station,
        "station_timezone": tz,
        "heavy_to_standard_local_time": "19:54",
        "standard_to_tail_local_time": "09:58",
        "cash_by_phase": {"HEAVY": 16.16, "STANDARD": 10.77, "TAIL": 0.25},
        "evidence_status": "DESIGN_CANDIDATE",
        "promoted": False,
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "valid_from_utc": "2026-08-23T00:00:00Z",
        "valid_until_utc": "2027-02-23T00:00:00Z",
    }
    row.update(overrides)
    return row


FLEET = [_binding("EGLC", "Europe/London"), _binding("LTAC", "Europe/Istanbul")]


def test_the_template_is_issued_under_each_stations_own_name_and_timezone() -> None:
    packet = renew.build_onboarding_packet(
        FLEET,
        station_timezones={"LLBG": "Asia/Jerusalem", "UUWW": "Europe/Moscow"},
        valid_from_utc="2026-10-04T16:00:00Z",
        scope="PRODUCTION_NO_LIVE_CANDIDATE",
    )
    policies = {row["station_id"]: row for row in packet["cash_phase_policies"]}
    assert set(policies) == {"LLBG", "UUWW"}
    llbg = policies["LLBG"]
    assert llbg["policy_version"] == "clip-candidate-LLBG-heavy-open-v1@20261004"
    assert llbg["station_timezone"] == "Asia/Jerusalem"
    assert llbg["cash_by_phase"] == FLEET[0]["cash_by_phase"]
    # Never backdated; ends with the fleet so one renewal carries everyone.
    assert llbg["valid_from_utc"] == "2026-10-04T16:00:00Z"
    assert llbg["valid_until_utc"] == "2027-02-23T00:00:00Z"
    manifests = {row["station_id"]: row for row in packet["cash_phase_authority_manifests"]}
    assert manifests["LLBG"]["artifact_sha256"] == llbg["content_sha256"]
    assert packet["packet_sha256"]


def test_a_template_that_is_not_uniform_is_refused() -> None:
    fleet = FLEET + [_binding("KLGA", "America/New_York", cash_by_phase={"HEAVY": 1.0, "STANDARD": 1.0, "TAIL": 1.0})]
    with pytest.raises(SystemExit):
        renew.build_onboarding_packet(
            fleet, station_timezones={"LLBG": "Asia/Jerusalem"},
            valid_from_utc="2026-10-04T16:00:00Z", scope="PRODUCTION_NO_LIVE_CANDIDATE",
        )


def test_versions_that_do_not_share_one_name_pattern_are_refused() -> None:
    fleet = FLEET + [_binding("KLGA", "America/New_York", policy_version="other-KLGA-v2@20260823")]
    with pytest.raises(SystemExit):
        renew.build_onboarding_packet(
            fleet, station_timezones={"LLBG": "Asia/Jerusalem"},
            valid_from_utc="2026-10-04T16:00:00Z", scope="PRODUCTION_NO_LIVE_CANDIDATE",
        )


def test_a_station_that_already_has_a_binding_is_refused() -> None:
    with pytest.raises(SystemExit):
        renew.build_onboarding_packet(
            FLEET, station_timezones={"EGLC": "Europe/London"},
            valid_from_utc="2026-10-04T16:00:00Z", scope="PRODUCTION_NO_LIVE_CANDIDATE",
        )
