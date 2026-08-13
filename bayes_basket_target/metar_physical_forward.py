"""Forward member production for the METAR physical daily-maximum system.

This is the lane that makes the system exist in the ledger.  Registration and
scope contracts landed first, but a system with no member rows has no record,
no RMSE, no weight -- it is a name.  This module writes the rows.

The out-of-sample story is deliberately forward-only.  There is no historical
archive to mine: the system's inputs are the airport's own reports, which are
retained, so past days *could* be reconstructed -- and they are, from the same
strict-as-of anchor the live path uses, because a projector that can compute
yesterday honestly is the same code that computes today.  What is refused is
waiting: every settled day becomes one more scored fold on its own, the
admission bar is the point-forecast profile (5 train + 3 holdout), and the
weight it earns ramps as ``sqrt(n/28)``.  Thin evidence is priced, not barred.

Anchor semantics mirror the TAF projector on purpose:

- **One row per station-day at d0, computed strictly as-of local noon.**  The
  archive row must be final -- once noon has passed, no later observation can
  change what the noon forecast was.  Noon is also the TAF's d0 hindsight
  boundary, so the two point systems are graded at the same information
  cutoff and their disagreement is meaningful rather than an artifact of one
  having seen more of the day.
- **The cycle label is sticky per station.**  Local noon lands on a fixed UTC
  hour per station, so the label is naturally constant; the sticky lookup
  guards the rare timezone shift, because a drifting label splits the scope's
  days at the flip and starves the holdout on both sides.

The prior and coefficients come from the station's trajectory-training
artifact regardless of its promotion state.  Those artifacts are parameter
sources here, not authorities: whether the *composite forecast* deserves
weight is exactly what this system's own forward OOS record decides, so
gating the input on the old layer's promotion would run the same "no data,
no launch" loop this lane exists to break.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .metar_path_state import compile_conditional_metar_path_state
from .metar_physical_tmax import (
    EXPECTED_MEMBER_COUNT,
    METHOD_VERSION,
    POINT_MEMBER_ID,
    PROVIDER_ID,
    SYSTEM_ID,
    build_physical_tmax_members,
)
from .official_native_forward_packet import POINT_FORECAST_SYSTEMS


SCHEMA = "metar_physical_member_run_projection_v1"
# The same boundary the TAF's d0 lane uses, for the same reason: a forecast
# graded after it could have seen the day's high is grading hindsight.
D0_ANCHOR_LOCAL_HOUR = 12
# Observations begin to cover full local days from here; earlier days cannot
# produce an honest full-prefix state and are not attempted.
EARLIEST_EVENT_DATE = "2026-08-01"
MIN_PREFIX_OBSERVATIONS = 3
# The conditioner reads none of these, but the path-state contract requires a
# root geometry.  A single zero-centred bucket is the identity element: it
# yields root_expected_tmax_c = 0.0 and contributes nothing the physical
# coefficient set can see.  The independence test in
# test_bayes_basket_target_metar_physical_tmax pins that this stays true.
_NULL_ROOT_PROBABILITIES = {"0": 1.0}
_NULL_BUCKET_CENTERS = {"0": 0.0}
# Artifact preference when several local-time buckets exist for a station.
# The anchor is noon, so the AFTERNOON prior (fitted from folds cut at 12:00
# local) is the matching one; MORNING is the nearest substitute.
_BUCKET_PREFERENCE = ("AFTERNOON", "MORNING", "EVENING", "NIGHT")


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _station_artifacts(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """Latest usable trajectory artifact per station, by bucket preference."""

    best: dict[str, tuple[int, str, dict[str, Any]]] = {}
    for station_id, scope_key, trained, artifact_json in conn.execute(
        "SELECT station_id,scope_key,trained_through_utc,artifact_json "
        "FROM basket_probability_evidence_artifact "
        "WHERE artifact_kind='METAR_TRAJECTORY_LIKELIHOOD'"
    ):
        try:
            bucket = str(scope_key).split("|")[2]
            rank = _BUCKET_PREFERENCE.index(bucket)
        except (IndexError, ValueError):
            continue
        current = best.get(str(station_id))
        candidate = (rank, str(trained), artifact_json)
        if (
            current is None
            or candidate[0] < current[0]
            or (candidate[0] == current[0] and candidate[1] > current[1])
        ):
            best[str(station_id)] = candidate
    parsed: dict[str, dict[str, Any]] = {}
    for station_id, (_rank, _trained, artifact_json) in best.items():
        try:
            artifact = json.loads(artifact_json)
        except (TypeError, ValueError):
            continue
        if artifact.get("remaining_warming_distribution"):
            parsed[station_id] = artifact
    return parsed


def _seed_system_registry(conn: sqlite3.Connection) -> None:
    registration = POINT_FORECAST_SYSTEMS[SYSTEM_ID]
    identity = {
        "ensemble_system_id": SYSTEM_ID,
        "canonical_name": registration["canonical_name"],
        "independence_family_id": registration["independence_family_id"],
        "provider_id": registration["provider_id"],
        "source_class": "NATIVE_ENSEMBLE_PROVIDER",
        "aggregator_or_republisher": 0,
        "expected_member_count": EXPECTED_MEMBER_COUNT,
        "member_identity_contract_version": (
            "metar_physical_tmax_single_point_v1"
        ),
        "publication_contract_version": (
            "recomputed_on_each_canonical_observation_v1"
        ),
        "valid_from_utc": "2026-08-01T00:00:00Z",
        "valid_until_utc": "9999-12-31T23:59:59Z",
    }
    conn.execute(
        "INSERT OR IGNORE INTO calibration_native_ensemble_system("
        "ensemble_system_id,canonical_name,independence_family_id,"
        "provider_id,source_class,aggregator_or_republisher,"
        "expected_member_count,member_identity_contract_version,"
        "publication_contract_version,valid_from_utc,valid_until_utc,"
        "content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (*identity.values(), content_sha256(identity)),
    )


def project_metar_physical_member_runs(
    conn: sqlite3.Connection,
    *,
    archive_conn: sqlite3.Connection,
    as_of_utc: str | None = None,
    earliest_event_date: str = EARLIEST_EVENT_DATE,
) -> dict[str, Any]:
    """Project noon-anchored physical tmax points into the member archive.

    Idempotent: one content-addressed row per (station, day) at d0, OR IGNORE
    on replay, existing days skipped before any computation.  Safe to run on
    every worker cycle; each pass writes only the anchors that have newly
    passed.
    """

    _seed_system_registry(conn)
    as_of = (
        datetime.now(timezone.utc) if as_of_utc is None else _utc(as_of_utc)
    )
    # Timezones come from the market registry and nowhere else.  The station
    # registry's timezone_name column carries America/New_York for 33 of the
    # 45 stations, which anchored the first projection's "noon" up to
    # thirteen hours off local and let it grade hindsight as skill.  The
    # market registry is the source the settlement-truth chain itself
    # resolves against; a station it cannot name unambiguously is skipped
    # closed rather than guessed.
    market_timezone: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in conn.execute(
        "SELECT DISTINCT station_id,settlement_timezone "
        "FROM basket_target_market_registry "
        "WHERE settlement_timezone<>''"
    ):
        station, zone = str(row[0]), str(row[1])
        if station in market_timezone and market_timezone[station] != zone:
            ambiguous.add(station)
        market_timezone[station] = zone
    stations = {
        str(row[0]): (
            market_timezone.get(str(row[0]), ""),
            str(row[1]),
        )
        for row in conn.execute(
            "SELECT station_id,settlement_definition "
            "FROM calibration_station_registry"
        )
        if str(row[0]) not in ambiguous
    }
    artifacts = _station_artifacts(conn)
    # Both keyed to this method version: the v1 rows written under wrong
    # anchors must neither mask a v2 recompute as already-projected nor leak
    # their cycle label into v2's sticky vocabulary.
    existing = {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT station_id,target_local_date "
            "FROM calibration_native_ensemble_historical_member_run "
            "WHERE ensemble_system_id=? AND archive_lead_bucket='d0' "
            "AND extraction_version=?",
            (SYSTEM_ID, METHOD_VERSION),
        )
    }
    sticky: dict[str, str] = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            "SELECT DISTINCT station_id,run_cycle "
            "FROM calibration_native_ensemble_historical_member_run "
            "WHERE ensemble_system_id=? AND archive_lead_bucket='d0' "
            "AND extraction_version=?",
            (SYSTEM_ID, METHOD_VERSION),
        )
    }
    earliest = datetime.fromisoformat(
        earliest_event_date + "T00:00:00"
    ).date()
    skipped = {
        "already_projected": 0,
        "anchor_not_passed": 0,
        "no_artifact": 0,
        "insufficient_prefix": 0,
        "state_incomplete": 0,
        "timezone_invalid": 0,
    }
    wrote = 0
    candidates = 0
    for station_id, (timezone_name, settlement_definition) in sorted(
        stations.items()
    ):
        artifact = artifacts.get(station_id)
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:  # noqa: BLE001 - registry rows are external input
            skipped["timezone_invalid"] += 1
            continue
        local_today = as_of.astimezone(tz).date()
        day = earliest
        while day <= local_today:
            event_date = day.isoformat()
            day += timedelta(days=1)
            if (station_id, event_date) in existing:
                skipped["already_projected"] += 1
                continue
            anchor = datetime.combine(
                datetime.fromisoformat(event_date + "T00:00:00").date(),
                datetime.min.time(),
                tzinfo=tz,
            ) + timedelta(hours=D0_ANCHOR_LOCAL_HOUR)
            if as_of < anchor:
                skipped["anchor_not_passed"] += 1
                continue
            candidates += 1
            if artifact is None:
                skipped["no_artifact"] += 1
                continue
            day_start = anchor - timedelta(hours=D0_ANCHOR_LOCAL_HOUR)
            rows = [
                {
                    "observation_id": str(row[0]),
                    "observed_at_utc": str(row[1]),
                    "temperature_c": row[2],
                    "raw_text": row[3],
                }
                for row in archive_conn.execute(
                    "SELECT observation_id,valid_at,"
                    "reported_temperature_c,raw_text "
                    "FROM reported_trajectory_observations "
                    "WHERE station_id=? AND valid_at>=? AND valid_at<=? "
                    "ORDER BY valid_at",
                    (station_id, _iso(day_start), _iso(anchor)),
                )
                if row[2] is not None
            ]
            if len(rows) < MIN_PREFIX_OBSERVATIONS:
                skipped["insufficient_prefix"] += 1
                continue
            try:
                state = compile_conditional_metar_path_state(
                    rows,
                    as_of_utc=_iso(anchor),
                    settlement_timezone=timezone_name,
                    root_probabilities=_NULL_ROOT_PROBABILITIES,
                    bucket_centers_c=_NULL_BUCKET_CENTERS,
                    station_id=station_id,
                    event_date=event_date,
                )
                body = build_physical_tmax_members(
                    state,
                    station_id=station_id,
                    event_date=event_date,
                    remaining_warming_distribution={
                        float(key): float(value)
                        for key, value in artifact[
                            "remaining_warming_distribution"
                        ].items()
                    },
                    path_model_coefficients=(
                        artifact.get("path_model_coefficients") or {}
                    ),
                    path_model_sigma_c=float(
                        artifact.get("path_model_sigma_c") or 1.0
                    ),
                    artifact_content_sha256=str(artifact["content_sha256"]),
                )
            except ContractError:
                skipped["state_incomplete"] += 1
                continue
            label = sticky.get(station_id)
            if label is None:
                label = f"{(anchor.astimezone(timezone.utc).hour // 6) * 6:02d}Z"
                sticky[station_id] = label
            point = float(body["members"][0]["raw_tmax_c"])
            identity = {
                "ensemble_system_id": SYSTEM_ID,
                "station_id": station_id,
                "target_local_date": event_date,
                "archive_lead_bucket": "d0",
                "run_cycle": label,
                "member_id": POINT_MEMBER_ID,
                "raw_tmax_c": f"{point:.12g}",
                "native_run_init_utc": _iso(anchor),
                "extraction_version": METHOD_VERSION,
            }
            sha = content_sha256(identity)
            conn.execute(
                "INSERT OR IGNORE INTO "
                "calibration_native_ensemble_historical_member_run("
                "historical_member_run_id,ensemble_system_id,station_id,"
                "target_local_date,archive_lead_bucket,run_cycle,member_id,"
                "member_role,raw_tmax_c,settlement_definition,"
                "extraction_version,provider_id,transport_provider_id,"
                "native_run_init_utc,offline_oos_eligible,"
                "archive_query_at_utc,source_url,raw_object_ref,"
                "source_object_sha256,qc_status,authority_scope,"
                "holdout_eligible,content_sha256) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "mphys-" + sha[:32], SYSTEM_ID, station_id, event_date,
                    "d0", label, POINT_MEMBER_ID, "CONTROL", point,
                    settlement_definition, METHOD_VERSION,
                    PROVIDER_ID, PROVIDER_ID, _iso(anchor), 1,
                    _iso(as_of), "", "",
                    str(body["content_sha256"]),
                    "ACCEPTED", "CALIBRATION_TRAIN_ONLY", 0, sha,
                ),
            )
            changed = int(conn.execute("SELECT changes()").fetchone()[0])
            wrote += changed
            if not changed:
                # This identity was pre-checked as absent, so zero changes
                # means OR IGNORE swallowed a constraint -- the exact failure
                # mode that let this system ship registered, contracted and
                # producing nothing while every insert died on a transport
                # CHECK.  It stays a hard error so it can never be quiet
                # again.
                raise ContractError(
                    "metar_physical_member_insert_silently_rejected:"
                    f"{station_id}:{event_date}"
                )
    return {
        "schema": SCHEMA,
        "candidate_count": candidates,
        "wrote_count": wrote,
        "station_count": len(stations),
        "stations_with_artifact": len(artifacts),
        "skipped": skipped,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }


__all__ = ["SCHEMA", "project_metar_physical_member_runs"]
