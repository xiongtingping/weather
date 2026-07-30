#!/usr/bin/env python3
"""Materialize independent production P0s for the configured board cities."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_multicity_posterior_runtime import (  # noqa: E402
    DEFAULT_ACTIVATION,
    DEFAULT_DB,
    materialize_multicity_production_p0s,
)
from production_probability_v6_activation_contract import validate_v6_activation  # noqa: E402
from bayes_basket_target.et_history import event_day_window  # noqa: E402


CONFIRMATION = "MATERIALIZE_MULTICITY_PRODUCTION_P0"


def _normalize_event_dates(values: list[str] | None) -> list[str]:
    """Require explicit, non-empty target days; never synthesize a forward day."""

    return sorted({str(value).strip() for value in (values or []) if str(value).strip()})


def _event_day_preflight(event_dates: list[str], *, as_of_utc: str) -> tuple[str, list[str]]:
    """Reject invalid or not-yet-started ET days before opening the database."""

    future: list[str] = []
    for event_date in event_dates:
        window = event_day_window(event_date, as_of_utc=as_of_utc)
        state = str(window.get("state") or "")
        if state == "not_started":
            future.append(event_date)
        elif state == "invalid":
            return "event_day_window_invalid", []
    if future:
        return "future_event_day_not_allowed", future
    return "", []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--activation", type=Path, default=DEFAULT_ACTIVATION)
    parser.add_argument("--event-date", action="append", dest="event_dates")
    parser.add_argument("--decision-as-of", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    decision_as_of = args.decision_as_of or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    days = _normalize_event_dates(args.event_dates)
    preflight_reason, blocked_dates = ("event_date_required", []) if not days else _event_day_preflight(
        days, as_of_utc=decision_as_of
    )
    if preflight_reason:
        result = {
            "schema": "materialize_multicity_production_posterior_script_v1",
            "ok": False,
            "state": "blocked",
            "reason": preflight_reason,
            "event_dates": days,
            "blocked_event_dates": blocked_dates,
            "places_orders": False,
        }
    elif not args.apply or args.confirm != CONFIRMATION:
        result = {
            "schema": "materialize_multicity_production_posterior_script_v1",
            "ok": False,
            "state": "blocked",
            "reason": "apply_confirmation_required",
            "event_dates": days,
            "places_orders": False,
        }
    else:
        try:
            activation = validate_v6_activation(json.loads(args.activation.read_text(encoding="utf-8")))
            result = materialize_multicity_production_p0s(
                db_path=args.db,
                activation=activation,
                event_dates=days,
                decision_as_of=decision_as_of,
            )
        except Exception as exc:
            result = {
                "schema": "materialize_multicity_production_posterior_script_v1",
                "ok": False,
                "state": "blocked",
                "reason": f"{type(exc).__name__}:{exc}",
                "places_orders": False,
            }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
