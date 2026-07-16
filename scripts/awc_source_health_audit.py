#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "data" / "logs" / "hard_source_direct_poller.log"
DEFAULT_STATE = ROOT / "data" / "awc_source_health_latest.json"


def _parse_iso(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _tail_text(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max(1, int(max_bytes))))
            return handle.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload) if isinstance(payload, Mapping) else {}
    except Exception:
        return {}


def _iter_log_entries(path: Path, *, max_bytes: int, since_ts: float) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw in _tail_text(path, max_bytes).splitlines():
        line = raw.strip()
        if not line:
            continue
        ts_text, sep, json_text = line.partition(" ")
        if not sep:
            continue
        ts = _parse_iso(ts_text)
        if ts is None or ts < since_ts:
            continue
        try:
            payload = json.loads(json_text)
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        item = dict(payload)
        item["_log_ts"] = ts_text
        item["_log_epoch"] = ts
        entries.append(item)
    entries.sort(key=lambda item: float(item.get("_log_epoch") or 0.0))
    return entries


def _number(value: Any) -> float | None:
    try:
        val = float(value)
    except Exception:
        return None
    if not math.isfinite(val):
        return None
    return val


def _percentile(values: list[float], pct: float) -> float | None:
    nums = sorted(v for v in values if math.isfinite(v))
    if not nums:
        return None
    if len(nums) == 1:
        return float(nums[0])
    rank = max(0.0, min(1.0, pct)) * (len(nums) - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return float(nums[low])
    frac = rank - low
    return float(nums[low] * (1.0 - frac) + nums[high] * frac)


def _error_class(error: Any) -> str:
    text = str(error or "").strip()
    if not text:
        return ""
    return text.split(":", 1)[0].strip() or text[:80]


def audit(args: argparse.Namespace) -> dict[str, Any]:
    now_ts = time.time()
    lookback_sec = max(60.0, float(args.lookback_min) * 60.0)
    entries = _iter_log_entries(Path(args.log), max_bytes=int(args.tail_bytes), since_ts=now_ts - lookback_sec)
    state = _load_json(Path(args.state))
    polls = len(entries)
    awc_success = 0
    awc_failed = 0
    awc_skipped = 0
    covered_failures = 0
    fetch_ms: list[float] = []
    fast_precheck_ms: list[float] = []
    error_counts: dict[str, int] = {}
    latest_success: dict[str, Any] = {}
    latest_entry: dict[str, Any] = entries[-1] if entries else {}

    for item in entries:
        awc = item.get("awc") if isinstance(item.get("awc"), Mapping) else {}
        posterior = item.get("posterior_target_fast_precheck")
        if not isinstance(posterior, Mapping) or not posterior:
            posterior = item.get("posterior_target")
        if not isinstance(posterior, Mapping):
            posterior = {}
        awc_ok = bool(awc.get("ok"))
        status = str(awc.get("status") or "").strip()
        if awc_ok:
            awc_success += 1
            latest_success = item
        elif status == "skipped":
            awc_skipped += 1
        else:
            awc_failed += 1
            err = _error_class(awc.get("error")) or status or "unknown"
            error_counts[err] = error_counts.get(err, 0) + 1
            if bool(posterior.get("ok")) and str(posterior.get("status") or ""):
                covered_failures += 1
        timings = awc.get("timings_ms") if isinstance(awc, Mapping) else {}
        if isinstance(timings, Mapping):
            val = _number(timings.get("awc_fetch_features_ms"))
            if val is not None:
                fetch_ms.append(val)
        top_timings = item.get("timings_ms") if isinstance(item.get("timings_ms"), Mapping) else {}
        val = _number(
            top_timings.get("posterior_fast_precheck_before_awc_ms")
            or top_timings.get("fast_precheck_before_awc_total_ms")
            or top_timings.get("posterior_fast_precheck_ms")
            or top_timings.get("fast_precheck_total_ms")
        )
        if val is not None:
            fast_precheck_ms.append(val)

    consecutive_failures = 0
    for item in reversed(entries):
        awc = item.get("awc") if isinstance(item.get("awc"), Mapping) else {}
        if bool(awc.get("ok")):
            break
        if str(awc.get("status") or "").strip() == "skipped":
            continue
        consecutive_failures += 1

    latest_success_ts = _parse_iso(latest_success.get("_log_ts")) if latest_success else None
    latest_poll_ts = _parse_iso(latest_entry.get("_log_ts")) if latest_entry else None
    latest_state_ts = _parse_iso(state.get("generated_at"))
    success_rate = (awc_success / polls) if polls else 0.0
    failure_coverage_rate = (covered_failures / awc_failed) if awc_failed else 1.0
    latest_success_age_sec = (now_ts - latest_success_ts) if latest_success_ts else None
    latest_poll_age_sec = (now_ts - latest_poll_ts) if latest_poll_ts else None
    latest_state_age_sec = (now_ts - latest_state_ts) if latest_state_ts else None

    blockers: list[str] = []
    warnings: list[str] = []
    if not entries:
        blockers.append("awc_poller_log_no_recent_entries")
    if latest_poll_age_sec is not None and latest_poll_age_sec > float(args.max_latest_poll_age_sec):
        blockers.append("awc_poller_latest_entry_stale")
    if latest_state_age_sec is None:
        warnings.append("awc_source_health_state_missing")
    elif latest_state_age_sec > float(args.max_latest_state_age_sec):
        warnings.append("awc_source_health_state_stale")
    if latest_success_age_sec is None:
        blockers.append("awc_no_success_in_lookback")
    elif latest_success_age_sec > float(args.max_latest_success_age_sec):
        blockers.append("awc_latest_success_stale")
    if polls and success_rate < float(args.warn_success_rate):
        warnings.append("awc_success_rate_low")
    if consecutive_failures >= int(args.warn_consecutive_failures):
        warnings.append("awc_consecutive_failures")
    failure_coverage_recovered = bool(
        awc_failed
        and failure_coverage_rate < float(args.min_failure_coverage_rate)
        and consecutive_failures == 0
        and latest_success_age_sec is not None
        and latest_success_age_sec <= float(args.max_latest_success_age_sec)
    )
    if awc_failed and failure_coverage_rate < float(args.min_failure_coverage_rate):
        if failure_coverage_recovered:
            warnings.append("awc_failure_not_covered_by_posterior_fast_precheck_recovered")
        else:
            blockers.append("awc_failure_not_covered_by_posterior_fast_precheck")

    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "details": {
            "log": str(Path(args.log)),
            "state": str(Path(args.state)),
            "lookback_min": float(args.lookback_min),
            "polls": polls,
            "awc_success": awc_success,
            "awc_failed": awc_failed,
            "awc_skipped": awc_skipped,
            "success_rate": round(success_rate, 4),
            "covered_failures": covered_failures,
            "failure_coverage_rate": round(failure_coverage_rate, 4),
            "failure_coverage_recovered": failure_coverage_recovered,
            "consecutive_failures": consecutive_failures,
            "error_counts": error_counts,
            "latest_poll_at": latest_entry.get("_log_ts") or "",
            "latest_poll_age_sec": round(latest_poll_age_sec, 3) if latest_poll_age_sec is not None else None,
            "latest_success_at": latest_success.get("_log_ts") if latest_success else "",
            "latest_success_age_sec": round(latest_success_age_sec, 3) if latest_success_age_sec is not None else None,
            "latest_awc_status": (latest_entry.get("awc") or {}).get("status") if isinstance(latest_entry.get("awc"), Mapping) else "",
            "latest_awc_error_class": _error_class((latest_entry.get("awc") or {}).get("error")) if isinstance(latest_entry.get("awc"), Mapping) else "",
            "latest_state_age_sec": round(latest_state_age_sec, 3) if latest_state_age_sec is not None else None,
            "latest_state": state,
            "latency_ms": {
                "awc_fetch_p50": _percentile(fetch_ms, 0.50),
                "awc_fetch_p95": _percentile(fetch_ms, 0.95),
                "posterior_fast_precheck_p50": _percentile(fast_precheck_ms, 0.50),
                "posterior_fast_precheck_p95": _percentile(fast_precheck_ms, 0.95),
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit AviationWeather AWC fetch health and posterior fast-precheck fallback.")
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--lookback-min", type=float, default=float(os.environ.get("PM_AWC_SOURCE_HEALTH_LOOKBACK_MIN", "30")))
    parser.add_argument("--tail-bytes", type=int, default=int(float(os.environ.get("PM_AWC_SOURCE_HEALTH_TAIL_BYTES", "20000000"))))
    parser.add_argument("--max-latest-poll-age-sec", type=float, default=float(os.environ.get("PM_AWC_SOURCE_HEALTH_MAX_POLL_AGE_SEC", "90")))
    parser.add_argument("--max-latest-state-age-sec", type=float, default=float(os.environ.get("PM_AWC_SOURCE_HEALTH_MAX_STATE_AGE_SEC", "90")))
    parser.add_argument("--max-latest-success-age-sec", type=float, default=float(os.environ.get("PM_AWC_SOURCE_HEALTH_MAX_SUCCESS_AGE_SEC", "600")))
    parser.add_argument("--warn-success-rate", type=float, default=float(os.environ.get("PM_AWC_SOURCE_HEALTH_WARN_SUCCESS_RATE", "0.35")))
    parser.add_argument("--warn-consecutive-failures", type=int, default=int(float(os.environ.get("PM_AWC_SOURCE_HEALTH_WARN_CONSECUTIVE_FAILURES", "5"))))
    parser.add_argument("--min-failure-coverage-rate", type=float, default=float(os.environ.get("PM_AWC_SOURCE_HEALTH_MIN_FAILURE_COVERAGE_RATE", "0.95")))
    args = parser.parse_args(argv)
    report = audit(args)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
