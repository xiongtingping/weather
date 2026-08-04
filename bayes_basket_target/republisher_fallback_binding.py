"""Immutable pointer contract for Open-Meteo no-live fallback evidence.

The Open-Meteo lane is intentionally separate from native model authority.
This module binds *already retained* strict historical deterministic reports
to an auditable no-live fallback pointer.  It does not fetch data, mutate the
calibration database, create native-arrival/P1 authority, or produce a
probability.  A consumer may use a binding only after it has recorded that
the corresponding native object is unavailable for the same station/day.

The pointer is useful for the deterministic AIFS and ICON Global reports that
already contain 20 train + 8 holdout closed-AWC evidence.  Regional/ICON-EPS
reports are retained as candidate-only status until their own strict OOS gate
passes; OOS from another product is never transferred.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

from .contracts import ContractError, content_sha256
from .resource_bounds import MAX_JSON_INPUT_BYTES, load_bounded_json


SCHEMA = "bayes_basket_target_republisher_no_live_fallback_binding_v1"
BINDING_VERSION = "republisher_no_live_fallback_v1"
SOURCE_ROLE = "aggregator_republisher"
TRANSPORT_PROVIDER_ID = "OPEN_METEO"
AUTHORITY_MODE = "PRODUCTION_NO_LIVE_REPUBLISHER_FALLBACK"
TEMPORAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"
OBSERVATIONAL_SCOPE = "observational_candidate_not_oos"
MIN_TRAIN_DAYS = 20
MIN_HOLDOUT_DAYS = 8
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINDING_PATH = (
    ROOT
    / "config"
    / "bayes_basket_target"
    / "republisher_no_live_fallback_binding_v1.json"
)


# The deterministic reports use human-readable model ids while the live
# Open-Meteo packet uses the immutable ensemble product ids below.  Keeping
# this mapping in the linkage layer prevents a caller from guessing a model
# lineage from a provider string or from a station name.
REPUBLISHER_MODEL_SYSTEM_IDS = {
    "AIFS": "ECMWF_AIFS_ENS_GLOBAL",
    "ICON": "DWD_ICON_GLOBAL_EPS",
}
REPUBLISHER_SYSTEM_MODEL_IDS = {
    system_id: model_id
    for model_id, system_id in REPUBLISHER_MODEL_SYSTEM_IDS.items()
}


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"republisher_binding_{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"republisher_binding_{name}_sha_invalid")
    return result


def _utc(name: str, value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"republisher_binding_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"republisher_binding_{name}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _verify_hashed_mapping(raw: Mapping[str, Any], *, label: str) -> str:
    supplied = _sha(label, raw.get("content_sha256"))
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError(f"republisher_binding_{label}_sha_mismatch")
    return supplied


def _verify_deterministic_report(report: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Verify a retained deterministic Open-Meteo report and return PASS rows."""

    if report.get("schema") != "deterministic_republisher_strict_oos_v1":
        raise ContractError("republisher_binding_report_schema_invalid")
    report_sha = _verify_hashed_mapping(report, label="report")
    if (
        report.get("source_role") != SOURCE_ROLE
        or report.get("transport_provider_id") != TRANSPORT_PROVIDER_ID
        or report.get("formal_p0_eligible") is not False
        or report.get("state") not in {"READY_CANDIDATES", "PRODUCTION_NO_LIVE_OOS_PASS"}
    ):
        raise ContractError("republisher_binding_report_lineage_invalid")
    policy = report.get("oos_policy")
    if not isinstance(policy, Mapping):
        raise ContractError("republisher_binding_report_boundary_invalid")
    try:
        future_event_count = int(policy.get("future_event_count"))
        orders_submitted = int(policy.get("orders_submitted"))
    except (TypeError, ValueError) as exc:
        raise ContractError("republisher_binding_report_boundary_invalid") from exc
    if (
        policy.get("temporal_scope") != TEMPORAL_SCOPE
        or future_event_count != 0
        or policy.get("native_arrival_index_written") is not False
        or policy.get("p1_authority_written") is not False
        or policy.get("formal_live_authority_written") is not False
        or policy.get("production_database_written") is not False
        or policy.get("calls_order_gateway") is not False
        or orders_submitted != 0
    ):
        raise ContractError("republisher_binding_report_boundary_invalid")
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        raise ContractError("republisher_binding_report_candidates_missing")
    passed: list[dict[str, Any]] = []
    seen_stations: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ContractError("republisher_binding_candidate_invalid")
        candidate_sha = _verify_hashed_mapping(candidate, label="candidate")
        station = _text("station_id", candidate.get("station_id")).upper()
        if station in seen_stations:
            raise ContractError("republisher_binding_candidate_duplicate_station")
        seen_stations.add(station)
        try:
            train_n = int(candidate.get("train_n"))
            holdout_n = int(candidate.get("holdout_n"))
            future_count = int(candidate.get("future_event_count"))
        except (TypeError, ValueError) as exc:
            raise ContractError("republisher_binding_candidate_counts_invalid") from exc
        contract_ids = candidate.get("historical_oos_contract_ids")
        predictions = candidate.get("predictions")
        if (
            candidate.get("state") != "PRODUCTION_NO_LIVE_OOS_PASS"
            or train_n < MIN_TRAIN_DAYS
            or holdout_n < MIN_HOLDOUT_DAYS
            or future_count != 0
            or candidate.get("oos_event_temporal_scope") != TEMPORAL_SCOPE
            or candidate.get("native_arrival_index_eligible") is not False
            or candidate.get("p1_eligible") is not False
            or candidate.get("formal_p0_eligible") is not False
            or not isinstance(contract_ids, list)
            or len(contract_ids) != holdout_n
            or len({str(value) for value in contract_ids}) != len(contract_ids)
            or not isinstance(predictions, list)
            or len(predictions) != holdout_n
        ):
            # Rejected candidates and shortfalls are intentionally not bound.
            # A report may contain them, but a PASS pointer cannot silently
            # downgrade the OOS threshold or reuse their evidence.
            continue
        for prediction in predictions:
            if not isinstance(prediction, Mapping):
                raise ContractError("republisher_binding_prediction_invalid")
            if (
                prediction.get("receipt_class") != "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
                or prediction.get("temporal_scope") != TEMPORAL_SCOPE
                or prediction.get("historical_oos_contract_id") in (None, "")
            ):
                raise ContractError("republisher_binding_prediction_lineage_invalid")
        passed.append({
            "station_id": station,
            "candidate_content_sha256": candidate_sha,
            "model_id": _text("model_id", report.get("model_id")).upper(),
            "provider_id": _text("provider_id", report.get("provider_id")),
            "transport_provider_id": TRANSPORT_PROVIDER_ID,
            "source_role": SOURCE_ROLE,
            "authority_mode": AUTHORITY_MODE,
            "native_source_unavailable_required": True,
            "native_arrival_index_eligible": False,
            "native_formal_p0_eligible": False,
            "p1_eligible": False,
            "formal_live_authority": False,
            "oos_event_temporal_scope": TEMPORAL_SCOPE,
            "future_event_count": 0,
            "train_n": train_n,
            "holdout_n": holdout_n,
            "train_start_date": _text("train_start_date", candidate.get("train_start_date")),
            "train_end_date": _text("train_end_date", candidate.get("train_end_date")),
            "holdout_start_date": _text("holdout_start_date", candidate.get("holdout_start_date")),
            "holdout_end_date": _text("holdout_end_date", candidate.get("holdout_end_date")),
            "historical_oos_contract_ids": sorted(str(value) for value in contract_ids),
        })
    return report_sha, passed


def _verify_regional_audit(audit: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Retain regional candidate status without transferring strict OOS."""

    if audit.get("schema") != "regional_model_candidate_closure_audit_v1":
        raise ContractError("republisher_binding_regional_audit_schema_invalid")
    audit_sha = _verify_hashed_mapping(audit, label="regional_audit")
    models = audit.get("models")
    if not isinstance(models, list):
        raise ContractError("republisher_binding_regional_models_missing")
    result: list[dict[str, Any]] = []
    for model in models:
        if not isinstance(model, Mapping):
            raise ContractError("republisher_binding_regional_model_invalid")
        if model.get("source_class") != SOURCE_ROLE:
            raise ContractError("republisher_binding_regional_lineage_invalid")
        totals = model.get("totals")
        if not isinstance(totals, Mapping):
            raise ContractError("republisher_binding_regional_totals_missing")
        result.append({
            "model_key": _text("regional_model_key", model.get("model_key")),
            "provider_id": str(model.get("configured_model_parameter") or ""),
            "source_role": SOURCE_ROLE,
            "transport_provider_id": TRANSPORT_PROVIDER_ID,
            "model_scope": "regional" if model.get("model_key") not in {
                "CMA", "ICON", "ECMWF_AIFS_GLOBAL"
            } else "global",
            "activation_state": "CANDIDATE_ONLY",
            "fallback_eligible": False,
            "native_arrival_index_eligible": False,
            "native_formal_p0_eligible": False,
            "p1_eligible": False,
            "formal_live_authority": False,
            "strict_oos_pass_count": int(totals.get("strict_oos_pass") or 0),
            "historical_republisher_member_run_count": int(
                totals.get("open_meteo_republisher_member_runs") or 0
            ),
            "blockers": sorted(str(value) for value in (model.get("fail_closed_reasons") or [])),
        })
    return audit_sha, result


def build_republisher_fallback_binding(
    *,
    deterministic_reports: Sequence[Mapping[str, Any]],
    regional_audit: Mapping[str, Any],
    as_of_utc: str,
    source_artifacts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build a content-addressed, append-only no-live fallback pointer.

    Only PASS deterministic reports with a complete 20+8 closed-day cohort
    become ``BOUND_NO_LIVE_FALLBACK`` rows.  Regional candidates are carried
    as explicit non-eligible rows so a later operator cannot mistake their
    retained Open-Meteo data for a promotion.
    """

    as_of = _utc("as_of_utc", as_of_utc)
    if not deterministic_reports:
        raise ContractError("republisher_binding_deterministic_reports_missing")
    report_refs: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for report in deterministic_reports:
        report_sha, rows = _verify_deterministic_report(report)
        model_id = _text("model_id", report.get("model_id")).upper()
        if model_id in seen_models:
            raise ContractError("republisher_binding_model_duplicate")
        seen_models.add(model_id)
        report_refs.append({
            "schema": str(report["schema"]),
            "model_id": model_id,
            "provider_id": _text("provider_id", report.get("provider_id")),
            "transport_provider_id": TRANSPORT_PROVIDER_ID,
            "source_role": SOURCE_ROLE,
            "report_content_sha256": report_sha,
            "candidate_count": len(rows),
            "station_count": int(report.get("station_count") or 0),
            "missing_truth_scopes": list(report.get("missing_truth_scopes") or []),
        })
        for row in rows:
            seed = {
                "binding_version": BINDING_VERSION,
                "model_id": row["model_id"],
                "station_id": row["station_id"],
                "candidate_content_sha256": row["candidate_content_sha256"],
                "historical_oos_contract_ids": row["historical_oos_contract_ids"],
            }
            binding_id = "republisher-fallback-" + content_sha256(seed)[:32]
            bindings.append({
                "binding_id": binding_id,
                **seed,
                "provider_id": row["provider_id"],
                "transport_provider_id": TRANSPORT_PROVIDER_ID,
                "source_role": SOURCE_ROLE,
                "source_kind": "open_meteo_aggregator",
                "model_scope": "global",
                "activation_state": "BOUND_NO_LIVE_FALLBACK",
                "native_preferred": True,
                "native_source_unavailable_required": True,
                "authority_mode": AUTHORITY_MODE,
                "native_arrival_index_eligible": False,
                "native_formal_p0_eligible": False,
                "p1_eligible": False,
                "formal_live_authority": False,
                "production_no_live_only": True,
                "oos_event_temporal_scope": TEMPORAL_SCOPE,
                "future_event_count": 0,
                "min_train_days": MIN_TRAIN_DAYS,
                "min_holdout_days": MIN_HOLDOUT_DAYS,
                "train_n": row["train_n"],
                "holdout_n": row["holdout_n"],
                "train_start_date": row["train_start_date"],
                "train_end_date": row["train_end_date"],
                "holdout_start_date": row["holdout_start_date"],
                "holdout_end_date": row["holdout_end_date"],
            })
    regional_sha, regional_rows = _verify_regional_audit(regional_audit)
    identity = {
        "schema": SCHEMA,
        "binding_version": BINDING_VERSION,
        "as_of_utc": as_of,
        "authority": {
            "authority_mode": AUTHORITY_MODE,
            "source_role": SOURCE_ROLE,
            "transport_provider_id": TRANSPORT_PROVIDER_ID,
            "native_preferred": True,
            "native_source_unavailable_required": True,
            "native_arrival_index_eligible": False,
            "native_formal_p0_eligible": False,
            "p1_eligible": False,
            "formal_live_authority": False,
            "production_no_live_only": True,
            "historical_oos_activation_required": True,
            "oos_event_temporal_scope": TEMPORAL_SCOPE,
            "future_observations_scope": OBSERVATIONAL_SCOPE,
            "future_event_count": 0,
        },
        "source_artifacts": [dict(item) for item in source_artifacts],
        "deterministic_reports": sorted(report_refs, key=lambda item: item["model_id"]),
        "bindings": sorted(bindings, key=lambda item: (item["model_id"], item["station_id"])),
        "regional_candidate_audit": {
            "schema": str(regional_audit["schema"]),
            "content_sha256": regional_sha,
            "candidate_count": len(regional_rows),
            "models": sorted(regional_rows, key=lambda item: item["model_key"]),
        },
        "selection_contract": {
            "official_native_first": True,
            "same_station_event_scope_only": True,
            "native_missing_or_invalid_required": True,
            "open_meteo_cannot_enter_native_arrival": True,
            "open_meteo_cannot_enter_p1": True,
            "open_meteo_cannot_enter_formal_live": True,
            "future_data_is_not_oos": True,
            "minimum_train_days": MIN_TRAIN_DAYS,
            "minimum_holdout_days": MIN_HOLDOUT_DAYS,
        },
        "state": "READY_NO_LIVE_FALLBACK_POINTER" if bindings else "PENDING_STRICT_OOS",
        "binding_count": len(bindings),
        "bound_model_ids": sorted({item["model_id"] for item in bindings}),
        "candidate_only_model_ids": sorted({item["model_key"] for item in regional_rows}),
        "formal_p0_eligible": False,
        "native_arrival_index_written": False,
        "p1_authority_written": False,
        "formal_live_authority_written": False,
        "production_database_written": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def validate_republisher_fallback_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an emitted pointer before a no-live consumer reads it."""

    if binding.get("schema") != SCHEMA:
        raise ContractError("republisher_binding_schema_invalid")
    _verify_hashed_mapping(binding, label="binding")
    authority = binding.get("authority")
    if not isinstance(authority, Mapping):
        raise ContractError("republisher_binding_authority_missing")
    for key in (
        "native_arrival_index_eligible",
        "native_formal_p0_eligible",
        "p1_eligible",
        "formal_live_authority",
    ):
        if authority.get(key) is not False:
            raise ContractError("republisher_binding_native_boundary_invalid")
    if (
        authority.get("source_role") != SOURCE_ROLE
        or authority.get("transport_provider_id") != TRANSPORT_PROVIDER_ID
        or authority.get("authority_mode") != AUTHORITY_MODE
        or authority.get("native_source_unavailable_required") is not True
        or authority.get("historical_oos_activation_required") is not True
    ):
        raise ContractError("republisher_binding_authority_invalid")
    bindings = binding.get("bindings")
    if not isinstance(bindings, list):
        raise ContractError("republisher_binding_rows_missing")
    for row in bindings:
        if not isinstance(row, Mapping):
            raise ContractError("republisher_binding_row_invalid")
        for key in (
            "native_arrival_index_eligible",
            "native_formal_p0_eligible",
            "p1_eligible",
            "formal_live_authority",
        ):
            if row.get(key) is not False:
                raise ContractError("republisher_binding_row_boundary_invalid")
        try:
            future_event_count = int(row.get("future_event_count"))
            train_n = int(row.get("train_n"))
            holdout_n = int(row.get("holdout_n"))
        except (TypeError, ValueError) as exc:
            raise ContractError("republisher_binding_row_contract_invalid") from exc
        if (
            row.get("source_role") != SOURCE_ROLE
            or row.get("transport_provider_id") != TRANSPORT_PROVIDER_ID
            or row.get("activation_state") != "BOUND_NO_LIVE_FALLBACK"
            or row.get("native_source_unavailable_required") is not True
            or future_event_count != 0
            or row.get("oos_event_temporal_scope") != TEMPORAL_SCOPE
            or train_n < MIN_TRAIN_DAYS
            or holdout_n < MIN_HOLDOUT_DAYS
        ):
            raise ContractError("republisher_binding_row_contract_invalid")
    return dict(binding)


@dataclass(frozen=True)
class RepublisherFallbackBindingSnapshot:
    """Validated, immutable view of the no-live republisher pointer.

    The pointer is a configuration/evidence handoff, not a calibration
    activation.  Keeping the validated source reports alongside the binding
    rows lets production prove that a row still points at the exact retained
    20-train/8-holdout candidate instead of trusting a copied hash alone.
    """

    payload: Mapping[str, Any]
    bindings_by_model_station: Mapping[tuple[str, str], Mapping[str, Any]]
    reports_by_model: Mapping[str, Mapping[str, Any]]
    path: Path | None = None

    @property
    def content_sha256(self) -> str:
        return str(self.payload.get("content_sha256") or "")

    @property
    def station_ids(self) -> tuple[str, ...]:
        return tuple(sorted({key[1] for key in self.bindings_by_model_station}))

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(sorted({key[0] for key in self.bindings_by_model_station}))


def _load_source_artifact(path: Path, *, expected: Mapping[str, Any]) -> Mapping[str, Any]:
    """Load one bounded pointer source and verify both file/content hashes."""

    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ContractError("republisher_binding_source_symlink_forbidden")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError("republisher_binding_source_unavailable") from exc
    digest_builder = hashlib.sha256()
    try:
        size = 0
        with resolved.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_JSON_INPUT_BYTES:
                    raise ContractError("republisher_binding_source_too_large")
                digest_builder.update(chunk)
    except ContractError:
        raise
    except OSError as exc:
        raise ContractError("republisher_binding_source_unavailable") from exc
    digest = digest_builder.hexdigest()
    if digest != str(expected.get("file_sha256") or "").lower():
        raise ContractError("republisher_binding_source_file_sha_mismatch")
    raw = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(raw, Mapping):
        raise ContractError("republisher_binding_source_mapping_required")
    declared = str(expected.get("content_sha256") or "").lower()
    if declared != str(raw.get("content_sha256") or "").lower():
        raise ContractError("republisher_binding_source_content_sha_mismatch")
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    if declared != content_sha256(identity):
        raise ContractError("republisher_binding_source_content_hash_invalid")
    return raw


def _verify_pointer_source_closure(
    binding: Mapping[str, Any],
    *,
    path: Path,
) -> tuple[dict[str, Mapping[str, Any]], dict[tuple[str, str], Mapping[str, Any]]]:
    """Verify every bound row against its retained deterministic report."""

    refs = binding.get("source_artifacts")
    if not isinstance(refs, list) or not refs:
        raise ContractError("republisher_binding_source_artifacts_missing")
    reports: dict[str, Mapping[str, Any]] = {}
    candidates: dict[tuple[str, str], Mapping[str, Any]] = {}
    for raw_ref in refs:
        if not isinstance(raw_ref, Mapping):
            raise ContractError("republisher_binding_source_artifact_invalid")
        raw_path = str(raw_ref.get("path") or "").strip()
        if not raw_path:
            raise ContractError("republisher_binding_source_path_missing")
        source_path = Path(raw_path)
        if not source_path.is_absolute():
            source_path = path.parent.parent.parent / source_path
        report = _load_source_artifact(source_path, expected=raw_ref)
        schema = str(report.get("schema") or "")
        if schema == "deterministic_republisher_strict_oos_v1":
            model_id = str(report.get("model_id") or "").strip().upper()
            if model_id not in REPUBLISHER_MODEL_SYSTEM_IDS:
                raise ContractError("republisher_binding_source_model_invalid")
            if model_id in reports:
                raise ContractError("republisher_binding_source_model_duplicate")
            reports[model_id] = report
            raw_candidates = report.get("candidates")
            if not isinstance(raw_candidates, list):
                raise ContractError("republisher_binding_source_candidates_missing")
            for candidate in raw_candidates:
                if not isinstance(candidate, Mapping):
                    raise ContractError("republisher_binding_source_candidate_invalid")
                if candidate.get("state") != "PRODUCTION_NO_LIVE_OOS_PASS":
                    continue
                station_id = str(candidate.get("station_id") or "").strip().upper()
                key = (model_id, station_id)
                if key in candidates:
                    raise ContractError("republisher_binding_source_candidate_duplicate")
                candidates[key] = candidate
        elif schema == "regional_model_candidate_closure_audit_v1":
            # The regional audit is carried only to preserve candidate-only
            # evidence.  It has no binding rows and must not become a model
            # activation source.
            continue
        else:
            raise ContractError("republisher_binding_source_schema_invalid")

    for row in binding.get("bindings") or []:
        if not isinstance(row, Mapping):
            raise ContractError("republisher_binding_row_invalid")
        model_id = str(row.get("model_id") or "").strip().upper()
        station_id = str(row.get("station_id") or "").strip().upper()
        candidate = candidates.get((model_id, station_id))
        if candidate is None:
            raise ContractError("republisher_binding_candidate_pointer_orphan")
        if str(row.get("candidate_content_sha256") or "").lower() != str(
            candidate.get("content_sha256") or ""
        ).lower():
            raise ContractError("republisher_binding_candidate_hash_mismatch")
        for key in (
            "train_n",
            "holdout_n",
            "train_start_date",
            "train_end_date",
            "holdout_start_date",
            "holdout_end_date",
        ):
            if str(row.get(key)) != str(candidate.get(key)):
                raise ContractError("republisher_binding_candidate_contract_mismatch")
        pointer_contracts = sorted(str(value) for value in row.get("historical_oos_contract_ids") or [])
        candidate_contracts = sorted(
            str(value) for value in candidate.get("historical_oos_contract_ids") or []
        )
        if pointer_contracts != candidate_contracts:
            raise ContractError("republisher_binding_candidate_contract_ids_mismatch")
    return reports, {
        (str(row["model_id"]).upper(), str(row["station_id"]).upper()): row
        for row in binding.get("bindings") or []
        if isinstance(row, Mapping)
    }


def load_republisher_fallback_binding(
    path: Path | str = DEFAULT_BINDING_PATH,
) -> RepublisherFallbackBindingSnapshot:
    """Load and deeply verify one immutable no-live fallback pointer."""

    candidate_path = Path(path).expanduser()
    if candidate_path.is_symlink():
        raise ContractError("republisher_binding_symlink_forbidden")
    try:
        resolved = candidate_path.resolve(strict=True)
        raw = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError("republisher_binding_unavailable") from exc
    validated = validate_republisher_fallback_binding(raw)
    if validated.get("state") != "READY_NO_LIVE_FALLBACK_POINTER":
        raise ContractError("republisher_binding_not_ready")
    reports, rows = _verify_pointer_source_closure(validated, path=resolved)
    if set(reports) != set(REPUBLISHER_MODEL_SYSTEM_IDS):
        raise ContractError("republisher_binding_source_model_set_incomplete")
    if int(validated.get("binding_count") or 0) != len(rows):
        raise ContractError("republisher_binding_count_mismatch")
    return RepublisherFallbackBindingSnapshot(
        payload=validated,
        bindings_by_model_station=rows,
        reports_by_model=reports,
        path=resolved,
    )


def republisher_binding_rows_for_packet(
    snapshot: RepublisherFallbackBindingSnapshot | Mapping[str, Any],
    packet: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Return pointer rows for all recognized AIFS/ICON products in a packet.

    This is a lineage check only.  It deliberately does not grant P0
    authority; callers must still require a separate exact cross-ensemble
    activation and prove that the native packet was unavailable first.
    """

    if not isinstance(packet, Mapping):
        raise ContractError("republisher_binding_packet_invalid")
    if packet.get("schema") != "open_meteo_native_ensemble_forward_packet_v1":
        raise ContractError("republisher_binding_packet_schema_invalid")
    station_id = str(packet.get("station_id") or "").strip().upper()
    event_date = str(packet.get("event_date") or "").strip()
    if not station_id or not event_date:
        raise ContractError("republisher_binding_packet_scope_invalid")
    authority = packet.get("source_authority")
    if not isinstance(authority, Mapping):
        raise ContractError("republisher_binding_packet_authority_missing")
    if (
        authority.get("source_role") != SOURCE_ROLE
        or authority.get("native_arrival_index_eligible") is not False
        or authority.get("formal_p0_eligible") is not False
        or authority.get("production_no_live_calibration_candidate") is not True
        or authority.get("historical_oos_contract_bound") is not False
        or authority.get("can_substitute_for_formal_promotion") is not False
        or packet.get("evidence_role") != OBSERVATIONAL_SCOPE
        or packet.get("oos_event_temporal_scope") != OBSERVATIONAL_SCOPE
        or packet.get("strict_oos_sample_eligible") is not False
    ):
        raise ContractError("republisher_binding_packet_authority_invalid")
    systems = packet.get("systems")
    if not isinstance(systems, list) or not systems:
        raise ContractError("republisher_binding_packet_systems_missing")
    if isinstance(snapshot, RepublisherFallbackBindingSnapshot):
        rows_by_key = snapshot.bindings_by_model_station
    else:
        rows_by_key = {
            (
                str(row.get("model_id") or "").strip().upper(),
                str(row.get("station_id") or "").strip().upper(),
            ): row
            for row in snapshot.get("bindings") or []
            if isinstance(row, Mapping)
        }
    result: dict[str, Mapping[str, Any]] = {}
    for system in systems:
        if not isinstance(system, Mapping):
            raise ContractError("republisher_binding_packet_system_invalid")
        system_id = str(system.get("ensemble_system_id") or "").strip()
        model_id = REPUBLISHER_SYSTEM_MODEL_IDS.get(system_id)
        if model_id is None:
            continue
        candidate_authority = system.get("candidate_authority")
        if (
            system.get("transport_provider_id") != TRANSPORT_PROVIDER_ID
            or not isinstance(candidate_authority, Mapping)
            or candidate_authority.get("source_role") != SOURCE_ROLE
            or candidate_authority.get("formal_p0_eligible") is not False
            or candidate_authority.get("native_arrival_index_eligible")
            is not False
        ):
            raise ContractError("republisher_binding_packet_system_authority_invalid")
        row = rows_by_key.get((model_id, station_id))
        if row is None:
            raise ContractError("republisher_binding_packet_station_unbound")
        result[model_id] = row
    if not result:
        raise ContractError("republisher_binding_packet_model_unbound")
    return result


__all__ = [
    "AUTHORITY_MODE",
    "BINDING_VERSION",
    "MIN_HOLDOUT_DAYS",
    "MIN_TRAIN_DAYS",
    "SCHEMA",
    "DEFAULT_BINDING_PATH",
    "REPUBLISHER_MODEL_SYSTEM_IDS",
    "REPUBLISHER_SYSTEM_MODEL_IDS",
    "RepublisherFallbackBindingSnapshot",
    "build_republisher_fallback_binding",
    "load_republisher_fallback_binding",
    "republisher_binding_rows_for_packet",
    "validate_republisher_fallback_binding",
]
