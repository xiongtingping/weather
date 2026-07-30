#!/usr/bin/env python3
"""Run the offline strict-as-of window and cross-season replay.

The default inputs are frozen derived artifacts under the report directory.
This command never opens the production database and never imports a gateway.
The output is an audit receipt, not a promotion or live-authorization switch.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.strict_asof_replay import DEFAULT_TARGET_TIMEZONE, evaluate_strict_asof_replay
from bayes_basket_target.contracts import ContractError


REPORT_DERIVED = ROOT / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/derived"
DEFAULT_MODEL = REPORT_DERIVED / "official_nwp_hourly_crossseason_7stations.csv"
DEFAULT_OBSERVATIONS = REPORT_DERIVED / "iem_metar_reports_annual_offline_7stations.csv"


def _current_utc() -> str:
    """Return the invocation clock for an omitted replay as-of."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_csv(path: Path) -> Iterable[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _receipt_sha256(receipt: dict[str, Any]) -> str:
    identity = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def run(
    *,
    model_path: Path = DEFAULT_MODEL,
    observation_path: Path = DEFAULT_OBSERVATIONS,
    as_of_utc: str = "",
    target_timezone: str = DEFAULT_TARGET_TIMEZONE,
) -> dict[str, Any]:
    as_of = str(as_of_utc or _current_utc())
    if not model_path.exists() or not observation_path.exists():
        missing = [str(path) for path in (model_path, observation_path) if not path.exists()]
        raise FileNotFoundError(", ".join(missing))
    try:
        result = evaluate_strict_asof_replay(
            _read_csv(model_path),
            _read_csv(observation_path),
            as_of_utc=as_of,
            target_timezone=target_timezone,
        )
    except ContractError as exc:
        as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(timezone.utc)
        result = {
            "schema": "bayes_basket_target_strict_asof_replay_v1",
            "state": "BLOCKED_EVIDENCE",
            "target_timezone": target_timezone,
            "as_of_utc": as_of_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "anchor_local_date": as_of_dt.astimezone(ZoneInfo(target_timezone)).date().isoformat(),
            "failure_reason": f"{type(exc).__name__}:{exc}",
            "boundary": {
                "offline_research_only": True,
                "uses_production_database": False,
                "writes_production_state": False,
                "reads_probability_state": False,
                "reads_market_state": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "promotion_allowed": False,
            },
        }
    result["input_artifacts"] = {
        "model_hourly": {"path": str(model_path), "sha256": _file_sha256(model_path)},
        "observations": {"path": str(observation_path), "sha256": _file_sha256(observation_path)},
    }
    result["status"] = (
        "PASS"
        if result.get("state") == "EVALUATED"
        and result["windows"]["mutually_exclusive"]
        and result["windows"]["same_maturity"]
        and result["crossseason"]["strict_asof"]
        and result["crossseason"]["lineage_complete"]
        and result["crossseason"]["all_four_seasons_present"]
        and result["crossseason"]["cross_station"]
        else "BLOCKED_EVIDENCE"
        if result.get("state") == "BLOCKED_EVIDENCE"
        else "FAIL"
    )
    result["receipt_sha256"] = _receipt_sha256(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-hourly", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--observations", type=Path, default=DEFAULT_OBSERVATIONS)
    parser.add_argument(
        "--as-of-utc",
        default="",
        help="Strict decision clock; defaults to the current UTC at invocation time.",
    )
    parser.add_argument("--target-timezone", default=DEFAULT_TARGET_TIMEZONE)
    parser.add_argument("--write-receipt", type=Path)
    args = parser.parse_args()
    receipt = run(
        model_path=args.model_hourly,
        observation_path=args.observations,
        as_of_utc=args.as_of_utc,
        target_timezone=args.target_timezone,
    )
    if args.write_receipt is not None:
        path = args.write_receipt if args.write_receipt.is_absolute() else ROOT / args.write_receipt
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
