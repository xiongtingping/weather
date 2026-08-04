#!/usr/bin/env python3
"""Read-only reconcile of held YES balances through the local CLOB gateway.

The endpoint is an execution-guard snapshot, never an order path.  The
receipt is bounded to the active token list and is suitable for updating the
local inventory's active-token projection without asserting that an old Data
API position export is current.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "data" / "reported_trajectory_held_yes_tokens.json"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / "reported_trajectory_held_yes_clob_reconcile_latest.json"
MAX_TOKENS = 64
MAX_RESPONSE_BYTES = 1 * 1024 * 1024
SCHEMA = "reported_trajectory_held_yes_clob_reconcile_v1"


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scope_sha(token_ids: list[str]) -> str:
    """Hash the exact, canonical reconciliation scope.

    The active projection may only be shrunk after every known historical YES
    token has been inspected.  Binding the receipt to a sorted token list
    makes a partial `--token-id` diagnostic receipt unusable for that state
    transition.
    """

    return hashlib.sha256("\x00".join(token_ids).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _post_snapshot(url: str, token_id: str, timeout: float) -> dict[str, Any]:
    body = json.dumps({
        "token_id": token_id,
        "side": "SELL",
        "limit_price": 0.99,
        "size": 1,
        "notional": 0.99,
    }, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("clob_guard_response_bytes_exceeded")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError("clob_guard_response_mapping_required")
    return dict(value)


def run(*, inventory_path: Path, output_path: Path, gateway_url: str, timeout: float, retries: int, token_ids_override: list[str] | None = None) -> dict[str, Any]:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if not isinstance(inventory, Mapping):
        raise RuntimeError("held_yes_inventory_mapping_required")
    requested_scope = (
        token_ids_override
        if token_ids_override is not None
        else inventory.get("token_ids") or []
    )
    token_ids = sorted({
        str(value).strip()
        for value in requested_scope
        if str(value).strip()
    })
    if not token_ids or len(token_ids) > MAX_TOKENS:
        raise RuntimeError("held_yes_active_token_scope_invalid")
    rows: list[dict[str, Any]] = []
    for token_id in token_ids:
        last_error = ""
        response: dict[str, Any] | None = None
        for attempt in range(max(1, retries)):
            try:
                response = _post_snapshot(gateway_url, token_id, timeout)
                break
            except (OSError, ValueError, RuntimeError) as exc:
                last_error = f"{type(exc).__name__}:{exc}"
                if attempt + 1 < max(1, retries):
                    time.sleep(0.5)
        if response is None:
            rows.append({"token_id": token_id, "state": "UNAVAILABLE", "error": last_error})
            continue
        balance = response.get("conditional_balance_allowance")
        balance_value = None
        if isinstance(balance, Mapping):
            raw_balance = balance.get("balance")
            try:
                balance_value = float(raw_balance)
            except (TypeError, ValueError):
                balance_value = None
        if (
            balance_value is not None
            and (not math.isfinite(balance_value) or balance_value < 0.0)
        ):
            balance_value = None
        rows.append({
            "token_id": token_id,
            # A balance proof remains useful when the same execution-guard
            # response correctly blocks a SELL for absent/stale book depth.
            # Book evidence is validated by the independent WS gate; do not
            # turn that execution block into a false zero/unknown balance.
            "state": "READY" if balance_value is not None else "EVIDENCE_ONLY",
            "balance": balance_value,
            "conditional_balance_allowance": balance,
            "execution_guard_ok": response.get("ok") is True,
            "evidence_errors": list(response.get("evidence_errors") or []),
            "observed_at": response.get("observed_at"),
            "content_sha256": _sha(response),
        })
    zero_ids = sorted(row["token_id"] for row in rows if row.get("balance") == 0)
    nonzero_ids = sorted(row["token_id"] for row in rows if isinstance(row.get("balance"), (int, float)) and row["balance"] > 0)
    unavailable_ids = sorted(row["token_id"] for row in rows if row.get("state") == "UNAVAILABLE")
    evidence_only_ids = sorted(
        row["token_id"] for row in rows if row.get("state") == "EVIDENCE_ONLY"
    )
    result = {
        "schema": SCHEMA,
        "observed_at": _now(),
        "source": "official_local_clob_execution_guard_snapshot",
        "inventory_source_sha256": _sha(inventory),
        "gateway_url": gateway_url,
        "token_count": len(token_ids),
        "scope_token_ids": token_ids,
        "scope_sha256": _scope_sha(token_ids),
        "rows": rows,
        "zero_token_ids": zero_ids,
        "nonzero_token_ids": nonzero_ids,
        "unavailable_token_ids": unavailable_ids,
        "evidence_only_token_ids": evidence_only_ids,
        "unverified_balance_token_ids": evidence_only_ids,
        # A missing conditional balance is not a zero balance.  Do not allow
        # an incomplete local CLOB read to remove a held token from scope.
        "complete": (
            len(rows) == len(token_ids)
            and not unavailable_ids
            and not evidence_only_ids
        ),
        "evidence_only": True,
        "authorizes_order": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    result["receipt_sha256"] = _sha(result)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def update_active_projection(inventory_path: Path, receipt: Mapping[str, Any]) -> None:
    """Atomically project only a full-scope CLOB-proven non-zero set.

    Targeted `--token-id` runs remain useful diagnostics, but cannot change
    the active held-YES projection.  Otherwise a transient or accidental
    partial query could make a real YES position disappear from the exit gate.
    """

    if receipt.get("complete") is not True:
        raise RuntimeError("held_yes_clob_reconcile_incomplete")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if not isinstance(inventory, dict):
        raise RuntimeError("held_yes_inventory_mapping_required")
    known_list = sorted({
        str(value).strip() for value in inventory.get("token_ids") or []
        if str(value).strip()
    })
    known = set(known_list)
    receipt_scope = [str(value).strip() for value in receipt.get("scope_token_ids") or []]
    active = sorted({
        str(value).strip() for value in receipt.get("nonzero_token_ids") or []
        if str(value).strip()
    })
    zero = {str(value).strip() for value in receipt.get("zero_token_ids") or [] if str(value).strip()}
    evidence_only = {str(value).strip() for value in receipt.get("evidence_only_token_ids") or [] if str(value).strip()}
    unavailable = {str(value).strip() for value in receipt.get("unavailable_token_ids") or [] if str(value).strip()}
    if (
        not known
        or receipt_scope != known_list
        or receipt.get("scope_sha256") != _scope_sha(known_list)
        or receipt.get("token_count") != len(known_list)
        or evidence_only
        or unavailable
        or set(active) | zero != known
        or set(active) & zero
    ):
        raise RuntimeError("held_yes_clob_reconcile_scope_incomplete")
    if not set(active).issubset(known):
        raise RuntimeError("held_yes_clob_token_outside_inventory_scope")
    inventory["active_token_ids"] = active
    inventory["observed_at"] = str(receipt.get("observed_at") or "")
    inventory["active_token_ids_source"] = "official_local_clob_execution_guard_snapshot"
    inventory["active_token_ids_receipt_sha256"] = str(receipt.get("receipt_sha256") or "")
    inventory["active_token_ids_observed_at"] = str(receipt.get("observed_at") or "")
    raw = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if inventory_path.is_symlink():
        raise RuntimeError("held_yes_inventory_symlink_forbidden")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{inventory_path.name}.", dir=str(inventory_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, inventory_path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8765/execution-guard-snapshot")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--update-active-projection", action="store_true")
    parser.add_argument("--token-id", action="append", default=[])
    args = parser.parse_args()
    try:
        result = run(
            inventory_path=args.inventory.expanduser().resolve(),
            output_path=args.output.expanduser().resolve(),
            gateway_url=str(args.gateway_url),
            timeout=max(1.0, float(args.timeout)),
            retries=max(1, min(3, int(args.retries))),
            token_ids_override=(list(args.token_id) if args.token_id else None),
        )
        if args.update_active_projection:
            update_active_projection(args.inventory.expanduser().resolve(), result)
            result["active_projection_updated"] = True
        print(json.dumps({key: result[key] for key in ("schema", "complete", "token_count", "zero_token_ids", "nonzero_token_ids", "unavailable_token_ids", "receipt_sha256")}, ensure_ascii=False, sort_keys=True))
        return 0 if result["complete"] else 2
    except Exception as exc:
        print(json.dumps({"schema": SCHEMA, "state": "BLOCKED_FAIL_CLOSED", "reason": f"{type(exc).__name__}:{exc}", "calls_order_gateway": False, "orders_submitted": 0}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
