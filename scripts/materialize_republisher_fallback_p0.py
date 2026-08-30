#!/usr/bin/env python3
"""Materialize only exact historical-OOS Open-Meteo fallback P0 packets.

The command is deliberately narrow: it reuses retained inbox packets, never
fetches the network, never treats forward data as OOS, and writes only the
no-live P0 handoff/SSOT.  Official-native packets are attempted first; a
republisher packet is admitted only when the compiler proves its exact
station×event-day activation.  P0-M/P1 and order paths are not touched.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.native_ensemble_p0 import (  # noqa: E402
    compile_native_ensemble_p0,
    persist_native_ensemble_forward_prediction,
)
from bayes_basket_target.production_worker import (  # noqa: E402
    DEFAULT_TARGET_ACTIVATION,
    DEFAULT_TARGET_MARKET_MANIFEST,
    DEFAULT_TARGET_NATIVE_OOS_SNAPSHOT_MANIFEST,
    DEFAULT_TARGET_P0_INBOX,
    TargetProductionWorker,
    _upsert_native_p0_inbox,
    open_target_writer_if_enabled,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from scripts.audit_republisher_fallback_p0_candidates import (  # noqa: E402
    INBOX_SCHEMA,
    OFFICIAL_SCHEMA,
    REPUBLISHER_SCHEMA,
    _latest_by_station,
    _load_inbox,
    _normalize_republisher,
    _packet_index,
)


DEFAULT_OFFICIAL = ROOT / "data/runtime/bayes_basket_target_nwp/official_native_ensemble_forward_inbox_20260803_refreshed.json"
DEFAULT_REPUBLISHER = ROOT / "data/runtime/bayes_basket_target_nwp/native_ensemble_forward_inbox.json"
DEFAULT_EVENT_DATE = "2026-08-03"
DEFAULT_DECISION = "2026-08-03T00:00:00Z"
DEFAULT_OUTPUT = ROOT / "output/audits/republisher_fallback_p0_materialization_20260803.json"


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("decision_at_utc_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _packet_indexes(
    official_path: Path,
    republisher_path: Path,
    event_date: str,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]], dict[str, str]]:
    official_raw, official_sha = _load_inbox(official_path)
    republisher_raw, republisher_sha = _load_inbox(republisher_path)
    official = _packet_index(
        [
            item for item in official_raw["packets"]
            if isinstance(item, Mapping) and str(item.get("event_date") or "") == event_date
        ],
        schema=OFFICIAL_SCHEMA,
    )
    republisher = _packet_index(
        [
            item for item in republisher_raw["packets"]
            if isinstance(item, Mapping) and str(item.get("event_date") or "") == event_date
        ],
        schema=REPUBLISHER_SCHEMA,
        normalize_republisher=True,
    )
    return (
        {station: packet for station, (_, packet) in _latest_by_station(official).items()},
        {station: packet for station, (_, packet) in _latest_by_station(republisher).items()},
        {"official": official_sha, "republisher": republisher_sha},
    )


def materialize(
    *,
    db: Path,
    official: Path,
    republisher: Path,
    event_date: str,
    decision_at_utc: str,
    p0_inbox: Path,
    output: Path,
) -> dict[str, Any]:
    decision = _utc(decision_at_utc)
    official_by_station, republisher_by_station, inbox_sha = _packet_indexes(
        official.resolve(strict=True), republisher.resolve(strict=True), event_date
    )
    stations = sorted(set(official_by_station) | set(republisher_by_station))
    if len(stations) > expected_station_count():
        raise RuntimeError("fallback_station_count_exceeded")

    read_conn = sqlite3.connect(
        f"file:{db.expanduser().resolve(strict=True)}?mode=ro", uri=True
    )
    read_conn.execute("PRAGMA query_only=ON")
    read_conn.execute("PRAGMA busy_timeout=30000")
    compiled: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    try:
        for station in stations:
            official_packet = official_by_station.get(station)
            republisher_packet = republisher_by_station.get(station)
            selected: dict[str, Any] | None = None
            official_reason = "not_attempted"
            if official_packet is not None:
                try:
                    selected = dict(
                        compile_native_ensemble_p0(
                            read_conn,
                            forward_packet=official_packet,
                            decision_at_utc=decision,
                        )
                    )
                    official_reason = "compiled"
                except Exception as exc:
                    official_reason = f"{type(exc).__name__}:{exc}"
            if selected is None and republisher_packet is not None:
                try:
                    candidate = dict(
                        compile_native_ensemble_p0(
                            read_conn,
                            forward_packet=republisher_packet,
                            decision_at_utc=decision,
                        )
                    )
                    packet = candidate.get("packet")
                    lineage = (
                        packet.get("native_probability_envelope", {}).get("lineage", {})
                        if isinstance(packet, Mapping)
                        else {}
                    )
                    # The current fallback package is admitted here only for
                    # exact historical d0 activation.  d1/d2 are not inferred
                    # from d0 and remain explicitly blocked.
                    if str(lineage.get("lead_bucket") or "") != "d0":
                        raise RuntimeError("republisher_d0_exact_required")
                    if str(lineage.get("transport_lineage_class") or "") != "aggregator_republisher":
                        raise RuntimeError("republisher_lineage_invalid")
                    if str(lineage.get("native_arrival_index_eligible") or "").lower() != "false":
                        raise RuntimeError("republisher_native_arrival_must_be_false")
                    if str(lineage.get("native_formal_p0_eligible") or "").lower() != "false":
                        raise RuntimeError("republisher_native_formal_p0_must_be_false")
                    selected = candidate
                    compiled.append(candidate)
                    fallback_reason = "compiled_d0_exact"
                except Exception as exc:
                    fallback_reason = f"{type(exc).__name__}:{exc}"
            else:
                fallback_reason = "not_attempted"
            rows.append({
                "station_id": station,
                "event_date": event_date,
                "official_packet_present": official_packet is not None,
                "republisher_packet_present": republisher_packet is not None,
                "official_attempt": official_reason,
                "selected_source": (
                    "aggregator_republisher" if selected is not None and selected.get("source_class") == "calibrated_native_cross_ensemble" and republisher_packet is not None and selected.get("packet", {}).get("native_probability_envelope", {}).get("lineage", {}).get("transport_lineage_class") == "aggregator_republisher" else "none"
                ),
                "fallback_attempt": fallback_reason,
                "compiled": selected is not None and selected in compiled,
            })
    finally:
        read_conn.close()

    if not compiled:
        raise RuntimeError("no_exact_d0_fallback_p0")
    # Persist the bounded handoff first; the worker validates every packet
    # again before its atomic SSOT transaction.
    inbox_merge = _upsert_native_p0_inbox(
        p0_inbox.resolve(), compiled_packets=compiled, generated_at_utc=decision
    )

    writer_env = {
        "PM_TARGET_PRODUCTION_WRITER": "1",
        "PM_TARGET_EXECUTION_MODE": "production_no_live",
    }
    import os

    os.environ.update(writer_env)
    writer = open_target_writer_if_enabled(
        db.resolve(), activation_path=DEFAULT_TARGET_ACTIVATION
    )
    if writer is None:
        raise RuntimeError("target_writer_not_enabled")
    worker: TargetProductionWorker | None = None
    prediction_receipts: list[dict[str, Any]] = []
    try:
        for candidate in compiled:
            prediction_receipts.append(
                persist_native_ensemble_forward_prediction(writer, candidate)
            )
        worker = TargetProductionWorker(
            db.resolve(),
            commit_conn=writer,
            activation_path=DEFAULT_TARGET_ACTIVATION,
            market_manifest_path=DEFAULT_TARGET_MARKET_MANIFEST,
            native_oos_snapshot_path=DEFAULT_TARGET_NATIVE_OOS_SNAPSHOT_MANIFEST,
            p0_inbox_path=p0_inbox.resolve(),
            station_allowlist=sorted({str(x["station_id"]) for x in rows if x["compiled"]}),
            event_date_allowlist=[event_date],
        )
        ingress = worker.materialize_resident_p0(as_of_utc=decision)
    finally:
        if worker is not None:
            worker.close()
        else:
            writer.close()

    identity = {
        "schema": "bayes_basket_target_republisher_fallback_p0_materialization_v1",
        "event_date": event_date,
        "decision_at_utc": decision,
        "database": str(db.resolve()),
        "official_inbox": {"path": str(official.resolve()), "content_sha256": inbox_sha["official"]},
        "republisher_inbox": {"path": str(republisher.resolve()), "content_sha256": inbox_sha["republisher"]},
        "compiled_count": len(compiled),
        "compiled_station_ids": sorted({str(x["station_id"]) for x in rows if x["compiled"]}),
        "row_count": len(rows),
        "rows": rows,
        "p0_inbox_merge": inbox_merge,
        "prediction_receipts": prediction_receipts,
        "resident_p0_ingress": ingress,
        "boundary": {
            "network_fetches": 0,
            "writes_p0": bool(ingress.get("writes_target_ssot")),
            "writes_p0m": False,
            "writes_p1": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "future_data_used_as_oos": False,
            "native_arrival_index_written": False,
            "formal_live_authority_written": False,
        },
        "policy": {
            "official_native_first": True,
            "republisher_requires_exact_historical_oos_activation": True,
            "d1_without_historical_oos_remains_blocked": True,
            "republisher_is_not_native_arrival": True,
            "republisher_is_not_p1": True,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--official-inbox", type=Path, default=DEFAULT_OFFICIAL)
    parser.add_argument("--republisher-inbox", type=Path, default=DEFAULT_REPUBLISHER)
    parser.add_argument("--event-date", default=DEFAULT_EVENT_DATE)
    parser.add_argument("--decision-at-utc", default=DEFAULT_DECISION)
    parser.add_argument("--p0-inbox", type=Path, default=DEFAULT_TARGET_P0_INBOX)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = materialize(
        db=args.db,
        official=args.official_inbox,
        republisher=args.republisher_inbox,
        event_date=str(args.event_date),
        decision_at_utc=str(args.decision_at_utc),
        p0_inbox=args.p0_inbox,
        output=args.output,
    )
    write_bounded_json_if_changed(args.output.resolve(), result, limit=MAX_JSON_OUTPUT_BYTES)
    print(json.dumps({
        "schema": result["schema"],
        "compiled_count": result["compiled_count"],
        "compiled_station_ids": result["compiled_station_ids"],
        "p0_writes": result["resident_p0_ingress"].get("writes_target_ssot"),
        "blockers": result["resident_p0_ingress"].get("blockers", []),
        "network_fetches": 0,
        "orders_submitted": 0,
        "output": str(args.output.resolve()),
        "content_sha256": result["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
