"""Resident-service route contract for the target candidate compiler.

The former monitor/poller/SPECI and embedded probability-worker fan-out is
retired.  The only resident weather decision route is the activation-gated,
no-live target compiler embedded in ``com.weather.weather_basket_yes``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "production_probability_v6_live_route_contract_v3"
CONFIRMATION = "ATTEST_RESIDENT_IDENTIFIABLE_PHYSICAL_ROUTE"
ROOT = Path(__file__).resolve().parent
ROUTE_FILES = ("scripts/weather_basket_yes_daemon.py",)
REQUIRED_ROUTE_TOKENS = (
    "TargetProductionWorker",
    "compile_production_cycle_from_ssot",
    "basket_consumes_materialized_probability",
    '"execution_mode": "production_no_live"',
)
FORBIDDEN_ROUTE_TOKENS = (
    "watch_zuuu_speci_once.py",
    "polymarket_monitor_daemon.sh",
    "apply_amsc_probability_fast_lane_v6_from_plan.py",
    "IdentifiablePhysicalProbabilityWorker",
    "ProspectivePhysicalEvidenceCaptureWorker",
    "physical_probability_worker.run",
    "prospective_evidence_worker.run",
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _resident_target(value: str) -> str:
    missing = [token for token in REQUIRED_ROUTE_TOKENS if token not in value]
    forbidden = [token for token in FORBIDDEN_ROUTE_TOKENS if token in value]
    if missing:
            raise ValueError("resident_target_route_missing:" + ",".join(missing))
    if forbidden:
        raise ValueError("retired_parallel_route_reachable:" + ",".join(forbidden))
    return value


def build_v6_live_route_targets(root: Path = ROOT) -> dict[str, bytes]:
    base = Path(root).expanduser().resolve()
    relative = ROUTE_FILES[0]
    current = (base / relative).read_text(encoding="utf-8")
    return {relative: _resident_target(current).encode("utf-8")}


def audit_v6_live_routes(root: Path = ROOT) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    try:
        targets = build_v6_live_route_targets(base)
        installer = (
            base / "scripts/install_weather_basket_yes_user_services.sh"
        ).read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": [str(exc)],
            "read_only": True,
            "places_orders": False,
        }
    files = {}
    for relative, target in targets.items():
        current = (base / relative).read_bytes()
        files[relative] = {
            "current_sha256": _sha(current),
            "target_sha256": _sha(target),
            "active": current == target,
        }
    no_fourth_service = not any(
        token in installer
        for token in (
            "identifiable_physical_probability",
            "prospective_physical_evidence",
        )
    )
    blockers = [
            f"resident_target_route_not_active:{name}"
        for name, value in files.items()
        if not value["active"]
    ]
    if not no_fourth_service:
        blockers.append("fourth_probability_service_definition_forbidden")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "resident_route_active" if not blockers else "blocked",
        "blockers": blockers,
        "files": files,
        "resident_service": "com.weather.weather_basket_yes",
        "target_candidate_orderless": True,
        "target_activation_gated": True,
        "no_fourth_service_definition": no_fourth_service,
        "retired_monitor_and_parallel_ingress_routes": True,
        "read_only": True,
        "places_orders": False,
    }


def activate_v6_live_routes(root: Path = ROOT) -> dict[str, Any]:
    """Attest the code route; production epoch activation is a separate step."""

    result = audit_v6_live_routes(root)
    if result.get("ok") is not True:
        raise ValueError("resident_physical_route_attestation_failed")
    return {
        **result,
        "state": "resident_route_attested_no_mutation",
        "writes_files": False,
        "activates_probability_epoch": False,
    }


__all__ = [
    "CONFIRMATION",
    "FORBIDDEN_ROUTE_TOKENS",
    "REQUIRED_ROUTE_TOKENS",
    "ROUTE_FILES",
    "SCHEMA_VERSION",
    "activate_v6_live_routes",
    "audit_v6_live_routes",
    "build_v6_live_route_targets",
]
