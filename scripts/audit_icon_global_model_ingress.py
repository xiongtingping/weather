#!/usr/bin/env python3
"""Audit DWD ICON Global deterministic and Global EPS lineage together."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json_if_changed,
)


DEFAULT_CAPTURE = ROOT / "output/audits/icon_global_official_forward_capture_20260801.json"
DEFAULT_DECODE = ROOT / "output/audits/icon_global_official_katl_native_decode_20260801.json"
DEFAULT_EPS = ROOT / "output/audits/dwd_icon_global_eps_candidate_closure_20260801.json"
DEFAULT_OUTPUT = ROOT / "output/audits/icon_global_model_ingress_20260801.json"


def build_receipt(capture_path: Path, decode_path: Path, eps_path: Path) -> dict[str, Any]:
    capture = load_bounded_json(capture_path)
    decode = load_bounded_json(decode_path)
    eps = load_bounded_json(eps_path)
    fanout = capture.get("fanout") if isinstance(capture, dict) else None
    curves = decode.get("curves") if isinstance(decode, dict) else None
    official_ready = (
        isinstance(capture, dict)
        and capture.get("schema") == "bayes_basket_target_icon_forward_capture_scheduler_v1"
        and int(capture.get("captured_count") or 0)
        + int(capture.get("reused_count") or 0) == 24
        and int(capture.get("blocked_count") or 0) == 0
        and isinstance(fanout, dict)
        and int(fanout.get("expected_arrival_count") or 0) == 1080
        and int(fanout.get("captured_count") or 0) == 1080
        and int(fanout.get("blocked_count") or 0) == 0
        and isinstance(decode, dict)
        and int(decode.get("decoded_point_count") or 0) == 24
        and isinstance(curves, dict)
        and int(curves.get("complete_curve_count") or 0) == 1
    )
    eps_models = eps.get("models") if isinstance(eps, dict) else None
    eps_ready = (
        isinstance(eps_models, list)
        and len(eps_models) == 2
        and {
            str(item.get("ensemble_system_id") or "")
            for item in eps_models
            if isinstance(item, dict)
        } == {"DWD_ICON_EPS_SEAMLESS", "DWD_ICON_GLOBAL_EPS"}
        and eps.get("fail_closed") is True
        and all(
            item.get("source_class") == "aggregator_republisher"
            and item.get("formal_p0_eligible") is False
            and item.get("native_arrival_index_eligible") is False
            for item in eps_models
            if isinstance(item, dict)
        )
    )
    blockers = []
    if not official_ready:
        blockers.append("DWD_ICON_GLOBAL:official_native_evidence_invalid")
    if not eps_ready:
        blockers.append("DWD_ICON_GLOBAL_EPS:republisher_lineage_invalid")
    identity = {
        "schema": "icon_global_model_ingress_audit_v1",
        "state": (
            "OFFICIAL_GLOBAL_CAPTURED_EPS_REPUBLISHER_OOS_PENDING"
            if not blockers
            else "BLOCKED_FAIL_CLOSED"
        ),
        "official_deterministic_global": {
            "product_id": "DWD_ICON_GLOBAL",
            "provider_id": "icon_global_official",
            "source_role": "official_native",
            "model_scope": "global",
            "native_arrival_index_eligible": True,
            "formal_p0_eligible_without_strict_oos": False,
            "captured_or_reused_valid_times": int(capture.get("captured_count") or 0)
            + int(capture.get("reused_count") or 0),
            "fanout_captured_count": int((fanout or {}).get("captured_count") or 0),
            "decoded_point_count": int(decode.get("decoded_point_count") or 0),
            "complete_curve_count": int((curves or {}).get("complete_curve_count") or 0),
            "evidence_verified": official_ready,
            "capture_path": str(capture_path),
            "decode_path": str(decode_path),
        },
        "global_eps_republisher_candidates": {
            "system_ids": ["DWD_ICON_EPS_SEAMLESS", "DWD_ICON_GLOBAL_EPS"],
            "model_scope": "global",
            "source_role": "aggregator_republisher",
            "transport_provider_id": "OPEN_METEO",
            "native_arrival_index_eligible": False,
            "formal_p0_eligible": False,
            "strict_oos_activation_present": False,
            "lineage_verified": eps_ready,
            "candidate_manifest_path": str(eps_path),
        },
        "regional_products_excluded": ["DWD_ICON_EU_EPS", "DWD_ICON_D2_EPS"],
        "blockers": blockers,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--decode", type=Path, default=DEFAULT_DECODE)
    parser.add_argument("--eps", type=Path, default=DEFAULT_EPS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    receipt = build_receipt(
        args.capture.expanduser().resolve(strict=True),
        args.decode.expanduser().resolve(strict=True),
        args.eps.expanduser().resolve(strict=True),
    )
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, receipt)
    print(json.dumps({
        "state": receipt["state"],
        "official_valid_times": receipt["official_deterministic_global"]["captured_or_reused_valid_times"],
        "official_fanout": receipt["official_deterministic_global"]["fanout_captured_count"],
        "official_complete_curves": receipt["official_deterministic_global"]["complete_curve_count"],
        "eps_lineage_verified": receipt["global_eps_republisher_candidates"]["lineage_verified"],
        "output": str(output),
        "wrote": wrote,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not receipt["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
