import argparse
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "awc_source_health_audit",
    ROOT / "scripts" / "awc_source_health_audit.py",
)
audit_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit_module)


def _iso(offset_sec: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_sec)).isoformat()


def _args(log: Path, state: Path) -> argparse.Namespace:
    return argparse.Namespace(
        log=str(log),
        state=str(state),
        lookback_min=30,
        tail_bytes=100_000,
        max_latest_poll_age_sec=90,
        max_latest_state_age_sec=90,
        max_latest_success_age_sec=600,
        warn_success_rate=0.35,
        warn_consecutive_failures=5,
        min_failure_coverage_rate=0.95,
    )


def _write_log(path: Path, entries: list[tuple[str, dict]]) -> None:
    path.write_text(
        "\n".join(f"{ts} {json.dumps(payload, separators=(',', ':'))}" for ts, payload in entries) + "\n",
        encoding="utf-8",
    )


def test_uncovered_awc_failure_is_warning_after_latest_success(tmp_path: Path) -> None:
    log = tmp_path / "poller.log"
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"generated_at": _iso(-1)}), encoding="utf-8")
    _write_log(
        log,
        [
            (
                _iso(-30),
                {
                    "awc": {"ok": False, "status": "error", "error": "ReadTimeout"},
                    "posterior_target_fast_precheck": {"ok": False, "status": ""},
                },
            ),
            (
                _iso(-2),
                {
                    "awc": {"ok": True, "status": "skipped"},
                    "posterior_target_fast_precheck": {"ok": True, "status": "unchanged"},
                },
            ),
        ],
    )

    report = audit_module.audit(_args(log, state))

    assert report["ok"] is True
    assert report["blockers"] == []
    assert "awc_failure_not_covered_by_posterior_fast_precheck_recovered" in report["warnings"]
    assert report["details"]["failure_coverage_recovered"] is True


def test_uncovered_awc_failure_still_blocks_when_latest_poll_failed(tmp_path: Path) -> None:
    log = tmp_path / "poller.log"
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"generated_at": _iso(-1)}), encoding="utf-8")
    _write_log(
        log,
        [
            (
                _iso(-30),
                {
                    "awc": {"ok": True, "status": "success"},
                    "posterior_target_fast_precheck": {"ok": True, "status": "unchanged"},
                },
            ),
            (
                _iso(-2),
                {
                    "awc": {"ok": False, "status": "error", "error": "ReadTimeout"},
                    "posterior_target_fast_precheck": {"ok": False, "status": ""},
                },
            ),
        ],
    )

    report = audit_module.audit(_args(log, state))

    assert report["ok"] is False
    assert "awc_failure_not_covered_by_posterior_fast_precheck" in report["blockers"]
    assert report["details"]["failure_coverage_recovered"] is False
