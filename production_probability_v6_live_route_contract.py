"""Deterministic production ingress route target and controlled activator."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "production_probability_v6_live_route_contract_v1"
CONFIRMATION = "ACTIVATE_PHYSICAL_STATE_BAYES_V6_LIVE_ROUTES"
ROOT = Path(__file__).resolve().parent
ROUTE_FILES = (
    "scripts/amsc_zuuu_metar_taf_poll_once.sh",
    "scripts/watch_zuuu_speci_once.py",
    "scripts/polymarket_monitor_daemon.sh",
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _replace_once(value: str, old: str, new: str, *, label: str) -> str:
    count = value.count(old)
    if count == 0 and new in value:
        return value
    if count != 1:
        raise ValueError(f"v6_live_route_source_drift:{label}:count={count}")
    return value.replace(old, new, 1)


def _target_poller(value: str) -> str:
    result = value.replace(
        "apply_amsc_probability_fast_lane_from_plan.py",
        "apply_amsc_probability_fast_lane_v6_from_plan.py",
    )
    if "APPLY_AMSC_PROBABILITY_FAST_LANE_V6" not in result:
        result = result.replace(
            "APPLY_AMSC_PROBABILITY_FAST_LANE",
            "APPLY_AMSC_PROBABILITY_FAST_LANE_V6",
        )
    result = result.replace(
        "apply_amsc_taf_forecast_revision_from_plan.py",
        "apply_amsc_taf_forecast_revision_v6_from_plan.py",
    )
    required = (
        "apply_amsc_probability_fast_lane_v6_from_plan.py",
        "APPLY_AMSC_PROBABILITY_FAST_LANE_V6",
        "apply_amsc_taf_forecast_revision_v6_from_plan.py",
    )
    if not all(token in result for token in required):
        raise ValueError("v6_live_route_poller_target_incomplete")
    return result


def _target_speci(value: str) -> str:
    old_import = """from production_reported_x_amsc_archive_probability_ingress import (  # noqa: E402
    apply_reported_x_amsc_archive_probability_ingress,
)
from production_probability_successor_activation_contract import PRODUCTION_PATH as DEFAULT_PRODUCTION_ACTIVATION  # noqa: E402
from production_probability_p0_runtime import DEFAULT_RESOLUTION_AUTHORITY  # noqa: E402
"""
    new_import = """from production_probability_v6_amsc_archive_ingress import (  # noqa: E402
    apply_v6_amsc_archive_probability_ingress,
)
from production_probability_v6_p0_input_runtime import DEFAULT_RESOLUTION_AUTHORITY  # noqa: E402

DEFAULT_PRODUCTION_ACTIVATION = ROOT / "config" / "production_probability_runtime_activation.json"
"""
    result = _replace_once(value, old_import, new_import, label="speci_import")
    result = result.replace(
        "apply_reported_x_amsc_archive_probability_ingress",
        "apply_v6_amsc_archive_probability_ingress",
    )
    result = result.replace(
        '"schema": "production_reported_x_amsc_archive_probability_ingress_v1"',
        '"schema": "production_probability_v6_amsc_archive_ingress_v1"',
    )
    forbidden = (
        "production_reported_x_amsc_archive_probability_ingress",
        "apply_reported_x_amsc_archive_probability_ingress",
        "production_probability_successor_activation_contract",
    )
    if any(token in result for token in forbidden) or "apply_v6_amsc_archive_probability_ingress" not in result:
        raise ValueError("v6_live_route_speci_target_incomplete")
    return result


def _target_monitor(value: str) -> str:
    old = """  \"$PY\" \"$ROOT/scripts/ensure_next_live_physical_p10_cycle_shadow.py\" \\
    --event-date \"$event_date\" \\
    --shadow-db \"$AMSC_P10_SHADOW_DB\" \\
    --apply --confirm ENSURE_NEXT_LIVE_PHYSICAL_P10_CYCLE_SHADOW \\
"""
    new = """  \"$PY\" \"$ROOT/scripts/prepare_next_day_production_probability_v6.py\" \\
    --db \"$AMSC_ZUUU_SPECI_REALTIME_DB\" \\
    --activation \"$AMSC_ZUUU_PRODUCTION_PROBABILITY_ACTIVATION_PATH\" \\
    --event-date \"$event_date\" \\
    --apply --confirm PREPARE_NEXT_DAY_PRODUCTION_PHYSICAL_STATE_BAYES_V6 \\
"""
    result = _replace_once(value, old, new, label="next_day_p0_monitor_route")
    if (
        "prepare_next_day_production_probability_v6.py" not in result
        or "PREPARE_NEXT_DAY_PRODUCTION_PHYSICAL_STATE_BAYES_V6" not in result
    ):
        raise ValueError("v6_live_route_monitor_target_incomplete")
    return result


def build_v6_live_route_targets(root: Path = ROOT) -> dict[str, bytes]:
    base = Path(root).expanduser().resolve()
    poller = (base / ROUTE_FILES[0]).read_text(encoding="utf-8")
    speci = (base / ROUTE_FILES[1]).read_text(encoding="utf-8")
    monitor = (base / ROUTE_FILES[2]).read_text(encoding="utf-8")
    return {
        ROUTE_FILES[0]: _target_poller(poller).encode("utf-8"),
        ROUTE_FILES[1]: _target_speci(speci).encode("utf-8"),
        ROUTE_FILES[2]: _target_monitor(monitor).encode("utf-8"),
    }


def audit_v6_live_routes(root: Path = ROOT) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    try:
        targets = build_v6_live_route_targets(base)
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": [str(exc)],
            "read_only": True,
        }
    files = {}
    for relative, target in targets.items():
        current = (base / relative).read_bytes()
        files[relative] = {
            "current_sha256": _sha(current),
            "target_sha256": _sha(target),
            "active": current == target,
        }
    monitor = (base / "scripts/polymarket_monitor_daemon.sh").read_text(encoding="utf-8")
    monitor_checks = {
        "regular_metar_half_hour_scheduler": "now_epoch % 1800" in monitor,
        "regular_metar_immutable_poller": "run_amsc_zuuu_metar_taf_poll_immutable.sh" in monitor,
        "speci_realtime_watcher": "watch_zuuu_speci_once.py" in monitor,
        "next_day_v6_p0_prepared_off_hot_path": "prepare_next_day_production_probability_v6.py" in targets[ROUTE_FILES[2]].decode("utf-8"),
    }
    blockers = [f"v6_live_route_not_active:{name}" for name, value in files.items() if not value["active"]]
    blockers.extend(f"v6_monitor_route_missing:{name}" for name, value in monitor_checks.items() if not value)
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "v6_live_routes_active" if not blockers else "not_active",
        "blockers": blockers,
        "files": files,
        "monitor_checks": monitor_checks,
        "metar_half_hour_and_hour_trigger_same_v6_ingress": True if not blockers else False,
        "speci_same_grade_realtime_v6_ingress": True if not blockers else False,
        "taf_amd_future_revision_v6_ingress": True if not blockers else False,
        "next_day_v6_p0_off_hot_path": True if not blockers else False,
        "v5_fallback": False,
        "read_only": True,
        "places_orders": False,
    }


def activate_v6_live_routes(root: Path = ROOT) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    targets = build_v6_live_route_targets(base)
    for relative, target in targets.items():
        path = base / relative
        temporary = path.with_name(f".{path.name}.v6-route.{os.getpid()}")
        temporary.write_bytes(target)
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    result = audit_v6_live_routes(base)
    if result.get("ok") is not True:
        raise RuntimeError("v6_live_route_post_apply_audit_failed")
    return {**result, "applied": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply:
        if args.confirm != CONFIRMATION:
            result = {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["confirmation_missing"]}
        else:
            result = activate_v6_live_routes(args.root)
    else:
        result = audit_v6_live_routes(args.root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONFIRMATION",
    "ROUTE_FILES",
    "SCHEMA_VERSION",
    "activate_v6_live_routes",
    "audit_v6_live_routes",
    "build_v6_live_route_targets",
]
