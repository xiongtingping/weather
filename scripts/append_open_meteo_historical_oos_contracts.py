#!/usr/bin/env python3
"""Bind retained Open-Meteo archive rows to closed AWC historical truth.

This is an append-only research/no-live operation.  It never changes an
existing member row and never marks a native arrival or P1 authority.  A
successor row is appended with an explicit historical contract pointer so the
OOS loader can distinguish a closed-day retrospective republisher sample from
an unbound/forward observation.

No network access is performed: only already-retained raw object hashes and
the existing AWC truth ledger are used.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo
from bayes_basket_target.fleet import expected_station_count

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / (
    "open_meteo_historical_oos_contract_append_20260802.json"
)
MAX_STATIONS = expected_station_count()
CONTRACT_VERSION = "open_meteo_historical_oos_v1"
RECEIPT_CLASS = "HISTORICAL_ASOF_REPUBLISHER_RECEIPT"
TEMPORAL_SCOPE = "CLOSED_HISTORICAL_AWC_TRUTH"
SUCCESSOR_EXTRACTION = (
    "open_meteo_archived_daily_tmax_member_v1__historical_oos_contract_v1"
)


def _sha256_identity(value: Mapping[str, Any]) -> str:
    import hashlib

    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_missing")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _day_end(day: str, timezone_name: str) -> str:
    local = datetime.fromisoformat(day).replace(
        tzinfo=ZoneInfo(timezone_name)
    ) + timedelta(days=1)
    return _utc_text(local)


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _truth_rows(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    query = """
        SELECT truth_id, station_id, local_date, official_tmax_c,
               revision_no, canonical_committed_at_utc
        FROM calibration_station_daily_high_truth
        WHERE source_lane='AWC' AND qc_status='ACCEPTED'
          AND authority_scope IN ('FORMAL_NATIVE_ELIGIBLE',
                                  'NO_LIVE_REPLAY_CANDIDATE')
          AND canonical_committed_at_utc IS NOT NULL
          AND julianday(canonical_committed_at_utc)<=julianday(?)
        ORDER BY station_id, local_date, revision_no
    """
    for raw in conn.execute(query, (as_of_utc,)):
        key = (str(raw[1]).upper(), str(raw[2]))
        candidate = {
            "truth_id": str(raw[0]),
            "station_id": key[0],
            "local_date": key[1],
            "official_tmax_c": float(raw[3]),
            "revision_no": int(raw[4]),
            "canonical_committed_at_utc": str(raw[5]),
        }
        previous = rows.get(key)
        if previous is None or candidate["revision_no"] >= previous["revision_no"]:
            rows[key] = candidate
    return rows


def _station_timezones(conn: sqlite3.Connection) -> dict[str, str]:
    result: dict[str, str] = {}
    rows = conn.execute(
        "SELECT station_id, settlement_timezone "
        "FROM basket_target_market_registry "
        "WHERE station_id IS NOT NULL AND station_id<>'' "
        "AND settlement_timezone IS NOT NULL AND settlement_timezone<>'' "
        "GROUP BY station_id, settlement_timezone"
    ).fetchall()
    grouped: dict[str, set[str]] = defaultdict(set)
    for station, timezone_name in rows:
        grouped[str(station).upper()].add(str(timezone_name))
    for station, values in grouped.items():
        if len(values) == 1:
            result[station] = next(iter(values))
    return result


def _build_candidates(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    station_filter: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    products = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT ensemble_system_id, expected_member_count "
            "FROM calibration_republisher_ensemble_product"
        )
    }
    timezones = _station_timezones(conn)
    truth = _truth_rows(conn, as_of_utc=as_of_utc)
    columns = _table_columns(
        conn, "calibration_republisher_ensemble_historical_member_run"
    )
    required = {
        "historical_member_run_id", "ensemble_system_id", "station_id",
        "target_local_date", "archive_lead_bucket", "run_cycle",
        "member_id", "member_role", "raw_tmax_c", "settlement_definition",
        "extraction_version", "provider_id", "transport_provider_id",
        "native_run_init_utc", "offline_oos_eligible", "archive_query_at_utc",
        "source_url", "raw_object_ref", "source_object_sha256", "qc_status",
        "authority_scope", "holdout_eligible", "content_sha256",
        "historical_oos_contract_id", "historical_oos_eligible",
    }
    missing = sorted(required - set(columns))
    if missing:
        raise RuntimeError("open_meteo_historical_schema_missing:" + ",".join(missing))
    selected = sorted(required)
    groups: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    query = (
        "SELECT " + ",".join(selected) +
        " FROM calibration_republisher_ensemble_historical_member_run"
        " WHERE qc_status='ACCEPTED' AND authority_scope='CALIBRATION_TRAIN_ONLY'"
        " AND holdout_eligible=0 AND transport_provider_id='OPEN_METEO'"
    )
    for raw in conn.execute(query):
        row = dict(zip(selected, raw))
        station = str(row["station_id"]).upper()
        if station_filter and station not in station_filter:
            continue
        key = (
            str(row["ensemble_system_id"]), station,
            str(row["target_local_date"]),
            str(row["archive_lead_bucket"]),
            str(row["run_cycle"]),
            str(row["archive_query_at_utc"]),
        )
        groups[key].append(row)

    # The append-only contract migration deliberately keeps the original
    # train-only member row and appends a successor row carrying the
    # historical contract.  A raw group therefore commonly contains two
    # rows per member.  Build a scope index of already-bound contracts so a
    # rerun can reuse the exact immutable contract (and, importantly, does
    # not append another successor merely because the current wall clock
    # changed).
    existing_contracts: dict[
        tuple[str, str, str, str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    contract_table = conn.execute(
        "SELECT historical_oos_contract_id,contract_version,"
        "ensemble_system_id,station_id,target_local_date,"
        "archive_lead_bucket,run_cycle,station_day_closed,"
        "station_day_end_utc,station_day_closed_at_utc,awc_truth_id,"
        "awc_truth_revision_no,awc_truth_asof_utc,awc_truth_source_lane,"
        "receipt_class,temporal_scope,future_event_count,"
        "historical_oos_eligible,republisher_receipt_sha256,"
        "content_sha256,created_at_utc "
        "FROM calibration_republisher_historical_oos_contract "
        "WHERE historical_oos_eligible=1"
    )
    contract_columns = [
        "historical_oos_contract_id", "contract_version",
        "ensemble_system_id", "station_id", "target_local_date",
        "archive_lead_bucket", "run_cycle", "station_day_closed",
        "station_day_end_utc", "station_day_closed_at_utc", "awc_truth_id",
        "awc_truth_revision_no", "awc_truth_asof_utc",
        "awc_truth_source_lane", "receipt_class", "temporal_scope",
        "future_event_count", "historical_oos_eligible",
        "republisher_receipt_sha256", "content_sha256", "created_at_utc",
    ]
    for raw in contract_table:
        contract = dict(zip(contract_columns, raw))
        contract_key = (
            str(contract["ensemble_system_id"]),
            str(contract["station_id"]).upper(),
            str(contract["target_local_date"]),
            str(contract["archive_lead_bucket"]),
            str(contract["run_cycle"]),
            str(contract.get("republisher_receipt_sha256") or ""),
        )
        existing_contracts[contract_key].append(contract)

    contracts: list[dict[str, Any]] = []
    successor_rows: list[dict[str, Any]] = []
    skipped: dict[str, int] = defaultdict(int)
    as_of = _parse_utc(as_of_utc)
    for key, rows in sorted(groups.items()):
        system_id, station, day, lead, run_cycle, snapshot = key
        if system_id not in products:
            skipped["unknown_product"] += 1
            continue
        if lead != "d0" or run_cycle != "AGGREGATOR_UNKNOWN_NATIVE_CYCLE":
            skipped["unsupported_scope"] += 1
            continue
        expected = products[system_id]
        # Prefer an already-bound successor for each member.  The selected
        # row set is the immutable evidence set used for completeness and
        # source-object hashing; the unbound predecessor remains available
        # only to decide whether a successor row still needs to be appended.
        rows_by_member: dict[str, dict[str, Any]] = {}
        for row in rows:
            member_id = str(row["member_id"])
            current = rows_by_member.get(member_id)
            row_bound = bool(
                int(row.get("historical_oos_eligible") or 0) == 1
                and str(row.get("historical_oos_contract_id") or "").strip()
            )
            if current is None:
                rows_by_member[member_id] = row
                continue
            current_bound = bool(
                int(current.get("historical_oos_eligible") or 0) == 1
                and str(current.get("historical_oos_contract_id") or "").strip()
            )
            if row_bound and not current_bound:
                rows_by_member[member_id] = row
        selected_rows = list(rows_by_member.values())
        if len(selected_rows) != expected or len(rows_by_member) != expected:
            skipped["incomplete_member_set"] += 1
            continue
        if (station, day) not in truth:
            skipped["awc_truth_missing"] += 1
            continue
        timezone_name = timezones.get(station)
        if not timezone_name:
            skipped["station_timezone_missing"] += 1
            continue
        try:
            day_end = _parse_utc(_day_end(day, timezone_name))
            truth_committed = _parse_utc(
                truth[(station, day)]["canonical_committed_at_utc"]
            )
            snapshot_at = _parse_utc(snapshot)
        except (ValueError, TypeError):
            skipped["timestamp_invalid"] += 1
            continue
        if day_end > as_of or truth_committed > as_of:
            skipped["future_or_unclosed"] += 1
            continue
        hashes = {str(row["source_object_sha256"]) for row in selected_rows}
        if len(hashes) != 1 or any(len(item) != 64 for item in hashes):
            skipped["raw_hash_mismatch"] += 1
            continue
        # A contract may be created after the archive query; the archive is
        # explicitly retrospective.  Its historical status comes from this
        # contract's closed AWC truth binding, not from a fabricated native
        # arrival timestamp.
        receipt_sha = next(iter(hashes))
        contract_key = (
            system_id, station, day, lead, run_cycle, receipt_sha
        )
        identity = {
            "contract_version": CONTRACT_VERSION,
            "ensemble_system_id": system_id,
            "station_id": station,
            "target_local_date": day,
            "archive_lead_bucket": lead,
            "run_cycle": run_cycle,
            "station_day_end_utc": _utc_text(day_end),
            # Preserve the source receipt precision.  Truncating a truth
            # receipt such as ``06:59:01.415Z`` to whole seconds would make
            # the append-only SSOT trigger (correctly) reject the contract as
            # claiming an as-of time before the canonical commit.
            "station_day_closed_at_utc": str(
                truth[(station, day)]["canonical_committed_at_utc"]
            ),
            "awc_truth_id": truth[(station, day)]["truth_id"],
            "awc_truth_revision_no": truth[(station, day)]["revision_no"],
            "awc_truth_asof_utc": str(
                truth[(station, day)]["canonical_committed_at_utc"]
            ),
            "awc_truth_source_lane": "AWC",
            "receipt_class": RECEIPT_CLASS,
            "temporal_scope": TEMPORAL_SCOPE,
            "future_event_count": 0,
            "historical_oos_eligible": 1,
            "republisher_receipt_sha256": receipt_sha,
            "created_at_utc": _utc_text(as_of),
        }
        # Reuse a prior contract when it binds the same truth revision and
        # source receipt.  Otherwise derive one immutable successor identity
        # from the current closed-day truth.  This keeps reruns idempotent
        # while still allowing a later AWC correction to create a new
        # contract lineage.
        reusable_contract = next(
            (
                item for item in existing_contracts.get(contract_key, [])
                if str(item.get("awc_truth_id") or "")
                == str(identity["awc_truth_id"])
                # Revision zero is a valid first canonical truth revision;
                # do not let ``0 or -1`` turn it into a false mismatch.
                and item.get("awc_truth_revision_no") is not None
                and int(item["awc_truth_revision_no"])
                == int(identity["awc_truth_revision_no"])
            ),
            None,
        )
        if reusable_contract is not None:
            contract_id = str(reusable_contract["historical_oos_contract_id"])
        else:
            contract_id = "om-hist-oos-" + _sha256_identity(identity)[:40]
            contract = {
                "historical_oos_contract_id": contract_id,
                **identity,
                "content_sha256": _sha256_identity({
                    "historical_oos_contract_id": contract_id,
                    **identity,
                }),
            }
            contracts.append(contract)
        # Append exactly one successor per member when the selected evidence
        # is not already bound to this contract.  This covers both the first
        # promotion (unbound predecessor -> new contract) and a later AWC
        # correction (old-contract successor -> new truth-revision contract).
        # Iterating ``selected_rows`` rather than the raw group is important:
        # a group can contain an unbound predecessor and an older bound
        # successor for the same member, and appending both would create a
        # duplicate member lineage.
        for row in selected_rows:
            if (
                int(row.get("historical_oos_eligible") or 0) == 1
                and str(row.get("historical_oos_contract_id") or "").strip()
                == contract_id
            ):
                continue
            successor_identity = {
                key_name: row[key_name]
                for key_name in selected
                if key_name not in {
                    "historical_member_run_id", "extraction_version",
                    "content_sha256", "historical_oos_contract_id",
                    "historical_oos_eligible",
                }
            }
            successor_identity["historical_member_run_id"] = (
                "om-hist-member-oos-" + _sha256_identity({
                    "old": row["historical_member_run_id"],
                    "contract": contract_id,
                })[:36]
            )
            successor_identity["extraction_version"] = SUCCESSOR_EXTRACTION
            successor_identity["historical_oos_contract_id"] = contract_id
            successor_identity["historical_oos_eligible"] = 1
            successor_identity["content_sha256"] = _sha256_identity(
                {k: v for k, v in successor_identity.items()
                 if k != "content_sha256"}
            )
            successor_rows.append(successor_identity)
    summary = {
        "group_count": len(groups),
        "contract_count": len(contracts),
        "successor_member_row_count": len(successor_rows),
        "stations": sorted({item["station_id"] for item in contracts}),
        "systems": sorted({item["ensemble_system_id"] for item in contracts}),
        "skipped": dict(sorted(skipped.items())),
    }
    return contracts, successor_rows, summary


def _commit(
    conn: sqlite3.Connection,
    *,
    contracts: list[dict[str, Any]],
    successor_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    contract_columns = [
        "historical_oos_contract_id", "contract_version",
        "ensemble_system_id", "station_id", "target_local_date",
        "archive_lead_bucket", "run_cycle", "station_day_closed",
        "station_day_end_utc", "station_day_closed_at_utc", "awc_truth_id",
        "awc_truth_revision_no", "awc_truth_asof_utc", "awc_truth_source_lane",
        "receipt_class", "temporal_scope", "future_event_count",
        "historical_oos_eligible", "republisher_receipt_sha256",
        "content_sha256", "created_at_utc",
    ]
    inserted_contracts = 0
    for raw in contracts:
        values = {
            **raw,
            "station_day_closed": 1,
        }
        placeholders = ",".join("?" for _ in contract_columns)
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO calibration_republisher_historical_oos_contract ("
            + ",".join(contract_columns) + ") VALUES (" + placeholders + ")",
            tuple(values[item] for item in contract_columns),
        )
        inserted_contracts += conn.total_changes - before
    row_columns = [
        str(row[1]) for row in conn.execute(
            "PRAGMA table_info(calibration_republisher_ensemble_historical_member_run)"
        )
    ]
    inserted_rows = 0
    for raw in successor_rows:
        values = {name: raw.get(name) for name in row_columns}
        placeholders = ",".join("?" for _ in row_columns)
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO calibration_republisher_ensemble_historical_member_run ("
            + ",".join(row_columns) + ") VALUES (" + placeholders + ")",
            tuple(values[name] for name in row_columns),
        )
        inserted_rows += conn.total_changes - before
    return inserted_contracts, inserted_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--station-id", action="append", default=[])
    parser.add_argument("--as-of-utc", default=None)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    db = args.db.expanduser().resolve(strict=True)
    if db.is_symlink():
        raise RuntimeError("open_meteo_contract_db_symlink_forbidden")
    as_of = args.as_of_utc or _utc_text(datetime.now(timezone.utc))
    _parse_utc(as_of)
    station_filter = {
        str(value).strip().upper() for value in args.station_id
        if str(value).strip()
    }
    if len(station_filter) > MAX_STATIONS:
        raise RuntimeError("open_meteo_contract_station_scope_invalid")
    mode = "rwc" if args.commit else "ro"
    uri = f"file:{db}?mode={mode}"
    with sqlite3.connect(uri, uri=True, timeout=60.0) as conn:
        if not args.commit:
            conn.execute("PRAGMA query_only=ON")
        contracts, successors, summary = _build_candidates(
            conn, as_of_utc=as_of, station_filter=station_filter
        )
        inserted_contracts = inserted_rows = 0
        if args.commit:
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                inserted_contracts, inserted_rows = _commit(
                    conn, contracts=contracts, successor_rows=successors
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    result = {
        "schema": "open_meteo_historical_oos_contract_append_v1",
        "as_of_utc": as_of,
        "database": str(db),
        "commit": bool(args.commit),
        "production_no_live": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "native_arrival_index_written": False,
        "p1_authority_written": False,
        "summary": summary,
        "inserted_contracts": inserted_contracts,
        "inserted_successor_member_rows": inserted_rows,
    }
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
