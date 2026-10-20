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


def test_history_row_records_the_process_footprint_and_host_swap() -> None:
    # 2026-10-20: the mean cycle rose 97 s -> 585 s over one process's 31 hours
    # and a restart reset it; nothing recorded how the footprint grew.
    daemon = _daemon()
    result = {"state": "cycle_committed", "stage_timings_ms": {"decision_loop": 10.0, "cycle_total": 10.0}}
    row = daemon.production_cycle_history_row(result, cycle_index=1, process_started_at_utc="2026-10-20T09:10:17Z", completed_at_utc="2026-10-20T09:20:00Z")
    memory = row["memory"]
    if sys.platform == "darwin":
        assert memory["footprint_mb"] > 0 and memory["resident_mb"] > 0
        assert memory["host_swap_used_mb"] is None or memory["host_swap_used_mb"] >= 0
    else:
        assert memory is None
