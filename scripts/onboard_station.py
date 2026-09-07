#!/usr/bin/env python3
"""New market to live station, as one staged pipeline.

Market discovery runs days ahead of station onboarding and the two were
joined by hand, which is how Jinan and Zhengzhou sat unlisted behind a
missing timezone for two days, and how Istanbul, Moscow and Tel Aviv --
listed with resolved stations and full timezones since 2026-08-22 -- were
invisible to the audit that existed to find them: it looked only for
listings missing a timezone, which they were not.

The pipeline is the onboarding checklist made executable.  Each stage
answers one question, records its answer, and refuses to let a later stage
paper over an earlier one's blocker.  Nothing is automatic past the point
where evidence is required: a station with no observations cannot be
settled however complete its market listing looks, and the last stage is a
re-baseline that changes content-addressed manifests fleet-wide, which is a
reviewed act rather than a scheduled one.

Stages
------

1. ``discover``   markets with a resolved station this fleet does not have.
2. ``assess``     the four onboarding preconditions, per candidate.
3. ``coordinates``fetch AWC stationinfo and propose the registry patch.
4. ``season``     derive the station's own anchor month from climatology.
5. ``rebaseline`` what changing the fleet costs, enumerated, never applied.

Read-only unless ``--commit``, and ``--commit`` reaches only stages 3 and 4
-- the ones that add a station's own metadata.  Stage 5 always reports.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import (  # noqa: E402
    expected_station_count,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)

ARCHIVE_DB = ROOT / "data" / "archive.db"
REGISTRY_SOURCE = ROOT / "bayes_basket_target" / "station_registry.py"
SEASON_MAP_BUILDER = ROOT / "scripts" / "build_station_season_map.py"
AUDIT = ROOT / "scripts" / "audit_new_market_station_admission.py"


def discover() -> list[dict[str, object]]:
    """Listed markets whose station this fleet does not carry.

    Reads the market registry rather than a curated city list: the point
    of discovery is to find what nobody has told us about yet.  A listing
    with no resolved station is reported separately -- it is a different
    problem, and Hong Kong is the standing example: 506 listings, a full
    timezone, and no station at all, because its market does not resolve
    off a NOAA site.  No coordinate can fix that; this system's truth lane
    cannot settle it.
    """

    known = {
        str(value).upper()
        for value in coordinate_registry_manifest()["station_ids"]
    }
    conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=60)
    try:
        rows = conn.execute(
            "SELECT city_slug, station_id, settlement_timezone,"
            "       COUNT(*), MIN(event_date), MAX(event_date)"
            " FROM reported_trajectory_market_registry"
            " GROUP BY city_slug, station_id, settlement_timezone"
            " ORDER BY city_slug"
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, object]] = []
    for city, station, timezone_name, listings, first, last in rows:
        station_id = str(station or "").strip().upper()
        if station_id and station_id in known:
            continue
        out.append({
            "city_slug": str(city or ""),
            "station_id": station_id,
            "settlement_timezone": str(timezone_name or ""),
            "listing_count": int(listings or 0),
            "first_event_date": str(first or ""),
            "last_event_date": str(last or ""),
            "state": (
                "NO_STATION_RESOLVED" if not station_id
                else "CANDIDATE"
            ),
        })
    return out


def assess() -> dict[str, object]:
    """Delegate to the audit that owns the admission rule.

    Not reimplemented here: the four preconditions and what counts as
    sufficient METAR are that script's contract, and a second copy of them
    would drift into a second answer.
    """

    result = subprocess.run(
        [sys.executable, str(AUDIT)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    summary: dict[str, object] = {"raw": lines[-12:]}
    for line in reversed(lines):
        if line.startswith("{"):
            try:
                summary["counts"] = json.loads(line)
            except ValueError:
                pass
            break
    summary["returncode"] = result.returncode
    if result.returncode != 0 and result.stderr:
        summary["stderr"] = result.stderr[-600:]
    return summary


def coordinates(station_id: str) -> dict[str, object]:
    """The registry patch this station needs, as text to review.

    Deliberately printed rather than written.  ``station_registry`` is a
    frozen projection of one AWC snapshot and carries that snapshot's hash;
    a coordinate appended by a script without moving
    ``SOURCE_SNAPSHOT_SHA256`` would claim provenance it does not have.
    """

    import httpx

    station = str(station_id).strip().upper()
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            "https://aviationweather.gov/api/data/stationinfo",
            params={"ids": station, "format": "json"},
        )
        response.raise_for_status()
        payload = response.json()
    entry = payload[0] if isinstance(payload, list) and payload else {}
    latitude, longitude = entry.get("lat"), entry.get("lon")
    if latitude is None or longitude is None:
        return {"station_id": station, "state": "STATIONINFO_INCOMPLETE"}
    return {
        "station_id": station,
        "state": "PATCH_READY",
        "latitude": float(latitude),
        "longitude": float(longitude),
        "site": entry.get("site"),
        "registry_patch": (
            f'    "{station}": ({float(latitude)}, {float(longitude)}),'
        ),
        "note": (
            "append to _COORDINATES in station_registry.py, then re-take "
            "SOURCE_SNAPSHOT_SHA256 from the stationinfo response the "
            "coordinates came from"
        ),
    }


def season(station_id: str) -> dict[str, object]:
    """Whether the station has a season anchor, and how to derive one.

    The anchor is the argmax of a 3-month centred running mean over nine
    years of ERA5 monthly means, and a station whose annual amplitude is
    under 2C carries no anchor at all and lives in ALLYEAR.  Neither can be
    guessed from latitude: KSFO and KLAX peak in September at 37 and 33
    degrees north, and WMKK and WSSS have no phase at all.
    """

    from bayes_basket_target.station_season import _SEASON_ANCHOR_MONTH

    station = str(station_id).strip().upper()
    if station in _SEASON_ANCHOR_MONTH:
        return {
            "station_id": station,
            "state": "ANCHORED",
            "anchor_month": _SEASON_ANCHOR_MONTH[station],
        }
    return {
        "station_id": station,
        "state": "ANCHOR_MISSING",
        "builder": str(SEASON_MAP_BUILDER.relative_to(ROOT)),
        "note": (
            "needs the frozen ERA5 same-day climatology capture for this "
            "station before the builder can derive an anchor; without one "
            "the station has no season vocabulary and P0/P1 lineage cannot "
            "label its artifacts"
        ),
    }


def rebaseline() -> dict[str, object]:
    """What changing the fleet size costs, enumerated rather than applied.

    The count itself is derived (``fleet.expected_station_count``), so no
    guard has to be edited.  What cannot be derived away is that several
    of those guards write the count into content-addressed manifests: their
    bytes change, their hashes change, and every artifact pinned to them
    unbinds.  That is the reviewed step, and naming the files is the most
    this pipeline should do on its own.
    """

    writers = [
        "canonical_activation_release.py",
        "model_system_coverage.py",
        "icon_eps_native_arrival_index.py",
        "station_policy_exact_stage_scorecard.py",
        "canonical_expected_scope_manifest.py",
        "pr12_authority_scope_projection.py",
    ]
    return {
        "current_fleet_size": expected_station_count(),
        "manifest_writers": writers,
        "cost": (
            "each writer stamps the fleet size into a content-addressed "
            "manifest; changing the fleet rewrites those hashes and unbinds "
            "artifacts pinned to them"
        ),
        # Measured, not estimated: the fleet was taken to 48 on 2026-08-28
        # and the suite run at both sizes.  Production imported and the
        # derived guards all followed, but 305 tests turned red -- 343 lines
        # across 147 files carry the literal, and most are fixtures that
        # BUILD a 45-station manifest rather than assertions that compare to
        # a constant, so they need three more real stations each, not a
        # changed number.  The change was rolled back on that evidence.
        "measured_test_debt": {
            "as_of": "2026-08-28",
            "fleet_tried": 48,
            "new_test_failures": 305,
            "literal_lines_in_tests": 343,
            "test_files": 147,
            "note": (
                "most are fixtures constructing a 45-station manifest, so "
                "they take three station entries each rather than an edited "
                "constant"
            ),
        },
        "evidence_still_required_per_station": [
            "arrival corpus across the model families",
            "settled truth history for OOS folds",
            "season anchor from its own climatology",
            "TAF/METAR capture on the resident's rotation",
        ],
        # What already works, so a later attempt does not redo it: the two
        # SSOT rows are written and idempotent (commit_station_onboarding_
        # rows.py), the ERA5 climatology for LTFM/UUWW/LLBG is captured, and
        # the anchor months derive cleanly -- 7, 7 and 8, amplitudes 18.7,
        # 27.5 and 14.9C, all far above the 2.0C ALLYEAR floor.
        "already_done_for_ltfm_uuww_llbg": [
            "calibration_station_registry row",
            "calibration_station_market_timezone_binding row",
            "nine years of ERA5 same-day climatology",
        ],
    }



# ---------------------------------------------------------------------------
# auto: the same stages, driven, so a new market does not wait for a person
# ---------------------------------------------------------------------------

#: Blockers ``auto`` is allowed to clear by itself.  Everything else is
#: evidence the station has to earn, and a sweep that "cleared" it would be
#: writing a claim rather than a fact.
_SELF_CLEARABLE = frozenset({"coordinate_registry_missing"})


def _assess_blockers() -> dict[str, list[str]]:
    """Per-station blockers, from the audit that owns the admission rule."""

    result = subprocess.run(
        [sys.executable, str(AUDIT)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    out: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] not in ("BLOCKED", "ADMISSIBLE"):
            continue
        station = parts[1].upper()
        reasons = parts[-1]
        out[station] = (
            [] if parts[0] == "ADMISSIBLE" or reasons == "-"
            else [r for r in reasons.split(",") if r]
        )
    return out


def _stationinfo(station_ids) -> tuple[dict[str, tuple[float, float]], str]:
    """One AWC call for the whole fleet, and that response's own digest.

    The registry carries a single snapshot hash for every coordinate in it,
    so adding a station means re-taking the snapshot -- and the honest way
    to re-take it is to ask for the whole set at once.  The digest returned
    is of the bytes actually received, and the caller checks the stations
    already in the registry still read the same, which turns a provenance
    chore into a check on what is already there.
    """

    import hashlib

    import httpx

    wanted = sorted({str(value).strip().upper() for value in station_ids})
    with httpx.Client(timeout=60.0) as client:
        response = client.get(
            "https://aviationweather.gov/api/data/stationinfo",
            params={"ids": ",".join(wanted), "format": "json"},
        )
        response.raise_for_status()
        body = response.content
    digest = hashlib.sha256(body).hexdigest()
    coordinates: dict[str, tuple[float, float]] = {}
    for entry in json.loads(body.decode("utf-8")):
        station = str(entry.get("icaoId") or entry.get("id") or "").strip().upper()
        latitude, longitude = entry.get("lat"), entry.get("lon")
        if not station or latitude is None or longitude is None:
            continue
        coordinates[station] = (round(float(latitude), 3), round(float(longitude), 3))
    return coordinates, digest


def _write_registry(new_rows, snapshot_sha256: str) -> list[str]:
    """Append coordinates to the registry and move its snapshot hash.

    This is the step that changes the fleet, because the registry IS the
    fleet -- ``fleet.expected_station_count`` reads its length.  Guards
    that compare against the count follow automatically; the manifests that
    WRITE the count into content-addressed bytes do not, and every artifact
    pinned to one of them unbinds here.  Nothing on the serving path reads
    those manifests (checked 2026-09-07 by walking the import graph from
    the daemon, the production worker, the materializer and the P0
    compiler: none of the six writers is reachable), so what this costs is
    offline rebuilds and the fixtures that construct a fleet-sized manifest.
    """

    source = REGISTRY_SOURCE.read_text()
    marker = "}\n\n\ndef _canonical"
    if marker not in source:
        raise SystemExit("registry layout changed; refusing to patch blindly")
    lines = "".join(
        f'    "{station}": ({latitude}, {longitude}),\n'
        for station, (latitude, longitude) in sorted(new_rows.items())
    )
    source = source.replace(marker, lines + marker, 1)
    old_sha = [
        line for line in source.splitlines()
        if line.startswith("SOURCE_SNAPSHOT_SHA256 = ")
    ]
    if len(old_sha) != 1:
        raise SystemExit("registry snapshot line not found; refusing to patch")
    source = source.replace(
        old_sha[0], f'SOURCE_SNAPSHOT_SHA256 = "{snapshot_sha256}"'
    )
    REGISTRY_SOURCE.write_text(source)
    return sorted(new_rows)


def _run(script: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    tail = (result.stdout or result.stderr or "").strip().splitlines()
    return result.returncode, " | ".join(tail[-3:])


def auto(*, commit: bool) -> dict[str, object]:
    """Discover, admit what is ready, and say what the rest is waiting for.

    The operator ruling this exists for (2026-09-07): "新市场自动发现、自动
    接入，而不是每次人工".  Every stage below is idempotent, so the sweep
    that calls this can run on a clock and do nothing on the days there is
    nothing to do.

    What it will not do is clear a blocker it cannot honestly clear.  A
    station with no observations is not onboarded by writing a row that
    says it has some; it is left in the report with its reason, and picked
    up by whichever pass runs after the observations arrive.
    """

    known = {
        str(value).upper()
        for value in coordinate_registry_manifest()["station_ids"]
    }
    candidates = [
        item for item in discover()
        if item["state"] == "CANDIDATE" and item["station_id"]
    ]
    blockers = _assess_blockers()
    ready, waiting = [], []
    for item in candidates:
        station = str(item["station_id"]).upper()
        reasons = [
            reason for reason in blockers.get(station, ["unassessed"])
            if reason not in _SELF_CLEARABLE
        ]
        if reasons:
            waiting.append({"station_id": station, "blockers": reasons,
                            "city_slug": item["city_slug"]})
        else:
            ready.append(station)
    report: dict[str, object] = {
        "fleet_before": len(known),
        "ready": sorted(ready),
        "waiting": waiting,
        "committed": False,
    }
    if not ready:
        report["note"] = "nothing admissible this pass"
        return report
    coordinates_by_station, snapshot = _stationinfo(sorted(known | set(ready)))
    missing = [s for s in ready if s not in coordinates_by_station]
    if missing:
        report["stationinfo_incomplete"] = missing
        ready = [s for s in ready if s not in missing]
        report["ready"] = sorted(ready)
        if not ready:
            return report
    # The re-take doubles as a check on what is already registered.
    from bayes_basket_target.station_registry import _COORDINATES

    drifted = {
        station: {"registry": list(current),
                  "stationinfo": list(coordinates_by_station[station])}
        for station, current in _COORDINATES.items()
        if station in coordinates_by_station
        and coordinates_by_station[station] != tuple(round(v, 3) for v in current)
    }
    if drifted:
        # Not this pipeline's call to make: a moved coordinate is either a
        # source correction or a source error, and both want a person.
        report["registry_drift"] = drifted
        report["note"] = "existing coordinates disagree with stationinfo; not patching"
        return report
    if not commit:
        report["note"] = "dry run; pass --commit to write"
        report["would_add"] = {s: list(coordinates_by_station[s]) for s in ready}
        report["snapshot_sha256"] = snapshot
        return report
    added = _write_registry(
        {s: coordinates_by_station[s] for s in ready}, snapshot
    )
    report["committed"] = True
    report["added"] = added
    report["fleet_after"] = len(known) + len(added)
    report["snapshot_sha256"] = snapshot
    stages: dict[str, object] = {}
    for station in added:
        code_rows, note_rows = _run(
            ROOT / "scripts" / "commit_station_onboarding_rows.py",
            "--station", station, "--commit",
        )
        code_clim, note_clim = _run(
            ROOT / "scripts" / "capture_station_same_day_climatology.py",
            "--station", station,
        )
        stages[station] = {
            "ssot_rows": {"rc": code_rows, "note": note_rows},
            "climatology": {"rc": code_clim, "note": note_clim},
            "season_anchor": season(station)["state"],
        }
    report["stages"] = stages
    return report

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        choices=("discover", "assess", "coordinates", "season", "rebaseline",
                 "auto", "all"),
        default="all",
    )
    parser.add_argument("--station", default="")
    parser.add_argument(
        "--commit", action="store_true",
        help="auto: actually write; without it the pass reports only",
    )
    args = parser.parse_args()
    report: dict[str, object] = {"schema": "station_onboarding_report_v1"}
    if args.stage == "auto":
        report["auto"] = auto(commit=bool(args.commit))
        print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    if args.stage in ("discover", "all"):
        report["discover"] = discover()
    if args.stage in ("assess", "all"):
        report["assess"] = assess()
    if args.stage in ("coordinates", "season") and not args.station:
        parser.error(f"--stage {args.stage} needs --station")
    if args.stage == "coordinates":
        report["coordinates"] = coordinates(args.station)
    if args.stage == "season":
        report["season"] = season(args.station)
    if args.stage == "all" and args.station:
        report["coordinates"] = coordinates(args.station)
        report["season"] = season(args.station)
    if args.stage in ("rebaseline", "all"):
        report["rebaseline"] = rebaseline()
    print(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
