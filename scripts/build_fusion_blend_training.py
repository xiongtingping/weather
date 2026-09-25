#!/usr/bin/env python3
"""Materialise the settled station-days the charter-199 fusion blend trains on.

One row per (station, event day, truth revision): the day-ahead P0 that was
knowable before the station's local midnight, its seated systems and their
per-system lead buckets, and the two things either objective averages -- each
system's calibrated-centre error, and its distribution's CRPS terms against
the settled high (``fusion_blend.row_statistics``).

The day-ahead root and the truth are chosen exactly as the fusion-weight study
chose them (docs/research/p0_physical_20260920/study.py ``extract``) without
the TAF pairing that study needed for its own question:

* root: the newest committed P0 whose decision AND creation both precede the
  local midnight, decided within the day before it; every seated system a
  registered native ensemble with a strictly positive weight, two families at
  least, every member receipt archived by the nominal decision;
* truth: accepted, AWC lane, FORMAL_NATIVE_ELIGIBLE, timestamps complete, the
  station's timezone, committed -- every such revision is its own row, known
  at the later of the local day's end and its commit/receipt/publication.

The systems' distributions come off the envelope where P0 recorded them
(since 2026-09-21, 431caed3), otherwise from the retained forward packet and
the exact artifact the compile used, re-derived with the compile's own
function and refused unless every centre matches the lineage to 1e-7 C (the
study's reconstruction, compare.reconstruct).  A station-day without them
still trains the centre objective.

SSOT is opened read-only; the only write is data/fusion_blend_training.db.
Append-only: a row, once written, is never rewritten.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import fusion_blend as FB  # noqa: E402
from bayes_basket_target.materialization import (  # noqa: E402
    rehydrate_lineage_documents,
)
from bayes_basket_target.native_ensemble_p0 import (  # noqa: E402
    _calibrated_system_probability,
    _resolved_bias_c,
)

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
PACKET_ARCHIVE = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "forward_packet_archive"
PACKET_BYTES_MAX = 2 * 1024 ** 2


def stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def decoded(value: object) -> object:
    if value in (None, ""):
        return None
    return json.loads(value) if isinstance(value, str) else value


def ssot_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def training_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    for statement in FB.TRAINING_DDL:
        conn.execute(statement)
    conn.commit()
    return conn


def day_ahead_root(conn: sqlite3.Connection, station: str, day: str, tz: str):
    midnight = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz))
    rows = conn.execute(
        "SELECT * FROM basket_probability_envelope"
        " INDEXED BY basket_probability_envelope_asof_idx"
        " WHERE station_id=? AND event_date=? AND state=? AND decision_at_utc>=?"
        " AND decision_at_utc<? AND stage=? ORDER BY decision_at_utc DESC LIMIT 300",
        (station, day, "COMMITTED", iso(midnight - timedelta(days=1)), iso(midnight), "P0"),
    ).fetchall()
    if len(rows) == 300:
        raise RuntimeError("root_row_limit")
    available = [
        row for row in rows
        if max(stamp(row["decision_at_utc"]), stamp(row["created_at"])) < midnight
    ]
    if not available:
        return None
    return max(
        available,
        key=lambda row: (
            max(stamp(row["decision_at_utc"]), stamp(row["created_at"])),
            row["decision_at_utc"],
        ),
    )


def formal_truths(conn: sqlite3.Connection, station: str, day: str, tz: str, cutoff: datetime):
    end = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz)) + timedelta(days=1)
    if end > cutoff:
        return []
    out = []
    for row in conn.execute(
        "SELECT * FROM calibration_station_daily_high_truth"
        " WHERE station_id=? AND local_date=? ORDER BY revision_no",
        (station, day),
    ):
        if not (
            row["qc_status"] == "ACCEPTED" and row["source_lane"] == "AWC"
            and row["authority_scope"] == "FORMAL_NATIVE_ELIGIBLE"
            and row["timestamps_complete"] == 1 and row["settlement_timezone"] == tz
            and row["canonical_committed_at_utc"]
            and stamp(row["canonical_committed_at_utc"]) <= cutoff
        ):
            continue
        known = max(
            end, *(stamp(row[k]) for k in (
                "canonical_committed_at_utc", "received_at_utc", "published_at_utc"
            ))
        )
        out.append((row, known))
    return out


def packet_probabilities(conn, lineage, station, day, weights, centers, intervals, artifact_cache):
    """The study's reconstruction, refused unless every centre matches."""

    provisional = decoded(lineage.get("provisional_uncalibrated_system_ids_json")) or []
    if provisional:
        return None, "NONE_PROVISIONAL"
    snapshot = str(lineage.get("native_forward_snapshot_sha256") or "")
    path = PACKET_ARCHIVE / day / f"{station}-{snapshot[:16]}.json.gz"
    if not snapshot or not path.exists():
        return None, "NONE_NO_PACKET"
    with gzip.open(path) as handle:
        raw = handle.read(PACKET_BYTES_MAX + 1)
    if len(raw) > PACKET_BYTES_MAX:
        return None, "NONE_PACKET_SIZE"
    packet = json.loads(raw)
    if (
        packet.get("station_id") != station or packet.get("event_date") != day
        or str(packet.get("snapshot_content_sha256", ""))[:16] not in path.name
        or packet.get("bucket_intervals") != intervals
    ):
        return None, "NONE_PACKET_MISMATCH"
    systems = {s["ensemble_system_id"]: s for s in packet.get("systems") or []}
    artifacts = decoded(lineage.get("system_artifact_shas_json")) or {}
    out = {}
    for system in weights:
        sha = artifacts.get(system)
        if not sha or system not in systems:
            return None, "NONE_ARTIFACT_OR_SYSTEM_MISSING"
        if sha not in artifact_cache:
            row = conn.execute(
                "SELECT * FROM calibration_native_ensemble_system_artifact WHERE content_sha256=?",
                (sha,),
            ).fetchone()
            artifact_cache[sha] = dict(row) if row else None
        artifact = artifact_cache[sha]
        if artifact is None or artifact["ensemble_system_id"] != system:
            return None, "NONE_ARTIFACT_MISSING"
        values = [float(member["raw_tmax_c"]) for member in systems[system]["members"]]
        bias = json.loads(artifact["bias_parameters_json"])
        spread = json.loads(artifact["spread_parameters_json"])
        reliability = json.loads(artifact["reliability_parameters_json"])
        mean = sum(values) / len(values)
        if abs(mean + _resolved_bias_c(mean, bias) - float(centers[system])) > 1e-7:
            return None, "NONE_CENTER_MISMATCH"
        out[system] = _calibrated_system_probability(
            values=values, intervals=intervals, bias_parameters=bias,
            spread_parameters=spread, reliability_parameters=reliability,
        )
    return out, "PACKET_RECONSTRUCTION"


def build(args: argparse.Namespace) -> dict[str, int]:
    started = time.monotonic()
    cutoff = datetime.now(timezone.utc)
    conn = ssot_connection(Path(args.ssot))
    out = training_connection(Path(args.db))
    native = {
        row["ensemble_system_id"]: row["independence_family_id"]
        for row in conn.execute(
            "SELECT ensemble_system_id, independence_family_id FROM calibration_native_ensemble_system"
        )
    }
    stations = {
        str(row[0]): str(row[1]) for row in conn.execute(
            "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
        )
    }
    existing = {
        (row[0], row[1], row[2]) for row in out.execute(
            "SELECT station_id, event_date, truth_revision FROM fusion_blend_training_fact"
        )
    }
    first = datetime.fromisoformat(args.since).date()
    last = cutoff.date()
    counts: dict[str, int] = {}

    def count(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    artifact_cache: dict[str, object] = {}
    for station, tz in sorted(stations.items()):
        day = first
        while day <= last:
            day_text = day.isoformat()
            day += timedelta(days=1)
            if time.monotonic() - started > args.budget_seconds:
                counts["stopped_on_budget"] = 1
                break
            truths = formal_truths(conn, station, day_text, tz, cutoff)
            if not truths:
                continue
            pending = [
                (row, known) for row, known in truths
                if (station, day_text, int(row["revision_no"])) not in existing
            ]
            if not pending:
                continue
            root = day_ahead_root(conn, station, day_text, tz)
            if root is None:
                count("no_day_ahead_p0")
                continue
            if root["settlement_timezone"] != tz:
                count("root_timezone_mismatch")
                continue
            lineage = rehydrate_lineage_documents(conn, decoded(root["lineage_json"]))
            weights = decoded(lineage.get("system_weights_json")) or {}
            if (
                len(weights) < 2 or not set(weights) <= set(native)
                or any(float(v) <= 0 for v in weights.values())
                or len({native[s] for s in weights}) < 2
            ):
                count("non_native_or_thin_pool")
                continue
            nominal = stamp(root["decision_at_utc"])
            arrivals = decoded(lineage.get("archive_query_at_utc_by_system_json")) or {}
            if set(weights) - set(arrivals) or any(stamp(arrivals[s]) > nominal for s in weights):
                count("member_receipt_not_asof")
                continue
            centers = decoded(lineage.get("native_calibrated_system_centers_c_json")) or {}
            if set(weights) - set(centers):
                count("system_centers_missing")
                continue
            intervals = decoded(lineage.get("bucket_intervals_c_json")) or {}
            recorded = decoded(lineage.get("native_system_probabilities_json")) or {}
            if recorded and set(weights) <= set(recorded):
                probabilities, source = recorded, "ENVELOPE"
            else:
                try:
                    probabilities, source = packet_probabilities(
                        conn, lineage, station, day_text, weights, centers,
                        intervals, artifact_cache,
                    )
                except (OSError, ValueError, KeyError) as error:
                    probabilities, source = None, "NONE_" + type(error).__name__
            lead = decoded(lineage.get("hour_lead_bucket_by_system_json")) or {}
            # Charter 199: the root's own receipt, when it has one -- the seat
            # weights it was handed and what the blend would serve (SHADOW) or
            # served (APPLIED).  The forward record compares the two here.
            receipt = decoded(lineage.get("fusion_blend_json")) or {}
            seat_weights = receipt.get("seat_weights") or weights
            blend_state = str(receipt.get("state") or "NONE")
            if blend_state == "SHADOW":
                shadow_weights = receipt.get("shadow_weights") or {}
                blend_state = "SHADOW_" + str(receipt.get("shadow_state") or "")
            elif blend_state == "APPLIED":
                shadow_weights = receipt.get("served_weights") or {}
            else:
                shadow_weights = {}
            families = decoded(lineage.get("system_family_ids_json")) or {}
            caps = decoded(lineage.get("family_weight_caps_json")) or {}
            if set(weights) - set(families) or {families[s] for s in weights} - set(caps):
                count("families_or_caps_missing")
                continue
            keys, _points = FB.representative_points(intervals)
            # The ladder-ordered vectors themselves, so a settled row can also
            # be scored as a target (NLL, bucket Brier, Reliability/Resolution).
            ordered_probabilities = {
                system: [float(probabilities[system][key]) for key in keys]
                for system in sorted(weights) if probabilities and system in probabilities
            }
            at = max(nominal, stamp(root["created_at"]))
            for truth, known in pending:
                try:
                    stats = FB.row_statistics(
                        intervals=intervals, truth_c=float(truth["official_tmax_c"]),
                        centers_c=centers, system_probabilities=probabilities,
                        systems=list(weights),
                    )
                    hit = FB.truth_bucket_index(
                        intervals, keys, float(truth["official_tmax_c"]),
                    )
                except (ValueError, KeyError) as error:
                    count("row_" + str(error))
                    continue
                identity = {
                    "schema": FB.FUSION_BLEND_TRAINING_SCHEMA,
                    "station_id": station, "event_date": day_text,
                    "truth_revision": int(truth["revision_no"]),
                    "settlement_timezone": tz,
                    "root_decision_at_utc": iso(at),
                    "root_checkpoint_id": root["checkpoint_id"],
                    "root_envelope_sha256": root["content_sha256"],
                    "root_posterior_version": str(root["posterior_version"]),
                    "truth_c": float(truth["official_tmax_c"]),
                    "truth_known_at_utc": iso(known),
                    "truth_id": str(truth["truth_id"]),
                    "systems_json": json.dumps(sorted(weights)),
                    "lead_by_system_json": json.dumps(lead, sort_keys=True),
                    "center_error_json": json.dumps(stats["center_error"], sort_keys=True),
                    "crps_systems_json": json.dumps(stats["crps_systems"]),
                    "crps_a_json": json.dumps(stats["crps_a"], sort_keys=True),
                    "crps_b_json": json.dumps(stats["crps_b"], sort_keys=True),
                    "probability_source": source,
                    "seat_weights_json": json.dumps({k: float(v) for k, v in seat_weights.items()}, sort_keys=True),
                    "fusion_blend_state": blend_state,
                    "shadow_weights_json": json.dumps({k: float(v) for k, v in shadow_weights.items()}, sort_keys=True),
                    "family_by_system_json": json.dumps({s: families[s] for s in sorted(weights)}, sort_keys=True),
                    "family_caps_json": json.dumps({f: float(caps[f]) for f in sorted({families[s] for s in weights})}, sort_keys=True),
                    "bucket_keys_json": json.dumps(keys),
                    "truth_bucket_index": hit,
                    "probabilities_json": json.dumps(ordered_probabilities, sort_keys=True),
                }
                sha = hashlib.sha256(
                    json.dumps(identity, sort_keys=True).encode()
                ).hexdigest()
                row = dict(identity)
                row.pop("schema")
                row.update(schema=FB.FUSION_BLEND_TRAINING_SCHEMA, created_at_utc=iso(datetime.now(timezone.utc)), content_sha256=sha)
                if not args.dry_run:
                    out.execute(
                        "INSERT OR IGNORE INTO fusion_blend_training_fact ("
                        + ",".join(row) + ") VALUES (" + ",".join("?" * len(row)) + ")",
                        tuple(row.values()),
                    )
                existing.add((station, day_text, int(truth["revision_no"])))
                count("written_" + source.split("_")[0].lower())
        if not args.dry_run:
            out.commit()
        if counts.get("stopped_on_budget"):
            break
    counts["rows_total"] = out.execute("SELECT count(*) FROM fusion_blend_training_fact").fetchone()[0]
    counts["seconds"] = int(time.monotonic() - started)
    out.close()
    conn.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default=(datetime.now(timezone.utc) - timedelta(days=6)).date().isoformat(),
                        help="first event date (default: six days back)")
    parser.add_argument("--db", default=str(FB.FUSION_BLEND_TRAINING_DB))
    parser.add_argument("--ssot", default=str(SSOT))
    parser.add_argument("--budget-seconds", type=float, default=600.0)
    parser.add_argument("--dry-run", action="store_true")
    counts = build(parser.parse_args())
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
