"""Activation-gated physical probability worker for the resident strategy service.

The worker is deliberately orderless.  It materializes the physical P0/P1
SSOT that the existing YES basket consumes, but it never calls the CLOB
gateway and never creates a fourth production service.  Until an explicitly
approved identifiable-physical activation is atomically installed, ``run``
returns before opening the database or writing the local raw archive.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from amsc_raw_history_archive import archive_amsc_poll, attach_archive_metadata
from archive_db_write_queue import connect_archive_db_rw
from identifiable_physical_state_bayes.core import (
    validate_observation_artifact,
    validate_state_model_artifact,
)
from identifiable_physical_state_bayes.reporting import (
    validate_reporting_operator_artifact,
)
from joint_weak_live_evidence_runtime import DEFAULT_CURVE_ROOT
from physical_state_bayes_v6.schema_contract import verify_schema
from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_amsc_archive_ingress import (
    apply_v6_amsc_archive_probability_ingress,
)
from production_probability_v6_canonical_event_drain_runtime import (
    DEFAULT_PHYSICAL_OBSERVATION_ARTIFACT,
    DEFAULT_PHYSICAL_REPORTING_OPERATOR_ARTIFACT,
    DEFAULT_PHYSICAL_STATE_MODEL_ARTIFACT,
    drain_v6_canonical_events,
    ensure_v6_daily_p0,
)
from production_probability_v6_market_inputs_runtime import prewarm_v6_market_inputs
from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256
from production_probability_v6_weak_revision_event_runtime import (
    apply_v6_current_weak_source_revisions,
)
from production_multicity_posterior_runtime import materialize_multicity_production_p0s
from scripts.fetch_zuuu_metar_taf import (
    PARSER_VERSION as AMSC_PARSER_VERSION,
    build_amsc_observation_bundle,
    source_payloads_from_amsc_payload,
)


ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "identifiable_physical_probability_worker_v1"
DEFAULT_DB = ROOT / "data/archive.db"
DEFAULT_ACTIVATION = ROOT / "config/production_probability_runtime_activation.json"
# Keep the physical-probability evidence archive separate from the legacy
# root-owned AMSC object store.  The resident user-domain worker must be able
# to append new evidence without requiring a privileged chown during startup.
DEFAULT_RAW_ARCHIVE = ROOT / "data/physical_probability_raw_history"
LOCAL_TIME_ZONE = ZoneInfo("Asia/Shanghai")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"physical_probability_json_not_mapping:{path.name}")
    return dict(value)


def _fingerprint(rows: list[Mapping[str, Any]]) -> str:
    payload = [
        {
            "raw_sha256": _text(row.get("raw_sha256")),
            "first_seen_at": _text(row.get("first_seen_at")),
        }
        for row in rows
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class IdentifiablePhysicalProbabilityWorker:
    """Run the orderless physical composer inside ``reported_trajectory_no``."""

    def __init__(
        self,
        db_path: Path = DEFAULT_DB,
        *,
        activation_path: Path = DEFAULT_ACTIVATION,
        raw_archive_dir: Path = DEFAULT_RAW_ARCHIVE,
        curve_root: Path = DEFAULT_CURVE_ROOT,
        state_artifact_path: Path = DEFAULT_PHYSICAL_STATE_MODEL_ARTIFACT,
        observation_artifact_path: Path = DEFAULT_PHYSICAL_OBSERVATION_ARTIFACT,
        reporting_artifact_path: Path = DEFAULT_PHYSICAL_REPORTING_OPERATOR_ARTIFACT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.activation_path = Path(activation_path).expanduser().resolve()
        self.raw_archive_dir = Path(raw_archive_dir).expanduser().resolve()
        self.curve_root = Path(curve_root).expanduser().resolve()
        self.state_artifact_path = Path(state_artifact_path).expanduser().resolve()
        self.observation_artifact_path = Path(observation_artifact_path).expanduser().resolve()
        self.reporting_artifact_path = Path(reporting_artifact_path).expanduser().resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_ingress_fingerprint = ""
        self._last_future_attempt_monotonic = 0.0
        self._last_future_result: dict[str, Any] | None = None
        self._last_future_attempt_at = ""
        self._last_future_completed_at = ""

    def _activation(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Return before touching data when the successor epoch is not active."""

        try:
            activation = validate_v6_activation(_read_json(self.activation_path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return None, {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked_activation_invalid",
                "reason": f"{type(exc).__name__}:{exc}",
                "places_orders": False,
            }
        if (
            _text(activation.get("composer_schema"))
            != IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
        ):
            return None, {
                "schema": SCHEMA_VERSION,
                "ok": True,
                "state": "disabled_until_identifiable_physical_activation",
                "database_opened": False,
                "raw_archive_written": False,
                "places_orders": False,
            }
        return activation, {}

    def _preflight(self, activation: Mapping[str, Any]) -> dict[str, Any]:
        state = validate_state_model_artifact(_read_json(self.state_artifact_path))
        observation = validate_observation_artifact(
            _read_json(self.observation_artifact_path)
        )
        reporting = validate_reporting_operator_artifact(
            _read_json(self.reporting_artifact_path)
        )
        expected = {
            "physical_state_model_artifact_sha256": state["artifact_sha256"],
            "physical_observation_artifact_sha256": observation["artifact_sha256"],
            "physical_reporting_operator_sha256": reporting["artifact_sha256"],
        }
        mismatches = [
            field
            for field, actual in expected.items()
            if _text(activation.get(field)) != _text(actual)
        ]
        artifacts = (state, observation, reporting)
        if mismatches:
            raise ValueError("physical_probability_activation_artifact_mismatch:" + ",".join(mismatches))
        if any(
            artifact.get("state") != "approved"
            or artifact.get("production_runtime_eligible") is not True
            or artifact.get("shadow_only") is not False
            for artifact in artifacts
        ):
            raise ValueError("physical_probability_artifact_not_production_approved")
        if _text(activation.get("runtime_manifest_sha256")) != v6_runtime_manifest_sha256():
            raise ValueError("physical_probability_runtime_manifest_drift")
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
            schema = verify_schema(conn)
        if schema.get("ok") is not True:
            raise ValueError("physical_probability_additive_schema_not_applied")
        return {"ok": True, "schema": schema}

    def _reported_rows(self, event_date: str) -> list[dict[str, Any]]:
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT raw_sha256,raw_text,first_seen_at,fetch_started_at,"
                "fetch_completed_at,valid_at,report_type,correction_suspected "
                "FROM reported_trajectory_observations "
                "WHERE source_lane='amsc' AND station_id='ZUUU' "
                "ORDER BY valid_at,first_seen_at,observation_id",
            ).fetchall()
        selected = []
        for row in rows:
            try:
                valid = datetime.fromisoformat(
                    _text(row["valid_at"]).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if valid.tzinfo is None:
                continue
            if valid.astimezone(LOCAL_TIME_ZONE).date().isoformat() == _text(
                event_date
            )[:10]:
                selected.append(dict(row))
        return selected

    def _archive_and_ingress(
        self,
        *,
        event_date: str,
        rows: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        completed_at = max(
            (
                _text(row.get("first_seen_at") or row.get("fetch_completed_at"))
                for row in rows
            ),
            default=_utc_now(),
        )
        attached: list[dict[str, Any]] = []
        payload_rows = []
        for row in rows:
            raw_text = _text(row.get("raw_text"))
            if not raw_text:
                continue
            payload_rows.append({"content": raw_text})
            first_seen_at = _text(
                row.get("first_seen_at") or row.get("fetch_completed_at")
            )
            source_rows = source_payloads_from_amsc_payload(
                {"data": [{"content": raw_text}]},
                fetched_at=first_seen_at,
                valid_time_reference_at=_text(row.get("valid_at")) or first_seen_at,
            )
            archive = archive_amsc_poll(
                archive_dir=self.raw_archive_dir,
                source_payloads=source_rows,
                poll_started_at=_text(row.get("fetch_started_at")) or first_seen_at,
                response_completed_at=first_seen_at,
                response_status=200,
                request_metadata={
                    "cccc": "ZUUU",
                    "source": "resident_reported_trajectory_no",
                },
                fetcher_version=AMSC_PARSER_VERSION,
            )
            attached.extend(attach_archive_metadata(source_rows, archive))
        payload = {"data": payload_rows}
        bundle = build_amsc_observation_bundle(
            payload,
            fetched_at=completed_at,
            source_payloads=attached,
        )
        source_payload = {
            "source_observations": list(bundle.get("source_observations") or []),
            "source_observation_ids": list(bundle.get("source_observation_ids") or []),
            "source_acquisition_trace": dict(bundle.get("source_acquisition_trace") or {}),
        }
        return apply_v6_amsc_archive_probability_ingress(
            db_path=self.db_path,
            raw_archive_dir=self.raw_archive_dir,
            event_date=event_date,
            decision_as_of=completed_at,
            activation_path=self.activation_path,
            source_observation_payload=source_payload,
            source_observation_created_at=completed_at,
            writer_queue_wait_sec=0.75,
            db_timeout_sec=0.75,
            db_busy_timeout_ms=500,
            curve_root=self.curve_root,
        )

    def _probability_cycle(
        self, *, activation: Mapping[str, Any], event_date: str, decision_as_of: str
    ) -> dict[str, Any]:
        market_inputs = prewarm_v6_market_inputs(
            event_date=event_date,
            strategy_projection_role="basket_probability_only",
        )
        with connect_archive_db_rw(
            self.db_path, owner="resident_identifiable_physical_probability"
        ) as conn:
            p0 = ensure_v6_daily_p0(
                conn,
                activation=activation,
                event_date=event_date,
                decision_as_of=decision_as_of,
                curve_root=self.curve_root,
                physical_state_model_artifact_path=self.state_artifact_path,
                physical_reporting_operator_artifact_path=self.reporting_artifact_path,
                market_inputs=market_inputs,
            )
            if p0.get("ok") is not True:
                return {"ok": False, "state": "p0_blocked", "daily_p0": p0}
            drained = drain_v6_canonical_events(
                conn,
                activation=activation,
                event_date=event_date,
                market_inputs=market_inputs,
                physical_state_model_artifact_path=self.state_artifact_path,
                physical_observation_artifact_path=self.observation_artifact_path,
                physical_reporting_operator_artifact_path=self.reporting_artifact_path,
            )
            if drained.get("ok") is not True:
                return {
                    "ok": False,
                    "state": "canonical_drain_blocked",
                    "daily_p0": p0,
                    "canonical_probability": drained,
                }
            weak = apply_v6_current_weak_source_revisions(
                conn,
                activation=activation,
                event_date=event_date,
                decision_as_of=decision_as_of,
                market_inputs=market_inputs,
                physical_state_model_artifact_path=self.state_artifact_path,
                physical_reporting_operator_artifact_path=self.reporting_artifact_path,
            )
        return {
            "ok": weak.get("ok") is True,
            "state": "probability_current" if weak.get("ok") is True else "weak_revision_blocked",
            "daily_p0": p0,
            "canonical_probability": drained,
            "weak_revision": weak,
        }

    def run(self) -> dict[str, Any]:
        activation, inactive = self._activation()
        if activation is None:
            return inactive
        try:
            preflight = self._preflight(activation)
            now = self.clock()
            if now.tzinfo is None:
                raise ValueError("physical_probability_clock_must_be_timezone_aware")
            decision_as_of = now.astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z")
            current_day = now.astimezone(LOCAL_TIME_ZONE).date().isoformat()
            rows = self._reported_rows(current_day)
            fingerprint = _fingerprint(rows)
            if rows and fingerprint != self._last_ingress_fingerprint:
                current = self._archive_and_ingress(
                    event_date=current_day,
                    rows=rows,
                )
                if current.get("ok") is True:
                    self._last_ingress_fingerprint = fingerprint
            else:
                current = self._probability_cycle(
                    activation=activation,
                    event_date=current_day,
                    decision_as_of=decision_as_of,
                )

            next_day = (
                now.astimezone(LOCAL_TIME_ZONE).date() + timedelta(days=1)
            ).isoformat()
            future: dict[str, Any] = {
                "ok": self._last_future_result.get("ok") is not False
                if self._last_future_result is not None
                else True,
                "state": "not_due",
                "event_date": next_day,
                "last_attempt_at": self._last_future_attempt_at or None,
                "last_completed_at": self._last_future_completed_at or None,
            }
            if self._last_future_result is not None:
                future["last_result"] = dict(self._last_future_result)
            mono = time.monotonic()
            if mono - self._last_future_attempt_monotonic >= 60.0:
                self._last_future_attempt_monotonic = mono
                self._last_future_attempt_at = decision_as_of
                future = self._probability_cycle(
                    activation=activation,
                    event_date=next_day,
                    decision_as_of=decision_as_of,
                )
                future["event_date"] = next_day
                self._last_future_result = dict(future)
                self._last_future_completed_at = _utc_now()
                future["last_attempt_at"] = self._last_future_attempt_at
                future["last_completed_at"] = self._last_future_completed_at
            # The same resident production service owns the configured board
            # stations.  This is deliberately orderless and independent from
            # ZUUU's canonical hard-observation drain: a missing city P0 must
            # not be represented by copying Chengdu, and a city-side retry
            # must not block the ZUUU order lane.
            multicity = materialize_multicity_production_p0s(
                db_path=self.db_path,
                activation=activation,
                event_dates=(current_day, next_day),
                decision_as_of=decision_as_of,
                state_model_artifact_path=self.state_artifact_path,
                reporting_operator_artifact_path=self.reporting_artifact_path,
            )
            all_ok = current.get("ok") is True and future.get("ok") is True
            return {
                "schema": SCHEMA_VERSION,
                "ok": all_ok,
                "state": "active" if all_ok else "blocked_fail_closed",
                "event_date": current_day,
                "current_day": current,
                "next_day": future,
                "multicity_posterior": multicity,
                "preflight": preflight,
                "order_gateway_imported": False,
                "places_orders": False,
            }
        except Exception as exc:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked_fail_closed",
                "reason": f"{type(exc).__name__}:{exc}",
                "places_orders": False,
            }


__all__ = [
    "DEFAULT_ACTIVATION",
    "DEFAULT_DB",
    "DEFAULT_RAW_ARCHIVE",
    "IdentifiablePhysicalProbabilityWorker",
    "SCHEMA_VERSION",
]
