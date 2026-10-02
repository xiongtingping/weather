"""The board after the 2026-09-26 clean-up (user: 「前端还是四大模型的显式」
「前端没有proper线相关内容」「融合分布层显示可以重拟了」「全面检查旧的内容」).

Pinned here: the model families are read off the production registry rather
than a list kept on the page; the Proper lane has a page fed by its own rows
and legs, YES and NO both; the centre/width card carries the refit's own
two-score verdict (charter 228) instead of inferring "可以重拟了" from a day
count; and the research render refreshes a cheap page the page render keeps
starving, not only the heavy ones.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import proper_bet_lane  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    OFFICIAL_ARCHIVE_SCOPE_CONTRACTS,
)


def test_the_families_are_the_production_registrys() -> None:
    systems = {system_id for system_id, *_rest in C.MEMBER_FAMILIES}
    assert systems == set(OFFICIAL_ARCHIVE_SCOPE_CONTRACTS)
    for system_id, label, transport, _archived in C.MEMBER_FAMILIES:
        assert transport == str(
            OFFICIAL_ARCHIVE_SCOPE_CONTRACTS[system_id]["transport_provider_id"]
        )
        assert label and label != system_id, system_id
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    template = source[source.index('HTML_TEMPLATE = r"""'):]
    # The headings count the families they are handed; none is typed.  (Two
    # code comments quote the old wording as history; only code is read.)
    code = "\n".join(
        line for line in template.splitlines()
        if not line.lstrip().startswith("//")
    )
    for fixed in ("四大模型", "四家"):
        assert fixed not in code, fixed


# ---------------------------------------------------------------------------
# The Proper lane's page.


def _evaluation(conn, **row) -> None:
    base = {
        "station_id": "KLGA", "event_date": "2026-09-28", "stage": "P0",
        "decision_at_utc": "2026-09-26T12:00:00Z", "rule_version": "proper_bet_v2",
        "planned_legs": 0, "planned_cost_usdc": 0.0,
        "evaluated_at_utc": "2026-09-26T12:00:05Z",
    }
    base.update(row)
    conn.execute(
        f"INSERT INTO {proper_bet_lane.EVALUATION_TABLE}(" + ",".join(base)
        + ") VALUES(" + ",".join("?" * len(base)) + ")",
        tuple(base.values()),
    )


def _leg(**leg) -> dict:
    base = {
        "decision_id": "proper_bet_decision_1", "station_id": "KLGA",
        "event_date": "2026-09-24", "stage": "P0", "bucket": "70-71°F",
        "side": "YES", "limit_price": 0.2, "requested_size": 10.0,
        "filled_size": 10.0, "filled_notional_usdc": 2.0, "state": "filled",
        "created_at": "2026-09-22T12:00:00Z", "order_intent_id": "i1",
    }
    base.update(leg)
    return base


def test_the_proper_page_reads_the_lanes_rows_and_its_own_legs(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    proper_bet_lane.ensure_lane_schema(conn)
    _evaluation(conn, planned_legs=3, planned_cost_usdc=1.5, divergence_kl=0.12,
                s_json=json.dumps({"70-71°F": 0.9, "72-73°F": -1.4, "68-69°F": 0.1}),
                hours_to_settlement=40.0, plan_state="active", passes=2)
    _evaluation(conn, station_id="EGLC", refusal="hours_to_settlement_below_minimum")
    book = {
        "settled": {("KLGA", "2026-09-24"): ("70-71°F",)},
        "legs": [
            _leg(),
            # A NO leg on a bucket that did not settle pays.
            _leg(order_intent_id="i2", bucket="72-73°F", side="NO",
                 limit_price=0.7, filled_notional_usdc=7.0),
            # Another lane's leg is not this page's.
            _leg(order_intent_id="i3", decision_id="tiebreak_decision_9"),
            # Sent, not filled, not settled.
            _leg(order_intent_id="i4", event_date="2026-09-28", filled_size=0.0,
                 filled_notional_usdc=0.0, state="open"),
        ],
    }
    monkeypatch.setattr(C, "_render_live_book", lambda _conn: book)

    out = C.collect_proper_bet_lane(conn)

    ev = out["evaluations"]
    assert ev["available"] and ev["total"] == 2 and ev["planned"] == 1
    assert ev["plan_states"] == {"active": 1, "none": 1}
    refused = {r["refusal"]: r["count"] for r in ev["refusals"]}
    assert refused == {"planned": 1, "hours_to_settlement_below_minimum": 1}
    top = next(r for r in ev["recent"] if r["station_id"] == "KLGA")["top_positions"]
    assert [p["bucket"] for p in top] == ["72-73°F", "70-71°F", "68-69°F"]

    trades = out["trades"]
    assert trades["sent"] == 3 and trades["filled"] == 2
    assert (trades["filled_yes"], trades["filled_no"]) == (1, 1)
    by_intent = {t["order_intent_id"]: t for t in trades["rows"]}
    assert by_intent["i1"]["hit"] is True and by_intent["i1"]["pnl_usdc"] == pytest.approx(8.0)
    assert by_intent["i2"]["hit"] is True and by_intent["i2"]["pnl_usdc"] == pytest.approx(3.0)
    assert by_intent["i4"]["hit"] is None
    assert trades["pnl_usdc"] == pytest.approx(11.0)
    assert trades["hit_rate"] == pytest.approx(1.0)


def test_the_proper_page_stands_without_its_table(monkeypatch) -> None:
    monkeypatch.setattr(C, "_render_live_book", lambda _conn: {"legs": [], "settled": {}})
    out = C.collect_proper_bet_lane(sqlite3.connect(":memory:"))
    assert out["evaluations"] == {"available": False}
    assert out["trades"]["sent"] == 0 and out["trades"]["roi"] is None


def test_the_proper_page_is_rendered_every_run_and_has_a_pane() -> None:
    assert "proper_bet_lane" in C.RENDER_EVERY_RUN_SECTIONS
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    pane = source[source.index('["proper", "Proper 下注线 · 宪章 231",'):]
    pane = pane[: pane.index("]") + 1]
    assert "properBetLane()" in pane and '"sec-proper-bet"' in pane


# ---------------------------------------------------------------------------
# The centre/width card's verdict.


def test_the_verdict_is_the_journals_two_scores() -> None:
    evidence = {
        "gate_rule": "logscore_and_brier_v1",
        "absolute_delta_c": -0.1, "absolute_width_lambda": 0.8,
        "forward_nll_gain": -0.002, "forward_brier_gain": 0.001,
        "forward_day_count": 12, "forward_cell_count": 900,
        "incumbent_veto": {
            "day_count": 9,
            "gate": {"d_logscore": 0.004, "ci_logscore": [0.001, 0.007],
                     "d_brier": 0.0005, "ci_brier": [-0.0002, 0.0011]},
        },
    }
    verdict = C._center_width_verdict(json.dumps(evidence))
    assert verdict == {
        "gate_rule": "logscore_and_brier_v1",
        "candidate_delta_c": -0.1, "candidate_width_lambda": 0.8,
        "logscore_gain": -0.002, "brier_gain": 0.001,
        "forward_days": 12, "forward_cells": 900,
        "veto_logscore": 0.004, "veto_logscore_ci": [0.001, 0.007],
        "veto_brier": 0.0005, "veto_brier_ci": [-0.0002, 0.0011],
        "veto_days": 9,
    }
    assert C._center_width_verdict(None) is None
    assert C._center_width_verdict("not json") is None
    assert C._center_width_verdict("[1, 2]") is None


def test_the_card_no_longer_says_it_may_refit_on_a_day_count() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "min_forward_nll_gain" not in source
    for outcome in ("REFUSED_FORWARD", "ACTIVATED", "UNCHANGED", "ACCUMULATING",
                    "INCUMBENT_VETOED"):
        assert outcome in source, outcome


# ---------------------------------------------------------------------------
# The research render and a starved page.


@pytest.fixture()
def pages(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {})
    monkeypatch.setattr(C, "PAGED_SECTION_TTL_SEC", 1800)
    return tmp_path


def _page(directory: Path, name: str, *, age_sec: float, cost: float, now: datetime) -> None:
    stamp = (now - timedelta(seconds=age_sec)).isoformat().replace("+00:00", "Z")
    (directory / f"{name}.json").write_text(json.dumps({
        "computed_at_utc": stamp, "build_sec": cost, "value": {},
    }), encoding="utf-8")


def test_a_cheap_page_the_page_render_starves_is_due(pages) -> None:
    now = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)
    heavy = C.RESEARCH_SECTION_MIN_COST_SEC
    _page(pages, "starved", age_sec=C.STARVED_PAGE_AGE_SEC + 60, cost=15.0, now=now)
    _page(pages, "fresh_enough", age_sec=3600, cost=15.0, now=now)
    _page(pages, "heavy_expired", age_sec=3600, cost=heavy, now=now)
    specs = {name: {} for name in ("starved", "fresh_enough", "heavy_expired", "never_built")}

    due = C.research_due(specs, now)

    names = [name for _overdue, name, _cost in due]
    # A page with no build yet first, then by how far past its lifetime.
    assert names == ["never_built", "starved", "heavy_expired"]
    assert "fresh_enough" not in names, "a cheap page is not starved inside three hours"


def test_the_research_render_stops_when_the_page_render_starts(pages, monkeypatch) -> None:
    now = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)
    for name in ("first", "second"):
        _page(pages, name, age_sec=C.STARVED_PAGE_AGE_SEC * 2, cost=5.0, now=now)
    monkeypatch.setattr(C, "_now", lambda: now)
    spec = {"module": "calibration_dashboard", "collector": "collect_chain"}
    monkeypatch.setattr(C, "_load_closed_readings",
                        lambda _name: {"first": spec, "second": spec})
    ran: list[str] = []
    monkeypatch.setattr(C, "_refresh_one_section",
                        lambda _db, name, _spec: ran.append(name) or f"{name} computed in 1s")
    monkeypatch.setattr(C, "_page_render_running", lambda: True)

    out = C.refresh_research_section(pages / "ssot.db")

    assert len(ran) == 1, "one section, then it gave way"
    assert "stopped: the page render started" in out


# ---------------------------------------------------------------------------
# The tiebreak lane's leg table.


def _page_function(name: str) -> str:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    template = source[source.index('HTML_TEMPLATE = r"""'):]
    start = template.index(f"\nfunction {name}(")
    return template[start: template.index("\n}\n", start)]


def test_the_tiebreak_table_leaves_out_what_moved_no_money() -> None:
    """User ruling 2026-09-30 「逐笔：显示全部已成交订单和对应版本」, which
    narrowed 2026-09-26 「秤砣线failed、cancelled的订单不显示」: the table lists
    what filled -- a cancelled rung that filled in part keeps its row, and a
    rung still resting with nothing filled is not drawn either."""

    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    body = _page_function("tiebreakLane")
    match = re.search(r"const shown=\(R\.rows\|\|\[\]\)\.filter\(.*?\);", body, re.S)
    assert match, "the tiebreak table no longer filters its rows"
    rows = [
        {"id": "filled", "state": "filled", "filled_size": 5},
        {"id": "failed", "state": "failed", "filled_size": 0},
        {"id": "cancelled_empty", "state": "cancelled", "filled_size": 0},
        {"id": "cancelled_part", "state": "cancelled", "filled_size": 2},
        {"id": "open", "state": "open", "filled_size": 0},
    ]
    script = (
        "const R = " + json.dumps({"rows": rows}) + ";\n" + match.group(0)
        + "\nconsole.log(JSON.stringify(shown.map(t => t.id)));\n"
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ["filled", "cancelled_part"]


def test_a_retired_sections_recorded_call_is_skipped_not_fatal(pages, monkeypatch) -> None:
    """2026-09-26 16:55Z: the research render died on the beta section's
    collector, removed from the page an hour before, and refreshed nothing."""

    now = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)
    _page(pages, "retired", age_sec=C.STARVED_PAGE_AGE_SEC * 9, cost=5.0, now=now)
    _page(pages, "current", age_sec=C.STARVED_PAGE_AGE_SEC * 2, cost=5.0, now=now)
    recorded = {
        "retired": {"module": "calibration_dashboard", "collector": "collect_beta_shadow"},
        "current": {"module": "calibration_dashboard", "collector": "collect_chain"},
    }
    assert C._live_specs(recorded) == {"current": recorded["current"]}
    monkeypatch.setattr(C, "_now", lambda: now)
    monkeypatch.setattr(C, "_load_closed_readings", lambda _name: dict(recorded))
    monkeypatch.setattr(C, "_page_render_running", lambda: False)
    ran: list[str] = []
    monkeypatch.setattr(C, "_refresh_one_section",
                        lambda _db, name, _spec: ran.append(name) or f"{name} computed in 1s")

    out = C.refresh_research_section(pages / "ssot.db")

    assert ran == ["current"], out


def test_the_cheap_starved_pages_go_before_a_heavier_more_overdue_one(pages) -> None:
    """2026-10-01: run_cadence (1,972 s) led every research run by overdue
    ratio, and certificate_authority (14 s) sat 17 hours old behind it."""

    now = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
    heavy = C.RESEARCH_SECTION_MIN_COST_SEC
    _page(pages, "run_cadence", age_sec=C.STARVED_PAGE_AGE_SEC * 9, cost=heavy * 6, now=now)
    _page(pages, "certificate_authority", age_sec=C.STARVED_PAGE_AGE_SEC * 2, cost=14.0, now=now)
    _page(pages, "ssot_capacity", age_sec=C.STARVED_PAGE_AGE_SEC * 3, cost=45.0, now=now)
    specs = {name: {} for name in ("run_cadence", "certificate_authority", "ssot_capacity")}

    names = [name for _overdue, name, _cost in C.research_due(specs, now)]

    assert names == ["ssot_capacity", "certificate_authority", "run_cadence"]


def test_a_heavy_page_that_never_fits_after_the_cheap_ones_goes_first(pages) -> None:
    """2026-10-02: bias_half_life (2,804 s) sat 102 hours old, run_cadence
    (1,900 s) 11 and member_depth 37 -- the budget rule passed each over
    behind the cheap pages and the lighter heavy ones, every run."""

    now = datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc)
    budget = C.RESEARCH_RUN_BUDGET_SEC
    _page(pages, "cheap", age_sec=C.STARVED_PAGE_AGE_SEC + 600, cost=60.0, now=now)
    _page(pages, "fits", age_sec=C.PAGED_SECTION_TTL_SEC * 20, cost=budget / 2, now=now)
    _page(pages, "giant_old", age_sec=C.PAGED_SECTION_TTL_SEC * 12, cost=budget * 1.5, now=now)
    _page(pages, "giant", age_sec=C.PAGED_SECTION_TTL_SEC * 4, cost=budget * 1.1, now=now)
    specs = {name: {} for name in ("cheap", "fits", "giant_old", "giant")}

    names = [name for _overdue, name, _cost in C.research_due(specs, now)]

    assert names == ["giant_old", "cheap", "fits", "giant"]


def test_the_cheap_pages_take_back_the_run_once_starved_past_the_cap(pages) -> None:
    now = datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc)
    _page(pages, "cheap", age_sec=C.RESEARCH_CHEAP_STARVE_CAP_SEC + 60, cost=60.0, now=now)
    _page(pages, "giant", age_sec=C.PAGED_SECTION_TTL_SEC * 40,
          cost=C.RESEARCH_RUN_BUDGET_SEC * 1.5, now=now)
    specs = {name: {} for name in ("cheap", "giant")}

    names = [name for _overdue, name, _cost in C.research_due(specs, now)]

    assert names == ["cheap", "giant"]


def test_a_research_run_shares_one_gap_wait_budget(pages, monkeypatch) -> None:
    now = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
    for name in ("first", "second", "third"):
        _page(pages, name, age_sec=C.STARVED_PAGE_AGE_SEC * 2, cost=5.0, now=now)
    monkeypatch.setattr(C, "_now", lambda: now)
    spec = {"module": "calibration_dashboard", "collector": "collect_chain"}
    monkeypatch.setattr(C, "_load_closed_readings",
                        lambda _name: {"first": spec, "second": spec, "third": spec})
    monkeypatch.setattr(C, "_page_render_running", lambda: False)
    monkeypatch.setattr(C, "_connect", lambda _db: __import__("sqlite3").connect(":memory:"))
    monkeypatch.setattr(C, "RENDER_GAP_WAIT_BUDGET_SEC", 0.4)
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 60.0)
    monkeypatch.setattr(C, "RENDER_GAP_POLL_SEC", 0.05)
    monkeypatch.setattr(C, "_production_gap_blocker", lambda _line=None: "production cycle in flight")
    monkeypatch.setattr(C, "_cached_section_value",
                        lambda name, collector, *a, **k: C._await_production_gap(name, C.LIVE_SECTION_GAP_CAP_SEC))
    import time as _time
    started = _time.monotonic()

    C.refresh_research_section(pages / "ssot.db")

    assert _time.monotonic() - started < 0.9, "three sections, one 0.4 s wait budget (not 0.4 s each)"


def test_the_heavy_pages_rotate_oldest_page_first(pages, monkeypatch) -> None:
    """2026-10-02: skill (3.5 h lifetime) sat 56 hours old while the
    shorter-lived truth_landing (2.5 h) out-ranked it by age / lifetime in
    every run -- every heavy page is overdue always, so the ratio only ever
    favoured the short lifetimes."""

    now = datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)
    heavy = C.RESEARCH_SECTION_MIN_COST_SEC
    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC",
                        {**C.SECTION_CACHE_TTL_SEC, "short": 2.5 * 3600, "skill": 3.5 * 3600})
    _page(pages, "short", age_sec=40 * 3600, cost=heavy * 2, now=now)
    _page(pages, "skill", age_sec=56 * 3600, cost=heavy * 2, now=now)
    specs = {name: {} for name in ("short", "skill")}

    names = [name for _overdue, name, _cost in C.research_due(specs, now)]

    assert names == ["skill", "short"]


def test_a_page_from_other_code_is_no_page(pages, monkeypatch) -> None:
    """2026-10-02: truth_landing's page predated a code change, so the page
    render showed nothing ("it has no page yet") while the research render
    ranked the file by its 48 h age behind older servable pages."""

    now = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    heavy = C.RESEARCH_SECTION_MIN_COST_SEC
    _page(pages, "older_servable", age_sec=100 * 3600, cost=heavy * 2, now=now)
    _page(pages, "stale_code", age_sec=48 * 3600, cost=heavy * 2, now=now)
    blob = json.loads((pages / "stale_code.json").read_text())
    blob["code_fingerprint"] = "not-this-code"
    (pages / "stale_code.json").write_text(json.dumps(blob))
    spec = {"module": "calibration_dashboard", "collector": "collect_chain"}
    names = [name for _o, name, _c in C.research_due(
        {"older_servable": spec, "stale_code": spec}, now
    )]
    assert names[0] == "stale_code"


def test_a_section_that_killed_the_last_run_waits_behind_the_others(pages) -> None:
    """2026-10-02 09:16Z: bias_half_life ran into the research timeout, kept
    its old page, stayed the oldest, and the rotation handed it the next run
    straight back."""

    now = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    heavy = C.RESEARCH_SECTION_MIN_COST_SEC
    budget = C.RESEARCH_RUN_BUDGET_SEC
    _page(pages, "killer", age_sec=150 * 3600, cost=budget * 1.5, now=now)
    _page(pages, "next_oldest", age_sec=60 * 3600, cost=budget * 1.2, now=now)
    _page(pages, "cheap", age_sec=C.STARVED_PAGE_AGE_SEC + 60, cost=30.0, now=now)
    C._save_closed_readings(C.RESEARCH_ATTEMPT_FILE, {
        "name": "killer", "started_at_utc": "2026-10-02T08:22:00Z",
    })
    C._note_research_death()
    assert not (pages / C.RESEARCH_ATTEMPT_FILE).exists()
    specs = {name: {} for name in ("killer", "next_oldest", "cheap")}

    names = [name for _o, name, _c in C.research_due(specs, now)]

    assert names == ["next_oldest", "cheap", "killer"]
    later = now + timedelta(seconds=C.RESEARCH_KILLED_BACKOFF_SEC)
    after = [n for _o, n, _c in C.research_due(specs, later)]
    assert after.index("killer") < after.index("next_oldest"), after


HEAVY_SECTIONS_WITH_A_STAND_IN = (
    "ai_models", "center_landing", "chain_volume",
    "charter_ev", "h5_readiness", "member_depth", "model_accuracy_trend",
    "p1_path_skill", "posterior_lane", "precip_classification",
    "refusal_history", "report_latency", "scorecard", "scorecard_previous",
    "skill", "ssot_capacity", "station_calibration_windows", "truth_landing",
)


def test_every_heavy_section_declares_what_to_show_without_a_page() -> None:
    """2026-10-02: a heavy section with no page (any change to its collector
    voids it) and no stand-in was read whole by the page render past its
    deadline; scorecard_previous got the render killed three runs running.
    Every section that has cost 200 s or more declares one."""

    import re

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    missing = []
    for name in HEAVY_SECTIONS_WITH_A_STAND_IN:
        call = re.search(
            r'_cached_section\(\s*"%s",.*?\n\s*\)' % re.escape(name), source, re.S
        )
        assert call is not None, name
        if "_unavailable=" not in call.group(0):
            missing.append(name)
    assert missing == []
