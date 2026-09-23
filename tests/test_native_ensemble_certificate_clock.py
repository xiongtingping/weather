"""Native-ensemble certificates never simply end (user ruling 2026-09-23).

A scope whose newest certificate is inside ten days of its end, with nothing
fresher behind it, is renewed: the same evidence under a new window, a new
generation, a receipt.  Fresh beats stale (a scope with a fresher row is left
alone), stale beats nothing (the renewed rows satisfy the reader's unchanged
window predicate), and the board reads the receipts.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_certificate_clock as clock  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.ssot import (  # noqa: E402
    apply_native_ensemble_certificate_renewal_schema,
    apply_target_ssot_schema,
    native_ensemble_certificate_renewal_ddl,
)

T0 = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
SYSTEMS = ("ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05")
STATION = "KMIA"
SEASON = "DESCENT"  # KMIA serves PEAK+DESCENT at T0, DESCENT alone in October
LEAD = "h24_48"
SETTLEMENT = "official_daily_tmax_c"
HEX = "0" * 64


def _utc(moment: datetime) -> str:
    return clock.utc_text(moment)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ssot_schema(conn)
    for system_id in SYSTEMS:
        conn.execute(
            "INSERT INTO calibration_native_ensemble_system VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                system_id, system_id, system_id.split("_")[0],
                system_id.split("_")[0], "NATIVE_ENSEMBLE_PROVIDER", 0, 51,
                "official_native_member_v1",
                "native_timestamps_required_for_formal_v1",
                "2026-07-01T00:00:00Z", "9999-12-31T23:59:59Z",
                content_sha256({"system": system_id}),
            ),
        )
    conn.commit()
    return conn


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    columns = tuple(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(row[column] for column in columns),
    )


def _mint_system(
    conn: sqlite3.Connection, *, system_id: str, run_cycle: str,
    valid_from: datetime, valid_until: datetime, generation: int = 1,
    bias_c: float = 1.5, train_end: str = "2026-08-20",
    holdout: tuple[str, str] = ("2026-08-21", "2026-08-28"),
    station_id: str = STATION, lead_bucket: str = LEAD,
    season_bin: str = SEASON,
) -> tuple[str, str]:
    """A system artifact and its exact-key activation; returns their ids."""

    identity = {
        "ensemble_system_id": system_id, "station_id": station_id,
        "season_bin": season_bin, "lead_bucket": lead_bucket,
        "run_cycle": run_cycle, "settlement_definition": SETTLEMENT,
        "calibration_method": "member_bias_spread_reliability_v1",
        "bias_parameters_json": json.dumps({"bias_c": bias_c}),
        "spread_parameters_json": json.dumps({"spread_factor": 2.0}),
        "reliability_parameters_json": json.dumps({"train_n_effective": 9.0}),
        "member_qc_contract_sha256": HEX,
        "train_start_date": "2026-07-25", "train_end_date": train_end,
        "holdout_start_date": holdout[0], "holdout_end_date": holdout[1],
        "rolling_origin_fold_count": 8, "effective_sample_n": 9.0,
        "oos_metrics_json": json.dumps({"crps": 0.9}),
        "valid_from_utc": _utc(valid_from),
        "valid_until_utc": _utc(valid_until),
    }
    sha = content_sha256(identity)
    artifact_id = "native-system-artifact-" + sha[:24]
    _insert(conn, "calibration_native_ensemble_system_artifact", {
        "system_artifact_id": artifact_id, **identity, "content_sha256": sha,
    })
    activation = {
        "artifact_kind": clock.SYSTEM_KIND, "artifact_id": artifact_id,
        "station_id": station_id, "ensemble_system_id_or_star": system_id,
        "season_bin": season_bin, "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "run_cycle_by_system_json": clock._json({system_id: run_cycle}),
        "run_cycle_by_system_sha256": content_sha256({system_id: run_cycle}),
        "settlement_definition": SETTLEMENT,
        "available_system_set_hash_or_star": "*",
        "generation": generation,
        "valid_from_utc": _utc(valid_from),
        "valid_until_utc": _utc(valid_until),
        "activated_at_utc": _utc(valid_from),
        "authority_scope": clock.AUTHORITY,
    }
    asha = content_sha256(activation)
    activation_id = "native-activation-" + asha[:24]
    _insert(conn, "calibration_native_ensemble_activation", {
        "activation_id": activation_id, **activation, "content_sha256": asha,
    })
    return artifact_id, activation_id


def _mint_cross(
    conn: sqlite3.Connection, *, cycle_by_system: dict[str, str],
    valid_from: datetime, valid_until: datetime, generation: int = 1,
    station_id: str = STATION, lead_bucket: str = LEAD,
    with_evidence: bool = True, season_bin: str = SEASON,
) -> tuple[str, str]:
    """A cross artifact (+ its selection evidence) and activation."""

    systems = sorted(cycle_by_system)
    cycle_json = clock._json(cycle_by_system)
    cycle_sha = content_sha256(cycle_by_system)
    run_cycle = "MIXED_NATIVE_CYCLES"
    evidence = {
        "station_id": station_id, "season_bin": season_bin,
        "lead_bucket": lead_bucket, "run_cycle": run_cycle,
        "run_cycle_by_system_json": cycle_json,
        "run_cycle_by_system_sha256": cycle_sha,
        "settlement_definition": SETTLEMENT, "method_version": "test_v1",
        "candidate_system_ids_json": json.dumps(systems),
        "selected_system_ids_json": json.dumps(systems),
        "selected_system_count": len(systems),
        "independent_family_count": len(systems),
        "selected_weights_json": json.dumps({s: 0.5 for s in systems}),
        "family_weights_json": json.dumps({s: 0.5 for s in systems}),
        "family_weight_caps_json": json.dumps({}),
        "supported_exact_masks_json": json.dumps([]),
        "train_start_date": "2026-07-25", "train_end_date": "2026-08-20",
        "holdout_start_date": "2026-08-21", "holdout_end_date": "2026-08-28",
        "rolling_origin_fold_count": 8, "effective_sample_n": 9.0,
        "oos_metrics_json": json.dumps({"crps": 0.8}),
        "cross_system_residual_correlation_json": json.dumps({}),
        "deterministic_baseline_metrics_json": json.dumps({}),
    }
    esha = content_sha256(evidence)
    if with_evidence:
        _insert(
            conn, "calibration_cross_native_ensemble_selection_evidence",
            {"selection_evidence_id": "sel-" + esha[:24], **evidence,
             "content_sha256": esha},
        )
    artifact = {
        "station_id": station_id, "season_bin": season_bin,
        "lead_bucket": lead_bucket, "run_cycle": run_cycle,
        "run_cycle_by_system_json": cycle_json,
        "run_cycle_by_system_sha256": cycle_sha,
        "settlement_definition": SETTLEMENT,
        "ensemble_system_ids_json": json.dumps(systems),
        "ensemble_system_count": len(systems),
        "independent_family_count": len(systems),
        "supported_exact_masks_json": json.dumps([]),
        "selection_evidence_sha256": esha,
        "combination_method": "inverse_crps_weighted_v1",
        "family_weight_caps_json": json.dumps({}),
        "probability_pathway": "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE",
        "valid_from_utc": _utc(valid_from),
        "valid_until_utc": _utc(valid_until),
    }
    sha = content_sha256(artifact)
    artifact_id = "native-cross-artifact-" + sha[:24]
    _insert(conn, "calibration_cross_native_ensemble_artifact", {
        "cross_ensemble_artifact_id": artifact_id, **artifact,
        "content_sha256": sha,
    })
    set_hash = content_sha256(systems)
    activation = {
        "artifact_kind": clock.CROSS_KIND, "artifact_id": artifact_id,
        "station_id": station_id, "ensemble_system_id_or_star": "*",
        "season_bin": season_bin, "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "run_cycle_by_system_json": cycle_json,
        "run_cycle_by_system_sha256": cycle_sha,
        "settlement_definition": SETTLEMENT,
        "available_system_set_hash_or_star": set_hash,
        "generation": generation,
        "valid_from_utc": _utc(valid_from),
        "valid_until_utc": _utc(valid_until),
        "activated_at_utc": _utc(valid_from),
        "authority_scope": clock.AUTHORITY,
    }
    asha = content_sha256(activation)
    activation_id = "native-activation-" + asha[:24]
    _insert(conn, "calibration_native_ensemble_activation", {
        "activation_id": activation_id, **activation, "content_sha256": asha,
    })
    return artifact_id, activation_id


def _stalled_scope(conn: sqlite3.Connection) -> dict[str, str]:
    """Minted 25 days ago, ending in 5: inside the ten-day horizon."""

    minted = T0 - timedelta(days=25)
    ends = minted + timedelta(days=30)
    cycle_by_system = {SYSTEMS[0]: "00Z", SYSTEMS[1]: "06Z"}
    ids: dict[str, str] = {}
    for system_id, run_cycle in cycle_by_system.items():
        ids[system_id], ids[system_id + ":activation"] = _mint_system(
            conn, system_id=system_id, run_cycle=run_cycle,
            valid_from=minted, valid_until=ends,
        )
    ids["cross"], ids["cross:activation"] = _mint_cross(
        conn, cycle_by_system=cycle_by_system,
        valid_from=minted, valid_until=ends,
    )
    conn.commit()
    return ids


def _reader_window_rows(
    conn: sqlite3.Connection, *, kind: str, at: datetime, **scope: str,
) -> list[tuple]:
    """The reader's own predicate: valid_from <= at < valid_until, newest
    generation first."""

    where = " AND ".join(f"{column}=?" for column in scope)
    return conn.execute(
        "SELECT activation_id,artifact_id,generation "
        "FROM calibration_native_ensemble_activation "
        f"WHERE artifact_kind=? AND {where} AND authority_scope=? "
        "AND valid_from_utc<=? AND valid_until_utc>? "
        "ORDER BY generation DESC,activation_id",
        (kind, *scope.values(), clock.AUTHORITY, _utc(at), _utc(at)),
    ).fetchall()


# --------------------------------------------------------------------------


def test_schema_declares_the_renewal_table_once():
    conn = _db()
    names = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        )
    }
    assert clock.RENEWAL_TABLE in names
    assert "calibration_native_certificate_renewal_scope_idx" in names
    ddl = native_ensemble_certificate_renewal_ddl()
    assert ddl.count("CREATE TABLE") == 1 and ddl.count("CREATE INDEX") == 1
    assert ddl.count("CREATE TRIGGER") == 2  # append-only, like every table
    bare = sqlite3.connect(":memory:")
    assert not clock.table_exists(bare, clock.RENEWAL_TABLE)
    apply_native_ensemble_certificate_renewal_schema(bare)
    apply_native_ensemble_certificate_renewal_schema(bare)
    assert clock.table_exists(bare, clock.RENEWAL_TABLE)
    bare.execute(
        f"INSERT INTO {clock.RENEWAL_TABLE} VALUES "
        "('r1','CROSS_NATIVE_ENSEMBLE','KMIA','*','PEAK','h24_48','x',NULL,"
        "'a','b','c','d','2026-08-20','2026-08-28','2026-09-28T00:00:00Z',"
        "'2026-09-23T00:00:00Z','2026-10-23T00:00:00Z','why','"
        + "0" * 64 + "')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        bare.execute(f"DELETE FROM {clock.RENEWAL_TABLE}")
    with pytest.raises(sqlite3.IntegrityError):
        bare.execute(f"UPDATE {clock.RENEWAL_TABLE} SET run_cycle='y'")


def test_stalled_scope_is_renewed_with_its_members_and_receipted():
    conn = _db()
    ids = _stalled_scope(conn)
    plan = clock.plan_renewals(conn, now=T0)
    assert [item["role"] for item in plan["items"]] == [
        "cross", "cross_member", "cross_member",
    ]
    assert len(plan["stalled_cross_scopes"]) == 1
    # The two member scopes are stalled too, and are renewed once, as
    # members, not a second time on their own.
    assert len(plan["stalled_system_scopes"]) == 2
    assert plan["skipped"] == {}
    result = clock.renew_expiring_certificates(conn, now=T0, commit=True)
    assert result["committed"] is True
    assert result["counts"] == {
        "system_artifacts": 2, "cross_artifacts": 1,
        "activations": 3, "receipts": 3,
    }
    until = _utc(T0 + timedelta(days=clock.VALID_DAYS))
    # The renewed cross artifact carries the source's evidence, byte for
    # byte, under the new clocks and a new address.
    source, renewed = [
        dict(zip([d[0] for d in cur.description], row))
        for cur, row in (
            (cur, cur.fetchone()) for cur in (
                conn.execute(
                    "SELECT * FROM calibration_cross_native_ensemble_artifact "
                    "WHERE cross_ensemble_artifact_id=?", (ids["cross"],),
                ),
                conn.execute(
                    "SELECT * FROM calibration_cross_native_ensemble_artifact "
                    "WHERE cross_ensemble_artifact_id<>? ", (ids["cross"],),
                ),
            )
        )
    ]
    for column, value in source.items():
        if column in (
            "cross_ensemble_artifact_id", "content_sha256",
            "valid_from_utc", "valid_until_utc",
        ):
            assert renewed[column] != value
        else:
            assert renewed[column] == value, column
    assert renewed["valid_from_utc"] == _utc(T0)
    assert renewed["valid_until_utc"] == until
    # Each activation key got exactly one new generation.
    rows = conn.execute(
        "SELECT artifact_kind,ensemble_system_id_or_star,run_cycle,"
        "generation,valid_until_utc FROM calibration_native_ensemble_activation "
        "ORDER BY artifact_kind,ensemble_system_id_or_star,generation",
    ).fetchall()
    assert [row[3] for row in rows] == [1, 2, 1, 2, 1, 2]
    assert all(row[4] == until for row in rows if row[3] == 2)
    receipts = conn.execute(
        f"SELECT artifact_kind,ensemble_system_id_or_star,run_cycle,"
        f"source_activation_id,evidence_holdout_end_date,renewal_reason "
        f"FROM {clock.RENEWAL_TABLE} ORDER BY artifact_kind,"
        f"ensemble_system_id_or_star",
    ).fetchall()
    assert [row[0] for row in receipts] == [
        clock.CROSS_KIND, clock.SYSTEM_KIND, clock.SYSTEM_KIND,
    ]
    assert receipts[0][3] == ids["cross:activation"]
    assert {row[4] for row in receipts} == {"2026-08-28"}
    assert {row[5] for row in receipts} == {clock.RENEWAL_REASON}


def test_renewal_is_idempotent_and_stops_once_the_lane_is_fresh():
    conn = _db()
    _stalled_scope(conn)
    first = clock.renew_expiring_certificates(conn, now=T0, commit=True)
    assert first["planned"] == 3
    again = clock.renew_expiring_certificates(
        conn, now=T0 + timedelta(hours=1), commit=True,
    )
    assert again["planned"] == 0
    assert again["counts"]["activations"] == 0
    # Twenty days on, the renewals are inside the horizon again and are
    # renewed again -- unless the lane has minted something fresher.
    later = T0 + timedelta(days=21)
    plan = clock.plan_renewals(conn, now=later)
    assert len(plan["items"]) == 3
    fresh_from = later - timedelta(hours=2)
    _mint_cross(
        conn, cycle_by_system={SYSTEMS[0]: "00Z", SYSTEMS[1]: "06Z"},
        valid_from=fresh_from, valid_until=fresh_from + timedelta(days=30),
        generation=3, with_evidence=False,
    )
    for system_id, run_cycle in ((SYSTEMS[0], "00Z"), (SYSTEMS[1], "06Z")):
        _mint_system(
            conn, system_id=system_id, run_cycle=run_cycle,
            valid_from=fresh_from,
            valid_until=fresh_from + timedelta(days=30),
            generation=3, bias_c=0.7, train_end="2026-10-05",
            holdout=("2026-10-06", "2026-10-13"),
        )
    conn.commit()
    assert clock.plan_renewals(conn, now=later)["items"] == []


def test_fresh_beats_stale_a_scope_with_a_fresher_row_is_left_alone():
    conn = _db()
    minted = T0 - timedelta(days=25)
    cycle_by_system = {SYSTEMS[0]: "00Z", SYSTEMS[1]: "06Z"}
    for system_id, run_cycle in cycle_by_system.items():
        _mint_system(
            conn, system_id=system_id, run_cycle=run_cycle,
            valid_from=minted, valid_until=minted + timedelta(days=30),
        )
    _mint_cross(
        conn, cycle_by_system=cycle_by_system,
        valid_from=minted, valid_until=minted + timedelta(days=30),
    )
    # The lane is alive: a pairing minted three days ago ends in 27.
    recent = T0 - timedelta(days=3)
    _mint_cross(
        conn, cycle_by_system={SYSTEMS[0]: "12Z", SYSTEMS[1]: "12Z"},
        valid_from=recent, valid_until=recent + timedelta(days=30),
        with_evidence=False, generation=2,
    )
    conn.commit()
    plan = clock.plan_renewals(conn, now=T0)
    assert plan["stalled_cross_scopes"] == []
    # The two system scopes still have nothing fresher at any cycle, so
    # they are renewed on their own -- the reader's charter-101 borrow and
    # provisional peers read them.
    assert {item["role"] for item in plan["items"]} == {"system"}
    assert len(plan["items"]) == 2


def test_renewed_rows_satisfy_the_readers_window_predicate():
    conn = _db()
    ids = _stalled_scope(conn)
    at = T0 + timedelta(days=20)  # past the source's end (T0 + 5 days)
    assert _reader_window_rows(
        conn, kind=clock.CROSS_KIND, at=at, station_id=STATION,
        season_bin=SEASON, lead_bucket=LEAD,
    ) == []
    clock.renew_expiring_certificates(conn, now=T0, commit=True)
    cross_rows = _reader_window_rows(
        conn, kind=clock.CROSS_KIND, at=at, station_id=STATION,
        season_bin=SEASON, lead_bucket=LEAD,
    )
    assert len(cross_rows) == 1 and cross_rows[0][2] == 2
    assert cross_rows[0][1] != ids["cross"]
    renewed_cross_artifact = conn.execute(
        "SELECT valid_from_utc,valid_until_utc "
        "FROM calibration_cross_native_ensemble_artifact "
        "WHERE cross_ensemble_artifact_id=?", (cross_rows[0][1],),
    ).fetchone()
    assert renewed_cross_artifact[0] <= _utc(at) < renewed_cross_artifact[1]
    for system_id, run_cycle in ((SYSTEMS[0], "00Z"), (SYSTEMS[1], "06Z")):
        rows = _reader_window_rows(
            conn, kind=clock.SYSTEM_KIND, at=at, station_id=STATION,
            ensemble_system_id_or_star=system_id, season_bin=SEASON,
            lead_bucket=LEAD, run_cycle=run_cycle,
        )
        # Exactly one row at the top generation: the reader refuses a tie.
        assert len(rows) == 1 and rows[0][2] == 2
        artifact = conn.execute(
            "SELECT bias_parameters_json,valid_until_utc "
            "FROM calibration_native_ensemble_system_artifact "
            "WHERE system_artifact_id=? AND valid_from_utc<=? "
            "AND valid_until_utc>?",
            (rows[0][1], _utc(at), _utc(at)),
        ).fetchone()
        assert artifact is not None
        assert json.loads(artifact[0]) == {"bias_c": 1.5}


def test_dry_run_writes_nothing_and_runs_read_only():
    conn = _db()
    _stalled_scope(conn)
    before = conn.execute(
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation"
    ).fetchone()[0]
    result = clock.renew_expiring_certificates(conn, now=T0, commit=False)
    assert result["committed"] is False and result["planned"] == 3
    assert result["receipts"] == []
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation"
    ).fetchone()[0] == before
    conn.execute("PRAGMA query_only=1")
    clock.plan_renewals(conn, now=T0)  # a read-only connection suffices


def test_board_reads_renewed_scopes_then_recovery_then_unrenewed_ends():
    conn = _db()
    _stalled_scope(conn)
    # Before the lane runs: the scope ends in five days and nothing has
    # renewed it -- amber, and red inside three days.
    health = clock.certificate_health(conn, now=T0)
    assert health["health"] == "amber"
    assert health["renewed_scope_count"] == 0
    assert health["unrenewed_scope_count"] == 3  # cross + two system scopes
    assert "未续" in health["note"]
    health = clock.certificate_health(conn, now=T0 + timedelta(days=3))
    assert health["health"] == "red"
    # Renewed: amber, with the evidence age the board shows.
    clock.renew_expiring_certificates(conn, now=T0, commit=True)
    health = clock.certificate_health(conn, now=T0 + timedelta(days=1))
    assert health["health"] == "amber"
    assert health["unrenewed_scope_count"] == 0
    assert health["renewed_scope_count"] == 3
    cross = [
        row for row in health["renewed"]
        if row["artifact_kind"] == clock.CROSS_KIND
    ][0]
    assert cross["serving_on_renewal"] is True
    assert cross["evidence_holdout_end_date"] == "2026-08-28"
    assert cross["evidence_age_days"] == pytest.approx(27.5, abs=0.1)
    assert "续期证书" in health["note"]
    # A month-old evidence behind a renewal is red.
    assert clock.certificate_health(
        conn, now=T0 + timedelta(days=4),
    )["health"] == "red"
    # The lane recovers: a fresher pairing and fresher members, and the
    # renewals are reported as recovered, not serving.
    fresh_from = T0 + timedelta(days=2)
    _mint_cross(
        conn, cycle_by_system={SYSTEMS[0]: "00Z", SYSTEMS[1]: "06Z"},
        valid_from=fresh_from, valid_until=fresh_from + timedelta(days=30),
        generation=3, with_evidence=False,
    )
    for system_id, run_cycle in ((SYSTEMS[0], "00Z"), (SYSTEMS[1], "06Z")):
        _mint_system(
            conn, system_id=system_id, run_cycle=run_cycle,
            valid_from=fresh_from,
            valid_until=fresh_from + timedelta(days=30),
            generation=3, bias_c=0.7, train_end="2026-09-10",
            holdout=("2026-09-11", "2026-09-18"),
        )
    conn.commit()
    health = clock.certificate_health(conn, now=T0 + timedelta(days=2, hours=1))
    assert health["health"] == "green"
    assert health["renewed_scope_count"] == 0
    assert health["recovered_scope_count"] == 3


def test_board_survives_a_store_without_the_receipt_table():
    conn = _db()
    _stalled_scope(conn)
    conn.execute(f"DROP TABLE {clock.RENEWAL_TABLE}")
    conn.commit()
    health = clock.certificate_health(conn, now=T0)
    assert health["available"] is True
    assert health["health"] == "amber"
    assert "回执表尚未建立" in health["note"]
    # The first committed run installs it.
    clock.renew_expiring_certificates(conn, now=T0, commit=True)
    assert clock.table_exists(conn, clock.RENEWAL_TABLE)
    assert conn.execute(
        f"SELECT COUNT(*) FROM {clock.RENEWAL_TABLE}"
    ).fetchone()[0] == 3


def test_retired_labels_systems_and_unmapped_stations_are_not_kept_alive():
    conn = _db()
    minted = T0 - timedelta(days=25)
    ends = minted + timedelta(days=30)
    # A retired calendar label: never read again, never renewed.
    _mint_system(
        conn, system_id=SYSTEMS[0], run_cycle="00Z", valid_from=minted,
        valid_until=ends, season_bin="JJA",
    )
    # A label the station will not serve inside the renewed window.
    _mint_system(
        conn, system_id=SYSTEMS[0], run_cycle="00Z", valid_from=minted,
        valid_until=ends, season_bin="ASCENT",
    )
    # A product outside the active registry (charter 133 retired it).
    conn.execute(
        "INSERT INTO calibration_native_ensemble_system VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "OFFICIAL_AIRPORT_TAF_TX", "TAF TX", "TAF", "AWC",
            "NATIVE_ENSEMBLE_PROVIDER", 0, 1, "taf_v1", "taf_v1",
            "2026-07-01T00:00:00Z", "9999-12-31T23:59:59Z",
            content_sha256({"system": "TAF"}),
        ),
    )
    _mint_system(
        conn, system_id="OFFICIAL_AIRPORT_TAF_TX", run_cycle="d0",
        valid_from=minted, valid_until=ends,
    )
    # A cross pairing that includes the retired product.
    _mint_cross(
        conn, cycle_by_system={SYSTEMS[0]: "00Z", "OFFICIAL_AIRPORT_TAF_TX": "d0"},
        valid_from=minted, valid_until=ends,
    )
    # A station the season map does not know.
    _mint_system(
        conn, system_id=SYSTEMS[1], run_cycle="06Z", valid_from=minted,
        valid_until=ends, station_id="XXXX",
    )
    # A calendar lead token.
    _mint_system(
        conn, system_id=SYSTEMS[1], run_cycle="06Z", valid_from=minted,
        valid_until=ends, lead_bucket="d1",
    )
    conn.commit()
    assert clock.served_labels(STATION, now=T0, valid_days=30) == (
        "PEAK", "DESCENT",
    )
    assert clock.served_labels("XXXX", now=T0, valid_days=30) is None
    plan = clock.plan_renewals(conn, now=T0)
    assert plan["items"] == []
    # The retired product is refused once as its own system scope and once
    # inside the pairing; the pairing's cross scope itself carries no system.
    assert plan["skipped"] == {
        "label_not_served": 2, "retired_system": 2,
        "unmapped_station": 1, "lead_vocabulary": 1,
    }
    result = clock.renew_expiring_certificates(conn, now=T0, commit=True)
    assert result["counts"]["activations"] == 0
    health = clock.certificate_health(conn, now=T0)
    assert health["unrenewed_scope_count"] == 0
    assert health["health"] == "green"
