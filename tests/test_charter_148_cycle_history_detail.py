"""Charter 148: the cycle history keeps the stage order and each top span's detail."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _daemon():
    spec = importlib.util.spec_from_file_location("weather_basket_yes_daemon_c148", ROOT / "scripts" / "weather_basket_yes_daemon.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_history_row_keeps_the_order_the_stages_ran_and_the_span_detail() -> None:
    daemon = _daemon()
    result = {
        "state": "cycle_committed_partial",
        "stage_timings_ms": {"activation": 5.0, "registry_ingress": 900.0, "early_p1": 60000.0, "decision_loop": 30000.0, "unattributed": 1.0, "cycle_total": 91000.0},
        "stage_span_ms": {
            "decision_loop_quote_refresh": {"count": 40, "total_ms": 595000.0, "max_ms": 16000.0, "slowest": "2026-09-18"},
            "p1_load_observations": {"count": 45, "total_ms": 9000.0, "max_ms": 900.0, "slowest": "ZUUU|2026-09-18"},
        },
    }
    row = daemon.production_cycle_history_row(result, cycle_index=3, process_started_at_utc="2026-09-18T06:04:38Z", completed_at_utc="2026-09-18T06:20:00Z")
    assert row["stage_sequence"] == ["activation", "registry_ingress", "early_p1", "decision_loop"]
    assert row["top_span_detail"]["decision_loop_quote_refresh"] == {"count": 40, "max_ms": 16000.0, "slowest": "2026-09-18"}
    assert row["top_span_detail"]["p1_load_observations"]["slowest"] == "ZUUU|2026-09-18"
