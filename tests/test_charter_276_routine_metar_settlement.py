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


def test_the_shipped_baseline_validates_and_names_48_cities() -> None:
    cities = load_config()["settlement_source_contract"]["cities"]
    # 32 non-US, (补记一) the 11 US Fahrenheit cities, (补记二) the 5 Americas Celsius cities
    assert sum(1 for entry in cities.values() if entry.get("routine_metar_only_from") == "2026-10-23") == 48
    assert settlement_basis.routine_metar_only("ZUUU", "2026-10-23")
    assert not settlement_basis.routine_metar_only("ZUUU", "2026-10-22")
    assert settlement_basis.routine_metar_only("KLGA", "2026-10-23")
    assert not settlement_basis.routine_metar_only("KLGA", "2026-10-22")
    assert settlement_basis.routine_metar_only("SAEZ", "2026-10-23")


# The clauses of the nyc 2026-10-23 rules (Gamma, read 2026-10-21) that decide
# the settled number, verbatim (补记一).
US_AWC_RULES = (
    "This market will resolve to the temperature range that contains the highest temperature reported "
    "at LaGuardia Airport (KLGA) on 23 Oct '26.\n\n"
    "The qualifying value is the highest temperature reported in the hourly temperature remark of any "
    "routine (METAR) observation for LaGuardia Airport (KLGA) on that date, in tenths of a degree Celsius "
    "as reported. A report whose line begins with SPECI is a special observation and does not count.\n\n"
    "Each market in this event states its range in whole degrees Fahrenheit. The qualifying value is "
    "converted using \u00b0F = \u00b0C \u00d7 9/5 + 32 and rounded to the nearest whole degree, with a value "
    "ending in exactly half rounded up to the next higher whole degree, and that whole Fahrenheit value "
    "determines which range contains it.\n\n"
    "Changes to observations within this market's timeframe will be considered until the first datapoint "
    "for the following date has been published.\n\n"
    "https://aviationweather.gov/api/data/metar?ids=KLGA"
)


def test_a_us_fahrenheit_market_reads_the_routine_metar_reading_from_its_date() -> None:
    entry = load_config()["settlement_source_contract"]["cities"]["nyc"]
    contract = settlement_contract(US_AWC_RULES)
    assert (contract["family"], contract["station_id"], contract["unit"], contract["reading"]) == (
        AWC_FAMILY, "KLGA", "F", "ROUTINE_METAR_MAX")
    found = settlement_contract_deviations(
        contract, expected=expected_settlement_contract(entry, "2026-10-23"), bucket_unit="F")
    assert [item["reason"] for item in found if item["blocking"]] == []
    found = settlement_contract_deviations(
        contract, expected=expected_settlement_contract(entry, "2026-10-22"), bucket_unit="F")
    assert [item["reason"] for item in found if item["blocking"]] == ["official_resolution_family_changed"]


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


def test_a_moved_city_settles_its_earlier_days_on_the_station_it_replaced(tmp_path, monkeypatch) -> None:
    entry = {"station_id": "KDEN", "family": NOAA_FAMILY, "unit": "F", "hourly_data_only": True,
             "finality": "NEXT_DAY_FIRST_DATAPOINT", "routine_metar_only_from": "2026-10-23",
             "previous_station": {"station_id": "KBKF", "through": "2026-10-22"}}
    assert expected_settlement_contract(entry, "2026-10-22")["station_id"] == "KBKF"
    assert expected_settlement_contract(entry, "2026-10-22")["family"] == NOAA_FAMILY
    assert expected_settlement_contract(entry, "2026-10-23")["station_id"] == "KDEN"
    assert expected_settlement_contract(entry, "2026-10-23")["family"] == AWC_FAMILY
    assert "previous_station" not in expected_settlement_contract(entry, "2026-10-23")
    # the 10-23 denver rules name KDEN: no deviation at all on either side of the move
    rules = US_AWC_RULES.replace("LaGuardia Airport (KLGA)", "Denver International Airport (KDEN)").replace(
        "ids=KLGA", "ids=KDEN")
    found = settlement_contract_deviations(
        settlement_contract(rules), expected=expected_settlement_contract(entry, "2026-10-23"), bucket_unit="F")
    assert found == []
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"settlement_source_contract": {"cities": {"denver": entry}}}))
    monkeypatch.setattr(settlement_basis, "CONFIG_PATH", path)
    assert settlement_basis.settled_station("KDEN", "2026-10-22") == "KBKF"
    assert settlement_basis.settled_station("KDEN", "2026-10-23") == "KDEN"
    assert settlement_basis.settled_station("KLGA", "2026-10-22") == "KLGA"
    # routine-only follows the station the day settles on: KDEN from 10-23, KBKF never
    assert settlement_basis.routine_metar_only("KDEN", "2026-10-23")
    assert not settlement_basis.routine_metar_only("KBKF", "2026-10-22")


def test_the_repair_lane_reads_the_awc_page_as_the_rules_state_it() -> None:
    """Charter 225 on charter 276's markets: the AWC raw page the rules name is
    the evidence -- SPECI out, remark tenths first, the local day only."""

    from bayes_basket_target.settlement_source_truth import ContractError, decide, parse_awc_routine_metars

    url = "https://aviationweather.gov/api/data/metar?ids=KLGA&format=raw&date=2026-10-21T04:00:00Z&hours=27"
    page = "\n".join([
        "METAR KLGA 210351Z 30008KT 10SM CLR 14/02 A3010 RMK AO2 T01390022",   # 10-20 23:51 local
        "SPECI KLGA 201912Z 30008KT 10SM CLR 22/02 A3010 RMK AO2 T02220022",   # hottest, but a SPECI
        "METAR KLGA 201851Z 30008KT 10SM CLR 21/02 A3010 RMK AO2 T02060022",   # 20.6 C
        "METAR KLGA 200351Z 30008KT 10SM CLR 25/02 A3010 RMK AO2 T02500022",   # 10-19 local: outside
    ])
    got = parse_awc_routine_metars(page, station_id="KLGA", event_date="2026-10-20",
                                   settlement_timezone="America/New_York", unit="F", request_url=url)
    assert got["identity"]["speci_excluded"] == 1 and got["value"] == pytest.approx(69.08)
    assert decide(paid_bucket="68-69°F", unit="F", source_value=got["value"])[1] == pytest.approx(69.08)
    with pytest.raises(ContractError):
        parse_awc_routine_metars(page, station_id="KJFK", event_date="2026-10-20",
                                 settlement_timezone="America/New_York", unit="F", request_url=url)
