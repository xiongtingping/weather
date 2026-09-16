"""The beta lane's shadow readout (charter 128), pinned on a small book.

Three things must stay true: the replay cell a window lands in is decided
by the beta the lane served AT THAT WINDOW'S DECISION INSTANT (journal
as-of), never by today's; a window decided before the first fit is not
judged at all rather than judged as refused; and the section degrades to a
readable payload when the shadow tables are absent.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import beta_edge_store as store  # noqa: E402
import pytest  # noqa: E402

# Charter 140: these fixtures build the paper candidate table, and the estimator
# under test does not depend on the source; see ``ledger_evidence_source`` in
# conftest.
pytestmark = pytest.mark.usefixtures("ledger_evidence_source")


def _book() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE paper_trade_candidate_window("
        " candidate_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, raw_win_probability REAL,"
        " all_in_price REAL, hit INTEGER, settled_bucket TEXT, admissible INTEGER,"
        # Charter 128 v2: the replay judges on the market's belief, half a
        # spread under the all-in ask here.
        " market_mid REAL)"
    )
    return conn


def _window(conn, cid, *, day, at, raw, price, hit, adm, stage="P0"):
    conn.execute(
        "INSERT INTO paper_trade_candidate_window VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "S01", day, stage, at, raw, price, hit, "30°C", adm, price - 0.05),
    )


def _activate(conn, *, lcb, at):
    from datetime import date  # noqa: PLC0415

    today = date.today().isoformat()
    fit = {
        "stage_scope": "P0", "content_sha256": f"{abs(hash((lcb, at))) % 10**16:0>64d}",
        "table": {"schema": store.SCHEMA, "stage_scope": "P0", "price_basis": store.PRICE_BASIS,
                  "lead_band": store.WHOLE_STAGE,
                  "window_days": 28, "as_of_event_date": today, "beta": lcb + 0.1,
                  "beta_lcb": lcb, "beta_ucb": lcb + 0.2, "intercept": -0.05,
                  "row_count": 100, "day_count": 10, "accumulating": False},
        "evidence": {"anchor_utc": None, "excluded_before_anchor": 0,
                     "days_beta_positive": 7, "layers": {}},
        "accumulating": False, "row_count": 100, "day_count": 10,
    }
    store.persist_and_activate(conn, fit, now=at)
    store.journal_fit(conn, fit=fit, outcome="ACTIVATED", fitted_at_utc=at)


def test_the_replay_judges_each_window_by_the_beta_served_at_its_instant() -> None:
    from datetime import date, timedelta  # noqa: PLC0415

    conn = _book()
    store.ensure_schema(conn)
    d = (date.today() - timedelta(days=2)).isoformat()
    # Before any fit: not judged.
    _window(conn, "w0", day=d, at=f"{d}T00:00:00Z", raw=0.9, price=0.7, hit=1, adm=1)
    _activate(conn, lcb=0.0, at=f"{d}T01:00:00Z")
    # Served beta_lcb 0: beta refuses everything.  Production admitted w1,
    # refused w2.
    _window(conn, "w1", day=d, at=f"{d}T02:00:00Z", raw=0.9, price=0.7, hit=1, adm=1)
    _window(conn, "w2", day=d, at=f"{d}T02:00:00Z", raw=0.5, price=0.7, hit=0, adm=0)
    _activate(conn, lcb=1.0, at=f"{d}T03:00:00Z")
    # Served beta_lcb 1: the belief is the raw claim, so beta admits iff
    # raw clears the all-in price under charter 130's hurdle.
    _window(conn, "w3", day=d, at=f"{d}T04:00:00Z", raw=0.9, price=0.7, hit=0, adm=1)  # both admit
    _window(conn, "w4", day=d, at=f"{d}T04:00:00Z", raw=0.6, price=0.7, hit=1, adm=1)  # beta refuses
    _window(conn, "w5", day=d, at=f"{d}T04:00:00Z", raw=0.9, price=0.8, hit=1, adm=0)  # beta admits
    _window(conn, "w6", day=d, at=f"{d}T04:00:00Z", raw=0.3, price=0.8, hit=0, adm=0)  # both refuse
    conn.commit()
    out = C.collect_beta_shadow(conn)
    assert out["available"] and out["mode"] in {"off", "shadow", "on"}
    rep = out["replay"]
    assert rep["windows"] == 7 and rep["judged"] == 6
    cells = rep["cells"]
    assert cells["both_admit"]["n"] == 1
    assert cells["beta_refuses"]["n"] == 2          # w1 (lcb 0) and w4
    assert cells["beta_admits"]["n"] == 1           # w5
    assert cells["both_refuse"]["n"] == 2           # w2 and w6
    assert cells["beta_refuses"]["mean_hit_minus_price"] == round(((1 - 0.7) + (1 - 0.7)) / 2, 4)
    assert cells["beta_admits"]["mean_hit_minus_price"] == round(1 - 0.8, 4)
    day = rep["days"][0]
    assert day["date"] == d and day["windows"] == 6
    # w1, w3, w4 were admitted by production (w0 is not judged); w3, w5 by beta.
    assert day["current_admits"] == 3 and day["beta_admits"] == 2
    assert day["beta_refuses_of_admitted"] == 2 and day["beta_admits_of_refused"] == 1
    assert out["served"]["P0"]["beta_lcb"] == 1.0
    assert set(out["research"]) == {"P0", "P0-M", "P1"}


def test_the_section_survives_a_book_without_the_shadow_tables() -> None:
    conn = _book()
    out = C.collect_beta_shadow(conn)
    assert out["available"]
    assert out["served"] == {} and out["served_error"] == "no_beta_table"
    assert out["replay"]["judged"] == 0
    assert out["live"]["decisions"] == 0


def test_the_page_carries_the_section() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert '[betaShadow,"β 车道（影子）"]' in source
    assert '"beta_shadow": _cached_section(' in source
    assert "function betaShadow()" in source
