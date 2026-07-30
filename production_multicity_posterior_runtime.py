"""Production physical-state P0 materialization for non-ZUUU board stations.

The original v6 online writer was intentionally introduced with one calibrated
station.  The board, however, already has independent official AMSC/NWP
snapshots for several stations.  This module extends the same production
state-space composer to those station-day identities without copying a ZUUU
checkpoint or using a market bucket as weather evidence.

This is an orderless materializer.  It writes only the existing production
posterior/fact surfaces through the approved atomic writer; it never writes a
decision, intent, order, or hard-NO record.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from archive_db_write_queue import connect_archive_db_rw
from joint_forecast_weak_likelihood_runtime import build_joint_forecast_weak_likelihood
from joint_weak_baseline_artifact_runtime import load_approved_joint_weak_artifact
from physical_x_bucket_contract_runtime import build_physical_x_bucket_contract
from production_probability_physical_state_composer import compile_physical_p0_extension
from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_canonical_event_drain_runtime import _augment_snapshot
from production_probability_v6_canonical_event_drain_runtime import _weak_from_snapshot
from production_probability_v6_initial_writer import (
    commit_v6_initial_posterior,
    materialize_v6_initial_public_fact_row,
)
from production_probability_v6_p0_runtime import compile_v6_p0_candidate
from production_probability_v6_weak_revision_runtime import (
    compile_v6_weak_revision_candidate,
)
from production_probability_v6_weak_revision_writer import (
    commit_v6_weak_revision_transaction,
    materialize_v6_weak_revision_public_fact_row,
)
from bayes_basket_target.et_history import event_day_window
from weak_forecast_evidence_snapshot_runtime import build_weak_forecast_evidence_snapshot_row


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_ACTIVATION = ROOT / "config" / "production_probability_runtime_activation.json"
DEFAULT_STATE_MODEL = ROOT / "config/artifacts/identifiable_physical_state_model_v1.json"
DEFAULT_REPORTING_OPERATOR = ROOT / "config/artifacts/amsc_frozen_reporting_operator_v1.json"
DEFAULT_WEAK_ARTIFACT = (
    ROOT
    / "data/runtime/joint_forecast_weak_likelihood/approved/"
    / "joint_weak_d1_settlement_calibrated_v1.json"
)

SCHEMA_VERSION = "production_multicity_posterior_runtime_v1"
TARGET_VERSION = "physical_weather_day_v1"
# These are the configured China board cities.  RCSS is deliberately not
# included: it uses a different official aviation source/settlement timezone.
MULTICITY_STATIONS = (
    "ZBAA",
    "ZGGG",
    "ZGSZ",
    "ZHHH",
    "ZSPD",
    "ZSQD",
    "ZUCK",
)
CITY_SLUGS = {
    "ZBAA": "beijing",
    "ZGGG": "guangzhou",
    "ZGSZ": "shenzhen",
    "ZHHH": "wuhan",
    "ZSPD": "shanghai",
    "ZSQD": "qingdao",
    "ZUCK": "chongqing",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _day(value: Any) -> str:
    try:
        return date.fromisoformat(_text(value)[:10]).isoformat()
    except ValueError:
        return ""


def build_multicity_target_identity(*, station_id: str, event_date: str) -> str:
    station = _text(station_id).upper()
    day = _day(event_date)
    if not station or not day:
        raise ValueError("multicity_target_identity_invalid")
    return f"{TARGET_VERSION}:{station}:{day}"


def _season(day: str) -> str:
    month = date.fromisoformat(day).month
    return "spring" if month in (3, 4, 5) else "summer" if month in (6, 7, 8) else "autumn" if month in (9, 10, 11) else "winter"


def _lead_bucket(day: str, decision_as_of: str) -> str:
    decision = _instant(decision_as_of)
    if decision is None:
        return "d_minus_1"
    local_day_start = datetime.fromisoformat(day).replace(tzinfo=timezone(timedelta(hours=8)))
    hours = (decision.astimezone(local_day_start.tzinfo) - local_day_start).total_seconds() / 3600.0
    if hours < -24.0:
        return "d_minus_2_or_earlier"
    if hours < 0.0:
        return "d_minus_1"
    if hours < 6.0:
        return "d0_0000_0559"
    if hours < 9.0:
        return "d0_0600_0859"
    if hours < 12.0:
        return "d0_0900_1159"
    if hours < 15.0:
        return "d0_1200_1459"
    return "d0_1500_or_later"


def _regime_from_fields(features: Mapping[str, Any]) -> str:
    cloud = float(features.get("total_cloud_cover_pct")) if features.get("total_cloud_cover_pct") is not None else None
    precip = float(features.get("total_precipitation_mm")) if features.get("total_precipitation_mm") is not None else None
    if precip is not None and precip >= 1.0:
        return "wet_precipitation"
    if cloud is not None and cloud <= 25.0 and (precip is None or precip < 0.1):
        return "clear_dry"
    if cloud is not None and cloud >= 80.0:
        return "cloudy_stable"
    return "mixed_forecast_regime"


def _official_nwp_revisions(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    decision_as_of: str,
) -> list[dict[str, Any]]:
    cutoff = _instant(decision_as_of)
    if cutoff is None:
        raise ValueError("multicity_source_cutoff_invalid")
    revisions: list[dict[str, Any]] = []
    # GFS is intentionally excluded from the production basket weak source
    # set.  The SQL remains station/date bound so another city's source can
    # never satisfy this city's target.
    for provider in ("ecmwf", "icon"):
        try:
            rows = conn.execute(
                "SELECT * FROM official_nwp_station_snapshots "
                "WHERE station_id=? AND event_date=? AND provider_id=? "
                "AND state='available' ORDER BY valid_time_utc,created_at,snapshot_id",
                (station_id, event_date, provider),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        by_valid: dict[str, sqlite3.Row] = {}
        for row in rows:
            created = _instant(row["created_at"])
            if created is None or created > cutoff or not _text(row["valid_time_utc"]):
                continue
            previous = by_valid.get(_text(row["valid_time_utc"]))
            if previous is None or (_text(row["created_at"]), _text(row["snapshot_id"])) > (
                _text(previous["created_at"]),
                _text(previous["snapshot_id"]),
            ):
                by_valid[_text(row["valid_time_utc"])] = row
        if not by_valid:
            continue
        ordered = [by_valid[key] for key in sorted(by_valid)]
        numeric = [float(row["temperature_2m_c"]) for row in ordered if row["temperature_2m_c"] is not None]
        if not numeric:
            continue
        peak_row = max(
            (row for row in ordered if row["temperature_2m_c"] is not None),
            key=lambda row: float(row["temperature_2m_c"]),
        )
        curve: list[dict[str, Any]] = []
        for row in ordered:
            try:
                features = json.loads(_text(row["normalized_features_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                features = {}
            if not isinstance(features, Mapping):
                features = {}
            # Temperature is deliberately omitted from the forcing curve.
            # The physical kernel receives only approved exogenous controls;
            # forecast temperature remains weak evidence, never a trajectory.
            point = {
                "valid_time_utc": _text(row["valid_time_utc"]),
                "total_cloud_cover_pct": features.get("total_cloud_cover_pct"),
                "dewpoint_c": features.get("dewpoint_2m_c"),
                "specific_humidity": features.get("specific_humidity"),
                "precipitation_probability": features.get("precipitation_probability"),
                "physical_regime": {
                    "physical_evidence": {
                        key: features.get(key)
                        for key in (
                            "total_cloud_cover_pct",
                            "total_precipitation_mm",
                            "relative_humidity_700_pct",
                            "relative_humidity_850_pct",
                        )
                        if features.get(key) is not None
                    }
                },
            }
            curve.append(point)
        source_digest = _sha([_text(row["source_snapshot_sha256"]) for row in ordered])
        revision_id = f"multicity-{station_id.lower()}-{provider}-{source_digest[:24]}"
        peak_valid = _text(peak_row["valid_time_utc"])
        regime = _regime_from_fields(
            json.loads(_text(peak_row["normalized_features_json"]))
            if _text(peak_row["normalized_features_json"]) else {}
        )
        mode = {
            "mode_id": "official_station_curve",
            "mode_probability": 1.0,
            "forecast_peak_c": max(numeric),
            "forecast_sigma_c": 1.0,
            "forecast_peak_valid_time": peak_valid,
            "hourly_curve": curve,
            "physical_regime": {},
            "forecast_weather_regime": regime,
            "mode_origin": "official_station_snapshot_projection",
        }
        revisions.append(
            {
                "source_id": provider,
                "source_revision_id": revision_id,
                "source_class": "forecast_weak_source",
                "event_date": event_date,
                "available_at": max(_text(row["created_at"]) for row in ordered),
                "forecast_peak_c": max(numeric),
                "forecast_peak_valid_time": peak_valid,
                "forecast_sigma_c": 1.0,
                "raw_sha256": source_digest,
                "forecast_weather_regime": regime,
                "forecast_weather_regime_origin": "official_station_physical_fields_v1",
                "hourly_curve": curve,
                "physical_regime": {},
                "source_modes_schema": "forecast_source_modes_v1",
                "source_modes": [mode],
                "provenance": {
                    "kind": "official_nwp_station_snapshot_projection",
                    "station_id": station_id,
                    "snapshot_ids": [_text(row["snapshot_id"]) for row in ordered],
                    "temperature_peak_is_weak_evidence_not_hard_bound": True,
                    "temperature_curve_not_consumed_as_forcing": True,
                },
            }
        )
    return revisions


def _taf_revision(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    decision_as_of: str,
) -> dict[str, Any] | None:
    cutoff = _instant(decision_as_of)
    if cutoff is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM amsc_taf_snapshots WHERE station_id=? AND event_date=? "
            "AND state='available' ORDER BY issue_time_utc DESC,created_at DESC LIMIT 20",
            (station_id, event_date),
        ).fetchall()
    except sqlite3.Error:
        return None
    for candidate in row:
        if _instant(candidate["created_at"]) is None or _instant(candidate["created_at"]) > cutoff:
            continue
        regime = _text(candidate["forecast_weather_regime"]) or "unknown_forecast_regime"
        mode = {
            "mode_id": "official_taf_tx",
            "mode_probability": 1.0,
            "forecast_peak_c": float(candidate["peak_c"]),
            "forecast_sigma_c": 1.2,
            "forecast_peak_valid_time": _text(candidate["peak_valid_time_utc"]),
            "hourly_curve": [],
            "physical_regime": {},
            "forecast_weather_regime": regime,
            "mode_origin": "official_amsc_taf_snapshot",
        }
        return {
            "source_id": "taf",
            "source_revision_id": _text(candidate["source_revision_id"]),
            "source_class": "forecast_weak_source",
            "event_date": event_date,
            "available_at": _text(candidate["created_at"]),
            "forecast_peak_c": float(candidate["peak_c"]),
            "forecast_peak_valid_time": _text(candidate["peak_valid_time_utc"]),
            "forecast_sigma_c": 1.2,
            "raw_sha256": _text(candidate["raw_sha256"]),
            "forecast_weather_regime": regime,
            "forecast_weather_regime_origin": "official_amsc_taf_snapshot",
            "hourly_curve": [],
            "physical_regime": {},
            "source_modes_schema": "forecast_source_modes_v1",
            "source_modes": [mode],
            "provenance": {
                "kind": "official_amsc_taf_snapshot",
                "station_id": station_id,
                "taf_issue_time_utc": _text(candidate["issue_time_utc"]),
                "temperature_peak_is_weak_evidence_not_hard_bound": True,
            },
        }
    return None


def _source_revisions(conn: sqlite3.Connection, *, station_id: str, event_date: str, decision_as_of: str) -> list[dict[str, Any]]:
    rows = _official_nwp_revisions(
        conn, station_id=station_id, event_date=event_date, decision_as_of=decision_as_of
    )
    taf = _taf_revision(
        conn, station_id=station_id, event_date=event_date, decision_as_of=decision_as_of
    )
    if taf is not None:
        rows.append(taf)
    return rows


def _source_revision_identity(rows: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str, str], ...]:
    """Return the immutable source identity used to decide whether to advance.

    ``decision_as_of`` is intentionally not part of this key.  A resident
    worker may poll every minute; the same source revision must not create a
    successor checkpoint merely because the clock moved.  A new revision or a
    changed raw digest, however, must advance the station-day chain.
    """

    return tuple(
        sorted(
            (
                _text(row.get("source_id")),
                _text(row.get("source_revision_id")),
                _text(row.get("raw_sha256")),
            )
            for row in rows
            if _text(row.get("source_id")) and _text(row.get("source_revision_id"))
        )
    )


def _snapshot_source_revision_identity(snapshot: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    try:
        raw = json.loads(_text(snapshot.get("source_revisions_json")) or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = []
    return _source_revision_identity(
        [row for row in raw if isinstance(row, Mapping)]
    )


def _station_slug_and_rules(conn: sqlite3.Connection, *, station_id: str, event_date: str) -> tuple[str, dict[str, Any]]:
    row = conn.execute(
        "SELECT slug,registry_json,rules_sha256,settlement_timezone,settlement_unit "
        "FROM reported_trajectory_market_registry WHERE station_id=? AND event_date=? "
        "AND source_eligible=1 ORDER BY slug LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if row is None:
        return "", {}
    try:
        registry = json.loads(_text(row["registry_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        registry = {}
    return _text(row["slug"]), {
        "rules_sha256": _text(row["rules_sha256"]),
        "settlement_timezone": _text(row["settlement_timezone"]),
        "settlement_unit": _text(row["settlement_unit"]),
        "registry": registry if isinstance(registry, Mapping) else {},
    }


def _latest_metar_anchor(conn: sqlite3.Connection, *, station_id: str, decision_as_of: str) -> dict[str, Any]:
    cutoff = _instant(decision_as_of)
    rows = conn.execute(
        "SELECT observation_id,reported_temperature_c,valid_at,raw_sha256,first_seen_at "
        "FROM reported_trajectory_observations WHERE station_id=? "
        "AND source_lane='amsc' AND report_type IN ('METAR','SPECI') "
        "ORDER BY valid_at DESC,first_seen_at DESC LIMIT 40",
        (station_id,),
    ).fetchall()
    for row in rows:
        valid = _instant(row["valid_at"])
        if valid is not None and (cutoff is None or valid <= cutoff):
            return {
                "observation_id": _text(row["observation_id"]),
                "temperature_c": float(row["reported_temperature_c"]),
                "valid_at": _text(row["valid_at"]),
                "raw_sha256": _text(row["raw_sha256"]),
                "first_seen_at": _text(row["first_seen_at"]),
            }
    return {
        "observation_id": "",
        "temperature_c": 24.0,
        "valid_at": "",
        "raw_sha256": "",
        "first_seen_at": "",
    }


def _station_prior(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    decision_as_of: str,
    grid: Sequence[float],
    activation: Mapping[str, Any],
) -> dict[str, Any]:
    anchor = _latest_metar_anchor(conn, station_id=station_id, decision_as_of=decision_as_of)
    center = float(anchor["temperature_c"])
    sigma = 3.0
    weights = [math.exp(-0.5 * ((float(value) - center) / sigma) ** 2) for value in grid]
    total = sum(weights)
    if total <= 0.0:
        raise ValueError("multicity_station_prior_mass_invalid")
    latent_states = (
        "warming",
        "slowing_warming",
        "temporary_platform",
        "temporary_cooling",
        "cooling_then_rebound",
        "weather_suppressed",
        "terminal_cooling",
        "final_locked",
    )
    latent = {state: 1.0 / len(latent_states) for state in latent_states}
    core = {
        "prior_schema": "multicity_station_initial_physical_prior_v1",
        "station_id": station_id,
        "event_date": event_date,
        "decision_as_of": decision_as_of,
        "temperature_grid_c": list(grid),
        "continuous_distribution": {
            "grid": [
                {"temp_c": float(value), "mass": mass / total}
                for value, mass in zip(grid, weights)
            ]
        },
        "latent_state_prior_by_temperature": {f"{float(value):g}": dict(latent) for value in grid},
        "initial_state_anchor": anchor,
        "prior_policy": "station_specific_latest_official_amsc_metar_state_anchor",
        "boundaries": {
            "forecast_peak_not_consumed": True,
            "taf_nwp_temperature_peak_not_used_as_prior": True,
            "station_day_independent_from_zuuu": True,
            "hard_no_lane_not_consumed": True,
        },
    }
    return {
        "schema": "full_physical_p0_prior_context_v1",
        **core,
        "artifact_sha256": _sha(core),
        # The activation hash identifies the shared, approved state-space
        # prior model.  The station-specific mass and anchor are separately
        # content-addressed in artifact_sha256 and lineage_json.
        "prior_model_artifact_sha256": _text(activation.get("prior_model_artifact_sha256")),
        "prior_context_id": f"multicityprior_{_sha(core)[:24]}",
        "state": "available",
        "production_runtime_eligible": True,
        "shadow_only": False,
        "boundary": {
            "physical_x_only": True,
            "requires_separate_strong_transition_and_bridge_gate": True,
            "production_probability_input": True,
            "station_specific": True,
            "forecast_peak_not_consumed": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def _weak_envelope(
    *,
    station_id: str,
    event_date: str,
    decision_as_of: str,
    revisions: Sequence[Mapping[str, Any]],
    grid: Sequence[float],
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    regimes = {
        _text(row.get("forecast_weather_regime"))
        for row in revisions
        if _text(row.get("forecast_weather_regime"))
    }
    regime = next(iter(regimes)) if len(regimes) == 1 else "mixed_forecast_regime"
    context = {
        "season": _season(event_date),
        "weather_regime": regime,
        "lead_bucket": _lead_bucket(event_date, decision_as_of),
    }
    identity = {
        "schema": "joint_weak_live_forecast_evidence_v1",
        "event_date": event_date,
        "decision_as_of": decision_as_of,
        "station_id": station_id,
        "forecast_context": context,
        "source_revisions": [dict(row) for row in revisions],
    }
    snapshot_id = f"forev_{_sha(identity)[:24]}"
    weak = build_joint_forecast_weak_likelihood(
        temperature_grid_c=grid,
        source_revisions=revisions,
        artifact=artifact,
        forecast_context={**context, "forecast_evidence_snapshot_id": snapshot_id},
        event_date=event_date,
        decision_as_of=decision_as_of,
    )
    if weak.get("ok") is not True:
        raise ValueError("multicity_weak_likelihood_blocked:" + ",".join(weak.get("block_reasons") or []))
    evidence = {
        **identity,
        "forecast_evidence_snapshot_id": snapshot_id,
        "forecast_evidence_snapshot_sha256": _sha(identity),
        "available_source_ids": [
            source for source in ("ecmwf", "icon", "taf")
            if any(_text(row.get("source_id")) == source for row in revisions)
        ],
        "missing_source_ids": [
            source for source in ("ecmwf", "icon", "taf")
            if not any(_text(row.get("source_id")) == source for row in revisions)
        ],
        "source_trace": [],
    }
    weak["forecast_evidence_snapshot_id"] = snapshot_id
    return {
        "ok": True,
        "state": "available",
        "forecast_evidence": evidence,
        "joint_weak_likelihood": weak,
        "read_only": True,
        "production_runtime_eligible": True,
        "shadow_only": False,
        "boundary": {
            "forecast_only": True,
            "production_probability_input": True,
            "forecast_peak_is_weak_evidence": True,
            "temperature_path_not_used_as_forcing": True,
            "market_data_consumed": False,
            "observation_data_consumed": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def _resolution_contract(*, station_id: str, event_date: str, rules: Mapping[str, Any]) -> dict[str, Any]:
    core = {
        "schema": "multicity_physical_resolution_contract_v1",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_unit": _text(rules.get("settlement_unit")) or "C",
        "settlement_timezone": _text(rules.get("settlement_timezone")) or "Asia/Shanghai",
        "rules_sha256": _text(rules.get("rules_sha256")),
        "source_authority": "official_station_day_physical_x_pending_market_bridge",
        "bridge_closed": False,
        "validated": False,
        "market_bucket_probabilities_na_reason": "resolution_bridge_not_closed",
    }
    return {**core, "resolution_contract_id": f"multicity_physical_{_sha(core)[:24]}"}


def _materialize_multicity_weak_revision(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    station_id: str,
    event_date: str,
    decision_as_of: str,
    checkpoint: Mapping[str, Any],
    old_snapshot: Mapping[str, Any],
    weak_envelope: Mapping[str, Any],
    state_model_artifact: Mapping[str, Any],
    reporting_operator_artifact: Mapping[str, Any],
    source_revisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Commit one forward-only source revision for a non-ZUUU station-day."""

    target = build_multicity_target_identity(station_id=station_id, event_date=event_date)
    weak = _mapping(weak_envelope.get("joint_weak_likelihood"))
    snapshot = _augment_snapshot(
        build_weak_forecast_evidence_snapshot_row(
            weak_envelope,
            target_identity=target,
            event_date=event_date,
            decision_as_of=decision_as_of,
        ),
        weak,
    )
    new_id = _text(snapshot.get("forecast_evidence_snapshot_id"))
    old_id = _text(old_snapshot.get("forecast_evidence_snapshot_id"))
    if not new_id or new_id == old_id:
        return {
            "ok": True,
            "state": "revision_already_visible",
            "idempotent": True,
            "station_id": station_id,
            "event_date": event_date,
            "target_identity": target,
            "places_orders": False,
        }

    previous_weak = _weak_from_snapshot(
        conn,
        checkpoint,
        identifiable_physical=True,
    )
    candidate = compile_v6_weak_revision_candidate(
        activation=activation,
        previous_checkpoint=checkpoint,
        previous_snapshot=old_snapshot,
        revised_snapshot=snapshot,
        trigger_source_id=_text(
            max(
                source_revisions,
                key=lambda row: _text(row.get("available_at")),
                default={},
            ).get("source_id")
        ),
        trigger_source_revision_id=_text(
            max(
                source_revisions,
                key=lambda row: _text(row.get("available_at")),
                default={},
            ).get("source_revision_id")
        ),
        revision_kind="original",
        decision_as_of=decision_as_of,
        previous_weak_likelihood=previous_weak,
        revised_weak_likelihood=weak,
        physical_state_model_artifact=state_model_artifact,
        physical_reporting_operator_artifact=reporting_operator_artifact,
    )

    slug, _rules = _station_slug_and_rules(
        conn,
        station_id=station_id,
        event_date=event_date,
    )
    if not slug:
        parsed_day = date.fromisoformat(event_date)
        slug = (
            "highest-temperature-in-"
            + CITY_SLUGS.get(station_id, station_id.lower())
            + f"-on-{parsed_day.strftime('%B').lower()}-{parsed_day.day}-{parsed_day.year}"
        )
    latest = conn.execute(
        "SELECT h.fact_json FROM event_fact_latest l "
        "JOIN event_fact_history h ON h.id=l.fact_id "
        "WHERE l.slug=? AND l.event_date=?",
        (slug, event_date),
    ).fetchone()
    if latest is None:
        raise ValueError("multicity_weak_revision_current_public_fact_missing")
    update = _mapping(candidate.get("update_row"))
    successor = _mapping(candidate.get("checkpoint_row"))
    fact_snapshot_id = "fact-" + _text(update.get("weak_forecast_revision_update_id"))
    public = materialize_v6_weak_revision_public_fact_row(
        activation=activation,
        previous_fact_json=latest[0],
        checkpoint_row=successor,
        update_row=update,
        fact_snapshot_id=fact_snapshot_id,
        slug=slug,
        event_date=event_date,
        fact_as_of=decision_as_of,
        market_inputs=None,
    )
    try:
        fact = json.loads(_text(public.get("fact_json")) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("multicity_weak_revision_public_fact_invalid") from exc
    ssot = fact.get("posterior_ssot_fact")
    if not isinstance(ssot, dict):
        raise ValueError("multicity_weak_revision_public_ssot_missing")
    ssot.update(
        {
            "target_identity": target,
            "station_id": station_id,
            "city_posterior_scope": "independent_station_day_production",
            "city_probability_not_copied_from_zuuu": True,
            "source_revision_ids": [
                _text(row.get("source_revision_id"))
                for row in source_revisions
                if _text(row.get("source_revision_id"))
            ],
            "source_as_of": max(
                (_text(row.get("available_at")) for row in source_revisions),
                default="",
            ),
            "source_lineage_sha256": _sha(source_revisions),
            "freshness_state": "source_revision_successor_materialized",
            "materialization_mode": "production_successor_state_space_only",
            "market_projection_state": "not_materialized",
            "market_projection_reason": "physical_resolution_bridge_not_closed",
        }
    )
    public["fact_json"] = _json(fact)
    committed = commit_v6_weak_revision_transaction(
        conn,
        activation=activation,
        candidate=candidate,
        public_fact_row=public,
    )
    return {
        **committed,
        "station_id": station_id,
        "event_date": event_date,
        "target_identity": target,
        "slug": slug,
        "source_revision_ids": [
            _text(row.get("source_revision_id"))
            for row in source_revisions
            if _text(row.get("source_revision_id"))
        ],
        "previous_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "places_orders": False,
    }


def materialize_multicity_station_p0(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    station_id: str,
    event_date: str,
    decision_as_of: str,
    state_model_artifact: Mapping[str, Any],
    reporting_operator_artifact: Mapping[str, Any],
    weak_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    station = _text(station_id).upper()
    day = _day(event_date)
    target = build_multicity_target_identity(station_id=station, event_date=day)
    authorized = validate_v6_activation(activation)
    if _text(authorized.get("composer_schema")) != IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
        return {"ok": False, "state": "blocked", "station_id": station, "reason": "identifiable_physical_activation_required", "places_orders": False}
    existing = conn.execute(
        "SELECT l.posterior_checkpoint_id,p.replay_generation FROM production_initial_posterior_lineage l "
        "JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id "
        "WHERE l.target_identity=? AND l.activation_contract_sha256=?",
        (target, _text(authorized.get("activation_contract_sha256"))),
    ).fetchall()
    if len(existing) > 1:
        raise ValueError("multicity_p0_lineage_not_unique")
    revisions = _source_revisions(conn, station_id=station, event_date=day, decision_as_of=decision_as_of)
    if not revisions:
        return {
            "ok": False,
            "state": "blocked",
            "station_id": station,
            "event_date": day,
            "target_identity": target,
            "reason": "station_forecast_sources_not_materialized",
            "places_orders": False,
        }
    if existing:
        replay_generation = int(existing[0][1] or 0)
        conn.row_factory = sqlite3.Row
        checkpoint_row = conn.execute(
            "SELECT * FROM posterior_checkpoints WHERE target_identity=? "
            "AND replay_generation=? AND state='committed' "
            "ORDER BY rowid DESC LIMIT 1",
            (target, replay_generation),
        ).fetchone()
        if checkpoint_row is None:
            raise ValueError("multicity_p0_active_checkpoint_missing")
        checkpoint = dict(checkpoint_row)
        snapshot_row = conn.execute(
            "SELECT * FROM weak_forecast_evidence_snapshots "
            "WHERE forecast_evidence_snapshot_id=?",
            (_text(checkpoint.get("forecast_evidence_snapshot_id")),),
        ).fetchone()
        if snapshot_row is None:
            raise ValueError("multicity_p0_active_snapshot_missing")
        old_snapshot = dict(snapshot_row)
        if _source_revision_identity(revisions) == _snapshot_source_revision_identity(old_snapshot):
            return {
                "ok": True,
                "state": "already_initialized",
                "idempotent": True,
                "station_id": station,
                "event_date": day,
                "target_identity": target,
                "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
                "replay_generation": replay_generation,
                "source_revision_ids": [
                    _text(row.get("source_revision_id"))
                    for row in revisions
                    if _text(row.get("source_revision_id"))
                ],
                "places_orders": False,
            }
        grid_contract = build_physical_x_bucket_contract()
        if grid_contract.get("ok") is not True:
            raise ValueError("multicity_physical_grid_unavailable")
        grid = [float(value) for value in grid_contract["temperature_grid_c"]]
        weak_envelope = _weak_envelope(
            station_id=station,
            event_date=day,
            decision_as_of=decision_as_of,
            revisions=revisions,
            grid=grid,
            artifact=weak_artifact,
        )
        return _materialize_multicity_weak_revision(
            conn,
            activation=authorized,
            station_id=station,
            event_date=day,
            decision_as_of=decision_as_of,
            checkpoint=checkpoint,
            old_snapshot=old_snapshot,
            weak_envelope=weak_envelope,
            state_model_artifact=state_model_artifact,
            reporting_operator_artifact=reporting_operator_artifact,
            source_revisions=revisions,
        )
    grid_contract = build_physical_x_bucket_contract()
    if grid_contract.get("ok") is not True:
        raise ValueError("multicity_physical_grid_unavailable")
    grid = [float(value) for value in grid_contract["temperature_grid_c"]]
    weak_envelope = _weak_envelope(
        station_id=station,
        event_date=day,
        decision_as_of=decision_as_of,
        revisions=revisions,
        grid=grid,
        artifact=weak_artifact,
    )
    weak = weak_envelope["joint_weak_likelihood"]
    prior = _station_prior(
        conn,
        station_id=station,
        event_date=day,
        decision_as_of=decision_as_of,
        grid=grid,
        activation=authorized,
    )
    resolution = _resolution_contract(
        station_id=station,
        event_date=day,
        rules=_station_slug_and_rules(conn, station_id=station, event_date=day)[1],
    )
    state_reference = _text(prior["initial_state_anchor"].get("valid_at")) or decision_as_of
    generation = _text(_mapping(weak.get("forecast_scenario_paths")).get("sha256"))
    p0 = compile_v6_p0_candidate(
        activation=authorized,
        target_identity=target,
        event_date=day,
        decision_as_of=decision_as_of,
        state_reference_valid_at=state_reference,
        prior_context_id=_text(prior["prior_context_id"]),
        prior_model_artifact_sha256=_text(prior["prior_model_artifact_sha256"]),
        forecast_evidence_snapshot_id=_text(weak.get("forecast_evidence_snapshot_id")),
        weak_likelihood_artifact_sha256=_text(weak.get("artifact_sha256")),
        resolution_contract_id=_text(resolution["resolution_contract_id"]),
        forcing_generation_sha256=generation,
        initial_joint=(),
        candidate_scenario_responsibilities=_mapping(weak.get("candidate_scenario_responsibilities")),
        replay_generation=0,
        prior_context=prior,
        weak_likelihood=weak,
        physical_state_model_artifact=state_model_artifact,
        physical_reporting_operator_artifact=reporting_operator_artifact,
    )
    slug, rules = _station_slug_and_rules(conn, station_id=station, event_date=day)
    if not slug:
        slug = f"highest-temperature-in-{CITY_SLUGS.get(station, station.lower())}-on-{date.fromisoformat(day).strftime('%B').lower()}-{date.fromisoformat(day).day}-{date.fromisoformat(day).year}"
    fact_id = "fact-v6-multicity-p0-" + _sha({"checkpoint": p0["checkpoint_row"]["posterior_checkpoint_id"]})
    public = materialize_v6_initial_public_fact_row(
        activation=authorized,
        checkpoint_row=p0["checkpoint_row"],
        fact_snapshot_id=fact_id,
        slug=slug,
        event_date=day,
        fact_as_of=decision_as_of,
        correlation_id="correlation-v6-multicity-p0-" + _sha({"target": target, "as_of": decision_as_of}),
        market_inputs=None,
    )
    fact = json.loads(public["fact_json"])
    ssot = fact["posterior_ssot_fact"]
    ssot.update(
        {
            "target_identity": target,
            "station_id": station,
            "city_posterior_scope": "independent_station_day_production",
            "city_probability_not_copied_from_zuuu": True,
            "prior_context_id": _text(prior["prior_context_id"]),
            "prior_context_sha256": _text(prior["artifact_sha256"]),
            "prior_anchor_observation_id": _text(prior["initial_state_anchor"].get("observation_id")),
            "prior_anchor_valid_at": _text(prior["initial_state_anchor"].get("valid_at")),
            "prior_anchor_raw_sha256": _text(prior["initial_state_anchor"].get("raw_sha256")),
            "source_revision_ids": [
                _text(row.get("source_revision_id")) for row in revisions if _text(row.get("source_revision_id"))
            ],
            "source_as_of": max(_text(row.get("available_at")) for row in revisions),
            "source_lineage_sha256": _sha(revisions),
            "freshness_state": "source_snapshot_materialized",
            "materialization_mode": "production_p0_state_space_only",
            "market_projection_state": "not_materialized",
            "market_projection_reason": "physical_resolution_bridge_not_closed",
        }
    )
    public["fact_json"] = _json(fact)
    snapshot = _augment_snapshot(
        build_weak_forecast_evidence_snapshot_row(
            weak_envelope,
            target_identity=target,
            event_date=day,
            decision_as_of=decision_as_of,
        ),
        weak,
        temperature_prior=prior,
    )
    result = commit_v6_initial_posterior(
        conn,
        activation=authorized,
        p0_candidate=p0,
        weak_snapshot_row=snapshot,
        public_fact_row=public,
    )
    return {
        **result,
        "station_id": station,
        "event_date": day,
        "target_identity": target,
        "slug": slug,
        "source_revision_ids": [
            _text(row.get("source_revision_id")) for row in revisions if _text(row.get("source_revision_id"))
        ],
        "prior_context_id": _text(prior["prior_context_id"]),
        "source_as_of": max(_text(row.get("available_at")) for row in revisions),
        "places_orders": False,
    }


def materialize_multicity_production_p0s(
    *,
    db_path: Path | str = DEFAULT_DB,
    activation: Mapping[str, Any],
    event_dates: Iterable[str],
    decision_as_of: str,
    state_model_artifact_path: Path | str = DEFAULT_STATE_MODEL,
    reporting_operator_artifact_path: Path | str = DEFAULT_REPORTING_OPERATOR,
    weak_artifact_path: Path | str = DEFAULT_WEAK_ARTIFACT,
) -> dict[str, Any]:
    authorized = validate_v6_activation(activation)
    decision_clock = _instant(decision_as_of)
    if decision_clock is None:
        raise ValueError("multicity_source_cutoff_invalid")
    requested_dates = sorted({_day(value) for value in event_dates if _day(value)})
    if not requested_dates:
        raise ValueError("event_date_required")
    for day in requested_dates:
        window = event_day_window(day, as_of_utc=decision_clock)
        state = _text(window.get("state"))
        if state == "not_started":
            raise ValueError("future_event_day_not_allowed")
        if state == "invalid":
            raise ValueError("event_day_window_invalid")
    state_model = json.loads(Path(state_model_artifact_path).read_text(encoding="utf-8"))
    reporting_operator = json.loads(Path(reporting_operator_artifact_path).read_text(encoding="utf-8"))
    weak_artifact = json.loads(Path(weak_artifact_path).read_text(encoding="utf-8"))
    outcomes: list[dict[str, Any]] = []
    unique_dates = requested_dates
    with connect_archive_db_rw(db_path, owner="production_multicity_posterior_materializer") as conn:
        for day in unique_dates:
            for station in MULTICITY_STATIONS:
                try:
                    outcomes.append(
                        materialize_multicity_station_p0(
                            conn,
                            activation=authorized,
                            station_id=station,
                            event_date=day,
                            decision_as_of=decision_as_of,
                            state_model_artifact=state_model,
                            reporting_operator_artifact=reporting_operator,
                            weak_artifact=weak_artifact,
                        )
                    )
                except Exception as exc:
                    outcomes.append(
                        {
                            "ok": False,
                            "state": "blocked",
                            "station_id": station,
                            "event_date": day,
                            "reason": f"{type(exc).__name__}:{exc}",
                            "places_orders": False,
                        }
                    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": all(item.get("ok") is True for item in outcomes) if outcomes else False,
        "state": "materialized" if outcomes and all(item.get("ok") is True for item in outcomes) else "partial",
        "event_dates": unique_dates,
        "station_count": len(MULTICITY_STATIONS),
        "outcomes": outcomes,
        "production": True,
        "orderless": True,
        "places_orders": False,
    }


__all__ = [
    "DEFAULT_ACTIVATION",
    "DEFAULT_DB",
    "DEFAULT_REPORTING_OPERATOR",
    "DEFAULT_STATE_MODEL",
    "DEFAULT_WEAK_ARTIFACT",
    "MULTICITY_STATIONS",
    "SCHEMA_VERSION",
    "build_multicity_target_identity",
    "materialize_multicity_production_p0s",
    "materialize_multicity_station_p0",
]
