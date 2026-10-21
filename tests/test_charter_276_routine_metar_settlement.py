"""Charter 276: from 2026-10-23 the 32 non-US markets settle on AWC's routine
METARs only -- a report beginning SPECI does not count."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bayes_basket_target import settlement_basis
from bayes_basket_target.settlement_truth import materialize_settled_station_truth
from reported_trajectory_awc_ingress import ReportedTrajectoryAwcIngress
from reported_trajectory_market_registry import (
    AWC_FAMILY,
    NOAA_FAMILY,
    expected_settlement_contract,
    load_config,
    settlement_contract,
    settlement_contract_deviations,
)
from tests.test_bayes_basket_target_settlement_truth import _canonical, _observation, _target
from tests.test_reported_trajectory_awc_ingress import _conn, _poll

# The clauses of the chengdu 2026-10-23 rules (Gamma, read 2026-10-21) that
# decide the settled number, verbatim.
AWC_RULES = (
    "This market will resolve to the temperature range that contains the highest temperature reported "
    "at Chengdu Shuangliu International Airport (ZUUU) on 23 Oct '26.\n\n"
    "The qualifying value is the highest temperature reported in the hourly temperature remark of any "
    "routine (METAR) observation for Chengdu Shuangliu International Airport (ZUUU) on that date, in tenths "
    "of a degree Celsius as reported. A report whose line begins with SPECI is a special observation and "
    "does not count.\n\n"
    "Each market in this event states its range in whole degrees Celsius. The qualifying value is rounded to "
    "the nearest whole degree, with a value ending in exactly half rounded up to the next higher whole degree.\n\n"
    "Changes to observations within this market's timeframe will be considered until the first datapoint for "
    "the following date has been published.\n\n"
    "The resolution source for this market will be the routine observations for Chengdu Shuangliu "
    "International Airport (ZUUU) published here:\n"
    "https://aviationweather.gov/api/data/metar?ids=ZUUU&format=raw&date=2026-10-23T16:00:00Z&hours=27"
)
NOAA_RULES = (
    "This market will resolve to the temperature range that contains the highest temperature recorded by NOAA "
    "at the Chengdu Shuangliu International Airport Station in degrees Celsius on 22 Oct '26.\n\n"
    "The resolution source for this market will be information from NOAA, specifically the highest reading "
    "under the \"Temp\" column for all times on this day, available here: "
    "https://www.weather.gov/wrh/timeseries?site=zuuu\n\n"
    "Changes ... until the first data point for the following date has been published. whole degrees"
)
ENTRY = {"station_id": "ZUUU", "family": NOAA_FAMILY, "unit": "C", "hourly_data_only": False,
         "finality": "NEXT_DAY_FIRST_DATAPOINT", "routine_metar_only_from": "2026-10-23"}


def test_the_awc_rules_read_as_the_routine_metar_reading() -> None:
    contract = settlement_contract(AWC_RULES)
    assert contract == {
        "family": AWC_FAMILY, "station_id": "ZUUU", "unit": "C", "reading": "ROUTINE_METAR_MAX",
        "hourly_data_only": False, "whole_degree": True, "finality": "NEXT_DAY_FIRST_DATAPOINT",
    }
    # a rule that drops the SPECI clause is not the reading we reproduce
    assert settlement_contract(AWC_RULES.replace("does not count", "counts"))["reading"] == ""


def test_the_baseline_date_decides_which_report_set_is_expected() -> None:
    def blocking(text: str, day: str) -> list[str]:
        found = settlement_contract_deviations(
            settlement_contract(text), expected=expected_settlement_contract(ENTRY, day), bucket_unit="C")
        return [item["reason"] for item in found if item["blocking"]]

    assert blocking(AWC_RULES, "2026-10-23") == []
    assert blocking(NOAA_RULES, "2026-10-22") == []
    # the market moved off SPECI before (or back after) the date our lane drops it: blocked
    assert blocking(AWC_RULES, "2026-10-22") == ["official_resolution_family_changed"]
    assert blocking(NOAA_RULES, "2026-10-23") == ["official_resolution_family_changed"]


def test_the_shipped_baseline_validates_and_names_32_cities() -> None:
    cities = load_config()["settlement_source_contract"]["cities"]
    assert sum(1 for entry in cities.values() if entry.get("routine_metar_only_from") == "2026-10-23") == 32
    assert settlement_basis.routine_metar_only("ZUUU", "2026-10-23")
    assert not settlement_basis.routine_metar_only("ZUUU", "2026-10-22")
    assert not settlement_basis.routine_metar_only("KLGA", "2026-10-23")


@pytest.fixture
def routine_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A baseline that switches one station on one date."""

    def set_(station: str, start: str) -> None:
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps({"settlement_source_contract": {"cities": {
            "x": {"station_id": station, "routine_metar_only_from": start}}}}))
        monkeypatch.setattr(settlement_basis, "CONFIG_PATH", path)

    return set_


def _settle(start: str, routine_from) -> float:
    routine_from("RKPK", start)
    target = _target()
    target.execute("INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?)",
                   ("RKPK", "2026-07-28", "Asia/Seoul", "awc", 1, "e" * 64, "2026-07-29T00:00:00Z"))
    source = _canonical()
    for i, hour in enumerate(("02", "03", "04", "05")):
        _observation(source, observation_id=f"m{i}", valid_at=f"2026-07-28T{hour}:00:00Z", temperature_c=30)
    _observation(source, observation_id="s", valid_at="2026-07-28T04:20:00Z", temperature_c=33,
                 report_type="SPECI")
    result = materialize_settled_station_truth(target, source, station_id="RKPK", event_date="2026-07-28",
                                               as_of_utc="2026-07-28T19:00:00Z")
    return result["official_tmax_c"], target.execute(
        "SELECT observation_ids_json FROM calibration_station_daily_high_truth").fetchone()[0]


def test_the_truth_lane_leaves_speci_out_from_the_switch_date(routine_from) -> None:
    before, evidence_before = _settle("2026-07-29", routine_from)
    after, evidence_after = _settle("2026-07-28", routine_from)
    assert before == 33 and '"settled_reports"' not in evidence_before
    assert after == 30 and '"settled_reports":"ROUTINE_METAR_ONLY"' in evidence_after


def test_a_speci_does_not_raise_the_running_high_on_a_routine_day(routine_from) -> None:
    routine_from("ZBAA", "2026-07-18")
    conn = _conn()
    try:
        ingress = ReportedTrajectoryAwcIngress(conn, config=load_config())
        ingress.process_source_poll(_poll("METAR ZBAA 180200Z 18004MPS CAVOK 31/20 Q1000"),
                                    request_started_at="2026-07-18T02:00:00Z",
                                    response_completed_at="2026-07-18T02:00:00.100Z")
        result = ingress.process_source_poll(
            _poll("SPECI ZBAA 180252Z 18004MPS CAVOK 33/20 Q1000", source_report_time="2026-07-18T02:52:00Z"),
            request_started_at="2026-07-18T02:53:00Z", response_completed_at="2026-07-18T02:53:00.100Z")
        assert result["station_day_fact_updates"] == [{
            "event_date": "2026-07-18", "state": "report_does_not_settle",
            "reason": "speci_on_routine_metar_only_day"}]
        assert conn.execute("SELECT reported_max_c FROM reported_trajectory_station_day_bounds").fetchone() == (31,)
    finally:
        conn.close()


def test_p1_and_its_floors_do_not_read_speci_on_a_routine_day(routine_from) -> None:
    from bayes_basket_target.resident_canonical import load_resident_formal_observations
    from tests.test_bayes_basket_target_resident_canonical import _insert, _registry, _source

    conn = _source()
    _insert(conn, observation_id="hourly", valid_at="2026-07-26T15:00:00Z",
            committed_at="2026-07-26T15:01:00Z", temperature_c=29)
    _insert(conn, observation_id="speci", valid_at="2026-07-26T15:47:00Z",
            committed_at="2026-07-26T15:48:00Z", temperature_c=31, report_type="SPECI")

    def ids() -> list[str]:
        return [row["observation_id"] for row in load_resident_formal_observations(
            conn, registry_rows=_registry(), station_id="RKPK", event_date="2026-07-27",
            as_of_utc="2026-07-26T16:00:00Z")]

    routine_from("RKPK", "2026-07-28")
    assert ids() == ["hourly", "speci"]
    routine_from("RKPK", "2026-07-27")
    assert ids() == ["hourly"]
