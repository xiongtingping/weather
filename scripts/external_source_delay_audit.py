#!/usr/bin/env python3
"""Read-only audit report for external weather source lead/lag evidence."""

from __future__ import annotations

import argparse
import glob
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from email.utils import parsedate_to_datetime

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - old Python fallback
    ZoneInfo = None  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_GLOB = ROOT / "data" / "evidence" / "peak_window_evidence_chain_*.ndjson"
DEFAULT_JSON_REPORT = ROOT / "data" / "reports" / "external_source_delay_audit_latest.json"
DEFAULT_MD_REPORT = ROOT / "data" / "reports" / "external_source_delay_audit_latest.md"
DEFAULT_TZ = "Asia/Shanghai"


def parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_http_date(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_wu_display_local(value: Any, tz_name: str = DEFAULT_TZ) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)\s+\w+\s+on\s+"
        r"(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})",
        text,
        flags=re.I,
    )
    if not match:
        return None
    cleaned = (
        f"{match.group('hour')}:{match.group('minute')} {match.group('ampm').upper()} "
        f"{match.group('month')} {match.group('day')} {match.group('year')}"
    )
    try:
        local_naive = datetime.strptime(cleaned, "%I:%M %p %B %d %Y")
    except Exception:
        return None
    if ZoneInfo is None:
        return local_naive.replace(tzinfo=timezone.utc)
    try:
        return local_naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
    except Exception:
        return local_naive.replace(tzinfo=timezone.utc)


def dt_iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def round_or_none(value: Optional[float], digits: int = 3) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def iter_input_files(patterns: Iterable[str]) -> List[Path]:
    paths: set[Path] = set()
    for pattern in patterns:
        expanded = glob.glob(str(pattern))
        if expanded:
            paths.update(Path(item) for item in expanded)
        else:
            path = Path(pattern)
            if path.exists():
                paths.add(path)
    return sorted(paths, key=lambda path: path.stat().st_mtime if path.exists() else 0.0)


def iter_samples(paths: Iterable[Path]) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line_no, line in enumerate(lines, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            if not isinstance(payload, Mapping):
                continue
            sample = dict(payload)
            sample["_source_file"] = str(path)
            sample["_line_no"] = line_no
            samples.append(sample)
    samples.sort(key=lambda item: parse_iso(item.get("sample_started_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return samples


def source_valid_dt(source: Mapping[str, Any], tz_name: str) -> Optional[datetime]:
    valid = parse_iso(source.get("valid_time_utc"))
    if valid is not None:
        return valid
    valid = parse_iso(source.get("valid_time_local"))
    if valid is not None:
        return valid
    return parse_wu_display_local(source.get("valid_time_local_text"), tz_name)


def source_response_dt(source: Mapping[str, Any]) -> Optional[datetime]:
    transport = source.get("transport") if isinstance(source.get("transport"), Mapping) else {}
    if isinstance(transport, Mapping):
        parsed = parse_iso(transport.get("request_completed_at"))
        if parsed is not None:
            return parsed
        parsed = parse_http_date(transport.get("response_date"))
        if parsed is not None:
            return parsed
    return None


def source_row(sample: Mapping[str, Any], source: Mapping[str, Any]) -> Dict[str, Any]:
    tz_name = str(sample.get("timezone") or DEFAULT_TZ)
    valid_dt = source_valid_dt(source, tz_name)
    response_dt = source_response_dt(source)
    transport = source.get("transport") if isinstance(source.get("transport"), Mapping) else {}
    age_sec = (response_dt - valid_dt).total_seconds() if response_dt and valid_dt else None
    return {
        "sample_started_at": sample.get("sample_started_at") or "",
        "sample_completed_at": sample.get("sample_completed_at") or "",
        "station_id": sample.get("station_id") or "",
        "event_date": sample.get("event_date") or "",
        "source": source.get("source") or "",
        "provider": source.get("provider") or "",
        "ok": bool(source.get("ok")),
        "valid_time_utc": dt_iso(valid_dt),
        "valid_time_local": source.get("valid_time_local") or source.get("valid_time_local_text") or "",
        "temperature_c": number(source.get("temperature_c")),
        "today_max_c": number(source.get("today_max_c")),
        "response_time_utc": dt_iso(response_dt),
        "response_duration_ms": number(transport.get("duration_ms") if isinstance(transport, Mapping) else None),
        "response_status_code": transport.get("status_code") if isinstance(transport, Mapping) else None,
        "source_age_sec_at_response": round_or_none(age_sec),
        "error": source.get("error") or (transport.get("error") if isinstance(transport, Mapping) else "") or "",
        "_valid_epoch": valid_dt.timestamp() if valid_dt else None,
    }


def analyze_samples(samples: List[Mapping[str, Any]]) -> Dict[str, Any]:
    sample_reports: List[Dict[str, Any]] = []
    all_rows: List[Dict[str, Any]] = []
    for sample in samples:
        sources = sample.get("sources")
        if not isinstance(sources, list):
            continue
        rows = [source_row(sample, source) for source in sources if isinstance(source, Mapping)]
        rows = [row for row in rows if row.get("source")]
        by_source = {str(row.get("source")): row for row in rows}
        awc_epoch = by_source.get("awc_cache_metars_csv_gz", {}).get("_valid_epoch")
        twc_epoch = by_source.get("twc_current_icao_metric", {}).get("_valid_epoch")
        best_epoch = max((number(row.get("_valid_epoch")) for row in rows if row.get("ok")), default=None)
        for row in rows:
            valid_epoch = number(row.get("_valid_epoch"))
            row["lead_sec_vs_awc_cache"] = round_or_none(valid_epoch - awc_epoch) if valid_epoch is not None and awc_epoch is not None else None
            row["lead_sec_vs_twc_current"] = round_or_none(valid_epoch - twc_epoch) if valid_epoch is not None and twc_epoch is not None else None
            row["lag_sec_vs_best_valid_time"] = round_or_none(best_epoch - valid_epoch) if valid_epoch is not None and best_epoch is not None else None
            row.pop("_valid_epoch", None)
        lead_rows = sorted(
            [row for row in rows if row.get("ok") and row.get("valid_time_utc")],
            key=lambda row: (parse_iso(row.get("valid_time_utc")) or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )
        sample_reports.append(
            {
                "sample_started_at": sample.get("sample_started_at") or "",
                "sample_completed_at": sample.get("sample_completed_at") or "",
                "source_file": sample.get("_source_file") or "",
                "line_no": sample.get("_line_no"),
                "rows": rows,
                "leader_source": lead_rows[0].get("source") if lead_rows else "",
                "leader_valid_time_utc": lead_rows[0].get("valid_time_utc") if lead_rows else "",
                "awc_cache_valid_time_utc": by_source.get("awc_cache_metars_csv_gz", {}).get("valid_time_utc", ""),
                "twc_current_valid_time_utc": by_source.get("twc_current_icao_metric", {}).get("valid_time_utc", ""),
            }
        )
        all_rows.extend(rows)

    by_source_summary: Dict[str, Dict[str, Any]] = {}
    for row in all_rows:
        source = str(row.get("source") or "")
        if not source:
            continue
        item = by_source_summary.setdefault(
            source,
            {
                "source": source,
                "samples": 0,
                "ok_samples": 0,
                "latest_valid_time_utc": "",
                "latest_temperature_c": None,
                "max_lead_sec_vs_awc_cache": None,
                "median_age_sec_at_response": None,
                "_ages": [],
            },
        )
        item["samples"] += 1
        if row.get("ok"):
            item["ok_samples"] += 1
        current_latest = parse_iso(item.get("latest_valid_time_utc"))
        row_valid = parse_iso(row.get("valid_time_utc"))
        row_temp = row.get("temperature_c")
        latest_temp_missing = item.get("latest_temperature_c") in (None, "")
        if row_valid is not None and (
            current_latest is None
            or row_valid > current_latest
            or (row_valid == current_latest and latest_temp_missing and row_temp not in (None, ""))
        ):
            item["latest_valid_time_utc"] = row.get("valid_time_utc") or ""
            item["latest_temperature_c"] = row_temp
        lead = number(row.get("lead_sec_vs_awc_cache"))
        if lead is not None and (item["max_lead_sec_vs_awc_cache"] is None or lead > item["max_lead_sec_vs_awc_cache"]):
            item["max_lead_sec_vs_awc_cache"] = lead
        age = number(row.get("source_age_sec_at_response"))
        if age is not None:
            item["_ages"].append(age)
    for item in by_source_summary.values():
        ages = sorted(item.pop("_ages"))
        if ages:
            mid = len(ages) // 2
            item["median_age_sec_at_response"] = round_or_none(
                ages[mid] if len(ages) % 2 else (ages[mid - 1] + ages[mid]) / 2.0
            )
        item["ok_rate"] = round_or_none(item["ok_samples"] / item["samples"], 4) if item["samples"] else None

    return {
        "sample_count": len(sample_reports),
        "source_count": len(by_source_summary),
        "samples": sample_reports,
        "source_summary": sorted(by_source_summary.values(), key=lambda item: str(item.get("source"))),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# 外部源延迟/领先诊断报告")
    lines.append("")
    lines.append(f"- 生成时间: `{report.get('generated_at')}`")
    lines.append(f"- 样本数: `{report.get('sample_count')}`")
    lines.append("- 口径: 只读 NDJSON 证据链；不写事实层、不写交易决策、不下单。")
    lines.append("")
    lines.append("## 源汇总")
    lines.append("")
    lines.append("| source | ok_rate | latest_valid_time_utc | latest_temperature_c | max_lead_sec_vs_awc_cache | median_age_sec_at_response |")
    lines.append("|---|---:|---|---:|---:|---:|")
    for item in report.get("source_summary", []):
        if not isinstance(item, Mapping):
            continue
        lines.append(
            "| {source} | {ok_rate} | {latest_valid_time_utc} | {latest_temperature_c} | {max_lead} | {median_age} |".format(
                source=item.get("source", ""),
                ok_rate=item.get("ok_rate", ""),
                latest_valid_time_utc=item.get("latest_valid_time_utc", ""),
                latest_temperature_c=item.get("latest_temperature_c", ""),
                max_lead=item.get("max_lead_sec_vs_awc_cache", ""),
                median_age=item.get("median_age_sec_at_response", ""),
            )
        )
    lines.append("")
    lines.append("## 最近样本")
    lines.append("")
    samples = report.get("samples") if isinstance(report.get("samples"), list) else []
    for sample in samples[-5:]:
        if not isinstance(sample, Mapping):
            continue
        lines.append(f"### {sample.get('sample_started_at')}")
        lines.append("")
        lines.append(f"- leader: `{sample.get('leader_source')}` at `{sample.get('leader_valid_time_utc')}`")
        lines.append(f"- AWC cache valid: `{sample.get('awc_cache_valid_time_utc')}`")
        lines.append("")
        lines.append("| source | ok | valid_time | temp_c | lead_vs_awc_sec | age_sec | duration_ms |")
        lines.append("|---|---:|---|---:|---:|---:|---:|")
        for row in sample.get("rows", []):
            if not isinstance(row, Mapping):
                continue
            lines.append(
                "| {source} | {ok} | {valid} | {temp} | {lead} | {age} | {duration} |".format(
                    source=row.get("source", ""),
                    ok=int(bool(row.get("ok"))),
                    valid=row.get("valid_time_local") or row.get("valid_time_utc") or "",
                    temp=row.get("temperature_c", ""),
                    lead=row.get("lead_sec_vs_awc_cache", ""),
                    age=row.get("source_age_sec_at_response", ""),
                    duration=row.get("response_duration_ms", ""),
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    input_patterns = args.input or [str(DEFAULT_INPUT_GLOB)]
    paths = iter_input_files(input_patterns)
    samples = iter_samples(paths)
    if args.latest_samples > 0:
        samples = samples[-int(args.latest_samples) :]
    analysis = analyze_samples(samples)
    return {
        "schema": "external_source_delay_audit_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "input_files": [str(path) for path in paths],
        "guards": {
            "mode": "read_only_audit_report",
            "reads": "data/evidence/peak_window_evidence_chain_*.ndjson",
            "writes_reports_only": True,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "history_backfill": False,
        },
        **analysis,
    }


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", help="NDJSON file or glob. Can be repeated.")
    parser.add_argument("--latest-samples", type=int, default=200)
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_REPORT))
    parser.add_argument("--md-output", default=str(DEFAULT_MD_REPORT))
    parser.add_argument("--no-write", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    report = build_report(args)
    if not args.no_write:
        json_path = Path(args.json_output)
        md_path = Path(args.md_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        md_path.write_text(render_markdown(report), encoding="utf-8")
        print(f"[external-source-delay-audit] wrote {json_path}")
        print(f"[external-source-delay-audit] wrote {md_path}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
