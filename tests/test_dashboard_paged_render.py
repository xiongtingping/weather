"""The paged render (user ruling 2026-09-24: 「能分区分页渲染吗？不要一次性，找生产的空隙」).

The render used to be one read of the whole store.  The last one to finish
(2026-09-23 06:53Z) took 7,337 s, every run after 13:18Z was killed at the
1,500 s deadline, and two were killed by hand because their reads held the
WAL the resident needed truncated before it could reopen its writer.  The
page kept an eighteen-hour-old payload with red lamps on it.

What is pinned here: a section is a page of its own -- written when done,
reused by the next run, served past its lifetime once the run's budget is
spent -- it starts reading only in a production gap, and it gives its read
up when the WAL crosses the resident's maintenance line.  The sections where
a stale answer is a wrong answer are never paged.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _safety_rule_sets() -> tuple[set[str], set[str]]:
    """The two sets the section cache's safety rules pin, read from that file."""

    import ast

    tree = ast.parse((ROOT / "tests" / "test_dashboard_section_cache.py").read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("HEALTH_BANNER_SOURCES", "LIVE_SECTIONS"):
                found[node.targets[0].id] = set(ast.literal_eval(node.value))
    return found["HEALTH_BANNER_SOURCES"], found["LIVE_SECTIONS"]


HEALTH_BANNER_SOURCES, LIVE_BOOK_SECTIONS = _safety_rule_sets()


@pytest.fixture()
def paged(tmp_path, monkeypatch):
    db = tmp_path / "ssot.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path / "sections")
    monkeypatch.setattr(C, "SECTION_CACHE_ENABLED", True)
    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {"slow_moving": 3600})
    monkeypatch.setattr(C, "STATUS_JSON", tmp_path / "status.json")
    monkeypatch.setattr(C, "RENDER_GAP_POLL_SEC", 0.01)
    C._SECTION_CACHE_LOG.clear()
    C._paged_run_start(db)
    yield tmp_path
    C._PAGED_RUN.clear()
    C._SECTION_CACHE_LOG.clear()


def _collector(calls, value="v"):
    def collect(conn):
        calls.append(value)
        return {"value": value, "n": len(calls)}
    return collect


def _wal(tmp_path: Path, size: int) -> None:
    (tmp_path / "ssot.db-wal").write_bytes(b"\0" * size)


def _status(tmp_path: Path, **fields) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    (tmp_path / "status.json").write_text(
        json.dumps({"heartbeat_at": now, **fields}), encoding="utf-8"
    )


def test_a_section_without_a_lifetime_becomes_a_page_the_next_run_reuses(paged) -> None:
    calls: list[str] = []
    first = C._cached_section("analytics", _collector(calls), None)
    assert (paged / "sections" / "analytics.json").exists(), "the page must be written when done"
    C._SECTION_CACHE_LOG.clear()
    second = C._cached_section("analytics", _collector(calls), None)
    assert calls == ["v"], "the next run must resume from the page, not recompute"
    assert first == second
    assert C._SECTION_CACHE_LOG["analytics"]["reused"] is True


@pytest.mark.parametrize("name", sorted(C.RENDER_EVERY_RUN_SECTIONS))
def test_the_every_run_sections_are_never_paged(paged, name) -> None:
    calls: list[str] = []
    C._cached_section(name, _collector(calls), None)
    C._cached_section(name, _collector(calls), None)
    assert calls == ["v", "v"]
    assert not (paged / "sections" / f"{name}.json").exists()


def test_the_every_run_set_covers_both_safety_rules() -> None:
    """The banner's inputs and the live book, by their section names."""

    payload_to_section = {"stages": "band_stages", "truth": "truth_materialization"}
    banner = {
        payload_to_section.get(key, key) for key in HEALTH_BANNER_SOURCES
    } - {"worker"}  # read from the status file, not a section
    missing = (banner | set(LIVE_BOOK_SECTIONS)) - C.RENDER_EVERY_RUN_SECTIONS
    assert not missing, f"{sorted(missing)} would be served from a stale page"


def test_a_spent_budget_serves_the_last_page_and_says_so(paged, monkeypatch) -> None:
    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    # Past its lifetime, and the run's budget is gone.
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", -1.0)
    C._SECTION_CACHE_LOG.clear()
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert calls == ["old"], "a spent budget must not start another read"
    assert value["value"] == "old"
    entry = C._SECTION_CACHE_LOG["slow_moving"]
    assert entry["stale"] is True and entry["reused"] is True
    assert entry["stale_reason"] == "budget"
    assert C._PAGED_RUN["stale"] == {"slow_moving": "budget"}


def test_a_page_is_never_served_across_a_code_change(paged, monkeypatch) -> None:
    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    # A code change moves both: the full key and the code's own.
    blob["fingerprint"] = "retired"
    blob["code_fingerprint"] = "retired"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", -1.0)
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert value["value"] == "new", "a page from other code has another shape"


def test_a_section_waits_for_the_production_cycle_to_finish(paged, monkeypatch) -> None:
    _status(paged, production_inflight={"elapsed_sec": 30})
    seen: list[str | None] = []
    real = C._production_gap_blocker

    def blocker(wal_line=None):
        verdict = real(wal_line)
        seen.append(verdict)
        if len(seen) == 3:
            _status(paged)  # the cycle finished
        return verdict

    monkeypatch.setattr(C, "_production_gap_blocker", blocker)
    calls: list[str] = []
    C._cached_section("analytics", _collector(calls), None)
    assert seen[:3] == ["production cycle in flight"] * 3 and seen[-1] is None
    assert calls == ["v"]


def test_a_stale_status_file_does_not_hold_the_render(paged) -> None:
    (paged / "status.json").write_text(json.dumps({
        "heartbeat_at": "2026-09-01T00:00:00Z",
        "production_inflight": {"elapsed_sec": 30},
    }))
    assert C._production_gap_blocker() is None


def test_an_oversized_wal_is_not_a_gap(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)
    _wal(paged, 4096)
    assert C._production_gap_blocker() == "wal 4096"


def test_a_read_yields_the_wal_back_mid_statement(paged, monkeypatch) -> None:
    """The progress handler interrupts; the last page is served, not cached over."""

    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)
    crossing = {"now": False}

    # One collector for both runs: a page is only ever served to the code
    # that wrote it.
    def long_read(connection):
        if crossing["now"]:
            _wal(paged, 4096)  # the WAL crosses the line while we read
            C._PAGED_RUN["wal_checked"] = 0.0
        return {"value": connection.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
            " WHERE i < 200000) SELECT count(*) FROM n"
            if not crossing["now"] else
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
            " WHERE i < 50000000) SELECT count(*) FROM n"
        ).fetchone()[0]}

    C._cached_section("slow_moving", long_read, conn)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    crossing["now"] = True
    C._SECTION_CACHE_LOG.clear()
    value = C._cached_section("slow_moving", long_read, conn)
    assert value["value"] == 200000, "the last page, not a half-read"
    assert C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"] == "yielded"
    assert C._PAGED_RUN["yields"] == 1
    assert C._PAGED_RUN["armed"] is False, "the handler must be disarmed after the section"
    kept = json.loads((paged / "sections" / "slow_moving.json").read_text())
    assert kept["value"]["value"] == 200000


def test_a_collector_that_swallows_the_interrupt_is_never_cached(paged, monkeypatch) -> None:
    """With no page to serve instead, the run steps aside -- it never shows a half-read."""

    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)

    def degrading(connection):
        _wal(paged, 4096)
        C._PAGED_RUN["wal_checked"] = 0.0
        try:
            connection.execute(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
                " WHERE i < 50000000) SELECT count(*) FROM n"
            ).fetchone()
        except sqlite3.OperationalError:
            return {"available": False}
        return {"available": True}

    with pytest.raises(C.RenderYielded):
        C._cached_section("analytics", degrading, conn)
    assert not (paged / "sections" / "analytics.json").exists(), (
        "a degraded value must never become a page"
    )


def _long_count(connection):
    return connection.execute(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
        " WHERE i < 50000000) SELECT count(*) FROM n"
    ).fetchone()[0]


def test_an_every_run_section_never_starts_reading_past_the_line(paged, monkeypatch) -> None:
    """02:12Z: ``members`` read 254 s beside a truncate that came back busy."""

    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    _wal(paged, 4096)
    calls: list[str] = []
    with pytest.raises(C.RenderYielded, match="members: wal 4096"):
        C._cached_section("members", _collector(calls), None)
    assert calls == [], "not one read past the line"


def test_an_every_run_section_gives_its_read_up_at_the_line(paged, monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)

    def members(connection):
        _wal(paged, 4096)  # crosses mid-read
        C._PAGED_RUN["wal_checked"] = 0.0
        return _long_count(connection)

    with pytest.raises(C.RenderYielded, match="members: interrupted"):
        C._cached_section("members", members, conn)
    assert C._PAGED_RUN["armed"] is False


def test_a_cycle_in_flight_only_delays_an_every_run_section(paged, monkeypatch) -> None:
    """Disk contention is tolerable for a section with nothing else to serve."""

    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    _status(paged, production_inflight={"elapsed_sec": 30})
    calls: list[str] = []
    C._cached_section("members", _collector(calls), None)
    assert calls == ["v"]


def test_a_paged_section_with_no_page_yields_the_run_to_an_oversized_wal(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    _wal(paged, 4096)
    calls: list[str] = []
    with pytest.raises(C.RenderYielded, match="analytics: wal 4096"):
        C._cached_section("analytics", _collector(calls), None)
    assert calls == []


def test_a_yielded_run_keeps_the_page_and_exits_clean(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ssot.db"
    sqlite3.connect(db).close()
    out = tmp_path / "page.html"

    def yielding(_db):
        raise C.RenderYielded("members: wal 4096")

    monkeypatch.setattr(C, "build_payload", yielding)
    # main() sets these globals; registered first so teardown restores them.
    monkeypatch.setattr(C, "PAGED_RENDER", C.PAGED_RENDER)
    monkeypatch.setattr(C, "SECTION_CACHE_ENABLED", C.SECTION_CACHE_ENABLED)
    monkeypatch.setattr(sys, "argv", ["calibration_dashboard.py", "--db", str(db), "--html-out", str(out)])
    assert C.main() == 0
    assert not out.exists(), "a yielded run writes nothing; the previous page stays"


def test_a_section_fed_by_other_sections_is_keyed_on_them(paged) -> None:
    calls: list[str] = []

    def derived(conn, members):
        calls.append(members[0])
        return {"from": members[0]}

    assert C._cached_section("gaps_like", derived, None, ["a"])["from"] == "a"
    assert C._cached_section("gaps_like", derived, None, ["b"])["from"] == "b"
    assert C._cached_section("gaps_like", derived, None, ["b"])["from"] == "b"
    assert calls == ["a", "b"], "new inputs recompute; the same inputs reuse"


def test_the_every_run_sections_leave_room_for_themselves(paged, monkeypatch) -> None:
    """A paged section yields its turn to the every-run sections still to come."""

    C._PAGED_RUN["every_run_costs"] = {"live_days": 500.0}
    monkeypatch.setattr(C, "RENDER_EVERY_RUN_DEFAULT_COST_SEC", 0.0)
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", 400.0)
    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    blob["build_sec"] = 10.0
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert value["value"] == "old", "live_days' 500 s is spoken for"
    C._SECTION_CACHE_LOG["live_days"] = {"build_sec": 1.0}
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert value["value"] == "new", "once live_days has run, the room is back"


def test_only_the_timed_page_is_paged() -> None:
    """``--json`` and the server keep the one-pass build."""

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "PAGED_RENDER = bool(args.html_out) and SECTION_CACHE_ENABLED" in source
    assert C.PAGED_RENDER is False


def test_every_run_costs_are_recorded_as_each_one_finishes(paged) -> None:
    """A run killed before its end must still leave the next one its reserve."""

    C._cached_section("members", lambda conn: {"ok": True}, None)
    saved = json.loads((paged / "sections" / C.RENDER_EVERY_RUN_COSTS).read_text())
    assert "members" in saved, "recorded when the section finished, not at the end"


def test_an_every_run_section_with_no_record_still_gets_room(paged) -> None:
    C._PAGED_RUN["every_run_costs"] = {}
    reserve = C._every_run_reserve_sec()
    assert reserve == C.RENDER_EVERY_RUN_DEFAULT_COST_SEC * len(C.RENDER_EVERY_RUN_SECTIONS)
    C._SECTION_CACHE_LOG["live_days"] = {"build_sec": 1.0}
    assert C._every_run_reserve_sec() == reserve - C.RENDER_EVERY_RUN_DEFAULT_COST_SEC


def test_a_section_with_a_page_never_waits_for_a_gap(paged, monkeypatch) -> None:
    """No gap open right now: serve the page and move on, don't sit out the budget."""

    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    _status(paged, production_inflight={"elapsed_sec": 30})
    slept: list[float] = []
    monkeypatch.setattr(C.time, "sleep", lambda sec: slept.append(sec))
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert value["value"] == "old" and slept == []
    assert C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"] == "no gap"


def test_a_section_with_a_page_reads_in_an_open_gap(paged) -> None:
    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    _status(paged)  # no cycle in flight
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert value["value"] == "new"


def test_a_new_section_that_declares_what_to_show_does_not_stop_the_page(paged, monkeypatch) -> None:
    """member_depth's first read under heavy writes (03:17Z) yielded the whole run."""

    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)

    def depth(connection):
        _wal(paged, 4096)
        C._PAGED_RUN["wal_checked"] = 0.0
        return _long_count(connection)

    assert C._cached_section("member_depth_like", depth, conn, _unavailable={}) == {}
    entry = C._SECTION_CACHE_LOG["member_depth_like"]
    assert entry["unavailable"] is True and entry["cached"] is False
    assert not (paged / "sections" / "member_depth_like.json").exists()
    # Past the line before reading: not read at all.
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    calls: list[str] = []
    assert C._cached_section("other_new", _collector(calls), None, _unavailable=[]) == []
    assert calls == []


def test_an_every_run_section_cannot_declare_its_way_out(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    _wal(paged, 4096)
    with pytest.raises(C.RenderYielded):
        C._cached_section("members", _collector([]), None, _unavailable=[])


def test_the_run_shares_one_wait_budget(paged, monkeypatch) -> None:
    """03:30Z: 120 s per pageless section, no total, through back-to-back cycles."""

    _status(paged, production_inflight={"elapsed_sec": 30})
    monkeypatch.setattr(C, "RENDER_GAP_POLL_SEC", 50.0)
    slept: list[float] = []
    monkeypatch.setattr(C.time, "sleep", lambda sec: slept.append(sec))
    C._PAGED_RUN["gap_wait_left"] = 150.0
    C._cached_section("members", _collector([]), None)      # waits 120
    C._cached_section("gaps_like", _collector([]), None)    # 30 left
    C._cached_section("live_days", _collector([]), None)    # nothing left
    assert sum(slept) == pytest.approx(150.0)
    assert C._PAGED_RUN["gap_wait_left"] == 0.0


def test_a_page_whose_inputs_moved_is_still_a_page(paged, monkeypatch) -> None:
    """A date in the key: every morning it had no page and was read whole."""

    calls: list[str] = []
    collector = _collector(calls, "yesterday")
    C._cached_section("slow_moving", collector, None, _salt="2026-09-23")
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", -1.0)
    value = C._cached_section("slow_moving", collector, None, _salt="2026-09-24")
    assert value["value"] == "yesterday" and calls == ["yesterday"]
    reason = C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"]
    assert reason == "budget, inputs moved"


def test_a_page_with_moved_inputs_is_never_fresh(paged) -> None:
    """Past the budget it is a stale page; inside the budget it is recomputed."""

    calls: list[str] = []
    collector = _collector(calls, "x")
    C._cached_section("slow_moving", collector, None, _salt="a")
    C._cached_section("slow_moving", collector, None, _salt="b")
    assert calls == ["x", "x"]


def test_a_paged_run_builds_the_live_book_once(paged, monkeypatch) -> None:
    builds: list[int] = []
    monkeypatch.setattr(C.live_book, "build", lambda conn: builds.append(1) or {"legs": []})
    first = C._render_live_book(None)
    second = C._render_live_book(None)
    assert builds == [1] and first is second


def test_outside_a_paged_run_the_live_book_is_the_modules_to_cache(monkeypatch) -> None:
    C._PAGED_RUN.clear()
    builds: list[int] = []
    monkeypatch.setattr(C.live_book, "build", lambda conn: builds.append(1) or {})
    C._render_live_book(None)
    C._render_live_book(None)
    assert builds == [1, 1]


def test_a_read_in_progress_runs_on_between_the_two_lines(paged, monkeypatch) -> None:
    """「提线」: no read STARTS past 256 MiB; one in progress gives way only at 384."""

    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 8192)
    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)

    def members(connection):
        _wal(paged, 4096)  # past the start line, under the yield line
        C._PAGED_RUN["wal_checked"] = 0.0
        return connection.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
            " WHERE i < 300000) SELECT count(*) FROM n"
        ).fetchone()[0]

    assert C._cached_section("members", members, conn) == 300000
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    # A section with nothing else to serve may also START under the yield
    # line (06:22Z: live_readiness at 305 MiB took the whole run down) ...
    calls: list[str] = []
    C._cached_section("chain", _collector(calls), None)
    assert calls == ["v"]
    # ... one with something to serve instead does not start past 256.
    assert C._cached_section("new_view", _collector(calls), None, _unavailable={}) == {}
    assert calls == ["v"]
    # And past the yield line nothing without a fallback starts at all.
    _wal(paged, 16384)
    with pytest.raises(C.RenderYielded, match="live_readiness: wal 16384"):
        C._cached_section("live_readiness", _collector(calls), None)


def test_the_yield_line_sits_under_productions_wall() -> None:
    from bayes_basket_target.resource_bounds import MAX_TARGET_WAL_BYTES

    assert C.RENDER_MAX_WAL_BYTES < C.RENDER_YIELD_WAL_BYTES < MAX_TARGET_WAL_BYTES


def test_the_cycle_lamp_takes_the_latency_it_is_handed(monkeypatch) -> None:
    """The journal is the lamp; the medians are their own paged section."""

    def forbidden(conn):
        raise AssertionError("the medians were handed in; do not read them again")

    monkeypatch.setattr(C, "_report_to_probability_latency", forbidden)
    out = C.collect_cycle_time(sqlite3.connect(":memory:"), {"unavailable": True})
    assert out["latency"] == {"unavailable": True}


def test_the_truth_lamp_stands_when_its_table_is_unread(monkeypatch, tmp_path) -> None:
    """Its light is the retry engines'; an unread coverage table must not hide it."""

    monkeypatch.setattr(C, "TRUTH_SWEEP_STATUS", tmp_path / "missing.json")
    monkeypatch.setattr(C, "STATUS_JSON", tmp_path / "missing_status.json")
    out = C.collect_truth_materialization(
        sqlite3.connect(":memory:"), {"rows": [], "unavailable": True}
    )
    assert out["coverage"] == [] and out["coverage_unavailable"] is True
    assert out["health"] in ("amber", "red")
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "(T.coverage || []).length || T.coverage_unavailable" in source


def test_live_readiness_takes_the_difficulty_it_is_handed(monkeypatch) -> None:
    """The gates are the live book's; the two difficulty readings are a paged section."""

    def forbidden(conn):
        raise AssertionError("handed in; do not read every truth row again")

    monkeypatch.setattr(C, "collect_station_difficulty", forbidden)
    monkeypatch.setattr(C, "_collect_live_orders", lambda conn: {"pnl": {"by_station": []}})
    monkeypatch.setattr(C, "_render_live_book", lambda conn: {"settlements": []})
    monkeypatch.setattr(C.live_book, "station_days", lambda book: [])
    monkeypatch.setattr(
        C.live_book, "station_quality",
        lambda s, d: {"stations": {}, "split_event_date": None, "thresholds": {}},
    )
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth"
        " (station_id TEXT, local_date TEXT, official_tmax_c REAL)"
    )
    out = C.collect_live_readiness(
        conn, [{"station": "KLGA"}],
        {"climate_jump": {"KLGA": 1.234}, "shape_gap": {}},
    )
    row = next(r for r in out["stations"] if r["station"] == "KLGA")
    assert row["climate_jump_c"] == 1.23 and row["shape_gap_buckets"] is None


def test_live_selection_reads_the_runs_one_book(paged, monkeypatch) -> None:
    builds: list[int] = []
    monkeypatch.setattr(C.live_book, "build", lambda conn: builds.append(1) or {"legs": []})
    seen: list[object] = []
    monkeypatch.setattr(
        C.live_selection, "build",
        lambda conn, book=None, closed=None: seen.append(book) or {},
    )
    C._render_live_book(None)
    C._build_live_selection(None)
    assert builds == [1] and seen and seen[0] is C._PAGED_RUN["live_book"]


def test_a_closed_station_days_market_slug_is_read_once(paged) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_market_registry (station_id TEXT,"
        " event_date TEXT, slug TEXT, refreshed_at TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES"
        " ('KLGA','2026-09-01','nyc-sep-1','2026-09-01T00:00:00Z')"
    )
    rows = [{"station_id": "KLGA", "event_date": "2026-09-01"}]
    assert C._live_market_slugs(conn, rows, archive=paged / "none.db") == {"KLGA|2026-09-01": "nyc-sep-1"}
    conn.execute("DELETE FROM basket_target_market_registry")
    # Kept: the registry is not asked again for a settled day.
    assert C._live_market_slugs(conn, rows, archive=paged / "none.db") == {"KLGA|2026-09-01": "nyc-sep-1"}


def test_an_open_station_days_slug_is_always_read(paged) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_market_registry (station_id TEXT,"
        " event_date TEXT, slug TEXT, refreshed_at TEXT)"
    )
    today = C._now().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES (?,?,?,?)",
        ("KLGA", today, "old", "2026-09-01T00:00:00Z"),
    )
    rows = [{"station_id": "KLGA", "event_date": today}]
    C._live_market_slugs(conn, rows, archive=paged / "none.db")
    conn.execute("UPDATE basket_target_market_registry SET slug='relisted'")
    assert C._live_market_slugs(conn, rows, archive=paged / "none.db")[f"KLGA|{today}"] == "relisted"


def test_past_the_budget_a_pageless_section_with_a_stand_in_is_not_read(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", -1.0)
    calls: list[str] = []
    assert C._cached_section("slow_moving", _collector(calls), None, _unavailable={}) == {}
    assert calls == [] and C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"] == "budget"
    # Without a stand-in it still has to be read.
    C._cached_section("other_new", _collector(calls), None)
    assert calls == ["v"]


def test_a_live_baskets_plan_is_found_in_its_window_or_by_the_whole_day() -> None:
    """Window first (the plans compiled just before the decision), day second."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE auto_trade_decisions (decision_id TEXT, station_id TEXT,"
        " event_date TEXT, details_json TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_p0_preselection_plan (station_id TEXT,"
        " event_date TEXT, plan_json TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX basket_p0_preselection_created_idx"
        " ON basket_p0_preselection_plan(created_at)"
    )

    def plan(sha, center, at):
        conn.execute(
            "INSERT INTO basket_p0_preselection_plan VALUES (?,?,?,?)",
            ("KLGA", "2026-09-10", json.dumps({"compiled_plan": {
                "production_plan_sha256": sha, "plan": {"center_bucket": center},
            }}), at),
        )

    def decision(did, sha, at):
        conn.execute(
            "INSERT INTO auto_trade_decisions VALUES (?,?,?,?,?)",
            (did, "KLGA", "2026-09-10",
             json.dumps({"production_plan_sha256": sha}), at),
        )

    plan("near", "70", "2026-09-09T11:50:00Z")
    decision("d1", "near", "2026-09-09T12:00:00Z")
    plan("far", "72", "2026-09-09T02:00:00Z")      # hours before its decision
    decision("d2", "far", "2026-09-09T12:00:00Z")
    state = C._live_basket_plan_state(conn, [{"decision_id": "d1"}, {"decision_id": "d2"}])
    assert state["d1"]["center"] == "70"
    assert state["d2"]["center"] == "72", "outside every window: the whole day is read"


def test_a_closed_station_days_divergence_reading_is_kept(paged, monkeypatch) -> None:
    """Keyed on the day, the first fill's decision and the held buckets."""

    day = "2026-09-01"
    book = {
        "legs": [{"station_id": "KLGA", "event_date": day, "bucket": "70",
                  "filled_size": 5, "decision_at_utc": "2026-08-31T12:00:00Z",
                  "created_at": "2026-08-31T12:00:00Z"}],
        "settlements": [{"station_id": "KLGA", "event_date": day, "pnl_usdc": 1.0}],
        "settled": {("KLGA", day): ("70", "truth")},
    }
    monkeypatch.setattr(C, "_render_live_book", lambda conn: book)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history (history_id TEXT,"
        " station_id TEXT, event_date TEXT, state TEXT, stage TEXT, decision_at_utc TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_bucket_probability"
        " (history_id TEXT, bucket TEXT, ordinal INTEGER, probability REAL)"
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES"
        " ('h1','KLGA',?, 'COMMITTED','P1','2026-09-01T10:00:00Z')", (day,),
    )
    conn.executemany(
        "INSERT INTO basket_probability_bridge_bucket_probability VALUES (?,?,?,?)",
        [("h1", "70", 0, 0.6), ("h1", "71", 1, 0.4)],
    )
    first = C.collect_window_divergence(conn)
    assert first["evaluated_station_days"] == 1
    conn.execute("DELETE FROM basket_probability_bridge_bucket_probability")
    assert C.collect_window_divergence(conn) == first, "the settled day is not read again"
