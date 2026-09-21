#!/usr/bin/env python3
"""Everything a fleet move needs before the registry changes, done while
production runs on the fleet it has.

The move itself is one commit and one restart: the coordinate registry, the
fleet-labelled capture configs, the station lane map and the pr12 sweep's
PROJECTION pointer, together.  What that commit points at has to exist and
be proven first, because the pr12 sweep validates its sealed projection
against the live fleet every round and a mismatch stops the truth revisions
every P1 atomic write needs (2026-08-31).  This prepares that, in order:

1. ``authority``  append one new version of global-AWC source-authority rows
   for the prospective fleet.  Only the projection builder resolves these;
   the live path reads the hashes embedded in the projection it is pointed
   at, so new rows change nothing until a projection minted from them is
   pinned -- and they stay harmless if the move is abandoned.
2. ``mint``       build the prospective fleet's pr12 projection from those
   rows and the pinned doctrine (the four files at the hashes the builder
   requires, taken from git into a temporary directory -- three of the
   working copies were edited on 2026-09-08 and no longer match), and
   write it to a new file beside the current one.  Nothing points at it.
3. ``prove``      validate it against the prospective fleet, then run the
   pr12 contract builder read-only against it and report what the first
   real round would do: facts built, days quarantined because the old
   projection already closed them (expected: every closed day in the
   window), and whether the new stations produce facts.

The prospective fleet exists only inside this process: coordinates for the
added stations come from one AWC stationinfo call over the whole fleet (the
same call onboarding makes, and it refuses if any registered coordinate has
drifted), and are patched into the in-memory registry before anything that
derives the fleet is imported.  The working tree is not touched.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE = ROOT / "data" / "archive.db"
LANE_MAP = ROOT / "config" / "trading" / "bayes_basket_target_station_lane_map_v1.json"
DOCTRINE_DIR = "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/doctrine"
DOCTRINE_COMMIT = "e9d31583"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patch_prospective_fleet(added: list[str]) -> dict:
    """Extend the in-memory coordinate registry; refuse on any drift."""

    from bayes_basket_target import station_registry as registry

    onboard = _load_script("onboard_station")
    current = set(registry._COORDINATES)
    wanted = sorted(current | set(added))
    coordinates, digest = onboard._stationinfo(wanted)
    missing = [s for s in wanted if s not in coordinates]
    if missing:
        raise SystemExit(f"stationinfo_missing:{missing}")
    drifted = {
        s: (registry._COORDINATES[s], coordinates[s]) for s in current
        if tuple(round(v, 3) for v in registry._COORDINATES[s]) != coordinates[s]
    }
    if drifted:
        raise SystemExit(f"registered_coordinates_drifted:{drifted}")
    for station in added:
        registry._COORDINATES[station] = coordinates[station]
    registry.REGISTRY_SHA256 = hashlib.sha256(registry._canonical(registry._COORDINATES)).hexdigest()
    registry.SOURCE_SNAPSHOT_SHA256 = digest
    return {
        "fleet_size": len(registry._COORDINATES),
        "added": {s: list(coordinates[s]) for s in added},
        "stationinfo_snapshot_sha256": digest,
        "registry_sha256": registry.REGISTRY_SHA256,
    }


def prospective_lane_map(added: list[str]) -> tuple[dict, str]:
    from bayes_basket_target.contracts import content_sha256

    packet = json.loads(LANE_MAP.read_text(encoding="utf-8"))
    stations = dict(packet["stations"])
    for station in added:
        stations[station] = "awc"
    packet["stations"] = {k: stations[k] for k in sorted(stations)}
    # The same rule the retired authority migration used: the whole packet.
    return packet, content_sha256(packet)


def pinned_doctrine(target: Path) -> Path:
    from bayes_basket_target.pr12_authority_scope_projection import REQUIRED_DOCTRINE_SHA256

    target.mkdir(parents=True, exist_ok=True)
    for name, expected in REQUIRED_DOCTRINE_SHA256.items():
        blob = subprocess.run(
            ["git", "show", f"{DOCTRINE_COMMIT}:{DOCTRINE_DIR}/{name}"],
            cwd=ROOT, capture_output=True, check=True,
        ).stdout
        if hashlib.sha256(blob).hexdigest() != expected:
            raise SystemExit(f"pinned_doctrine_hash_mismatch:{name}")
        (target / name).write_bytes(blob)
    return target


def step_authority(fleet: list[str], *, version: str, lane_map_sha: str, commit: bool) -> dict:
    from bayes_basket_target import station_source_authority as ssa

    ssa.MAX_STATIONS = len(fleet)  # computed at import, before the patch
    mode = "rw" if commit else "ro"
    conn = sqlite3.connect(f"file:{SSOT}?mode={mode}", uri=True, timeout=120)
    try:
        if commit:
            conn.execute("PRAGMA busy_timeout=120000")
        existing = conn.execute(
            "SELECT activated_at_utc FROM calibration_station_source_authority "
            "WHERE authority_version=? ORDER BY activated_at_utc LIMIT 1",
            (version,),
        ).fetchone()
        activated = str(existing[0]) if existing else _now()
        report = ssa.migrate_global_awc_source_authorities(
            conn, station_ids=fleet, authority_version=version,
            activated_at_utc=activated, lane_map_content_sha256=lane_map_sha,
            commit=commit,
        )
    finally:
        conn.close()
    return {k: report[k] for k in ("state", "authority_version", "activated_at_utc",
                                   "station_count", "wrote_count", "idempotent_count")}


def step_mint(output: Path, *, doctrine: Path, as_of: str) -> dict:
    builder = _load_script("build_pr12_authority_scope_projection")
    argv = sys.argv
    sys.argv = [
        "build_pr12_authority_scope_projection.py", "--database", str(SSOT),
        "--doctrine-directory", str(doctrine), "--as-of-utc", as_of, "--output", str(output),
    ]
    try:
        builder.main()
    finally:
        sys.argv = argv
    projection = json.loads(output.read_text(encoding="utf-8"))
    return {"output": str(output.relative_to(ROOT)), "station_count": projection["station_count"],
            "content_sha256": projection["content_sha256"]}


def step_prove(projection_path: Path, *, added: list[str], scratch: Path) -> dict:
    from bayes_basket_target.pr12_authority_scope_projection import (
        validate_pr12_authority_scope_projection,
    )

    projection = validate_pr12_authority_scope_projection(
        json.loads(projection_path.read_text(encoding="utf-8"))
    )
    contract_builder = _load_script("build_pr12_awc_observation_contract")
    outputs = {k: scratch / f"pr12_{k}.json" for k in ("evidence", "contract", "receipt")}
    argv = sys.argv
    sys.argv = [
        "build_pr12_awc_observation_contract.py",
        "--archive-database", str(ARCHIVE), "--target-database", str(SSOT),
        "--as-of-utc", _now(), "--authority-projection", str(projection_path),
        "--window-days", "3",
        "--evidence-output", str(outputs["evidence"]),
        "--contract-output", str(outputs["contract"]),
        "--receipt-output", str(outputs["receipt"]),
    ]
    try:
        contract_builder.main()
    finally:
        sys.argv = argv
    contract = json.loads(outputs["contract"].read_text(encoding="utf-8"))
    evidence = json.loads(outputs["evidence"].read_text(encoding="utf-8"))
    revisions = contract.get("revision_facts") or []
    bounds = contract.get("day_bounds_facts") or []
    quarantine = evidence.get("conflicting_day_scope_quarantine") or {}
    by_station = {}
    for fact in revisions:
        by_station[fact["station_id"]] = by_station.get(fact["station_id"], 0) + 1
    return {
        "projection_valid_for_fleet": projection["station_count"],
        "revision_facts": len(revisions),
        "day_bounds_facts": len(bounds),
        "stations_with_revision_facts": len(by_station),
        "added_station_revision_facts": {s: by_station.get(s, 0) for s in added},
        "quarantined_days": quarantine.get("quarantined_day_count"),
        "quarantine_sample": [
            (q.get("station_id"), q.get("event_date"), q.get("fact_type"))
            for q in (quarantine.get("scopes") or [])[:6]
        ],
        "writes_database": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--add", nargs="+", required=True)
    parser.add_argument("--authority-version", default="global_awc_ssot_v2")
    parser.add_argument("--commit-authority", action="store_true",
                        help="write the new authority rows (otherwise validate only)")
    parser.add_argument("--mint", type=Path,
                        help="write the prospective projection here (needs committed rows)")
    parser.add_argument("--prove", type=Path, help="validate and dry-run this projection")
    args = parser.parse_args()
    added = sorted({s.strip().upper() for s in args.add})

    report: dict = {"fleet": patch_prospective_fleet(added)}
    from bayes_basket_target.station_registry import coordinate_registry_manifest

    fleet = list(coordinate_registry_manifest()["station_ids"])
    lane_map, lane_map_sha = prospective_lane_map(added)
    if sorted(lane_map["stations"]) != sorted(fleet):
        raise SystemExit("prospective_lane_map_is_not_the_fleet")
    report["lane_map_content_sha256"] = lane_map_sha
    report["authority"] = step_authority(
        fleet, version=args.authority_version, lane_map_sha=lane_map_sha,
        commit=bool(args.commit_authority),
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        if args.mint:
            doctrine = pinned_doctrine(tmp_path / "doctrine")
            report["mint"] = step_mint(args.mint.resolve(), doctrine=doctrine, as_of=_now())
        if args.prove:
            report["prove"] = step_prove(args.prove.resolve(), added=added, scratch=tmp_path)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
