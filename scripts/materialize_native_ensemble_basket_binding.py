#!/usr/bin/env python3
"""Materialize official-native scenario-role basket candidates (no-live).

The command reads an already materialized official-native scenario-role
artifact plus one or more probability-stage snapshots.  It writes only a
content-addressed candidate JSON artifact; it never reads CLOB, SQLite, or
the order gateway.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_basket_binding import (  # noqa: E402
    SCHEMA,
    build_native_ensemble_basket_binding,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    ResourceBoundError,
    load_bounded_json,
    write_bounded_json_if_changed,
)


def _blocked_artifact(*, reason: str, scenario_path: Path, stage_paths: list[Path]) -> dict[str, object]:
    identity: dict[str, object] = {
        "schema": SCHEMA,
        "method_version": "official_native_roles_stage_precedence_basket_candidates_v1",
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "state": "BLOCKED_FAIL_CLOSED",
        "blockers": [reason],
        "scenario_roles_input": str(scenario_path),
        "probability_stage_inputs": [str(path) for path in stage_paths],
        "writes_p0": False,
        "writes_p0m": False,
        "writes_p1": False,
        "writes_shared_live_database": False,
        "calls_clob": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-roles", type=Path, required=True)
    parser.add_argument(
        "--probability-stage",
        "--probability-snapshot",
        dest="stage_paths",
        action="append",
        type=Path,
        help="verified P0/P0-M/P1 snapshot; repeat for stage precedence",
    )
    parser.add_argument("--as-of-utc")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    stage_paths = list(args.stage_paths or [])
    if not stage_paths:
        parser.error("at least one --probability-stage/--probability-snapshot is required")
    try:
        scenario_path = args.scenario_roles.expanduser().resolve(strict=True)
        scenario = load_bounded_json(scenario_path)
        snapshots = [
            load_bounded_json(path.expanduser().resolve(strict=True))
            for path in stage_paths
        ]
        artifact = build_native_ensemble_basket_binding(
            scenario_roles_artifact=scenario,
            probability_stage_snapshots=snapshots,
            as_of_utc=args.as_of_utc,
        )
        output = args.output.expanduser().resolve()
        _artifact, wrote = artifact, write_bounded_json_if_changed(output, artifact)
        result = {
            "schema": SCHEMA,
            "state": artifact["state"],
            "content_sha256": artifact["content_sha256"],
            "station_count": artifact["station_count"],
            "blocked_station_count": artifact["blocked_station_count"],
            "output": str(output),
            "wrote": wrote,
            "calls_clob": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if artifact["state"] == "BUILT_NO_LIVE" else 2
    except (ContractError, ResourceBoundError, OSError, ValueError) as exc:
        output = args.output.expanduser().resolve()
        blocked = _blocked_artifact(
            reason=f"{type(exc).__name__}:{exc}",
            scenario_path=args.scenario_roles,
            stage_paths=stage_paths,
        )
        try:
            write_bounded_json_if_changed(output, blocked)
        except (OSError, ResourceBoundError):
            pass
        print(json.dumps({
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "output": str(output),
            "calls_clob": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
