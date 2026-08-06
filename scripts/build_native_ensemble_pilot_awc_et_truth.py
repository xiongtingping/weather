#!/usr/bin/env python3
"""Build an exact ET AWC truth revision contract for the native OOS pilot.

The source contract is the already decoded AWC historical replay.  This
builder never overwrites the earlier station-local-day revision.  If an exact
ET fact is already present it is replayed; otherwise the next explicit truth
revision is emitted together with a predecessor ledger.
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

from bayes_basket_target.canonical_contract import (  # noqa: E402
    CanonicalContract,
    CanonicalTruthFact,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)


SETTLEMENT_TIMEZONE = "America/New_York"
BUILDER_ID = "native_ensemble.pilot.awc_et_truth.v1"


def _seal(identity: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(identity)
    return {**value, "content_sha256": content_sha256(value)}


def _load_existing(
    conn: sqlite3.Connection, station_id: str, event_date: str
) -> list[CanonicalTruthFact]:
    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth' AND scope_key LIKE ? ORDER BY scope_key",
        (f"{station_id}|{event_date}|%",),
    ).fetchall()
    result: list[CanonicalTruthFact] = []
    for (raw_json,) in rows:
        raw = json.loads(str(raw_json))
        if not isinstance(raw, Mapping):
            raise ContractError("native_pilot_awc_truth_existing_mapping_required")
        result.append(CanonicalTruthFact.from_mapping(raw))
    return result


def build_contract(
    conn: sqlite3.Connection,
    *,
    source: Mapping[str, Any],
    station_ids: set[str],
    start_date: str,
    end_date: str,
    committed_at_utc: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        committed_at = datetime.fromisoformat(
            committed_at_utc.replace("Z", "+00:00")
        )
        if committed_at.tzinfo is None or committed_at.utcoffset() is None:
            raise ValueError("timezone required")
        committed_at_utc = committed_at.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace(
            "+00:00", "Z"
        )
    except ValueError as exc:
        raise ContractError("native_pilot_awc_truth_commit_time_invalid") from exc
    parsed = CanonicalContract.from_mapping(source).to_mapping()
    selected = [
        CanonicalTruthFact.from_mapping(raw)
        for raw in parsed["truth_facts"]
        if str(raw["station_id"]).upper() in station_ids
        and start_date <= str(raw["event_date"]) <= end_date
        and raw["settlement_timezone"] == SETTLEMENT_TIMEZONE
        and raw["source_lane"] == "AWC"
        and raw["closed_day"] is True
    ]
    expected = len(station_ids) * 28
    if len(selected) != expected:
        raise ContractError(
            f"native_pilot_awc_truth_exact_scope_incomplete:{len(selected)}:{expected}"
        )

    truth_facts: list[dict[str, Any]] = []
    ledger_rows: list[dict[str, Any]] = []
    for source_truth in sorted(selected, key=lambda item: (item.station_id, item.event_date)):
        existing = _load_existing(conn, source_truth.station_id, source_truth.event_date)
        exact = [
            item for item in existing
            if item.settlement_timezone == SETTLEMENT_TIMEZONE
            and item.truth_value == source_truth.truth_value
            and item.truth_content_sha256 == source_truth.truth_content_sha256
            and item.station_day_end_utc == source_truth.station_day_end_utc
            and item.source_lane == "AWC"
            and item.source_role == "AWC_HISTORICAL_REPLAY_INGRESS"
            and item.authority_scope == "PRODUCTION_NO_LIVE_ONLY"
            and item.closed_day is True
        ]
        if len(exact) > 1:
            raise ContractError("native_pilot_awc_truth_exact_revision_duplicate")
        predecessor = max(existing, key=lambda item: item.truth_revision_no) if existing else None
        if exact:
            fact = exact[0].to_mapping()
            state = "REPLAYED_EXACT_ET_REVISION"
        else:
            revision = 0 if predecessor is None else predecessor.truth_revision_no + 1
            truth_identity = {
                "station_id": source_truth.station_id,
                "event_date": source_truth.event_date,
                "settlement_timezone": SETTLEMENT_TIMEZONE,
                "truth_content_sha256": source_truth.truth_content_sha256,
                "truth_revision_no": revision,
            }
            identity = {
                **{
                    key: value
                    for key, value in source_truth.to_mapping().items()
                    if key not in {
                        "content_sha256", "truth_id", "truth_revision_no",
                        "canonical_committed_at_utc",
                    }
                },
                "truth_id": "awc-et-truth-" + content_sha256(truth_identity)[:32],
                "truth_revision_no": revision,
                "canonical_committed_at_utc": committed_at_utc,
            }
            fact = _seal(identity)
            CanonicalTruthFact.from_mapping(fact)
            state = "MATERIALIZED_NEW_ET_REVISION"
        truth_facts.append(fact)
        ledger_rows.append({
            "station_id": source_truth.station_id,
            "event_date": source_truth.event_date,
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "state": state,
            "truth_revision_no": fact["truth_revision_no"],
            "truth_fact_content_sha256": fact["content_sha256"],
            "predecessor_truth_fact_content_sha256": (
                None if predecessor is None else predecessor.content_sha256
            ),
        })

    contract_identity = {
        **{
            key: value for key, value in parsed.items()
            if key not in {"content_sha256", "as_of_utc", "builder_id", "truth_facts"}
        },
        "as_of_utc": committed_at_utc,
        "builder_id": BUILDER_ID,
        "truth_facts": truth_facts,
    }
    contract = _seal(contract_identity)
    CanonicalContract.from_mapping(contract)
    receipt = _seal({
        "schema": "native_ensemble_pilot_awc_et_truth_revision_receipt_v1",
        "as_of_utc": committed_at_utc,
        "station_ids": sorted(station_ids),
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "start_date": start_date,
        "end_date": end_date,
        "truth_fact_count": len(truth_facts),
        "new_revision_count": sum(
            row["state"] == "MATERIALIZED_NEW_ET_REVISION" for row in ledger_rows
        ),
        "replayed_revision_count": sum(
            row["state"] == "REPLAYED_EXACT_ET_REVISION" for row in ledger_rows
        ),
        "contract_content_sha256": contract["content_sha256"],
        "revision_ledger": ledger_rows,
    })
    return contract, receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--station-id", action="append", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--committed-at-utc", required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    args = parser.parse_args()
    station_ids = {value.strip().upper() for value in args.station_id if value.strip()}
    if not station_ids:
        parser.error("at least one station is required")
    with sqlite3.connect(f"file:{args.db.resolve(strict=True)}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        contract, receipt = build_contract(
            conn,
            source=load_bounded_json(args.source.resolve(strict=True)),
            station_ids=station_ids,
            start_date=args.start_date,
            end_date=args.end_date,
            committed_at_utc=args.committed_at_utc,
        )
    write_bounded_json(args.contract_output.resolve(), contract)
    write_bounded_json(args.receipt_output.resolve(), receipt)
    print(json.dumps({
        "truth_fact_count": receipt["truth_fact_count"],
        "new_revision_count": receipt["new_revision_count"],
        "replayed_revision_count": receipt["replayed_revision_count"],
        "contract_content_sha256": contract["content_sha256"],
        "receipt_content_sha256": receipt["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
