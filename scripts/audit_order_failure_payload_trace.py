#!/usr/bin/env python3
"""Audit that place-order failures preserve raw CLOB subprocess payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "scan_weather_market.py"


def _line_no(text: str, pos: int) -> int:
    return text.count("\n", 0, max(0, pos)) + 1


def _call_span(text: str, start: int) -> tuple[int, int]:
    open_pos = text.find("(", start)
    if open_pos < 0:
        return start, start
    depth = 0
    quote = ""
    escape = False
    for idx in range(open_pos, len(text)):
        ch = text[idx]
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = ""
            continue
        if ch in {"'", '"'}:
            quote = ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return start, idx + 1
    return start, len(text)


def _find_buffer_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    pos = 0
    needle = "_buffer_decision"
    while True:
        pos = text.find(needle, pos)
        if pos < 0:
            break
        start, end = _call_span(text, pos)
        chunk = text[start:end]
        if "place_order_failed" in chunk:
            preceding = text[max(0, start - 1200) : start]
            has_direct_fields = "_order_failure_detail_fields(" in chunk
            has_indirect_fields = (
                "details=fail_details" in chunk
                and "fail_details" in preceding
                and "_order_failure_detail_fields(" in preceding
            )
            calls.append(
                {
                    "line": _line_no(text, start),
                    "has_order_failure_detail_fields": bool(has_direct_fields or has_indirect_fields),
                    "detail_flow": "direct" if has_direct_fields else ("indirect" if has_indirect_fields else "missing"),
                    "has_reason": "place_order_failed" in chunk,
                }
            )
        pos = end if end > pos else pos + len(needle)
    return calls


def main() -> int:
    text = SCAN.read_text(encoding="utf-8")
    calls = _find_buffer_calls(text)
    raw_fields = {
        "order_error",
        "order_error_detail",
        "order_stderr",
        "order_stdout",
        "order_raw_payload_json",
        "order_raw_payload_keys",
        "order_raw_payload_truncated",
        "order_status",
        "order_status_text",
        "order_submit_started_at",
        "order_submit_completed_at",
        "order_submit_ms",
        "order_submit_outer_timeout_sec",
    }
    compact_missing = sorted(
        field for field in raw_fields if f'"{field}"' not in text and f"'{field}'" not in text
    )
    timing_return_ok = (
        "return False, _attach_order_submit_timing(payload)" in text
        or "return False, _attach_order_submit_timing(\n                payload," in text
    )
    exec_nonzero_ok = all(
        fragment in text
        for fragment in (
            '"error": "exec_nonzero"',
            '"stderr": res.stderr.strip()',
            '"stdout": res.stdout.strip()',
        )
    ) and timing_return_ok
    missing_failure_detail_lines = [
        int(item["line"]) for item in calls if not item["has_order_failure_detail_fields"]
    ]
    payload = {
        "ok": bool(calls) and not missing_failure_detail_lines and not compact_missing and exec_nonzero_ok,
        "scan_file": str(SCAN),
        "place_order_failed_buffer_calls": calls,
        "missing_order_failure_detail_lines": missing_failure_detail_lines,
        "compact_missing_raw_fields": compact_missing,
        "exec_nonzero_preserves_stdout_stderr": exec_nonzero_ok,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
