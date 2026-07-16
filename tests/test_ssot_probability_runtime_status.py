from __future__ import annotations

import json
import os

from scripts.run_resident_p10_shadow_inbox import _write_runtime_status
from scripts.ssot_probability_runtime_acceptance import _pid_alive


def test_resident_runtime_status_is_atomic_and_process_verifiable(tmp_path) -> None:
    path = tmp_path / "resident-status.json"

    _write_runtime_status(
        path,
        {
            "state": "running",
            "prepared_cycle_id": "p10pc_test",
            "target_identity": "physical_weather_day_v1:ZUUU:2026-07-14",
            "event_date": "2026-07-14",
            "processed_file_count": 0,
        },
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == "resident_p10_shadow_runtime_status_v1"
    assert payload["resident_pid"] == os.getpid()
    assert payload["places_orders"] is False
    assert _pid_alive(payload["resident_pid"]) is True
