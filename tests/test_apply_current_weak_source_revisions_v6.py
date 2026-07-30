from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scripts.apply_current_weak_source_revisions_v6 import _target_days
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_explicit_event_date_remains_single_target() -> None:
    assert _target_days(event_date="2026-07-17", day_offset=1) == ["2026-07-17"]


def test_default_d1_reconciliation_also_includes_d0() -> None:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    assert _target_days(event_date="", day_offset=1) == [
        today.isoformat(),
        (today + timedelta(days=1)).isoformat(),
    ]


def test_weak_revision_writer_uses_active_generation_not_global_snapshot_state() -> None:
    text = (ROOT / "production_probability_v6_weak_revision_writer.py").read_text(encoding="utf-8")
    bundle_guard = text.split("def _assert_db_bundle", 1)[1].split(
        "def commit_v6_weak_revision_transaction", 1
    )[0]
    assert "v6_weak_revision_predecessor_is_not_latest" in bundle_guard
    assert "v6_weak_revision_previous_snapshot_missing" in bundle_guard
    assert "v6_weak_revision_previous_snapshot_not_active" not in bundle_guard
