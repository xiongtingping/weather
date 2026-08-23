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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument(
        "--valid-from",
        required=True,
        help="Start of the new window; use the old window's end to leave no gap",
    )
    parser.add_argument("--valid-until", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    valid_from_utc = _utc(args.valid_from)
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
