"""ASOS carrier rows ride the main P1 chain as soft path evidence.

Soft in, hard out (user ruling 2026-08-29): settlement follows the formal
METAR series -- measured, the MADIS intra-hour maximum overshoots the
official settle on 35.7% of days -- so carrier rows feed the path state
(momentum, drawdown, the physical conditioner) and hold no hard rights.
These tests pin the admission rules of the wired-up chain:

* the carrier lanes are admitted only under an AWC-governed scope, with
  their exact lane/report-type pairing and the materializer's identity
  prefix, and the reader labels them SOFT_PATH;
* a carrier row never advances the running high, never closes a packet,
  and never carries a closing identity -- however hot its reading;
* the materializer ingests the open day's qc-eligible carrier rows
  idempotently, quantized to the METAR tenths grid, behind the station's
  OOS_READY artifact;
* the retained atomic identity machinery (formal-substitute era) still
  validates and commits, and ignores soft carrier rows entirely.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.metar_trajectory import (
    canonicalize_metar_trajectory_observations,
)
from bayes_basket_target.p1_compiler import compile_station_p1_packets
from bayes_basket_target.resident_canonical import (
    load_resident_formal_observations,
)


def _event(
    observation_id: str,
    *,
    lane: str,
    report_type: str,
    observed: str,
    received: str,
    temperature_c: float,
) -> dict:
    return {
        "event": {
            "observation_id": observation_id,
            "station_id": "KLGA",
            "event_date": "2026-08-09",
            "settlement_timezone": "America/New_York",
            "source_lane": lane,
            "report_type": report_type,
            "sequence": 1,
            "observed_at_utc": observed,
            "received_at_utc": received,
            "raw_sha256": "3" * 64,
            "qc_state": "ACCEPTED",
            "supersedes_observation_id": None,
        },
        "canonical_committed_at_utc": received,
        "temperature_c": temperature_c,
        "raw_text": None,
    }


def test_awc_prefix_admits_the_substitute_lanes_and_still_refuses_amsc() -> None:
    mixed = [
        _event(
            "awc-1",
            lane="awc",
            report_type="METAR",
            observed="2026-08-09T14:51:00.000000Z",
            received="2026-08-09T14:55:00.000000Z",
            temperature_c=20.0,
        ),
        _event(
            "asos_p1_sub",
            lane="madis_hfmetar",
            report_type="ASOS_HF",
            observed="2026-08-09T15:50:00.000000Z",
            received="2026-08-09T16:07:00.000000Z",
            temperature_c=21.0,
        ),
    ]
    ordered = canonicalize_metar_trajectory_observations(
        mixed,
        as_of_utc="2026-08-09T17:00:00.000000Z",
        station_id="KLGA",
        event_date="2026-08-09",
        source_lane="awc",
        settlement_timezone="America/New_York",
    )
    assert [item.event.observation_id for item in ordered] == [
        "awc-1",
        "asos_p1_sub",
    ]
    with pytest.raises(ContractError, match="source_lane_mismatch"):
        canonicalize_metar_trajectory_observations(
            [
                _event(
                    "amsc-1",
                    lane="amsc",
                    report_type="METAR",
                    observed="2026-08-09T15:00:00.000000Z",
                    received="2026-08-09T15:01:00.000000Z",
                    temperature_c=20.0,
                )
            ],
            as_of_utc="2026-08-09T17:00:00.000000Z",
            station_id="KLGA",
            event_date="2026-08-09",
            source_lane="awc",
            settlement_timezone="America/New_York",
        )
    # A non-AWC governed scope gains nothing: the trio is an AWC privilege.
    with pytest.raises(ContractError, match="source_lane_mismatch"):
        canonicalize_metar_trajectory_observations(
            [mixed[1]],
            as_of_utc="2026-08-09T17:00:00.000000Z",
            station_id="KLGA",
            event_date="2026-08-09",
            source_lane="amsc",
            settlement_timezone="America/New_York",
        )


def _source() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reported_trajectory_observations("
        "observation_id TEXT,source_lane TEXT,station_id TEXT,"
        "report_type TEXT,valid_at TEXT,reported_temperature_c REAL,"
        "raw_sha256 TEXT,raw_text TEXT,canonical_committed_at TEXT,"
        "correction_suspected INTEGER)"
    )
    return conn


_REGISTRY = [{
    "station_id": "KLGA",
    "event_date": "2026-08-09",
    "settlement_timezone": "America/New_York",
    "source_lane": "awc",
}]


def _insert(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    valid_at: str,
    committed_at: str,
    temperature_c: float,
    report_type: str = "METAR",
    source_lane: str = "awc",
) -> None:
    conn.execute(
        "INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            observation_id,
            source_lane,
            "KLGA",
            report_type,
            valid_at,
            temperature_c,
            "a" * 64,
            "",
            committed_at,
            0,
        ),
    )


def test_reader_admits_paired_substitutes_and_refuses_the_rest() -> None:
    conn = _source()
    _insert(
        conn,
        observation_id="hourly",
        valid_at="2026-08-09T14:51:00Z",
        committed_at="2026-08-09T14:55:00Z",
        temperature_c=29.0,
    )
    _insert(
        conn,
        observation_id="asos_p1_" + "f" * 32,
        valid_at="2026-08-09T15:50:00Z",
        committed_at="2026-08-09T16:07:00Z",
        temperature_c=30.1,
        report_type="ASOS_HF",
        source_lane="madis_hfmetar",
    )
    rows = load_resident_formal_observations(
        conn,
        registry_rows=_REGISTRY,
        station_id="KLGA",
        event_date="2026-08-09",
        as_of_utc="2026-08-09T17:00:00Z",
    )
    assert [
        (row["observation_id"], row["source_lane"], row["evidence_role"])
        for row in rows
    ] == [
        ("hourly", "awc", "FORMAL"),
        ("asos_p1_" + "f" * 32, "madis_hfmetar", "SOFT_PATH"),
    ]

    # Wrong pairing: a substitute lane may only carry its own report type.
    _insert(
        conn,
        observation_id="asos_p1_" + "e" * 32,
        valid_at="2026-08-09T16:53:00Z",
        committed_at="2026-08-09T17:03:00Z",
        temperature_c=30.5,
        report_type="METAR",
        source_lane="madis_hfmetar",
    )
    with pytest.raises(ContractError, match="resident_p1_report_type_invalid"):
        load_resident_formal_observations(
            conn,
            registry_rows=_REGISTRY,
            station_id="KLGA",
            event_date="2026-08-09",
            as_of_utc="2026-08-09T18:00:00Z",
        )


def test_reader_refuses_a_substitute_without_the_adapter_identity() -> None:
    conn = _source()
    _insert(
        conn,
        observation_id="freeride",
        valid_at="2026-08-09T15:50:00Z",
        committed_at="2026-08-09T16:07:00Z",
        temperature_c=30.1,
        report_type="ASOS_HF",
        source_lane="madis_hfmetar",
    )
    with pytest.raises(
        ContractError, match="resident_p1_asos_substitute_identity_invalid"
    ):
        load_resident_formal_observations(
            conn,
            registry_rows=_REGISTRY,
            station_id="KLGA",
            event_date="2026-08-09",
            as_of_utc="2026-08-09T17:00:00Z",
        )


def _p0_root():
    from bayes_basket_target.contracts import ProbabilityEnvelope

    return ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0",
        "station_id": "KLGA",
        "event_date": "2026-08-09",
        "settlement_timezone": "America/New_York",
        "decision_at_utc": "2026-08-09T04:10:00Z",
        "fresh_until_utc": "2026-08-09T06:00:00Z",
        "bucket_ordinals": {
            "75°F or below": 0,
            "76-77°F": 1,
            "78°F or higher": 2,
        },
        "probabilities": {
            "75°F or below": 0.2,
            "76-77°F": 0.5,
            "78°F or higher": 0.3,
        },
        "posterior_version": "p0",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "model_run_ids": "r",
            "settlement_timezone": "America/New_York",
            "season_bin": "JJA",
            "settlement_definition": "station_local_calendar_day_tmax",
        },
    })


_PACKET_REGISTRY = [
    {
        "station_id": "KLGA",
        "event_date": "2026-08-09",
        "settlement_timezone": "America/New_York",
        "settlement_unit": "F",
        "outcome": outcome,
        "registry_json": (
            '{"upper": ' + ("null" if upper is None else str(upper)) + "}"
        ),
    }
    for outcome, upper in (
        ("75°F or below", 75.0),
        ("76-77°F", 77.0),
        ("78°F or higher", None),
    )
]


def _observation(
    observation_id: str,
    *,
    lane: str,
    report_type: str,
    valid_at: str,
    committed_at: str,
    temperature_c: float,
) -> dict:
    return {
        "observation_id": observation_id,
        "station_id": "KLGA",
        "event_date_local": "2026-08-09",
        "settlement_timezone": "America/New_York",
        "reported_temperature_c": temperature_c,
        "valid_at_utc": valid_at,
        "canonical_committed_at_utc": committed_at,
        "raw_sha256": "1" * 64,
        "source_lane": lane,
        "report_type": report_type,
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
    }


def test_soft_rows_feed_the_path_but_never_close_or_advance() -> None:
    """Soft in, hard out: a hotter carrier reading moves no hard number.

    The 23.0C MADIS reading is real path evidence -- it rides the prefix --
    but settlement follows the METAR series, so the running high, the hard
    mask and every closing identity stay with the AWC reports.
    """

    observations = [
        _observation(
            "awc-morning",
            lane="awc",
            report_type="METAR",
            valid_at="2026-08-09T14:51:00Z",
            committed_at="2026-08-09T14:55:00Z",
            temperature_c=20.0,
        ),
        # An intra-hour carrier reading HOTTER than any METAR of the day.
        _observation(
            "asos_soft_" + "a" * 32,
            lane="madis_hfmetar",
            report_type="ASOS_HF",
            valid_at="2026-08-09T15:20:00Z",
            committed_at="2026-08-09T15:29:00Z",
            temperature_c=23.0,
        ),
        _observation(
            "awc-afternoon",
            lane="awc",
            report_type="METAR",
            valid_at="2026-08-09T15:51:00Z",
            committed_at="2026-08-09T15:55:00Z",
            temperature_c=21.0,
        ),
    ]
    packets = compile_station_p1_packets(
        predecessor=_p0_root(),
        observations=observations,
        registry_rows=_PACKET_REGISTRY,
        compilation_at_utc="2026-08-09T18:30:00Z",
    )
    # The soft 23.0 never advances: the 21.0 METAR still counts as a new
    # high over 20.0 and closes its own packet.  The tail state refresh
    # closes on that same METAR without advancing anything.
    assert [
        packet["observation_id"]
        for packet in packets
        if packet["reported_high_advanced"]
    ] == [
        "awc-morning",
        "awc-afternoon",
    ]
    proof = packets[-1]["trajectory_proof"]
    assert proof["running_high_c"] == 21.0
    assert packets[-1]["compiled_bound"]["reported_high_c"] == 21.0
    # The carrier reading is in the walked prefix the path state sees.
    walked = [
        item["event"]["observation_id"]
        for item in packets[-1]["trajectory_observations"]
    ]
    assert "asos_soft_" + "a" * 32 in walked
    lanes = [
        packet["expected_envelope"]["lineage"]["source_lane"]
        for packet in packets
    ]
    assert set(lanes) == {"AWC"}


def test_awc_only_days_never_emit_reanchor_packets() -> None:
    observations = [
        _observation(
            "awc-1",
            lane="awc",
            report_type="METAR",
            valid_at="2026-08-09T14:51:00Z",
            committed_at="2026-08-09T14:55:00Z",
            temperature_c=20.0,
        ),
        _observation(
            "awc-2",
            lane="awc",
            report_type="METAR",
            valid_at="2026-08-09T15:51:00Z",
            committed_at="2026-08-09T15:55:00Z",
            temperature_c=20.0,
        ),
    ]
    packets = compile_station_p1_packets(
        predecessor=_p0_root(),
        observations=observations,
        registry_rows=_PACKET_REGISTRY,
        compilation_at_utc="2026-08-09T18:30:00Z",
    )
    assert [
        packet["observation_id"]
        for packet in packets
        if packet["reported_high_advanced"]
    ] == ["awc-1"]
    # Everything else the walk emits on an AWC-only day is the tail state
    # refresh, which names the head it recomputes; a re-anchoring packet --
    # the AWC report that follows an ASOS-closed head -- carries no such name
    # and there is none here.
    assert [
        packet["observation_id"]
        for packet in packets
        if not packet["reported_high_advanced"]
        and not packet["state_refresh_of_checkpoint_id"]
    ] == []


def _now(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _archive() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE reported_trajectory_observations(
          observation_id TEXT PRIMARY KEY,
          source_lane TEXT NOT NULL
            CHECK(source_lane IN ('amsc','awc','asos_1min','madis_hfmetar')),
          station_id TEXT,
          report_type TEXT NOT NULL
            CHECK(report_type IN ('METAR','SPECI','ASOS_HF','ASOS_1MIN')),
          valid_at TEXT,reported_temperature_c REAL,
          raw_sha256 TEXT,raw_text TEXT,first_seen_at TEXT,parsed_at TEXT,
          canonical_committed_at TEXT,fetch_started_at TEXT,
          fetch_completed_at TEXT,source_poll_id TEXT,
          correction_suspected INTEGER DEFAULT 0);
        CREATE TABLE reported_trajectory_observation_origins(
          observation_id TEXT PRIMARY KEY,source_lane TEXT,station_id TEXT,
          origin_source_id TEXT,origin_country_code TEXT,
          origin_authority TEXT,origin_url TEXT,origin_transport TEXT,
          direct_country_official INTEGER,fallback_used INTEGER,
          fallback_reason TEXT,preferred_origin_source_id TEXT,
          preferred_origin_authority TEXT,
          official_source_registry_sha256 TEXT,origin_json TEXT,
          committed_at TEXT);
        CREATE TABLE reported_trajectory_market_registry(
          station_id TEXT,event_date TEXT,settlement_timezone TEXT,
          source_lane TEXT,source_eligible INTEGER,market_active INTEGER,
          market_accepting_orders INTEGER);
        CREATE TABLE reported_trajectory_asos_p1_oos_artifacts(
          content_sha256 TEXT PRIMARY KEY,station_id TEXT,state TEXT,
          authority_scope TEXT,train_count INTEGER,holdout_count INTEGER,
          artifact_json TEXT,canonical_committed_at_utc TEXT);
        CREATE TABLE reported_trajectory_asos_one_minute_observations(
          observation_id TEXT PRIMARY KEY,source_lane TEXT,station_id TEXT,
          provider_station_id TEXT,valid_at TEXT,temperature_c REAL,
          provider_temperature_f REAL,measurement_semantics TEXT,
          provider_id TEXT,source_class TEXT,upstream_authority TEXT,
          origin_url TEXT,raw_record_sha256 TEXT,source_receipt_sha256 TEXT,
          fetch_started_at TEXT,fetch_completed_at TEXT,
          canonical_committed_at TEXT);
        CREATE TABLE reported_trajectory_madis_hfmetar_observations(
          content_sha256 TEXT PRIMARY KEY,compressed_sha256 TEXT,
          station_id TEXT,provider_station_id TEXT,observed_at_utc TEXT,
          temperature_c REAL,temperature_qc_summary TEXT,
          temperature_sensor_status INTEGER,station_type TEXT,
          data_provider TEXT,measurement_semantics TEXT,
          raw_message_sha256 TEXT,qc_eligible INTEGER,
          canonical_committed_at_utc TEXT);
        CREATE TABLE reported_trajectory_madis_hfmetar_objects(
          compressed_sha256 TEXT PRIMARY KEY,object_hour_utc TEXT,
          object_url TEXT,compressed_bytes INTEGER,retained_path TEXT,
          source_lane TEXT,source_class TEXT,receipt_sha256 TEXT,
          fetch_started_at_utc TEXT,fetch_completed_at_utc TEXT);
        """
    )
    conn.execute(
        "INSERT INTO reported_trajectory_market_registry VALUES"
        "('KLGA','2026-08-09','America/New_York','awc',1,1,1)",
    )
    conn.execute(
        "INSERT INTO reported_trajectory_madis_hfmetar_objects VALUES"
        "('" + "c" * 64 + "','2026-08-09T15:00:00.000Z','u',1,'p',"
        "'MADIS_HFMETAR_ASOS','official_republisher','" + "e" * 64 + "',"
        "'2026-08-09T16:00:00.000Z','2026-08-09T16:00:30.000Z')",
    )
    return conn


def _oos_artifact_row(conn: sqlite3.Connection) -> dict:
    from madis_hfmetar import build_asos_p1_oos_artifact, match_metar_to_asos
    from madis_hfmetar import AsosTemperatureProduct

    minute_rows = [
        AsosTemperatureProduct(
            _now(f"2026-08-01T{hour:02d}:51:00Z"), 20.0 + hour * 0.01, "9" * 64
        )
        for hour in range(24)
    ] + [
        AsosTemperatureProduct(
            _now(f"2026-08-02T{hour:02d}:51:00Z"), 20.0 + hour * 0.01, "8" * 64
        )
        for hour in range(24)
    ]
    matches = [
        match_metar_to_asos(
            {
                "station_id": "KLGA",
                "observation_id": f"m{index}",
                "observed_at_utc": row.observed_at_utc.isoformat().replace(
                    "+00:00", "Z"
                ),
                "reported_temperature_c": row.temperature_c,
                "raw_sha256": "7" * 64,
            },
            minute_rows,
            [],
        )
        for index, row in enumerate(minute_rows)
    ]
    artifact = build_asos_p1_oos_artifact(matches)
    assert artifact["state"] == "OOS_READY"
    conn.execute(
        "INSERT INTO reported_trajectory_asos_p1_oos_artifacts "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            artifact["content_sha256"],
            "KLGA",
            artifact["state"],
            artifact["authority_scope"],
            artifact["train_count"],
            artifact["holdout_count"],
            __import__("json").dumps(artifact, sort_keys=True),
            "2026-08-08T00:00:00.000Z",
        ),
    )
    return artifact


def test_materializer_ingests_carrier_rows_as_soft_evidence() -> None:
    from asos_p1_slot_materializer import (
        materialize_scheduled_asos_p1_observations,
    )

    conn = _archive()
    _oos_artifact_row(conn)
    conn.execute(
        "INSERT INTO reported_trajectory_madis_hfmetar_observations VALUES"
        "('" + "b" * 64 + "','" + "c" * 64 + "','KLGA','KLGA',"
        "'2026-08-09T15:20:00.000Z',20.599994,'',0,'HF-METAR','HF-METAR',"
        "'ASOS_RUNNING_5_MINUTE_AVERAGE_DISSEMINATED_EVERY_5_MINUTES',"
        "'" + "d" * 64 + "',1,'2026-08-09T15:29:00.000Z')",
    )
    # A qc-ineligible row must never be minted.
    conn.execute(
        "INSERT INTO reported_trajectory_madis_hfmetar_observations VALUES"
        "('" + "f" * 64 + "','" + "c" * 64 + "','KLGA','KLGA',"
        "'2026-08-09T15:25:00.000Z',33.0,'X',1,'HF-METAR','HF-METAR',"
        "'ASOS_RUNNING_5_MINUTE_AVERAGE_DISSEMINATED_EVERY_5_MINUTES',"
        "'" + "d" * 64 + "',0,'2026-08-09T15:29:00.000Z')",
    )
    receipt = materialize_scheduled_asos_p1_observations(
        conn,
        ["KLGA"],
        now_utc=_now("2026-08-09T15:30:00Z"),
    )
    assert receipt["ok"], receipt
    assert receipt["inserted_observation_count"] == 1
    rows = conn.execute(
        "SELECT observation_id,source_lane,report_type,valid_at,"
        "reported_temperature_c,source_poll_id "
        "FROM reported_trajectory_observations "
        "WHERE source_lane='madis_hfmetar'"
    ).fetchall()
    assert len(rows) == 1
    observation_id, lane, report_type, valid_at, temperature, receipt_sha = rows[0]
    assert observation_id.startswith("asos_soft_")
    assert (lane, report_type) == ("madis_hfmetar", "ASOS_HF")
    assert valid_at == "2026-08-09T15:20:00.000Z"
    # Quantized to the METAR tenths grid: 20.599994 -> 20.6.
    assert temperature == 20.6
    # The MADIS object's fetch receipt rides the row for provenance.
    assert receipt_sha == "e" * 64
    origin = conn.execute(
        "SELECT origin_transport FROM reported_trajectory_observation_origins "
        "WHERE observation_id=?",
        (observation_id,),
    ).fetchone()
    assert origin == ("asos_p1_soft_path_carrier",)

    # Idempotent: the same reading is never minted twice.
    again = materialize_scheduled_asos_p1_observations(
        conn,
        ["KLGA"],
        now_utc=_now("2026-08-09T16:05:00Z"),
    )
    assert again["inserted_observation_count"] == 0


def test_materializer_needs_the_oos_artifact_and_soft_rows_skip_identity() -> None:
    from asos_p1_slot_materializer import (
        materialize_scheduled_asos_p1_observations,
    )

    conn = _archive()
    conn.execute(
        "INSERT INTO reported_trajectory_madis_hfmetar_observations VALUES"
        "('" + "b" * 64 + "','" + "c" * 64 + "','KLGA','KLGA',"
        "'2026-08-09T15:20:00.000Z',20.599994,'',0,'HF-METAR','HF-METAR',"
        "'ASOS_RUNNING_5_MINUTE_AVERAGE_DISSEMINATED_EVERY_5_MINUTES',"
        "'" + "d" * 64 + "',1,'2026-08-09T15:29:00.000Z')",
    )
    # No OOS artifact: nothing may be materialized, and the scope says why.
    receipt = materialize_scheduled_asos_p1_observations(
        conn,
        ["KLGA"],
        now_utc=_now("2026-08-09T15:30:00Z"),
    )
    assert receipt["inserted_observation_count"] == 0
    assert any(
        scope["state"] == "oos_artifact_missing"
        for scope in receipt["scopes"]
    )

    _oos_artifact_row(conn)
    receipt = materialize_scheduled_asos_p1_observations(
        conn,
        ["KLGA"],
        now_utc=_now("2026-08-09T15:30:00Z"),
    )
    assert receipt["inserted_observation_count"] == 1
    # Soft carriers never enter the atomic identity lane: the substitute
    # identity builder scans the retired formal prefix only.
    assert build_substitute_observation_contract(
        conn,
        as_of_utc="2026-08-09T17:00:00Z",
        window_days=3,
    ) is None


# ---------------------------------------------------------------------------
# The substitute identity lane: parallel facts that let a substitute-closed
# P1 root an atomic plan (user ruling 2026-08-28), without touching the PR12
# truth lane.
# ---------------------------------------------------------------------------

from bayes_basket_target.asos_substitute_observation_contract import (  # noqa: E402
    AUTHORITY_SCOPE,
    CanonicalSubstituteObservationRevisionFact,
    SUBSTITUTE_ORIGINS,
    SUBSTITUTE_TRANSPORT,
    build_substitute_observation_contract,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402


def _substitute_fact(**overrides) -> dict:
    identity = {
        "station_id": "KLGA",
        "observation_id": "asos_p1_" + "f" * 32,
        "revision_no": 1,
        "event_date": "2026-08-09",
        "settlement_timezone": "America/New_York",
        "source_lane": "MADIS_HFMETAR",
        "report_type": "ASOS_HF",
        "authority_scope": AUTHORITY_SCOPE,
        "observed_at_utc": "2026-08-09T15:50:00.000Z",
        "source_observation_committed_at_utc": "2026-08-09T16:07:00.000Z",
        "canonical_committed_at_utc": "2026-08-09T16:07:00.000Z",
        "reported_temperature_c": 30.6,
        "raw_object_kind": "MADIS_HFMETAR_DECODED_ROW_V1",
        "raw_object_sha256": "1" * 64,
        "source_receipt_sha256": "2" * 64,
        "asos_p1_oos_artifact_content_sha256": "3" * 64,
        "schedule_content_sha256": "4" * 64,
        "planned_metar_observation_at_utc": "2026-08-09T15:51:00.000Z",
        **SUBSTITUTE_ORIGINS["MADIS_HFMETAR"],
        "origin_transport": SUBSTITUTE_TRANSPORT,
        "qc_state": "ACCEPTED",
    }
    identity.update(overrides)
    return {**identity, "content_sha256": content_sha256(identity)}


def test_substitute_identity_fact_round_trip_and_refusals() -> None:
    fact = CanonicalSubstituteObservationRevisionFact.from_mapping(
        _substitute_fact()
    )
    assert fact.raw["source_lane"] == "MADIS_HFMETAR"
    # A lane may not borrow another lane's report type.
    with pytest.raises(ContractError, match="report_type_invalid"):
        CanonicalSubstituteObservationRevisionFact.from_mapping(
            _substitute_fact(report_type="METAR")
        )
    # AWC is not a substitute lane; its identities stay PR12's.
    with pytest.raises(ContractError, match="source_lane_invalid"):
        CanonicalSubstituteObservationRevisionFact.from_mapping(
            _substitute_fact(source_lane="AWC")
        )
    # The observed minute must be the frozen slot's own carrier minute.
    with pytest.raises(ContractError, match="slot_binding_invalid"):
        CanonicalSubstituteObservationRevisionFact.from_mapping(
            _substitute_fact(observed_at_utc="2026-08-09T15:45:00.000Z")
        )
    # Substitutes are immutable: revision 2 could only be fabrication.
    with pytest.raises(ContractError, match="revision_no_invalid"):
        CanonicalSubstituteObservationRevisionFact.from_mapping(
            _substitute_fact(revision_no=2)
        )


def test_substitute_fact_roots_an_atomic_bundle() -> None:
    from bayes_basket_target.atomic_ledger import build_target_atomic_bundle

    fact = _substitute_fact()
    bound = {
        "bound_id": "bound-sub-1",
        "station_id": "KLGA",
        "event_date": "2026-08-09",
        "bound_generation": 3,
        "hard_lower_bound": 87,
        "bound_json": {"source": "asos_substitute", "excluded_buckets": []},
    }
    plan = {
        "strategy_lane": "basket",
        "settlement_timezone": "America/New_York",
        "probability_checkpoint_id": "p1-sub-1",
        "quote_snapshot_id": "quote-sub-1",
        "lineage": {
            "probability_stage": "P1",
            "settlement_timezone": "America/New_York",
            "canonical_observation_event_id": fact["observation_id"],
            "truth_observation_revision_content_sha256": fact[
                "content_sha256"
            ],
            "canonical_observation_committed_at": fact[
                "source_observation_committed_at_utc"
            ],
            "hard_bound_id": "bound-sub-1",
            "source_lane": "MADIS_HFMETAR",
            "report_type": "ASOS_HF",
        },
    }
    decision = {
        "strategy_lane": "basket",
        "decided_state": "blocked",
        "will_order": False,
        "reason_codes": ["missing_quote"],
        "details": {"candidate_will_order": False, "no_live": True},
    }
    bundle = build_target_atomic_bundle(
        awc_observation_contract=None,
        observed_revision_fact=fact,
        truth_observation_revision_content_sha256=fact["content_sha256"],
        bound=bound,
        plan=plan,
        decision=decision,
        intents=[],
        reserved_amount_usdc=0.0,
        created_at_utc="2026-08-09T16:30:00Z",
        execution_version_vectors=[],
    )
    observation = bundle["observation"]
    assert observation["source_lane"] == "MADIS_HFMETAR"
    assert observation["report_type"] == "ASOS_HF"
    # The projection's receipt slot carries the substitute's own source
    # receipt (MADIS object fetch), under its historical key.
    assert observation["awc_source_receipt_sha256"] == fact[
        "source_receipt_sha256"
    ]
    assert (
        observation["truth_observation_revision_content_sha256"]
        == fact["content_sha256"]
    )


def test_attach_resolves_substitute_identity_facts() -> None:
    from bayes_basket_target.resident_canonical import (
        attach_truth_observation_revisions,
    )

    fact = _substitute_fact()
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE canonical_ssot_fact("
        "fact_id TEXT PRIMARY KEY, fact_type TEXT NOT NULL,"
        "scope_key TEXT NOT NULL, content_sha256 TEXT NOT NULL,"
        "fact_json TEXT NOT NULL, lineage_json TEXT NOT NULL,"
        "ingested_at_utc TEXT NOT NULL)"
    )
    import json as _json

    scope_key = "|".join([
        fact["station_id"], fact["observation_id"], "1",
        fact["observed_at_utc"], fact["canonical_committed_at_utc"],
    ])
    conn.execute(
        "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
        (
            "substitute_observation_revision:" + fact["content_sha256"],
            "substitute_observation_revision",
            scope_key,
            fact["content_sha256"],
            _json.dumps(fact, sort_keys=True),
            "{}",
            "2026-08-09T16:20:00.000Z",
        ),
    )
    rows = attach_truth_observation_revisions(
        conn,
        [{"observation_id": fact["observation_id"]}],
        station_id="KLGA",
        as_of_utc="2026-08-09T17:00:00Z",
    )
    assert rows[0]["truth_observation_revision_content_sha256"] == fact[
        "content_sha256"
    ]
    # Strict as-of: a fact staged after the decision stays invisible.
    early = attach_truth_observation_revisions(
        conn,
        [{"observation_id": fact["observation_id"]}],
        station_id="KLGA",
        as_of_utc="2026-08-09T16:00:00Z",
    )
    assert "truth_observation_revision_content_sha256" not in early[0]


def test_target_atomic_lineage_gate_admits_the_pairing_only() -> None:
    from bayes_basket_target.production_io import (
        target_atomic_facts_from_cycle,
    )

    def cycle(lane: str, report_type: str) -> dict:
        return {
            "probability_envelope": {
                "stage": "P1",
                "station_id": "KLGA",
                "event_date": "2026-08-09",
                "settlement_timezone": "America/New_York",
                "content_sha256": "9" * 64,
                "lineage": {
                    "canonical_observation_event_id": "asos_p1_" + "f" * 32,
                    "truth_observation_revision_content_sha256": "8" * 64,
                    "canonical_observation_committed_at": (
                        "2026-08-09T16:07:00.000Z"
                    ),
                    "settlement_timezone": "America/New_York",
                    "hard_bound_id": "bound-1",
                    "source_lane": lane,
                    "report_type": report_type,
                },
            },
        }

    accepted = target_atomic_facts_from_cycle(
        cycle("MADIS_HFMETAR", "ASOS_HF")
    )
    assert (
        accepted["truth_observation_revision_content_sha256"] == "8" * 64
    )
    with pytest.raises(ContractError, match="target_atomic_lineage_source_invalid"):
        target_atomic_facts_from_cycle(cycle("MADIS_HFMETAR", "METAR"))
    with pytest.raises(ContractError, match="target_atomic_lineage_source_invalid"):
        target_atomic_facts_from_cycle(cycle("AMSC", "METAR"))


def test_substitute_rooted_bundle_commits_through_the_ledger() -> None:
    """The whole money path: staged identity fact -> bundle -> commit."""

    from bayes_basket_target.atomic_ledger import (
        apply_target_ledger_schema,
        build_target_atomic_bundle,
        commit_target_atomic_bundle,
        initialize_capital_pool,
    )
    from bayes_basket_target.canonical_ingress import (
        stage_substitute_observation_contract,
    )
    from bayes_basket_target.canonical_ssot_v17_migration import (
        TARGET_TABLE_DDL as V17_TABLE_DDL,
        TARGET_TRIGGER_DDL as V17_TRIGGER_DDL,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA busy_timeout=10000")
    apply_target_ledger_schema(conn)
    conn.execute(V17_TABLE_DDL)
    for ddl in V17_TRIGGER_DDL:
        conn.execute(ddl)
    initialize_capital_pool(
        conn,
        balance_usdc=20.0,
        capital_epoch_id="epoch-1",
        updated_at_utc="2026-08-09T16:00:00Z",
    )
    fact = _substitute_fact()
    contract = {
        "schema": (
            "bayes_basket_target_canonical_substitute_observation_contract_v1"
        ),
        "as_of_utc": "2026-08-09T16:20:00.000Z",
        "builder_id": "test-builder",
        "revision_facts": [fact],
    }
    contract["content_sha256"] = content_sha256(contract)
    conn.execute("BEGIN IMMEDIATE")
    staged = stage_substitute_observation_contract(
        conn, contract, ingested_at_utc="2026-08-09T16:20:00.000Z"
    )
    conn.commit()
    assert staged["state"] == "STAGED_IN_CALLER_TRANSACTION"

    bound = {
        "bound_id": "bound-sub-2",
        "station_id": "KLGA",
        "event_date": "2026-08-09",
        "bound_generation": 3,
        "hard_lower_bound": 87,
        "bound_json": {"source": "asos_substitute", "excluded_buckets": []},
    }
    plan = {
        "strategy_lane": "basket",
        "settlement_timezone": "America/New_York",
        "probability_checkpoint_id": "p1-sub-2",
        "quote_snapshot_id": "quote-sub-2",
        "lineage": {
            "probability_stage": "P1",
            "settlement_timezone": "America/New_York",
            "canonical_observation_event_id": fact["observation_id"],
            "truth_observation_revision_content_sha256": fact[
                "content_sha256"
            ],
            "canonical_observation_committed_at": fact[
                "source_observation_committed_at_utc"
            ],
            "hard_bound_id": "bound-sub-2",
            "source_lane": "MADIS_HFMETAR",
            "report_type": "ASOS_HF",
        },
    }
    decision = {
        "strategy_lane": "basket",
        "decided_state": "blocked",
        "will_order": False,
        "reason_codes": ["missing_quote"],
        "details": {"candidate_will_order": False, "no_live": True},
    }
    bundle = build_target_atomic_bundle(
        awc_observation_contract=None,
        observed_revision_fact=fact,
        truth_observation_revision_content_sha256=fact["content_sha256"],
        bound=bound,
        plan=plan,
        decision=decision,
        intents=[],
        reserved_amount_usdc=0.0,
        created_at_utc="2026-08-09T16:30:00Z",
        execution_version_vectors=[],
    )
    receipt = commit_target_atomic_bundle(conn, bundle=bundle)
    assert str(receipt["state"]).upper() in {
        "COMMITTED", "ALREADY_COMMITTED"
    }, receipt
    row = conn.execute(
        "SELECT source_lane,report_type,awc_source_receipt_sha256,"
        "truth_observation_revision_content_sha256 "
        "FROM basket_target_observation_facts WHERE observation_id=?",
        (fact["observation_id"],),
    ).fetchone()
    assert row == (
        "MADIS_HFMETAR",
        "ASOS_HF",
        fact["source_receipt_sha256"],
        fact["content_sha256"],
    )


def test_trajectory_layer_requires_a_formal_observation() -> None:
    """An all-soft prefix has no settlement claim to stand on."""

    from bayes_basket_target.metar_trajectory import (
        compile_metar_trajectory_update_proof,
    )
    from bayes_basket_target.p1_compiler import (
        _fallback_trajectory_artifact,
    )

    root = _p0_root()
    artifact = _fallback_trajectory_artifact(
        root,
        source_lane="awc",
        maturity_bin="D0",
        as_of_utc="2026-08-09T16:00:00Z",
    )
    soft_only = [
        _event(
            "asos_soft_" + "a" * 32,
            lane="madis_hfmetar",
            report_type="ASOS_HF",
            observed="2026-08-09T15:20:00.000000Z",
            received="2026-08-09T15:29:00.000000Z",
            temperature_c=23.0,
        )
    ]
    with pytest.raises(
        ContractError, match="formal_observation_required"
    ):
        compile_metar_trajectory_update_proof(
            root,
            observations=soft_only,
            artifact=artifact,
            bucket_centers_c={
                "75°F or below": 23.3, "76-77°F": 24.4, "78°F or higher": 25.9,
            },
            bucket_upper_bounds_c={
                "75°F or below": 24.2, "76-77°F": 25.3, "78°F or higher": 99.0,
            },
            maturity_bin="D0",
            as_of_utc="2026-08-09T16:00:00Z",
            prefix_is_current=True,
        )


def test_path_state_tolerates_shared_minutes_and_soft_tail() -> None:
    """Dense carriers: equal minutes collapse, a soft tail keeps the state.

    Both were measured fail-closed in the 2026-08-29 uplift replay: a METAR
    and a carrier row on the same minute raised ``time_rewind`` (4 of 41
    samples) and a prefix ending on a text-less carrier row raised
    ``state_incomplete`` (17 of 41).
    """

    from bayes_basket_target.metar_path_state import (
        compile_conditional_metar_path_state,
    )

    metar = (
        "KLGA 091451Z 18008KT 10SM FEW250 20/12 A3012 RMK AO2 T02000117"
    )
    rows = [
        {
            "observation_id": "awc-1",
            "observed_at_utc": "2026-08-09T14:51:00.000000Z",
            "temperature_c": 20.0,
            "raw_text": metar,
        },
        # Same minute, second carrier: legal, not a rewind.
        {
            "observation_id": "asos_soft_1",
            "observed_at_utc": "2026-08-09T14:51:00.000000Z",
            "temperature_c": 20.1,
            "raw_text": None,
        },
        {
            "observation_id": "asos_soft_2",
            "observed_at_utc": "2026-08-09T15:20:00.000000Z",
            "temperature_c": 21.4,
            "raw_text": None,
        },
    ]
    state = compile_conditional_metar_path_state(
        rows,
        as_of_utc="2026-08-09T15:25:00.000000Z",
        settlement_timezone="America/New_York",
        root_probabilities={"a": 0.5, "b": 0.5},
        bucket_centers_c={"a": 20.0, "b": 22.0},
        future_curve=None,
        root_checkpoint_id="p0",
        root_probability_sha256="a" * 64,
        station_id="KLGA",
        event_date="2026-08-09",
    )
    # The soft tail carries the current temperature and the slope...
    assert state.current_temperature_c == 21.4
    assert state.momentum_c_per_hour > 0.0
    # ...while the last parsable METAR still speaks for the physical state.
    assert state.complete
    assert state.cloud_cover_fraction >= 0.0

    # A parsable state older than two hours is honestly incomplete again.
    stale = compile_conditional_metar_path_state(
        [
            dict(rows[0]),
            {
                "observation_id": "asos_soft_3",
                "observed_at_utc": "2026-08-09T17:05:00.000000Z",
                "temperature_c": 22.0,
                "raw_text": None,
            },
            {
                "observation_id": "asos_soft_4",
                "observed_at_utc": "2026-08-09T17:10:00.000000Z",
                "temperature_c": 22.1,
                "raw_text": None,
            },
        ],
        as_of_utc="2026-08-09T17:15:00.000000Z",
        settlement_timezone="America/New_York",
        root_probabilities={"a": 0.5, "b": 0.5},
        bucket_centers_c={"a": 20.0, "b": 22.0},
        future_curve=None,
        root_checkpoint_id="p0",
        root_probability_sha256="a" * 64,
        station_id="KLGA",
        event_date="2026-08-09",
    )
    assert not stale.complete
