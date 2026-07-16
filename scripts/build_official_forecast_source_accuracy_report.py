#!/usr/bin/env python3
"""Score only strict Tier-A forecast revisions against closed physical X.

Legacy/aggregator score files are intentionally not read here.  This report is
the formal replacement lane: it remains research-only until a separate weak
likelihood artifact completes walk-forward evaluation and approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
RESEARCH_ROOT = ROOT / "data" / "research" / "joint_forecast_weak_likelihood"
DEFAULT_EVENT_ROOT = RESEARCH_ROOT / "station_day_candidates"
SCHEMA_VERSION = "official_forecast_source_accuracy_report_v1"
MIN_RANKABLE_EVENT_DAYS = 30
ZONE = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _season(event_date: str) -> str:
    try:
        month = int(event_date[5:7])
    except (TypeError, ValueError):
        return "未知"
    return "春" if month in (3, 4, 5) else "夏" if month in (6, 7, 8) else "秋" if month in (9, 10, 11) else "冬"


def _horizon(cutoff: datetime) -> str:
    # The local midnight snapshot is the frozen D1 baseline; every later
    # same-day revision is D0.  This intentionally does not infer horizon
    # from a provider's nominal run time.
    local = cutoff.astimezone(ZONE)
    return "D1" if local.hour == 0 and local.minute == 0 else "D0"


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    errors = [float(row["error_c"]) for row in rows]
    days = sorted({_text(row.get("event_date")) for row in rows if _text(row.get("event_date"))})
    day_count = len(days)
    return {
        "sample_count": len(rows),
        "event_day_count": day_count,
        "event_date_from": days[0] if days else "",
        "event_date_through": days[-1] if days else "",
        "physical_x_mae_c": _mean([abs(value) for value in errors]),
        "mean_bias_c": _mean(errors),
        "rmse_c": math.sqrt(_mean([value * value for value in errors]) or 0.0) if errors else None,
        "ranking_state": "eligible_for_research_ranking" if day_count >= MIN_RANKABLE_EVENT_DAYS else "insufficient_independent_event_days",
        "ranking_na_reason": "" if day_count >= MIN_RANKABLE_EVENT_DAYS else f"event_day_count_below_{MIN_RANKABLE_EVENT_DAYS}",
    }


def _candidate_revision(candidate: Mapping[str, Any]) -> int:
    try:
        revision = int(candidate.get("candidate_revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    if revision > 0:
        return revision
    return 2 if isinstance(candidate.get("supersedes"), Mapping) else 1


def _valid_candidate_chain(
    *,
    path: Path,
    candidate: Mapping[str, Any],
    event_root: Path,
    cache: dict[Path, bool],
    visiting: set[Path],
) -> bool:
    """Require every successor to bind to the immediate immutable predecessor."""

    resolved = path.resolve()
    if resolved in cache:
        return cache[resolved]
    if resolved in visiting:
        cache[resolved] = False
        return False
    visiting.add(resolved)
    supersedes = candidate.get("supersedes")
    revision = _candidate_revision(candidate)
    if not isinstance(supersedes, Mapping):
        valid = revision == 1
    else:
        predecessor_text = _text(supersedes.get("path"))
        predecessor = Path(predecessor_text).expanduser().resolve() if predecessor_text else None
        try:
            root = event_root.resolve()
            predecessor_in_root = predecessor is not None and (predecessor == root or root in predecessor.parents)
        except OSError:
            predecessor_in_root = False
        predecessor_candidate: Mapping[str, Any] = {}
        if predecessor_in_root and predecessor is not None and predecessor.is_file():
            try:
                value = json.loads(predecessor.read_text(encoding="utf-8"))
                predecessor_candidate = value if isinstance(value, Mapping) else {}
            except (OSError, ValueError):
                predecessor_candidate = {}
        declared_predecessor_revision = supersedes.get("candidate_revision")
        try:
            expected_predecessor_revision = int(declared_predecessor_revision)
        except (TypeError, ValueError):
            # v2 corrections predate explicit revision fields.  They still
            # bind path+SHA and can only supersede a first-generation file.
            expected_predecessor_revision = 1 if _text(candidate.get("schema")) != "station_day_weak_physical_training_materialization_v3" else 0
        valid = bool(
            predecessor_candidate
            and _text(predecessor_candidate.get("event_date"))[:10] == _text(candidate.get("event_date"))[:10]
            and _text(predecessor_candidate.get("materialization_sha256")) == _text(supersedes.get("materialization_sha256"))
            and _candidate_revision(predecessor_candidate) == expected_predecessor_revision
            and revision == _candidate_revision(predecessor_candidate) + 1
            and predecessor is not None
            and _valid_candidate_chain(
                path=predecessor,
                candidate=predecessor_candidate,
                event_root=event_root,
                cache=cache,
                visiting=visiting,
            )
        )
    visiting.discard(resolved)
    cache[resolved] = valid
    return valid


def _load_candidate_events(event_root: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    excluded: dict[str, int] = defaultdict(int)
    candidates_by_day: dict[str, tuple[int, Path, Mapping[str, Any]]] = {}
    chain_cache: dict[Path, bool] = {}
    for path in sorted(event_root.glob("*.json")) if event_root.exists() else ():
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            excluded["candidate_json_unreadable"] += 1
            continue
        event_date = _text(candidate.get("event_date"))[:10] if isinstance(candidate, Mapping) else ""
        if not event_date:
            excluded["candidate_event_date_missing"] += 1
            continue
        if not _valid_candidate_chain(
            path=path,
            candidate=candidate,
            event_root=event_root,
            cache=chain_cache,
            visiting=set(),
        ):
            excluded["candidate_supersede_chain_invalid"] += 1
            continue
        # Corrections are append-only.  Prefer explicit revision; older v2
        # files retain deterministic precedence so they remain auditable.
        precedence = _candidate_revision(candidate)
        current = candidates_by_day.get(event_date)
        if current is None or (precedence, str(path)) > (current[0], str(current[1])):
            if current is not None:
                excluded["superseded_or_duplicate_station_day_candidate"] += 1
            candidates_by_day[event_date] = (precedence, path, candidate)
        else:
            excluded["superseded_or_duplicate_station_day_candidate"] += 1
    for _day, (_precedence, _path, candidate) in sorted(candidates_by_day.items()):
        envelope = candidate.get("weak_training_event_envelope") if isinstance(candidate, Mapping) else {}
        for event in (envelope.get("events") or []) if isinstance(envelope, Mapping) else []:
            if isinstance(event, Mapping):
                rows.append(dict(event))
            else:
                excluded["training_event_not_mapping"] += 1
    return rows, dict(sorted(excluded.items()))


def build_official_forecast_source_accuracy_report(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    excluded: dict[str, int] = defaultdict(int)
    for event in events:
        day = _text(event.get("event_date"))[:10]
        cutoff = _time(event.get("decision_as_of"))
        physical = event.get("physical_x_final") if isinstance(event.get("physical_x_final"), Mapping) else {}
        truth = _number(physical.get("temperature_c"))
        if not day or cutoff is None or truth is None:
            excluded["event_identity_cutoff_or_physical_x_missing"] += 1
            continue
        latest_by_source: dict[str, Mapping[str, Any]] = {}
        for revision in event.get("source_revisions") or []:
            if not isinstance(revision, Mapping):
                excluded["revision_not_mapping"] += 1
                continue
            source = _text(revision.get("source_id"))
            available = _time(revision.get("available_at"))
            peak = _number(revision.get("forecast_peak_c"))
            if not source or available is None or peak is None:
                excluded["revision_identity_or_peak_missing"] += 1
                continue
            if available > cutoff or revision.get("strict_asof_tier_a") is not True or revision.get("response_headers_observed") is not True:
                excluded["revision_not_strict_tier_a_asof"] += 1
                continue
            previous = latest_by_source.get(source)
            if previous is None or _time(previous.get("available_at")) < available:
                latest_by_source[source] = revision
        for source, revision in latest_by_source.items():
            peak = float(revision["forecast_peak_c"])
            groups[(source, _horizon(cutoff), _season(day))].append(
                {
                    "event_date": day,
                    "decision_as_of": cutoff.isoformat().replace("+00:00", "Z"),
                    "error_c": peak - truth,
                    "forecast_peak_c": peak,
                    "physical_x_final_c": truth,
                    "source_revision_id": _text(revision.get("source_revision_id")),
                }
            )
    rows = [
        {"source_id": source, "horizon": horizon, "season": season, **_summary(group)}
        for (source, horizon, season), group in sorted(groups.items())
    ]
    report = {
        "schema": SCHEMA_VERSION,
        "generated_at": _now(),
        "state": "available" if rows else "pending_formal_samples",
        "research_only": True,
        "production_weight_input": False,
        "label": "closed_amsc_physical_x_final",
        "method": {
            "forecast_input": "p1_strict_tier_a_forecast_revision_only",
            "d1_definition": "target_local_midnight_frozen_cutoff",
            "d0_definition": "same_target_day_cutoff_after_local_midnight",
            "bias_definition": "forecast_peak_c_minus_physical_x_final_c",
            "d0_independence_warning": "multiple D0 cutoffs per station day are not independent days; event_day_count governs ranking eligibility",
            "ranking_minimum_independent_event_days": MIN_RANKABLE_EVENT_DAYS,
        },
        "coverage": {
            "training_event_count": len(events),
            "scored_row_count": sum(len(group) for group in groups.values()),
            "distinct_event_days": len({_text(event.get("event_date"))[:10] for event in events if _text(event.get("event_date"))}),
            "excluded_counts": dict(sorted(excluded.items())),
        },
        "rows": rows,
        "guards": {
            "legacy_or_aggregator_forecasts_excluded": True,
            "changes_parameters": False,
            "promotes_artifact": False,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }
    identity = json.dumps({key: value for key, value in report.items() if key != "generated_at"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    report["report_sha256"] = hashlib.sha256(identity.encode()).hexdigest()
    return report


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-root", type=Path, default=DEFAULT_EVENT_ROOT)
    parser.add_argument("--output", type=Path, default=RESEARCH_ROOT / "official_accuracy_by_horizon_season.json")
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    events, load_excluded = _load_candidate_events(args.event_root.expanduser().resolve())
    report = build_official_forecast_source_accuracy_report(events)
    report["coverage"]["candidate_load_excluded_counts"] = load_excluded
    if not args.allow_research_write:
        raise ValueError("research_write_requires_explicit_allow_flag")
    output = args.output.expanduser().resolve()
    root = RESEARCH_ROOT.resolve()
    if output != root and root not in output.parents:
        raise ValueError("official_accuracy_report_output_outside_research_root")
    _atomic_write(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
