"""PR07 adapter from DWD ICON Global/EPS audits to shared coverage rows.

The module only parses local audit inputs.  Its final result is always the
canonical ``model_system_coverage`` matrix: one Global/EPS row for every
canonical station and d0/d1/d2.  It never aliases ICON-EU/D2, treats
Open-Meteo only as a republisher, or creates an activation/P0 artifact.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical_contract import TYPED_SCOPE_BLOCKERS
from .contracts import ContractError
from .model_system_coverage import (
    LEADS,
    MATRIX_SCHEMA,
    build_coverage_matrix,
    canonical_station_ids,
    make_coverage_row,
)
from .fleet import expected_station_count


OFFICIAL_READINESS_SCHEMA = "dwd_icon_global_eps_official_readiness_v1"
STRICT_OOS_SCHEMA = "dwd_icon_global_eps_official_strict_oos_v1"
DAILY_ARCHIVE_SCHEMA = "dwd_icon_global_eps_official_daily_members_v1"
REPUBLISHER_BINDING_SCHEMA = "bayes_basket_target_republisher_no_live_fallback_binding_v1"

GLOBAL_EPS = "DWD_ICON_GLOBAL_EPS"
EU_EPS = "DWD_ICON_EU_EPS"
D2_EPS = "DWD_ICON_D2_EPS"
EXPECTED_GLOBAL_MEMBER_COUNT = 40
MAX_AUDIT_INPUT_BYTES = 16 * 1024 * 1024
MAX_AUDIT_INPUT_FILES = 32
MAX_TOTAL_AUDIT_INPUT_BYTES = 64 * 1024 * 1024


def _mapping(value: Any, *, error: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(error)
    return value


def _sha(value: Any, *, error: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(error)
    return result


def _nonnegative_int(value: Any, *, error: str) -> int:
    if isinstance(value, bool):
        raise ContractError(error)
    try:
        result = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ContractError(error) from exc
    if result < 0:
        raise ContractError(error)
    return result


def _canonical_blocker(raw_reasons: Sequence[Any], *, fallback: str) -> str:
    """Map an audit reason to the one canonical blocker a terminal row needs."""

    text = " ".join(str(value).lower() for value in raw_reasons)
    if "timezone" in text:
        result = "TIMEZONE_MISMATCH"
    elif "hash" in text or "sha" in text:
        result = "HASH_MISMATCH"
    elif "truth" in text or "settlement_day_open" in text or "closed" in text:
        result = "TRUTH_NOT_CLOSED"
    elif "lineage" in text or "arrival" in text or "native" in text:
        result = "LINEAGE_MISMATCH"
    elif "run" in text or "cycle" in text or "lead" in text:
        result = "RUN_CYCLE_MISMATCH"
    elif "sample" in text or "train" in text or "holdout" in text:
        result = "OOS_SAMPLE_SHORT"
    else:
        result = fallback
    if result not in TYPED_SCOPE_BLOCKERS:
        raise ContractError("icon_eps_coverage_blocker_mapping_invalid")
    return result


def _official_readiness(document: Mapping[str, Any]) -> tuple[bool, str]:
    if document.get("schema") != OFFICIAL_READINESS_SCHEMA:
        raise ContractError("icon_eps_coverage_official_readiness_schema_invalid")
    products = _mapping(
        document.get("official_eps_products"),
        error="icon_eps_coverage_official_readiness_products_invalid",
    )
    global_eps = _mapping(
        products.get(GLOBAL_EPS),
        error="icon_eps_coverage_official_global_eps_missing",
    )
    if global_eps.get("source_role") != "official_native":
        raise ContractError("icon_eps_coverage_official_global_eps_role_invalid")
    ready_cohorts = _nonnegative_int(
        global_eps.get("station_member_cohort_ready_count"),
        error="icon_eps_coverage_official_global_eps_count_invalid",
    )
    adapter_ready = global_eps.get("native_adapter_execution_ready") is True
    reasons = global_eps.get("blockers")
    if not isinstance(reasons, list):
        raise ContractError("icon_eps_coverage_official_global_eps_blockers_invalid")
    if ready_cohorts <= 0:
        return False, "DATA_MISSING"
    if not adapter_ready:
        return False, _canonical_blocker(reasons, fallback="LINEAGE_MISMATCH")
    return True, ""


def _strict_oos_scopes(
    document: Mapping[str, Any], *, stations: Sequence[str]
) -> tuple[str, bool, dict[tuple[str, str], dict[str, Any]]]:
    if document.get("schema") != STRICT_OOS_SCHEMA:
        raise ContractError("icon_eps_coverage_strict_oos_schema_invalid")
    if document.get("product_id") != GLOBAL_EPS:
        raise ContractError("icon_eps_coverage_strict_oos_product_invalid")
    as_of_utc = str(document.get("as_of_utc") or "").strip()
    if not as_of_utc:
        raise ContractError("icon_eps_coverage_strict_oos_as_of_missing")
    source_sha = _sha(
        document.get("content_sha256"), error="icon_eps_coverage_strict_oos_content_sha_invalid"
    )
    source_authority = _mapping(
        document.get("source_authority"),
        error="icon_eps_coverage_strict_oos_authority_invalid",
    )
    if (
        source_authority.get("forecast_source_role") != "official_native"
        or source_authority.get("forecast_transport_provider_id") != "DWD"
        or source_authority.get("open_meteo_fallback_used") is not False
        or document.get("formal_p0_eligible") is not False
        or document.get("promotion_authority_generated") is not False
    ):
        raise ContractError("icon_eps_coverage_strict_oos_authority_invalid")
    native_arrival_indexed = source_authority.get("native_arrival_index_used") is True
    readiness = document.get("readiness")
    if not isinstance(readiness, list):
        raise ContractError("icon_eps_coverage_strict_oos_readiness_invalid")

    station_set = set(stations)
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in readiness:
        row = _mapping(raw, error="icon_eps_coverage_strict_oos_row_invalid")
        station = str(row.get("station_id") or "").strip().upper()
        lead = str(row.get("lead_bucket") or "").strip().lower()
        if station not in station_set:
            raise ContractError("icon_eps_coverage_strict_oos_station_invalid")
        if lead == "*":
            continue
        if lead not in LEADS:
            raise ContractError("icon_eps_coverage_strict_oos_lead_invalid")
        key = (station, lead)
        if key in indexed:
            raise ContractError("icon_eps_coverage_strict_oos_scope_ambiguous")
        raw_cycle = str(row.get("run_cycle") or "").strip()
        run_cycle = None if raw_cycle in {"", "*"} else raw_cycle
        reasons = row.get("blockers") or row.get("unresolved_reasons") or []
        if not isinstance(reasons, list):
            raise ContractError("icon_eps_coverage_strict_oos_blockers_invalid")
        indexed[key] = {
            "run_cycle": run_cycle,
            "state": str(row.get("state") or "").strip(),
            "train_count": _nonnegative_int(
                row.get("training_case_count"), error="icon_eps_coverage_strict_oos_count_invalid"
            ),
            "holdout_count": _nonnegative_int(
                row.get("strict_holdout_case_count"),
                error="icon_eps_coverage_strict_oos_count_invalid",
            ),
            "reasons": reasons,
            "raw_receipt_sha256": (
                _sha(
                    row.get("raw_receipt_sha256"),
                    error="icon_eps_coverage_strict_oos_raw_receipt_sha_invalid",
                )
                if row.get("raw_receipt_sha256") is not None
                else None
            ),
        }
    return as_of_utc, native_arrival_indexed, {
        key: {**value, "source_evidence_sha256": source_sha}
        for key, value in indexed.items()
    }


def _validate_candidate_daily_archives(archives: Sequence[Mapping[str, Any]]) -> None:
    """Validate candidates without granting lead coverage from their dates."""

    for raw in archives:
        archive = _mapping(raw, error="icon_eps_coverage_daily_archive_invalid")
        if (
            archive.get("schema") != DAILY_ARCHIVE_SCHEMA
            or archive.get("ensemble_system_id") != GLOBAL_EPS
            or _nonnegative_int(
                archive.get("member_count"), error="icon_eps_coverage_daily_archive_count_invalid"
            ) != EXPECTED_GLOBAL_MEMBER_COUNT
            or _nonnegative_int(
                archive.get("station_count"), error="icon_eps_coverage_daily_archive_count_invalid"
            ) != expected_station_count()
        ):
            raise ContractError("icon_eps_coverage_daily_archive_identity_invalid")
        authority = _mapping(
            archive.get("source_authority"),
            error="icon_eps_coverage_daily_archive_authority_invalid",
        )
        if (
            authority.get("source_role") != "official_native"
            or authority.get("transport_provider_id") != "DWD"
            or archive.get("native_arrival_index_eligible") is not False
            or archive.get("formal_p0_eligible") is not False
        ):
            raise ContractError("icon_eps_coverage_daily_archive_authority_invalid")


def _validate_republisher_binding(document: Mapping[str, Any] | None) -> None:
    if document is None:
        return
    if document.get("schema") != REPUBLISHER_BINDING_SCHEMA:
        raise ContractError("icon_eps_coverage_republisher_schema_invalid")
    authority = _mapping(
        document.get("authority"), error="icon_eps_coverage_republisher_authority_invalid"
    )
    if (
        authority.get("source_role") != "aggregator_republisher"
        or authority.get("transport_provider_id") != "OPEN_METEO"
        or authority.get("native_arrival_index_eligible") is not False
        or authority.get("native_formal_p0_eligible") is not False
        or authority.get("p1_eligible") is not False
        or authority.get("formal_live_authority") is not False
    ):
        raise ContractError("icon_eps_coverage_republisher_authority_invalid")
    bindings = document.get("bindings")
    if not isinstance(bindings, list):
        raise ContractError("icon_eps_coverage_republisher_bindings_invalid")
    for raw in bindings:
        row = _mapping(raw, error="icon_eps_coverage_republisher_binding_invalid")
        system = str(row.get("ensemble_system_id") or row.get("system_id") or "").strip()
        if system not in {GLOBAL_EPS, EU_EPS, D2_EPS}:
            continue
        if (
            row.get("source_role") != "aggregator_republisher"
            or row.get("native_arrival_index_eligible") is not False
            or row.get("native_formal_p0_eligible") is not False
            or row.get("p1_eligible") is not False
            or row.get("formal_live_authority") is not False
        ):
            raise ContractError("icon_eps_coverage_republisher_binding_authority_invalid")


def _row_fields(
    *,
    station: str,
    lead: str,
    official_ready: bool,
    official_blocker: str,
    native_arrival_indexed: bool,
    scope: Mapping[str, Any] | None,
    as_of_utc: str,
    source_evidence_sha256: str,
) -> dict[str, Any]:
    state = "BLOCKED"
    blocker = official_blocker
    run_cycle = None
    train_count = 0
    holdout_count = 0
    strict_asof = False
    closed_awc_truth = False
    oos_evidence_sha256 = None
    single_system_artifact_sha256 = None
    raw_receipt_sha256 = None

    if official_ready:
        if scope is None:
            blocker = "RUN_CYCLE_MISMATCH"
        else:
            run_cycle = scope["run_cycle"]
            train_count = scope["train_count"]
            holdout_count = scope["holdout_count"]
            if run_cycle is None:
                blocker = "RUN_CYCLE_MISMATCH"
            elif not native_arrival_indexed:
                state = "DATA_READY"
                blocker = "LINEAGE_MISMATCH"
            else:
                raw_blocker = _canonical_blocker(scope["reasons"], fallback="OOS_SAMPLE_SHORT")
                strict_asof = scope["state"] == "STRICT_OOS_READY_NOT_BINDING"
                closed_awc_truth = strict_asof
                if (
                    strict_asof
                    and train_count >= 20
                    and holdout_count >= 8
                    and not scope["reasons"]
                    and scope["raw_receipt_sha256"] is not None
                ):
                    state = "OOS_READY"
                    blocker = ""
                    oos_evidence_sha256 = source_evidence_sha256
                    single_system_artifact_sha256 = source_evidence_sha256
                    raw_receipt_sha256 = scope["raw_receipt_sha256"]
                else:
                    state = "DATA_READY"
                    blocker = (
                        "LINEAGE_MISMATCH"
                        if strict_asof and scope["raw_receipt_sha256"] is None
                        else raw_blocker
                    )

    if state != "OOS_READY" and blocker not in TYPED_SCOPE_BLOCKERS:
        raise ContractError("icon_eps_coverage_terminal_blocker_invalid")
    return {
        "station_id": station,
        "product_id": GLOBAL_EPS,
        "provider_id": "DWD",
        "model_family": "global_ensemble",
        "independence_family_id": "DWD_ICON_EPS",
        "source_class": "native_official",
        "lead_bucket": lead,
        "run_cycle": run_cycle,
        "as_of_utc": as_of_utc,
        "state": state,
        "blockers": [] if state == "OOS_READY" else [blocker],
        "evidence_role": "historical_strict_oos",
        "strict_asof": strict_asof,
        "closed_awc_truth": closed_awc_truth,
        "train_count": train_count,
        "holdout_count": holdout_count,
        "source_evidence_sha256": source_evidence_sha256,
        "raw_receipt_sha256": raw_receipt_sha256,
        "oos_evidence_sha256": oos_evidence_sha256,
        "single_system_artifact_sha256": single_system_artifact_sha256,
        "native_arrival_indexed": native_arrival_indexed,
        "formal_live_authority": False,
        "cross_selection_count": 0,
        "cross_activation_count": 0,
        "p0_count": 0,
    }


def build_icon_global_eps_coverage_matrix(
    *,
    official_readiness: Mapping[str, Any],
    strict_oos: Mapping[str, Any],
    daily_archives: Sequence[Mapping[str, Any]] = (),
    republisher_binding: Mapping[str, Any] | None = None,
    compiled_at_utc: str | None = None,
) -> dict[str, Any]:
    """Return only the shared canonical coverage matrix for Global/EPS."""

    stations = canonical_station_ids()
    official_ready, official_blocker = _official_readiness(official_readiness)
    source_as_of_utc, native_arrival_indexed, scopes = _strict_oos_scopes(
        strict_oos, stations=stations
    )
    as_of_utc = str(compiled_at_utc or source_as_of_utc).strip()
    try:
        source_as_of = datetime.fromisoformat(source_as_of_utc.replace("Z", "+00:00"))
        compiled_at = datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("icon_eps_coverage_compiled_at_invalid") from exc
    if (
        source_as_of.tzinfo is None
        or source_as_of.utcoffset() is None
        or compiled_at.tzinfo is None
        or compiled_at.utcoffset() is None
        or source_as_of.astimezone(timezone.utc) > compiled_at.astimezone(timezone.utc)
    ):
        raise ContractError("icon_eps_coverage_compiled_before_source")
    _validate_candidate_daily_archives(daily_archives)
    _validate_republisher_binding(republisher_binding)
    source_evidence_sha256 = _sha(
        strict_oos.get("content_sha256"), error="icon_eps_coverage_strict_oos_content_sha_invalid"
    )
    rows = tuple(
        make_coverage_row(
            **_row_fields(
                station=station,
                lead=lead,
                official_ready=official_ready,
                official_blocker=official_blocker,
                native_arrival_indexed=native_arrival_indexed,
                scope=scopes.get((station, lead)),
                as_of_utc=as_of_utc,
                source_evidence_sha256=source_evidence_sha256,
            )
        )
        for station in stations
        for lead in LEADS
    )
    return build_coverage_matrix(
        rows,
        expected_product_ids=(GLOBAL_EPS,),
        as_of_utc=as_of_utc,
    )


def render_icon_global_eps_terminal_matrix(matrix: Mapping[str, Any]) -> str:
    """Render the shared matrix without defining a second terminal contract."""

    if matrix.get("schema") != MATRIX_SCHEMA:
        raise ContractError("icon_eps_coverage_matrix_schema_invalid")
    rows = matrix.get("rows")
    if not isinstance(rows, list) or len(rows) != expected_station_count() * len(LEADS):
        raise ContractError("icon_eps_coverage_matrix_rows_invalid")
    by_station: dict[str, dict[str, Mapping[str, Any]]] = {}
    for raw in rows:
        row = _mapping(raw, error="icon_eps_coverage_terminal_row_invalid")
        station = str(row.get("station_id") or "").strip().upper()
        lead = str(row.get("lead_bucket") or "").strip().lower()
        if lead not in LEADS or lead in by_station.setdefault(station, {}):
            raise ContractError("icon_eps_coverage_terminal_scope_ambiguous")
        by_station[station][lead] = row
    if set(by_station) != set(canonical_station_ids()) or any(
        set(values) != set(LEADS) for values in by_station.values()
    ):
        raise ContractError("icon_eps_coverage_terminal_scope_incomplete")
    lines = ["DWD ICON Global/EPS canonical coverage matrix", "station | d0 | d1 | d2"]
    for station in canonical_station_ids():
        values = []
        for lead in LEADS:
            row = by_station[station][lead]
            blocker = "READY" if not row["blockers"] else row["blockers"][0]
            values.append(f"{row['state']}/{blocker}")
        lines.append(f"{station} | " + " | ".join(values))
    lines.append(
        "cross_selection_count=0; cross_activation_count=0; p0_count=0"
    )
    return "\n".join(lines)


def _read_audit(path: Path) -> Mapping[str, Any]:
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size > MAX_AUDIT_INPUT_BYTES:
        raise ContractError("icon_eps_coverage_audit_input_invalid")
    try:
        with resolved.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("icon_eps_coverage_audit_input_invalid") from exc
    return _mapping(value, error="icon_eps_coverage_audit_input_invalid")


def _read_bounded_audits(paths: Sequence[Path]) -> tuple[Mapping[str, Any], ...]:
    if not paths or len(paths) > MAX_AUDIT_INPUT_FILES:
        raise ContractError("icon_eps_coverage_audit_input_count_invalid")
    resolved = tuple(Path(path).expanduser().resolve(strict=True) for path in paths)
    sizes = []
    for path in resolved:
        if not path.is_file() or path.stat().st_size > MAX_AUDIT_INPUT_BYTES:
            raise ContractError("icon_eps_coverage_audit_input_invalid")
        sizes.append(path.stat().st_size)
    if sum(sizes) > MAX_TOTAL_AUDIT_INPUT_BYTES:
        raise ContractError("icon_eps_coverage_audit_input_total_exceeded")
    return tuple(_read_audit(path) for path in resolved)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-readiness", type=Path, required=True)
    parser.add_argument("--strict-oos", type=Path, required=True)
    parser.add_argument("--daily-archive", type=Path, action="append", default=[])
    parser.add_argument("--republisher-binding", type=Path)
    parser.add_argument("--compiled-at-utc")
    args = parser.parse_args(argv)
    paths = [args.official_readiness, args.strict_oos, *args.daily_archive]
    if args.republisher_binding is not None:
        paths.append(args.republisher_binding)
    documents = _read_bounded_audits(paths)
    official_readiness, strict_oos = documents[:2]
    daily_end = 2 + len(args.daily_archive)
    matrix = build_icon_global_eps_coverage_matrix(
        official_readiness=official_readiness,
        strict_oos=strict_oos,
        daily_archives=documents[2:daily_end],
        republisher_binding=(documents[daily_end] if args.republisher_binding else None),
        compiled_at_utc=args.compiled_at_utc,
    )
    print(render_icon_global_eps_terminal_matrix(matrix))
    return 0


__all__ = [
    "D2_EPS",
    "EU_EPS",
    "GLOBAL_EPS",
    "MAX_AUDIT_INPUT_BYTES",
    "MAX_AUDIT_INPUT_FILES",
    "MAX_TOTAL_AUDIT_INPUT_BYTES",
    "build_icon_global_eps_coverage_matrix",
    "main",
    "render_icon_global_eps_terminal_matrix",
]


if __name__ == "__main__":  # pragma: no cover - CLI wrapper.
    raise SystemExit(main())
