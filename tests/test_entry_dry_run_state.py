import unittest
from datetime import datetime, timedelta, timezone

from scan_weather_market import (
    _is_dry_run_entry_shadow,
    _resolve_plain_lcb_risk_reentry_cooldown,
)


class EntryDryRunStateTest(unittest.TestCase):
    def test_dry_run_entry_without_order_or_position_is_shadow_state(self):
        self.assertTrue(
            _is_dry_run_entry_shadow(
                {
                    "state": "entry",
                    "last_order_id": "",
                    "position_size": 0,
                    "last_status": "dry_run",
                    "last_error": "",
                }
            )
        )

    def test_repaired_dry_run_no_order_failure_is_shadow_state(self):
        self.assertTrue(
            _is_dry_run_entry_shadow(
                {
                    "state": "entry_failed",
                    "last_order_id": "",
                    "position_size": None,
                    "last_action": "repair",
                    "last_status": "dry_run",
                    "last_error": "no_order_id",
                }
            )
        )

    def test_real_order_id_is_not_shadow_state(self):
        self.assertFalse(
            _is_dry_run_entry_shadow(
                {
                    "state": "entry",
                    "last_order_id": "0xabc",
                    "position_size": 0,
                    "last_status": "dry_run",
                }
            )
        )

    def test_real_position_is_not_shadow_state(self):
        self.assertFalse(
            _is_dry_run_entry_shadow(
                {
                    "state": "entry_failed",
                    "last_order_id": "",
                    "position_size": 1.25,
                    "last_status": "dry_run",
                }
            )
        )

    def test_real_failure_is_not_shadow_state(self):
        self.assertFalse(
            _is_dry_run_entry_shadow(
                {
                    "state": "entry_failed",
                    "last_order_id": "",
                    "position_size": 0,
                    "last_status": "failed",
                    "last_error": "insufficient_balance",
                }
            )
        )

    def test_plain_lcb_cooldown_ignores_dry_run_exit_shadow(self):
        now = datetime(2026, 5, 5, 4, 0, tzinfo=timezone.utc)
        blocked, meta = _resolve_plain_lcb_risk_reentry_cooldown(
            {
                "state": "exit",
                "position_size": 0,
                "last_order_id": "",
                "last_action": "entry",
                "last_status": "dry_run",
                "last_error": "",
                "last_action_at": (now - timedelta(seconds=30)).isoformat(),
                "exit_reason_codes": '["stop_loss"]',
            },
            now_utc=now,
            cooldown_sec=3600,
        )

        self.assertFalse(blocked)
        self.assertEqual(meta["cooldown_ignored_reason"], "dry_run_no_position_shadow")

    def test_plain_lcb_cooldown_uses_real_exit_action_time(self):
        now = datetime(2026, 5, 5, 4, 0, tzinfo=timezone.utc)
        blocked, meta = _resolve_plain_lcb_risk_reentry_cooldown(
            {
                "state": "exit",
                "position_size": 0,
                "last_order_id": "0xexit",
                "last_action": "exit",
                "last_status": "matched",
                "last_action_at": (now - timedelta(seconds=30)).isoformat(),
                "exit_reason_codes": '["stop_loss"]',
            },
            now_utc=now,
            cooldown_sec=3600,
        )

        self.assertTrue(blocked)
        self.assertEqual(meta["cooldown_anchor_field"], "last_action_at")

    def test_plain_lcb_cooldown_ignores_housekeeping_action_time(self):
        now = datetime(2026, 5, 5, 4, 0, tzinfo=timezone.utc)
        blocked, meta = _resolve_plain_lcb_risk_reentry_cooldown(
            {
                "state": "exit",
                "position_size": 0,
                "last_order_id": "",
                "last_action": "exit_reconcile",
                "last_status": "no_position",
                "last_action_at": (now - timedelta(seconds=30)).isoformat(),
                "exit_reason_codes": '["stop_loss"]',
            },
            now_utc=now,
            cooldown_sec=3600,
        )

        self.assertFalse(blocked)
        self.assertEqual(meta["last_action_at_ignored"], "exit_reconcile")


if __name__ == "__main__":
    unittest.main()
