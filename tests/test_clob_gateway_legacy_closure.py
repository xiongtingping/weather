"""Static closure contract for the target-only local CLOB gateway."""

from __future__ import annotations

import subprocess
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "scripts" / "clob_order_service.mjs"
WRAPPER = ROOT / "scripts" / "clob_order_service.sh"


def test_gateway_closes_legacy_snapshot_and_sequential_deadline_compatibility() -> None:
    source = SERVICE.read_text(encoding="utf-8")

    assert "dynamic_three_leg" not in source
    assert "/dynamic-three-leg-snapshot" not in source
    assert "dynamic_snapshot" not in source
    assert "sequential_bayes" not in source
    assert "target_execution_hard_deadline_at" in source
    assert 'reason: "target_execution_hard_deadline_missing_or_invalid"' in source
    assert 'reason: "target_execution_hard_deadline_expired"' in source
    assert source.count("assertTargetDirectEntryDeadline();") == 2
    assert "observation_to_order_hard_deadline_at" not in source
    assert "signal_to_order_hard_deadline_at" not in source


def test_gateway_wrapper_is_system_only_and_no_live() -> None:
    wrapper = WRAPPER.read_text(encoding="utf-8")

    assert '"${WEATHER_SERVICE_DOMAIN:-}" != "system"' in wrapper
    assert '"${PM_TARGET_EXECUTION_MODE:-}" != "production_no_live"' in wrapper
    assert "system_domain_required" in wrapper
    assert "production_no_live_required" in wrapper
    assert "/bin/sleep" not in wrapper
    assert "reported_trajectory_core_only" not in wrapper
    assert "WEATHER_SERVICE_DOMAIN=user" not in wrapper


def test_target_direct_entry_deadline_is_explicit_and_fail_closed(tmp_path: Path) -> None:
    script = f"""
      import {{ targetDirectEntryHardDeadline }} from {json.dumps(SERVICE.as_uri())};
      const direct = {{direct_target_order_context:true, side:'BUY'}};
      const missing = targetDirectEntryHardDeadline(direct, Date.parse('2026-07-27T00:00:00Z'));
      const expired = targetDirectEntryHardDeadline(
        {{...direct, target_execution_hard_deadline_at:'2026-07-26T23:59:59Z'}},
        Date.parse('2026-07-27T00:00:00Z'),
      );
      const exactBoundary = targetDirectEntryHardDeadline(
        {{...direct, target_execution_hard_deadline_at:'2026-07-27T00:00:00Z'}},
        Date.parse('2026-07-27T00:00:00Z'),
      );
      const valid = targetDirectEntryHardDeadline(
        {{...direct, target_execution_hard_deadline_at:'2026-07-27T00:00:01Z'}},
        Date.parse('2026-07-27T00:00:00Z'),
      );
      if (missing.ok || missing.reason !== 'target_execution_hard_deadline_missing_or_invalid') process.exit(11);
      if (expired.ok || expired.reason !== 'target_execution_hard_deadline_expired') process.exit(12);
      if (exactBoundary.ok || exactBoundary.reason !== 'target_execution_hard_deadline_expired') process.exit(14);
      if (!valid.ok || valid.required !== true) process.exit(13);
    """
    environment = {
        **os.environ,
        "PM_DATA_DIR": str(tmp_path),
        "PM_ARCHIVE_DB": str(tmp_path / "archive.db"),
    }
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        cwd=ROOT,
        env=environment,
    )


def test_gateway_sources_are_syntax_valid() -> None:
    subprocess.run(["node", "--check", str(SERVICE)], check=True, cwd=ROOT)
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True, cwd=ROOT)
