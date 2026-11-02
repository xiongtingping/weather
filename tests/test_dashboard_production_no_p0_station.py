"""The 生产周期 line names a station with no P0 on its open day, its reason
and since which event day (user 2026-10-18 「把看板「生产周期」那行改成直接写出
没有 P0 的站和原因，例如「WSSS 无 P0：OOS 不过气候基线，自 10-15」」)."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import calibration_dashboard as C  # noqa: E402

NOW = datetime(2026, 10, 18, 2, 0, tzinfo=timezone.utc)   # WSSS local 10-18 10:00


def _store(rows: list[tuple[str, str, str]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_station_market_timezone_binding (station_id TEXT, settlement_timezone TEXT)")
    conn.executemany("INSERT INTO calibration_station_market_timezone_binding VALUES (?,?)",
                     [("WSSS", "Asia/Singapore"), ("WMKK", "Asia/Kuala_Lumpur")])
    conn.execute("CREATE TABLE basket_target_ws_scope_serving_state (station_id TEXT, event_date TEXT,"
                 " actionable INTEGER, scope_blocker TEXT)")
    conn.executemany("INSERT INTO basket_target_ws_scope_serving_state VALUES (?,?,0,?)", rows)
    return conn


STATUS = {"production": {"native_ensemble_ingress": {"active_activation_p0_replay": {"failures": [
    {"station_id": "WSSS", "event_date": "2026-10-18",
     "reason": "ContractError:FAIL_CLOSED_NO_P0:native_cross_oos_evaluation_not_passing"},
    {"station_id": "WMKK", "event_date": "2026-10-19",
     "reason": "ContractError:FAIL_CLOSED_NO_P0:native_cross_oos_evaluation_not_passing"},
]}}}}
OPEN = "resident_target_ws_open_scope_rootless"


def test_a_station_rootless_today_is_named_with_reason_and_since() -> None:
    conn = _store([("WSSS", "2026-10-16", OPEN), ("WSSS", "2026-10-17", OPEN), ("WSSS", "2026-10-18", OPEN),
                   ("WSSS", "2026-10-12", OPEN)])          # an older, broken-off run is not "since"
    rows = C._stations_without_p0(STATUS, conn, now=NOW)
    assert rows == [{"station_id": "WSSS", "reason": "OOS 不过气候基线", "since": "2026-10-16"}]


def test_a_station_whose_today_has_p0_is_not_named() -> None:
    # WMKK's 10-19 is refused but its open day (10-18) has a root: not a station out.
    conn = _store([("WMKK", "2026-10-19", "resident_target_ws_future_scope_rootless")])
    assert C._stations_without_p0(STATUS, conn, now=NOW) == []


def test_the_line_leads_with_the_station_and_turns_amber() -> None:
    status = {"production": {"state": "COMMITTED", "committed_scope_count": 82, "blocked_scope_count": 1,
                             "blocked_scope_reasons": {}, "worker_checked_at": NOW.isoformat()},
              "heartbeat_at": NOW.isoformat()}
    out = C._production_health(status, [{"station_id": "WSSS", "reason": "OOS 不过气候基线", "since": "2026-10-16"}])
    assert out["production_note"].startswith("WSSS 无 P0：OOS 不过气候基线，自 10-16")
    assert out["production_health"] in ("amber", "red")
    assert "无 P0" not in C._production_health(status)["production_note"]


def test_the_page_render_reads_the_kept_section_not_the_ssot(tmp_path, monkeypatch) -> None:
    # 2026-10-20 (user 「改」): read in the page render, this query raised
    # PageReadsNoSSOT on every draw from 2026-10-18 01:31Z and the board
    # redrew that payload for two and a half days.
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setattr(C, "PAGE_READ_ONLY", True)
    kept = [{"station_id": "WSSS", "reason": "OOS 不过气候基线", "since": "2026-10-17"}]
    (tmp_path / "stations_without_p0.json").write_text(json.dumps({
        "computed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "code_fingerprint": C._section_fingerprint(C._stations_without_p0_section, ""),
        "value": kept,
    }), encoding="utf-8")

    class _NoSSOT:
        def execute(self, *_a, **_k):
            raise AssertionError("the page render read the SSOT")

    assert C._cached_section(
        "stations_without_p0", C._stations_without_p0_section, _NoSSOT(), _unavailable=[],
    ) == kept
