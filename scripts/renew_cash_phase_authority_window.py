#!/usr/bin/env python3
"""Re-issue the active cash-phase authority window without changing a policy.

The authority manifest is what lets the resident market ingress resolve a
station's cash phase, and it carries an explicit validity window.  When that
window lapses the ingress fails closed on
``cash_phase_authority_manifest_not_unique`` -- which reads like a duplicate
but is raised for *zero* active rows just as much as for two -- and the whole
opening chain stops behind it.  That is what happened at 2026-08-23T00:00:00Z,
thirty-one days after the only generation this table had ever held.

Renewal is deliberately not a policy edit.  Every station keeps the phase
boundaries and cash template it already had; only the window moves, and both
the policy artifact and its manifest have to move together because the
resolver compares the two windows field for field.  The policy version is
suffixed with the new window's start so the append-only insert lands as a new
generation rather than being silently ignored as an existing key, and so the
lineage of renewals stays readable in the table itself.

Emits a bundle; it does not write.  Ingest it with
``scripts/ingest_bayes_basket_target_artifacts.py``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.artifact_ingress import SCHEMA as INGRESS_SCHEMA
from bayes_basket_target.cash_phase import BasketCashPhasePolicyArtifact
from bayes_basket_target.cash_phase_authority import CashPhaseAuthorityManifest
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.resource_bounds import write_bounded_json
from bayes_basket_target.station_registry import coordinate_registry_manifest

DEFAULT_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"
DEFAULT_OUTPUT = ROOT / "output/audits/cash_phase_authority_window_renewal.json"
# A renewal marker rather than a new name: the base version is what the policy
# has always been called, and everything after the separator is which window
# this generation authorises.
VERSION_WINDOW_SEPARATOR = "@"


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _base_version(policy_version: str) -> str:
    return str(policy_version).split(VERSION_WINDOW_SEPARATOR, 1)[0]


def active_bindings(
    conn: sqlite3.Connection, *, scope: str, observed_at_utc: str
) -> list[dict[str, Any]]:
    """The (policy, manifest) pairs whose window contains ``observed_at_utc``.

    Joined on all four binding fields rather than on the version alone: the
    resolver requires the manifest and its policy to agree on content hash and
    on both window ends, so a pair that does not already agree is not a
    binding this tool may carry forward.
    """

    rows = conn.execute(
        """
        SELECT p.policy_version, p.station_id, p.station_timezone,
               p.heavy_to_standard_local_time, p.standard_to_tail_local_time,
               p.cash_by_phase_json, p.evidence_status, p.promoted,
               m.authority_scope, p.valid_from_utc, p.valid_until_utc
        FROM basket_cash_phase_authority_manifest m
        JOIN basket_cash_phase_policy_artifact p
          ON p.policy_version = m.policy_version
         AND p.content_sha256 = m.artifact_sha256
         AND p.valid_from_utc = m.valid_from_utc
         AND p.valid_until_utc = m.valid_until_utc
        WHERE m.authority_scope = ?
        ORDER BY p.station_id, p.policy_version
        """,
        (scope,),
    ).fetchall()
    bindings: list[dict[str, Any]] = []
    for row in rows:
        (
            policy_version,
            station_id,
            station_timezone,
            heavy_time,
            tail_time,
            cash_json,
            evidence_status,
            promoted,
            authority_scope,
            valid_from_utc,
            valid_until_utc,
        ) = row
        bindings.append(
            {
                "policy_version": str(policy_version),
                "station_id": str(station_id),
                "station_timezone": str(station_timezone),
                "heavy_to_standard_local_time": str(heavy_time),
                "standard_to_tail_local_time": str(tail_time),
                "cash_by_phase": json.loads(cash_json),
                "evidence_status": str(evidence_status),
                "promoted": bool(promoted),
                "authority_scope": str(authority_scope),
                "valid_from_utc": str(valid_from_utc),
                "valid_until_utc": str(valid_until_utc),
            }
        )
    return bindings


def latest_bindings(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One binding per station: the generation whose window ends last.

    A lapsed window is exactly what this tool exists to replace, so selecting
    on "still valid" would find nothing on the day it is needed most.
    """

    by_station: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        station = binding["station_id"]
        current = by_station.get(station)
        if current is None or binding["valid_until_utc"] > current["valid_until_utc"]:
            by_station[station] = binding
    return [by_station[key] for key in sorted(by_station)]


def build_renewal_packet(
    bindings: list[dict[str, Any]],
    *,
    valid_from_utc: str,
    valid_until_utc: str,
    scope: str,
) -> dict[str, Any]:
    window_tag = valid_from_utc[:10].replace("-", "")
    cash_policies: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    for binding in bindings:
        if binding["promoted"]:
            # A promoted policy is not a candidate and its window is not this
            # tool's to move; renewing it here would launder a promotion.
            raise SystemExit(
                f"promoted policy in candidate scope: {binding['policy_version']}"
            )
        policy = BasketCashPhasePolicyArtifact.create(
            policy_version=(
                f"{_base_version(binding['policy_version'])}"
                f"{VERSION_WINDOW_SEPARATOR}{window_tag}"
            ),
            station_id=binding["station_id"],
            station_timezone=binding["station_timezone"],
            heavy_to_standard_local_time=binding["heavy_to_standard_local_time"],
            standard_to_tail_local_time=binding["standard_to_tail_local_time"],
            cash_by_phase=binding["cash_by_phase"],
            valid_from_utc=valid_from_utc,
            valid_until_utc=valid_until_utc,
            evidence_status=binding["evidence_status"],
            promoted=False,
        )
        cash_policies.append(policy.to_dict())
        manifests.append(
            CashPhaseAuthorityManifest.create(
                station_id=policy.station_id,
                policy_version=policy.policy_version,
                artifact_sha256=policy.content_sha256,
                station_timezone=policy.station_timezone,
                valid_from_utc=policy.valid_from_utc,
                valid_until_utc=policy.valid_until_utc,
                authority_scope=scope,
            ).to_dict()
        )
    packet: dict[str, Any] = {
        "schema": INGRESS_SCHEMA,
        "policy_authority_scope": scope,
        "execution_mode": "production_no_live",
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "cash_phase_policies": cash_policies,
        "cash_phase_authority_manifests": manifests,
        "renewal": {
            "renews_window_from_utc": valid_from_utc,
            "renews_window_until_utc": valid_until_utc,
            "station_count": len(cash_policies),
            "carried_policy_versions": sorted(
                _base_version(item["policy_version"]) for item in bindings
            ),
        },
    }
    packet["packet_sha256"] = content_sha256(packet)
    return packet


def build_onboarding_packet(
    bindings: list[dict[str, Any]],
    *,
    station_timezones: dict[str, str],
    valid_from_utc: str,
    scope: str,
) -> dict[str, Any]:
    """The fleet's cash-phase template, issued to stations that have none.

    A station onboarded after the table was seeded has no binding at all, and
    the resident ingress then refuses every one of its snapshots on
    ``resident_cash_phase_authority_manifest_not_unique`` (zero rows) --
    LLBG, LTFM and UUWW, joined 2026-10-04, three blocked scopes every cycle.

    Only a uniform template may be carried: every existing binding has to
    agree on both phase boundaries, the cash by phase, the evidence status
    and the window's end, and every base version has to be the same name
    with its own station in it.  Anything else would be choosing one
    station's policy for another, so it is refused rather than guessed.
    The window runs from ``valid_from_utc`` (never backdated before the
    station had a binding) to the fleet's own end, so the next renewal
    carries all stations together.
    """

    if not bindings:
        raise SystemExit("no fleet bindings to take a template from")
    shapes = {
        (
            item["heavy_to_standard_local_time"],
            item["standard_to_tail_local_time"],
            json.dumps(item["cash_by_phase"], sort_keys=True),
            item["evidence_status"],
            bool(item["promoted"]),
            item["valid_until_utc"],
        )
        for item in bindings
    }
    if len(shapes) != 1:
        raise SystemExit(f"fleet cash-phase template is not uniform: {len(shapes)} shapes")
    affixes = set()
    for item in bindings:
        base = _base_version(item["policy_version"])
        marker = f"-{item['station_id']}-"
        if base.count(marker) != 1:
            raise SystemExit(f"policy version does not name its station once: {base}")
        affixes.add(tuple(base.split(marker)))
    if len(affixes) != 1:
        raise SystemExit("fleet policy versions do not share one name pattern")
    prefix, suffix = next(iter(affixes))
    template = bindings[0]
    if template["promoted"]:
        raise SystemExit("promoted template in candidate scope")
    valid_until_utc = template["valid_until_utc"]
    if valid_from_utc >= valid_until_utc:
        raise SystemExit("valid_from must precede the fleet window's end")
    bound = {item["station_id"] for item in bindings}
    window_tag = valid_from_utc[:10].replace("-", "")
    cash_policies: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    for station in sorted(station_timezones):
        if station in bound:
            raise SystemExit(f"station already bound: {station}")
        policy = BasketCashPhasePolicyArtifact.create(
            policy_version=(
                f"{prefix}-{station}-{suffix}{VERSION_WINDOW_SEPARATOR}{window_tag}"
            ),
            station_id=station,
            station_timezone=station_timezones[station],
            heavy_to_standard_local_time=template["heavy_to_standard_local_time"],
            standard_to_tail_local_time=template["standard_to_tail_local_time"],
            cash_by_phase=template["cash_by_phase"],
            valid_from_utc=valid_from_utc,
            valid_until_utc=valid_until_utc,
            evidence_status=template["evidence_status"],
            promoted=False,
        )
        cash_policies.append(policy.to_dict())
        manifests.append(
            CashPhaseAuthorityManifest.create(
                station_id=policy.station_id,
                policy_version=policy.policy_version,
                artifact_sha256=policy.content_sha256,
                station_timezone=policy.station_timezone,
                valid_from_utc=policy.valid_from_utc,
                valid_until_utc=policy.valid_until_utc,
                authority_scope=scope,
            ).to_dict()
        )
    packet: dict[str, Any] = {
        "schema": INGRESS_SCHEMA,
        "policy_authority_scope": scope,
        "execution_mode": "production_no_live",
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "cash_phase_policies": cash_policies,
        "cash_phase_authority_manifests": manifests,
        "onboarding": {
            "window_from_utc": valid_from_utc,
            "window_until_utc": valid_until_utc,
            "station_ids": sorted(station_timezones),
            "template_from_station_count": len(bindings),
        },
    }
    packet["packet_sha256"] = content_sha256(packet)
    return packet


def unbound_fleet_timezones(
    conn: sqlite3.Connection, *, bound_stations: set[str]
) -> dict[str, str]:
    """Each fleet station with no binding, with its market timezone.

    The timezone comes from the market binding table and nowhere else (the
    seed registry's column is wrong for most of the fleet); a station with
    no binding there, or with two, is refused.
    """

    fleet = {
        str(value).upper()
        for value in coordinate_registry_manifest()["station_ids"]
    }
    out: dict[str, str] = {}
    for station in sorted(fleet - bound_stations):
        zones = {
            str(row[0])
            for row in conn.execute(
                "SELECT settlement_timezone"
                " FROM calibration_station_market_timezone_binding"
                " WHERE station_id=?",
                (station,),
            )
        }
        if len(zones) != 1:
            raise SystemExit(f"market timezone not unique for {station}: {sorted(zones)}")
        out[station] = next(iter(zones))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument(
        "--valid-from",
        required=True,
        help="Start of the new window; use the old window's end to leave no gap",
    )
    parser.add_argument(
        "--valid-until",
        help="Required to renew; ignored with --onboard (the fleet's end)",
    )
    parser.add_argument(
        "--onboard",
        action="store_true",
        help="Issue the fleet template to fleet stations with no binding",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    valid_from_utc = _utc(args.valid_from)
    if args.onboard:
        database = resolve_target_database(args.db)
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60.0)
        try:
            bindings = latest_bindings(
                active_bindings(
                    conn, scope=args.scope, observed_at_utc=valid_from_utc
                )
            )
            zones = unbound_fleet_timezones(
                conn, bound_stations={item["station_id"] for item in bindings}
            )
        finally:
            conn.close()
        if not zones:
            print("every fleet station already has a cash-phase binding")
            return 0
        packet = build_onboarding_packet(
            bindings,
            station_timezones=zones,
            valid_from_utc=valid_from_utc,
            scope=args.scope,
        )
        output = (
            args.output if args.output != DEFAULT_OUTPUT
            else DEFAULT_OUTPUT.with_name("cash_phase_authority_onboarding.json")
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        write_bounded_json(output, packet)
        print(
            f"{len(zones)} stations onboarded {valid_from_utc} -> "
            f"{packet['onboarding']['window_until_utc']}: {', '.join(sorted(zones))}\n"
            f"  packet: {output}"
        )
        return 0
    if not args.valid_until:
        raise SystemExit("--valid-until is required to renew")
    valid_until_utc = _utc(args.valid_until)
    if valid_from_utc >= valid_until_utc:
        raise SystemExit("valid_from must precede valid_until")

    database = resolve_target_database(args.db)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60.0)
    try:
        bindings = latest_bindings(
            active_bindings(
                conn,
                scope=args.scope,
                observed_at_utc=valid_from_utc,
            )
        )
    finally:
        conn.close()
    if not bindings:
        raise SystemExit(f"no cash-phase bindings found for scope {args.scope}")

    packet = build_renewal_packet(
        bindings,
        valid_from_utc=valid_from_utc,
        valid_until_utc=valid_until_utc,
        scope=args.scope,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_bounded_json(args.output, packet)
    carried = packet["renewal"]["station_count"]
    print(
        f"{carried} stations renewed {valid_from_utc} -> {valid_until_utc}\n"
        f"  window replaces: {bindings[0]['valid_from_utc']} -> "
        f"{bindings[0]['valid_until_utc']}\n"
        f"  packet: {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
