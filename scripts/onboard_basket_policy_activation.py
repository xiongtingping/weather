#!/usr/bin/env python3
"""Issue the fleet's basket policy activations to fleet stations that have none.

Every seeded station holds the same twelve ``basket_policy_activation`` rows
(P0 / P0-M / P1 x d0 / d1 / d2 / d2_fallback), each pointing at the global
prior through declared fallbacks and at no station artifact.  A station
onboarded later has none, so every compile of it is refused on
``production_basket_policy_artifacts_missing`` and no decision fact is ever
written for it -- LLBG, LTFM and UUWW from 2026-10-04, once their cash-phase
authority was issued.

Only a uniform template is carried: every station's rows must be the same
rows with its own name in the activation id, or the template is refused
rather than one station's activation chosen for another.  The window runs
from ``--valid-from`` (never backdated) to the fleet's own end.

Emits a packet; ingest it with ``scripts/ingest_bayes_basket_target_artifacts.py``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.artifact_ingress import SCHEMA as INGRESS_SCHEMA  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.policy import StationBasketPolicyActivation  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402

DEFAULT_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"
DEFAULT_OUTPUT = ROOT / "output/audits/basket_policy_activation_onboarding.json"
COLUMNS = (
    "activation_id", "station_id", "stage", "season_bin", "maturity_bin",
    "liquidity_bin", "strategy_version", "global_prior_id",
    "target_artifact_id", "reserve_artifact_id", "cadence_artifact_id",
    "fallback_families_json", "generation", "valid_from_utc",
    "valid_until_utc",
)


def active_rows(conn: sqlite3.Connection, *, observed_at_utc: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT " + ",".join(COLUMNS) + " FROM basket_policy_activation"
        " WHERE valid_from_utc <= ? AND valid_until_utc > ?",
        (observed_at_utc, observed_at_utc),
    ).fetchall()
    return [dict(zip(COLUMNS, row)) for row in rows]


def _shape(row: dict[str, Any]) -> tuple:
    """The row with its station taken out of it."""

    station = str(row["station_id"])
    marker = f"-{station.lower()}-"
    activation_id = str(row["activation_id"])
    if activation_id.count(marker) != 1:
        raise SystemExit(f"activation id does not name its station once: {activation_id}")
    return (
        tuple(activation_id.split(marker)),
        *(row[key] for key in COLUMNS[2:]),
    )


def build_onboarding_packet(
    rows: list[dict[str, Any]],
    *,
    stations: list[str],
    valid_from_utc: str,
    scope: str = DEFAULT_SCOPE,
) -> dict[str, Any]:
    by_station: dict[str, set[tuple]] = {}
    for row in rows:
        by_station.setdefault(str(row["station_id"]), set()).add(_shape(row))
    if not by_station:
        raise SystemExit("no fleet activations to take a template from")
    templates = {frozenset(shapes) for shapes in by_station.values()}
    if len(templates) != 1:
        raise SystemExit(f"fleet policy activations are not uniform: {len(templates)} shapes")
    template = sorted(next(iter(templates)))
    activations: list[dict[str, Any]] = []
    for station in sorted(stations):
        if station in by_station:
            raise SystemExit(f"station already has activations: {station}")
        for shape in template:
            (prefix, suffix), stage, season, maturity, liquidity, strategy, prior, \
                target, reserve, cadence, fallback_json, generation, _from, until = shape
            if valid_from_utc >= str(until):
                raise SystemExit("valid_from must precede the fleet window's end")
            activations.append(StationBasketPolicyActivation.create(
                activation_id=f"{prefix}-{station.lower()}-{suffix}",
                station_id=station,
                stage=stage,
                season_bin=season,
                maturity_bin=maturity,
                liquidity_bin=liquidity,
                strategy_version=strategy,
                global_prior_id=prior,
                target_artifact_id=target,
                reserve_artifact_id=reserve,
                cadence_artifact_id=cadence,
                fallback_families=tuple(json.loads(fallback_json)),
                generation=int(generation),
                valid_from_utc=valid_from_utc,
                valid_until_utc=str(until),
                activated_at_utc=valid_from_utc,
                promoted=False,
            ).to_dict())
    packet: dict[str, Any] = {
        "schema": INGRESS_SCHEMA,
        "policy_authority_scope": scope,
        "execution_mode": "production_no_live",
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "policy_activations": activations,
        "onboarding": {
            "window_from_utc": valid_from_utc,
            "station_ids": sorted(stations),
            "rows_per_station": len(template),
            "template_from_station_count": len(by_station),
        },
    }
    packet["packet_sha256"] = content_sha256(packet)
    return packet


def _utc(value: str) -> str:
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--valid-from", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    valid_from_utc = _utc(args.valid_from)
    database = resolve_target_database(args.db)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60.0)
    try:
        rows = active_rows(conn, observed_at_utc=valid_from_utc)
    finally:
        conn.close()
    fleet = {str(value).upper() for value in coordinate_registry_manifest()["station_ids"]}
    missing = sorted(fleet - {str(row["station_id"]) for row in rows})
    if not missing:
        print("every fleet station already has basket policy activations")
        return 0
    packet = build_onboarding_packet(rows, stations=missing, valid_from_utc=valid_from_utc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_bounded_json(args.output, packet)
    print(
        f"{len(missing)} stations x {packet['onboarding']['rows_per_station']} activations"
        f" from {valid_from_utc}: {', '.join(missing)}\n  packet: {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
