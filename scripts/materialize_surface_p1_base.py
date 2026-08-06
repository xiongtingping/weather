#!/usr/bin/env python3
"""Materialize one retained surface-winner/P1_BASE chain through V17 ingress."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_ingress import ingest_canonical_contract  # noqa: E402
from bayes_basket_target.canonical_ssot_schema import inspect_canonical_ssot_schema  # noqa: E402
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.p1_base_contract import build_p1_base_contract  # noqa: E402
from bayes_basket_target.p1_base_contract import CanonicalP1BaseContract  # noqa: E402
from bayes_basket_target.pure_yes_basket_plan_contract import (  # noqa: E402
    build_pure_yes_basket_plan_contract,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.surface_observation_contract import (  # noqa: E402
    CanonicalSurfaceObservationContract,
    CanonicalSurfaceObservationWinnerContract,
    build_surface_observation_contract,
    build_surface_observation_winner_contract,
    seal_candidate_terminal,
)


CONFIRMATION = "materialize-surface-p1-base-production-no-live"
SETTLEMENT_TIMEZONE = "America/New_York"
AWC_MEASUREMENT_SEMANTICS = "AWC_METAR_SPECI_REMARK_TENTHS_CELSIUS"


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("surface_p1_materializer_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _awc_packet(
    evidence_root: Path,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> dict[str, object]:
    awc_root = evidence_root / "awc"
    observations = load_bounded_json((awc_root / "observations.json").resolve(strict=True))
    fetch_receipt = load_bounded_json((awc_root / "fetch_receipt.json").resolve(strict=True))
    if (
        fetch_receipt.get("content_sha256")
        != content_sha256({
            key: value for key, value in fetch_receipt.items()
            if key != "content_sha256"
        })
        or observations.get("observation_count")
        != len(observations.get("observations") or [])
    ):
        raise ContractError("surface_p1_materializer_awc_artifact_invalid")
    rows: list[dict[str, object]] = []
    for source in observations["observations"]:
        if str(source.get("station_id") or "").upper() != station_id:
            raise ContractError("surface_p1_materializer_awc_station_mismatch")
        valid_at = _utc(str(source["valid_at_utc"]))
        local_date = datetime.fromisoformat(
            valid_at.replace("Z", "+00:00")
        ).astimezone(ZoneInfo(SETTLEMENT_TIMEZONE)).date().isoformat()
        if local_date != event_date:
            raise ContractError("surface_p1_materializer_awc_event_date_mismatch")
        raw_sha = str(source["raw_sha256"]).lower()
        raw_path = awc_root / "raw" / "objects" / raw_sha[:2] / f"{raw_sha}.json.gz"
        if raw_path.is_symlink() or not raw_path.is_file():
            raise ContractError("surface_p1_materializer_awc_raw_missing")
        with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
            raw = json.load(handle)
        raw_text = str(raw.get("raw_text") or "")
        if hashlib.sha256(raw_text.encode("utf-8")).hexdigest() != raw_sha:
            raise ContractError("surface_p1_materializer_awc_raw_hash_mismatch")
        if __import__("re").search(
            r"(?:^|\s)T[01]\d{3}[01]\d{3}(?:\s|$)", raw_text
        ) is None:
            raise ContractError("surface_p1_materializer_awc_tenths_missing")
        reported = Decimal(str(source["reported_temperature_c"]))
        rows.append({
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "source_lane": "AWC",
            "source_class": "official_awc",
            "source_priority": 100,
            "provider_id": "NOAA_AWC",
            "report_type": str(source["report_type"]).upper(),
            "measurement_semantics": AWC_MEASUREMENT_SEMANTICS,
            "observation_id": f"AWC:{raw_sha}",
            "valid_at_utc": valid_at,
            "source_committed_at_utc": _utc(str(source["first_seen_at_utc"])),
            "canonical_committed_at_utc": _utc(
                str(source["canonical_committed_at_utc"])
            ),
            "reported_temperature_c": str(reported),
            "temperature_resolution_c": "0.1",
            "hard_lower_bound_c": str(reported - Decimal("0.05")),
            "hard_lower_bound_method": "ROUND_NEAREST_RESOLUTION_LOWER_EDGE",
            "raw_record_sha256": raw_sha,
            "source_evidence_receipt_sha256": fetch_receipt["content_sha256"],
            "origin_url": str(raw["source_url"]),
            "qc_state": str(source["qc_status"]),
            "correction_suspected": bool(source["correction_suspected"]),
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "state": "MATERIALIZED_UNPUBLISHED",
        })
    return build_surface_observation_contract(
        observation_inputs=rows, as_of_utc=as_of_utc
    )


def _iem_terminal(
    evidence_root: Path,
    *,
    station_id: str,
    event_date: str,
) -> dict[str, object]:
    manifest = load_bounded_json(
        (evidence_root / "asos" / "manifest.json").resolve(strict=True)
    )
    downloads = [
        row for row in manifest.get("downloads") or []
        if station_id in (row.get("station_ids") or [])
        and row.get("event_date") == event_date
    ]
    if len(downloads) != 1:
        raise ContractError("surface_p1_materializer_iem_download_not_unique")
    download = downloads[0]
    raw_path = (ROOT / str(download["raw_path"])).resolve(strict=True)
    if raw_path.is_symlink() or ROOT not in raw_path.parents:
        raise ContractError("surface_p1_materializer_iem_raw_path_invalid")
    payload = raw_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != download["raw_sha256"]:
        raise ContractError("surface_p1_materializer_iem_raw_hash_mismatch")
    rows: list[tuple[str, Decimal]] = []
    for row in csv.DictReader(payload.decode("utf-8", "strict").splitlines()):
        try:
            stamp = datetime.strptime(
                str(row.get("valid(UTC)") or ""), "%Y-%m-%d %H:%M"
            ).replace(tzinfo=timezone.utc)
            temp_f = Decimal(str(row.get("tmpf") or ""))
        except Exception:
            continue
        rows.append((
            stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            (temp_f - Decimal("32")) * Decimal("5") / Decimal("9"),
        ))
    if not rows or len(rows) != int(download["report_count"]):
        raise ContractError("surface_p1_materializer_iem_report_count_mismatch")
    maximum = max(rows, key=lambda item: (item[1], item[0]))
    set_sha = content_sha256({
        "raw_sha256": download["raw_sha256"],
        "report_count": len(rows),
        "valid_at_utc": [item[0] for item in rows],
    })
    return seal_candidate_terminal({
        "source_lane": "IEM",
        "source_class": "aggregator_republisher",
        "source_priority": 220,
        "eligibility_state": "INELIGIBLE",
        "terminal_reason": "COVERAGE_INCOMPLETE",
        "observation_count": len(rows),
        "coverage_complete": False,
        "source_evidence_receipt_sha256": download["source_receipt_sha256"],
        "observation_set_sha256": set_sha,
        "observed_max_c": str(maximum[1]),
        "max_valid_at_utc": maximum[0],
        "max_observation_content_sha256": None,
    })


def _madis_absence_receipt(
    archive_db: Path,
    *,
    station_id: str,
    event_date: str,
    checked_at_utc: str,
) -> dict[str, object]:
    zone = ZoneInfo(SETTLEMENT_TIMEZONE)
    start = datetime.fromisoformat(event_date).replace(tzinfo=zone).astimezone(timezone.utc)
    end = (datetime.fromisoformat(event_date).replace(tzinfo=zone) + timedelta(days=1)).astimezone(timezone.utc)
    start_text, end_text = _utc(start.isoformat()), _utc(end.isoformat())
    with sqlite3.connect(f"file:{archive_db.resolve(strict=True)}?mode=ro", uri=True) as conn:
        object_count = int(conn.execute(
            "SELECT COUNT(*) FROM reported_trajectory_madis_hfmetar_objects "
            "WHERE object_hour_utc>=? AND object_hour_utc<?",
            (start_text, end_text),
        ).fetchone()[0])
        observation_count = int(conn.execute(
            "SELECT COUNT(*) FROM reported_trajectory_madis_hfmetar_observations "
            "WHERE station_id=? AND observed_at_utc>=? AND observed_at_utc<?",
            (station_id, start_text, end_text),
        ).fetchone()[0])
    stat = archive_db.stat()
    identity: dict[str, object] = {
        "schema": "canonical_surface_madis_absence_receipt_v1",
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "source_lane": "MADIS",
        "source_class": "official_republisher",
        "source_priority": 210,
        "query_window_start_utc": start_text,
        "query_window_end_utc": end_text,
        "checked_at_utc": _utc(checked_at_utc),
        "retained_object_count": object_count,
        "retained_observation_count": observation_count,
        "archive_database_identity": {
            "device": int(stat.st_dev),
            "inode": int(stat.st_ino),
            "size_bytes": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        },
        "query_result_content_sha256": content_sha256({
            "station_id": station_id,
            "start": start_text,
            "end": end_text,
            "object_count": object_count,
            "observation_count": observation_count,
        }),
        "state": "DATA_MISSING" if not object_count and not observation_count else "DATA_PRESENT",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _awc_terminal(packet: dict[str, object]) -> dict[str, object]:
    parsed = CanonicalSurfaceObservationContract.from_mapping(packet)
    ordered = sorted(parsed.observation_facts, key=lambda item: (
        item.raw["valid_at_utc"], item.raw["report_type"], item.content_sha256
    ))
    maximum = sorted(ordered, key=lambda item: (
        -Decimal(item.raw["reported_temperature_c"]),
        item.raw["valid_at_utc"], item.content_sha256,
    ))[0]
    return seal_candidate_terminal({
        "source_lane": "AWC",
        "source_class": "official_awc",
        "source_priority": 100,
        "eligibility_state": "ELIGIBLE",
        "terminal_reason": "OFFICIAL_HOUR_COVERAGE_COMPLETE",
        "observation_count": len(ordered),
        "coverage_complete": True,
        "source_evidence_receipt_sha256": maximum.raw[
            "source_evidence_receipt_sha256"
        ],
        "observation_set_sha256": content_sha256([
            item.content_sha256 for item in ordered
        ]),
        "observed_max_c": maximum.raw["reported_temperature_c"],
        "max_valid_at_utc": maximum.raw["valid_at_utc"],
        "max_observation_content_sha256": maximum.content_sha256,
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--archive-database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--p0-snapshot-sha256", required=True)
    parser.add_argument("--observation-as-of-utc", required=True)
    parser.add_argument("--winner-as-of-utc", required=True)
    parser.add_argument("--decision-at-utc", required=True)
    parser.add_argument("--commit-production-no-live-v17", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    if args.commit_production_no_live_v17 and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    if not args.commit_production_no_live_v17 and args.confirm is not None:
        parser.error("--confirm is commit-only")
    station_id = args.station_id.strip().upper()
    evidence_root = args.evidence_root.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    observation_packet = _awc_packet(
        evidence_root,
        station_id=station_id,
        event_date=args.event_date,
        as_of_utc=args.observation_as_of_utc,
    )
    madis_receipt = _madis_absence_receipt(
        args.archive_database.resolve(strict=True),
        station_id=station_id,
        event_date=args.event_date,
        checked_at_utc=args.winner_as_of_utc,
    )
    if madis_receipt["state"] != "DATA_MISSING":
        raise ContractError("surface_p1_materializer_madis_data_present_requires_observations")
    write_bounded_json(evidence_root / "madis_absence_receipt.json", madis_receipt)
    terminals = [
        _iem_terminal(evidence_root, station_id=station_id, event_date=args.event_date),
        seal_candidate_terminal({
            "source_lane": "MADIS",
            "source_class": "official_republisher",
            "source_priority": 210,
            "eligibility_state": "DATA_MISSING",
            "terminal_reason": "NO_RETAINED_OBJECT_AT_ASOF",
            "observation_count": 0,
            "coverage_complete": False,
            "source_evidence_receipt_sha256": madis_receipt["content_sha256"],
            "observation_set_sha256": content_sha256([]),
            "observed_max_c": None,
            "max_valid_at_utc": None,
            "max_observation_content_sha256": None,
        }),
        _awc_terminal(observation_packet),
    ]
    database = args.database.resolve(strict=True)
    conn = sqlite3.connect(database, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    if inspect_canonical_ssot_schema(conn).get("state") != "V17":
        raise ContractError("surface_p1_materializer_v17_required")
    committed = bool(args.commit_production_no_live_v17)
    try:
        conn.execute("BEGIN IMMEDIATE")
        observation_receipt = ingest_canonical_contract(
            conn,
            observation_packet,
            commit=True,
            finalize_transaction=False,
            ingested_at_utc=args.observation_as_of_utc,
            external_evidence_root=evidence_root / "awc",
        )
        winner_packet = build_surface_observation_winner_contract(
            conn,
            station_id=station_id,
            event_date=args.event_date,
            settlement_timezone=SETTLEMENT_TIMEZONE,
            candidate_terminals=terminals,
            as_of_utc=args.winner_as_of_utc,
        )
        winner_receipt = ingest_canonical_contract(
            conn,
            winner_packet,
            commit=True,
            finalize_transaction=False,
            ingested_at_utc=args.winner_as_of_utc,
            external_evidence_root=evidence_root,
        )
        p1_packet = build_p1_base_contract(
            conn,
            p0_forecast_snapshot_content_sha256=args.p0_snapshot_sha256,
            surface_observation_winner_content_sha256=CanonicalSurfaceObservationWinnerContract.from_mapping(
                winner_packet
            ).winner_facts[0].content_sha256,
            decision_at_utc=args.decision_at_utc,
        )
        p1_receipt = ingest_canonical_contract(
            conn,
            p1_packet,
            commit=True,
            finalize_transaction=False,
            ingested_at_utc=args.decision_at_utc,
        )
        p1_fact = CanonicalP1BaseContract.from_mapping(p1_packet).p1_base_facts[0]
        basket_packet = build_pure_yes_basket_plan_contract(
            conn,
            p1_base_content_sha256=p1_fact.content_sha256,
            plan_at_utc=args.decision_at_utc,
        )
        basket_receipt = ingest_canonical_contract(
            conn,
            basket_packet,
            commit=True,
            finalize_transaction=False,
            ingested_at_utc=args.decision_at_utc,
        )
        if committed:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    write_bounded_json(output_dir / "surface_observation_contract.json", observation_packet)
    write_bounded_json(output_dir / "surface_observation_ingress_receipt.json", observation_receipt)
    write_bounded_json(output_dir / "candidate_terminals.json", {
        "candidate_terminals": terminals,
        "content_sha256": content_sha256(terminals),
    })
    write_bounded_json(output_dir / "surface_observation_winner_contract.json", winner_packet)
    write_bounded_json(output_dir / "surface_observation_winner_ingress_receipt.json", winner_receipt)
    write_bounded_json(output_dir / "p1_base_contract.json", p1_packet)
    write_bounded_json(output_dir / "p1_base_ingress_receipt.json", p1_receipt)
    write_bounded_json(output_dir / "pure_yes_basket_plan_contract.json", basket_packet)
    write_bounded_json(
        output_dir / "pure_yes_basket_plan_ingress_receipt.json", basket_receipt
    )
    summary = {
        "state": "COMMITTED" if committed else "PREVIEW_ROLLED_BACK",
        "station_id": station_id,
        "event_date": args.event_date,
        "surface_observation_count": len(observation_packet["observation_facts"]),
        "winner_count": len(winner_packet["winner_facts"]),
        "p1_base_count": len(p1_packet["p1_base_facts"]),
        "pure_yes_basket_plan_count": len(basket_packet["basket_plan_facts"]),
        "selected_source_lane": winner_packet["winner_facts"][0]["selected_source_lane"],
        "reported_high_c": winner_packet["winner_facts"][0]["reported_high_c"],
        "hard_lower_bound_c": winner_packet["winner_facts"][0]["hard_lower_bound_c"],
        "writes_database": committed,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    summary["content_sha256"] = content_sha256(summary)
    write_bounded_json(output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
