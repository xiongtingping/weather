"""Charter 225: truth follows the market's official settlement source.

User ruling 2026-09-25, 「全部按结算」: where a settled market paid a bucket the
standing truth is not in, the day is re-settled from that market's own
resolution page as a SETTLEMENT_SOURCE revision -- its own lane, formal like
AWC, newest revision wins across both -- and the AWC revision stays as audit.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import band_reliability_store as band
from bayes_basket_target import delta_v2_settlement_label as lab
from bayes_basket_target import settlement_source_truth as sst
from bayes_basket_target.canonical_contract import (
    FORMAL_AWC_TRUTH_ROLE,
    FORMAL_TRUTH_LANES,
    FORMAL_TRUTH_ROLES,
    SETTLEMENT_SOURCE_TRUTH_LANE,
    SETTLEMENT_SOURCE_TRUTH_ROLE,
    CanonicalTruthFact,
)
from bayes_basket_target.contracts import ContractError, content_sha256

STATION, DAY, ZONE = "EDDM", "2026-08-01", "Europe/Berlin"
DAY_END = "2026-08-01T22:00:00Z"


def _fact(**change) -> dict:
    identity = {
        "truth_id": "truth_x",
        "station_id": STATION,
        "event_date": DAY,
        "truth_revision_no": 1,
        "truth_value": 30.0,
        "truth_content_sha256": "a" * 64,
        "canonical_committed_at_utc": "2026-09-25T08:00:00Z",
        "station_day_end_utc": DAY_END,
        "closed_day": True,
        "source_lane": SETTLEMENT_SOURCE_TRUTH_LANE,
        "source_role": SETTLEMENT_SOURCE_TRUTH_ROLE,
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
        "settlement_timezone": ZONE,
        **change,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_the_settlement_source_lane_is_formal_and_keeps_its_own_role() -> None:
    assert FORMAL_TRUTH_LANES == ("AWC", SETTLEMENT_SOURCE_TRUTH_LANE)
    assert set(FORMAL_TRUTH_ROLES) == {FORMAL_AWC_TRUTH_ROLE, SETTLEMENT_SOURCE_TRUTH_ROLE}
    assert CanonicalTruthFact.from_mapping(_fact()).source_lane == SETTLEMENT_SOURCE_TRUTH_LANE
    # Never relabelled: the lane and its role come as a pair.
    with pytest.raises(ContractError, match="source_role_invalid"):
        CanonicalTruthFact.from_mapping(_fact(source_role=FORMAL_AWC_TRUTH_ROLE))
    with pytest.raises(ContractError, match="source_role_invalid"):
        CanonicalTruthFact.from_mapping(_fact(source_lane="AWC"))
    with pytest.raises(ContractError, match="authority_mismatch"):
        CanonicalTruthFact.from_mapping(_fact(authority_scope="PRODUCTION_NO_LIVE_ONLY"))
    with pytest.raises(ContractError, match="source_lane_invalid"):
        CanonicalTruthFact.from_mapping(_fact(source_lane="WU"))


def test_the_paid_bucket_decides_what_the_page_may_say() -> None:
    assert sst.paid_bucket_bounds("30°C") == (30.0, 30.0, "C")
    assert sst.paid_bucket_bounds("88-89°F") == (88.0, 89.0, "F")
    assert sst.paid_bucket_bounds("35°C or higher") == (35.0, math.inf, "C")
    assert sst.paid_bucket_bounds("23°C or below") == (-math.inf, 23.0, "C")
    # The page inside the paid bucket: the page's own value.
    assert sst.decide(paid_bucket="90-91°F", unit="F", source_value=89.6) == (
        sst.BASIS_SOURCE_IN_BUCKET, 89.6,
    )
    # The page revised after finality, a one-degree bucket: that degree.
    assert sst.decide(paid_bucket="28°C", unit="C", source_value=30.0) == (
        sst.BASIS_PAID_DEGREE, 28.0,
    )
    # A range or an open edge the page does not support: undetermined.
    with pytest.raises(ContractError, match="value_undetermined"):
        sst.decide(paid_bucket="23°C or below", unit="C", source_value=28.0)
    with pytest.raises(ContractError, match="value_undetermined"):
        sst.decide(paid_bucket="88-89°F", unit="F", source_value=90.2)
    with pytest.raises(ContractError, match="unit_mismatch"):
        sst.decide(paid_bucket="30°C", unit="F", source_value=86.0)
    assert sst.to_celsius(89.6, "F") == 32.0
    assert sst.whole_degree(sst.to_celsius(100, "F") * 9 / 5 + 32) == 100


def _wu_html(*, icao=STATION, zone=ZONE, date="2026-8-1", unit="C", high=30) -> str:
    return (
        "<html><body>"
        f'<airport-body data-location-id="{icao}:9:DE" data-time-zone="{zone}" '
        f'data-icao-code="{icao}" data-country-code="de" data-mode="daily" data-date="{date}">'
        "<h2>Daily Observations</h2><table><tbody><tr>\n"
        f"        <td>2:50 PM</td>\n        <td class=\"temp\">{high - 1} °{unit}</td>\n"
        "      </tr><tr>\n"
        f"        <td>4:50 PM</td>\n        <td class=\"temp temp-high\">{high} °{unit}</td>\n"
        "      </tr><tr>\n"
        "        <td>5:20 PM</td>\n        <td class=\"temp\"><span class=\"no-data\">-</span></td>\n"
        "      </tr><tr>\n"
        f"        <td>4:20 AM</td>\n        <td class=\"temp temp-low\">{high - 15} °{unit}</td>\n"
        "      </tr></tbody></table></airport-body></body></html>"
    )


def test_the_wu_page_is_read_from_its_own_table_and_binding() -> None:
    parsed = sst.parse_wu_daily_history(
        _wu_html(), station_id=STATION, event_date=DAY, settlement_timezone=ZONE, unit="C"
    )
    # The high row is marked temp-high; missing it read every day a degree low.
    assert parsed["value"] == 30.0 and parsed["row_count"] == 4 and len(parsed["readings"]) == 3
    for html, code in (
        (_wu_html(icao="EDDF"), "identity_mismatch"),
        (_wu_html(date="2026-8-2"), "identity_mismatch"),
        (_wu_html(zone="Europe/Paris"), "timezone_mismatch"),
        (_wu_html(unit="F"), "unit_mismatch"),
    ):
        with pytest.raises(ContractError, match=code):
            sst.parse_wu_daily_history(
                html, station_id=STATION, event_date=DAY, settlement_timezone=ZONE, unit="C"
            )


def _noaa_summary(**change) -> dict:
    summary = {
        "site": "KMIA", "day": "2026-09-02",
        "station": {"STID": "KMIA", "TIMEZONE": "America/New_York", "SHORTNAME": "ASOS/AWOS"},
        "units_air_temp": "Fahrenheit", "n_rows_day": 317,
        "response_sha256": "b" * 64, "response_bytes": 1, "request_url": "https://x/?token=REDACTED",
        "max_all": {"value": 91.4, "rows": [["2026-09-02T11:15:00-0400", 91.4, "METAR KMIA ... AUTO"]]},
        "hourly_view": {"n_rows": 30, "max": {"value": 89.6, "rows": [["2026-09-02T13:45:00-0400", 89.6, "KMIA SPECI"]]}},
    }
    summary.update(change)
    return summary


def test_a_fahrenheit_noaa_market_reads_the_hourly_view() -> None:
    parsed = sst.read_noaa_summary(
        _noaa_summary(), station_id="KMIA", event_date="2026-09-02",
        settlement_timezone="America/New_York", unit="F",
    )
    assert parsed["view"] == "hourly_view" and parsed["value"] == 89.6
    celsius = sst.read_noaa_summary(
        _noaa_summary(units_air_temp="Celsius"), station_id="KMIA", event_date="2026-09-02",
        settlement_timezone="America/New_York", unit="C",
    )
    assert celsius["view"] == "all" and celsius["value"] == 91.4
    with pytest.raises(ContractError, match="unit_mismatch"):
        sst.read_noaa_summary(
            _noaa_summary(), station_id="KMIA", event_date="2026-09-02",
            settlement_timezone="America/New_York", unit="C",
        )


# --- the write, end to end, on the production schema ---------------------------

def _ssot(tmp_path: Path) -> sqlite3.Connection:
    from bayes_basket_target.ssot import apply_production_ssot_schema

    conn = sqlite3.connect(str(tmp_path / "ssot.db"), isolation_level=None)
    apply_production_ssot_schema(conn)
    registry = {"station_id": STATION, "icao_code": STATION, "city_label": "Munich",
                "timezone_name": ZONE, "source_lane": "AWC",
                "settlement_definition": "official_daily_tmax_c", "registry_version": "test",
                "source_role": FORMAL_AWC_TRUTH_ROLE}
    conn.execute(
        "INSERT INTO calibration_station_registry(" + ",".join(registry) + ",content_sha256)"
        " VALUES(" + ",".join("?" * (len(registry) + 1)) + ")",
        (*registry.values(), content_sha256(registry)),
    )
    return conn


def _awc_truth(conn: sqlite3.Connection, *, value: float) -> str:
    """One AWC formal truth for the station-day, through canonical ingress."""

    from bayes_basket_target.canonical_contract import CONTRACT_VERSION, SCHEMA
    from bayes_basket_target.canonical_ingress import (
        AWC_DAILY_TRUTH_PROJECTION_SCHEMA, ingest_canonical_contract,
    )
    from bayes_basket_target.contracts import canonical_json

    evidence = {"schema": "settlement_truth_source_evidence_v1", "station_id": STATION,
                "event_date": DAY, "settlement_timezone": ZONE, "source_lane": "AWC",
                "observations": [{"reported_temperature_c": value}]}
    evidence_sha = content_sha256(evidence)
    truth_id = "truth_" + content_sha256({"station_id": STATION, "event_date": DAY,
                                          "truth_revision_no": 0,
                                          "truth_content_sha256": evidence_sha})[:40]
    identity = {
        "truth_id": truth_id, "station_id": STATION, "event_date": DAY, "truth_revision_no": 0,
        "truth_value": value, "truth_content_sha256": evidence_sha,
        "canonical_committed_at_utc": "2026-08-10T00:00:00Z", "station_day_end_utc": DAY_END,
        "closed_day": True, "source_lane": "AWC", "source_role": FORMAL_AWC_TRUTH_ROLE,
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE", "settlement_timezone": ZONE,
    }
    truth = {**identity, "content_sha256": content_sha256(identity)}
    legacy = {
        "truth_id": truth_id, "station_id": STATION, "local_date": DAY, "settlement_timezone": ZONE,
        "official_tmax_c": value, "source_lane": "AWC", "settlement_definition": "official_daily_tmax_c",
        "revision_no": 0, "published_at_utc": "2026-08-01T21:50:00Z",
        "received_at_utc": "2026-08-01T21:55:00Z", "canonical_committed_at_utc": "2026-08-10T00:00:00Z",
        "timestamps_complete": 1, "qc_status": "ACCEPTED", "source_role": FORMAL_AWC_TRUTH_ROLE,
        "source_payload_sha256": evidence_sha, "observation_ids_json": canonical_json(evidence),
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
    }
    row = {**truth, "settlement_definition": "official_daily_tmax_c",
           "published_at_utc": legacy["published_at_utc"], "received_at_utc": legacy["received_at_utc"],
           "timestamps_complete": 1, "qc_status": "ACCEPTED",
           "observation_ids_json": legacy["observation_ids_json"], "content_sha256": content_sha256(legacy)}
    contract = {"schema": SCHEMA, "contract_version": CONTRACT_VERSION, "as_of_utc": "2026-08-10T00:00:00Z",
                "builder_id": "awc.closed_day.truth.v1", "model_facts": [], "ensemble_member_facts": [],
                "native_arrival_facts": [], "ensemble_member_receipt_manifest_facts": [],
                "ensemble_member_arrival_binding_facts": [], "truth_facts": [truth],
                "residual_facts": [], "activation_facts": []}
    ingest_canonical_contract(
        conn, {**contract, "content_sha256": content_sha256(contract)}, commit=True,
        awc_daily_truth_projection={"schema": AWC_DAILY_TRUTH_PROJECTION_SCHEMA, "truth_rows": [row]},
    )
    return truth_id


def _case(**market) -> dict:
    raw = _wu_html().encode()
    return {
        "station_id": STATION, "event_date": DAY, "settlement_timezone": ZONE,
        "market": {"slug": "highest-temperature-in-munich-on-august-1-2026", "family": sst.WU_FAMILY,
                   "unit": "C", "resolution_source": "https://www.wunderground.com/history/daily/de/munich/EDDM",
                   "winning_bucket": "30°C", "closed": True, **market},
        "source": {"family": sst.WU_FAMILY, "capture_url": "https://www.wunderground.com/x/date/2026-8-1",
                   "object_ref": "data/settlement_source_captures/EDDM/2026-08-01/wu_daily_history.html.gz",
                   "raw_bytes": raw, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                   "captured_at_utc": "2026-09-25T07:40:00Z"},
    }


def test_a_wrong_truth_is_re_settled_as_its_own_lane_and_stays_so(tmp_path) -> None:
    from bayes_basket_target.settlement_truth import materialize_settled_station_truth

    conn = _ssot(tmp_path)
    old_id = _awc_truth(conn, value=20.0)
    receipt = sst.materialize_settlement_source_truth(conn, case=_case(), as_of_utc="2026-09-25T08:00:00Z")
    assert receipt["state"] == "committed" and receipt["wrote"] is True
    assert receipt["revision_no"] == 1 and receipt["official_tmax_c"] == 30.0
    assert receipt["supersedes"]["truth_id"] == old_id
    rows = conn.execute(
        "SELECT revision_no,source_lane,source_role,official_tmax_c FROM calibration_station_daily_high_truth "
        "WHERE station_id=? AND local_date=? ORDER BY revision_no", (STATION, DAY),
    ).fetchall()
    assert rows == [(0, "AWC", FORMAL_AWC_TRUTH_ROLE, 20.0),
                    (1, SETTLEMENT_SOURCE_TRUTH_LANE, SETTLEMENT_SOURCE_TRUTH_ROLE, 30.0)]
    assert conn.execute(
        "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type='truth' AND scope_key=?",
        (f"{STATION}|{DAY}|1",),
    ).fetchone()[0] == 1
    # Replayed, it stands; the AWC writer does not append over it.
    again = sst.materialize_settlement_source_truth(conn, case=_case(), as_of_utc="2026-09-25T09:00:00Z")
    assert again["state"] == "settlement_source_truth_stands" and again["wrote"] is False
    awc = materialize_settled_station_truth(
        conn, sqlite3.connect(":memory:"), station_id=STATION, event_date=DAY,
        as_of_utc="2026-09-25T08:30:00Z",
    )
    assert awc["state"] == "settlement_source_truth_stands" and awc["wrote"] is False


def test_a_truth_already_in_the_paid_bucket_is_left_alone(tmp_path) -> None:
    conn = _ssot(tmp_path)
    _awc_truth(conn, value=30.0)
    receipt = sst.materialize_settlement_source_truth(conn, case=_case(), as_of_utc="2026-09-25T08:00:00Z")
    assert receipt["state"] == "standing_truth_in_paid_bucket" and receipt["wrote"] is False
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_station_daily_high_truth WHERE station_id=?", (STATION,)
    ).fetchone()[0] == 1


def test_the_capture_must_be_the_bytes_it_names() -> None:
    case = _case()
    case["source"]["raw_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="raw_sha256_mismatch"):
        sst.build_evidence(case)
    with pytest.raises(ContractError, match="market_not_resolved"):
        sst.build_evidence(_case(closed=False))


# --- the readers take the newest revision across both lanes --------------------

def _truth_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth(truth_id TEXT UNIQUE, station_id TEXT, local_date TEXT,"
        " settlement_definition TEXT, revision_no INTEGER, source_lane TEXT, source_role TEXT, qc_status TEXT,"
        " timestamps_complete INTEGER, canonical_committed_at_utc TEXT, official_tmax_c REAL,"
        " authority_scope TEXT, content_sha256 TEXT, UNIQUE(station_id, local_date, settlement_definition, revision_no))"
    )
    conn.executemany(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("t0", STATION, DAY, "official_daily_tmax_c", 0, "AWC", FORMAL_AWC_TRUTH_ROLE, "ACCEPTED",
             1, "2026-08-10T00:00:00Z", 20.0, "FORMAL_NATIVE_ELIGIBLE", "c" * 64),
            ("t1", STATION, DAY, "official_daily_tmax_c", 1, SETTLEMENT_SOURCE_TRUTH_LANE,
             SETTLEMENT_SOURCE_TRUTH_ROLE, "ACCEPTED", 1, "2026-09-25T08:00:00Z", 30.0,
             "FORMAL_NATIVE_ELIGIBLE", "d" * 64),
        ],
    )


def test_the_label_lane_labels_against_the_repair() -> None:
    conn = sqlite3.connect(":memory:")
    _truth_table(conn)
    conn.execute(
        "CREATE TABLE basket_policy_training_fact(fact_id TEXT UNIQUE, station_id TEXT, event_date TEXT,"
        " settlement_timezone TEXT, probability_envelope_id TEXT, policy_version_vector_json TEXT,"
        " feature_json TEXT, authority_scope TEXT)"
    )
    conn.execute("CREATE TABLE basket_policy_training_label(fact_id TEXT, truth_id TEXT, UNIQUE(fact_id, truth_id))")
    conn.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?,?,?)",
        ("d2fact_0001", STATION, DAY, ZONE, "env", "{}", "{}", lab.FACT_AUTHORITY_SCOPE),
    )
    conn.execute("INSERT INTO basket_policy_training_label VALUES('d2fact_0001','t0')")
    page = lab._label_candidate_page(
        conn, rowid_where="f.rowid>?", rowid_params=(0,), as_of="2026-09-25T09:00:00Z", limit=10
    )
    assert [(row[1], row[8], row[10]) for row in page] == [("d2fact_0001", "t1", 30.0)]
    # Before the repair was committed, the AWC revision was the newest.
    earlier = lab._label_candidate_page(
        conn, rowid_where="f.rowid>?", rowid_params=(0,), as_of="2026-09-01T00:00:00Z", limit=10
    )
    assert earlier == []


def test_the_band_lane_sees_one_newest_value_per_day() -> None:
    conn = sqlite3.connect(":memory:")
    _truth_table(conn)
    assert band._truth(conn) == {(STATION, DAY): 30.0}


# --- the live table is widened in place ----------------------------------------

def test_the_truth_table_widens_without_moving_what_refers_to_it(tmp_path) -> None:
    import scripts.widen_truth_lane_settlement_source as widen

    conn = sqlite3.connect(str(tmp_path / "old.db"), isolation_level=None)
    old_ddl = widen.NEW_DDL.replace(widen.NEW, widen.TABLE).replace(
        widen.WIDENED_CHECK, "CHECK(source_lane IN ('AMSC','AWC'))"
    )
    conn.execute(old_ddl)
    for _name, ddl in widen.LIVE_TRIGGERS:
        conn.execute(ddl)
    conn.execute(
        "CREATE TABLE calibration_model_station_residual(residual_id TEXT PRIMARY KEY,"
        " truth_id TEXT NOT NULL REFERENCES calibration_station_daily_high_truth(truth_id))"
    )
    conn.execute(
        "CREATE TABLE calibration_republisher_historical_oos_contract(historical_oos_contract_id TEXT,"
        " awc_truth_id TEXT REFERENCES calibration_station_daily_high_truth(truth_id))"
    )
    conn.execute(
        "CREATE TRIGGER calibration_republisher_historical_oos_contract_truth_guard BEFORE INSERT ON"
        " calibration_republisher_historical_oos_contract WHEN NOT EXISTS (SELECT 1 FROM"
        " calibration_station_daily_high_truth AS t WHERE t.truth_id=NEW.awc_truth_id AND t.source_lane='AWC')"
        " BEGIN SELECT RAISE(ABORT,'historical_republisher_oos_truth_binding_invalid'); END"
    )
    row = ("t0", STATION, DAY, ZONE, 20.0, "AWC", "official_daily_tmax_c", 0, None, None,
           "2026-08-10T00:00:00Z", 1, "ACCEPTED", FORMAL_AWC_TRUTH_ROLE, "c" * 64, "{}",
           "FORMAL_NATIVE_ELIGIBLE", "d" * 64)
    conn.execute(f"INSERT INTO {widen.TABLE} VALUES({','.join('?' * len(row))})", row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO {widen.TABLE} VALUES({','.join('?' * len(row))})",
                     ("t1", *row[1:5], SETTLEMENT_SOURCE_TRUTH_LANE, row[6], 1, *row[8:]))
    assert widen.widen(conn, apply=False)["state"] == "DRY_RUN"
    receipt = widen.widen(conn, apply=True)
    assert receipt["state"] == "REBUILT" and receipt["row_count_after"] == 1
    assert set(receipt["referrers_after"].values()) == {"points_at_live"}
    conn.execute(f"INSERT INTO {widen.TABLE} VALUES({','.join('?' * len(row))})",
                 ("t1", *row[1:5], SETTLEMENT_SOURCE_TRUTH_LANE, row[6], 1, *row[8:]))
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(f"UPDATE {widen.TABLE} SET official_tmax_c=1 WHERE truth_id='t0'")
    with pytest.raises(sqlite3.IntegrityError, match="replaced_table_immutable"):
        conn.execute(f"INSERT INTO {widen.BACKUP} VALUES({','.join('?' * len(row))})", ("t9", *row[1:]))
    # The republisher guard still reads the live table, and still binds AWC only.
    conn.execute("INSERT INTO calibration_republisher_historical_oos_contract VALUES('c0','t0')")
    with pytest.raises(sqlite3.IntegrityError, match="truth_binding_invalid"):
        conn.execute("INSERT INTO calibration_republisher_historical_oos_contract VALUES('c1','t1')")
    assert widen.widen(conn, apply=True)["state"] == "ALREADY_WIDENED"
