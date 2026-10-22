"""A truth sweep stopped by its budget left scopes unknown, not missing."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
_spec = importlib.util.spec_from_file_location("calibration_dashboard", ROOT / "scripts" / "calibration_dashboard.py")
C = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("calibration_dashboard", C)
_spec.loader.exec_module(C)


def test_scopes_the_pass_never_reached_are_not_settled_days_without_truth(tmp_path, monkeypatch) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sweep = tmp_path / "sweep.json"
    # 2026-10-22: stopped after 10 of 143 open-day scopes.
    sweep.write_text(json.dumps({
        "swept_at_utc": now, "backlog_scopes": 143, "attempted": 10, "not_reached": 133,
        "open_day_skipped": 10, "refused": 0, "deferred_locked": 0, "backlog_after": 143,
        "open_by_day": {"2026-10-22": 10},
    }))
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"started_at": now, "production": {"truth_materialization": {"checked_at_utc": now}}}))
    monkeypatch.setattr(C, "TRUTH_SWEEP_STATUS", sweep)
    monkeypatch.setattr(C, "STATUS_JSON", status)
    out = C.collect_truth_materialization(None, {"rows": []})
    note = json.dumps(out, ensure_ascii=False)
    assert "133 个已结算站日" not in note
