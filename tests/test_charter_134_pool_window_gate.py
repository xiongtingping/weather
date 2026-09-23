"""Charter 134 (七): the stable-pool seat is certified on its rolling record.

The pool seats a fixed set with nothing chosen on the holdout, so one key is
re-scored every scan on its three newest shared days.  Judged on those three
days alone the verdict flipped on noise and a rejected seat went dark to the
compile.  The gate now reads the key's own newest ten scored days (each from
the newest evaluation that scored it), and a seat whose own newest holdout
scores the log score more than half again climatology's is rejected whatever the record
says.  These tests pin the rule, its boundaries, the guard, and the wiring
from the evaluation table back into the build.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    STABLE_POOL_GATE_BREAKAGE_MAX_RELATIVE_NLL_LOSS,
    STABLE_POOL_GATE_LOOKBACK_DAYS,
    STABLE_POOL_GATE_RULE,
    STABLE_POOL_GATE_WINDOW_DAYS,
    _cross_oos_gate,
    _stable_pool_gate_history,
    _stable_pool_window_gate,
    build_native_ensemble_oos,
    persist_native_ensemble_oos_evaluations,
)
from bayes_basket_target.ssot import apply_target_ssot_schema  # noqa: E402
from tests.test_stable_pool_serving import store  # noqa: E402,F401

CLIMATE = {"brier": 0.80, "crps": 1.00, "nll": 2.00}
GOOD = {"brier": 0.60, "crps": 0.50, "nll": 1.50}
BAD = {"brier": 0.95, "crps": 1.40, "nll": 2.60}


def _days(end: str, count: int) -> list[str]:
    last = date.fromisoformat(end)
    return [
        (last - timedelta(days=offset)).isoformat()
        for offset in reversed(range(count))
    ]


def _own(days: list[str], learned: dict, climate: dict = CLIMATE):
    rows = [
        {"event_date": day, "learned": dict(learned), "climate": dict(climate)}
        for day in days
    ]
    return rows, dict(learned), dict(climate)


def _gate(own_days, own_learned, history, *, own_climate=CLIMATE):
    rows, learned, climate = _own(own_days, own_learned, own_climate)
    return _stable_pool_window_gate(
        own_learned_metrics=learned,
        own_climate_metrics=climate,
        own_daily_scores=rows,
        history=history,
        holdout_end_date=own_days[-1],
    )


def test_a_three_day_loss_inside_a_winning_record_passes() -> None:
    days = _days("2026-09-21", 10)
    history = {day: (dict(GOOD), dict(CLIMATE)) for day in days[:7]}
    gate = _gate(days[7:], BAD, history)

    assert gate["own_holdout_gate"]["pass"] is False
    assert gate["pass"] is True
    assert gate["path"] == "WINDOW_STRICT"
    assert gate["rule"] == STABLE_POOL_GATE_RULE
    assert gate["membership_mode"] == "stable_pool"
    assert gate["window_event_dates"] == days
    assert gate["window_day_count"] == 10


def test_a_three_day_win_inside_a_losing_record_is_withdrawn() -> None:
    days = _days("2026-09-21", 10)
    history = {day: (dict(BAD), dict(CLIMATE)) for day in days[:7]}
    gate = _gate(days[7:], GOOD, history)

    assert gate["own_holdout_gate"]["pass"] is True
    assert gate["pass"] is False
    assert gate["path"] == "REJECTED"


@pytest.mark.parametrize("learned", [GOOD, BAD])
def test_a_key_with_only_its_own_holdout_is_judged_as_before(learned) -> None:
    days = _days("2026-09-21", 3)
    gate = _gate(days, learned, {})
    before = _cross_oos_gate(learned, CLIMATE)

    assert gate["pass"] is before["pass"]
    assert gate["own_holdout_gate"] == before
    assert gate["window_day_count"] == 3


def test_the_window_holds_ten_days_and_the_eleventh_is_not_read() -> None:
    days = _days("2026-09-21", 11)
    # The oldest day alone would sink the record; it is the eleventh.
    history = {days[0]: ({"brier": 50.0, "crps": 50.0, "nll": 50.0}, dict(CLIMATE))}
    history.update({day: (dict(GOOD), dict(CLIMATE)) for day in days[1:8]})
    gate = _gate(days[8:], GOOD, history)

    assert STABLE_POOL_GATE_WINDOW_DAYS == 10
    assert gate["window_day_count"] == 10
    assert days[0] not in gate["window_event_dates"]
    assert gate["pass"] is True


def test_days_older_than_the_lookback_are_not_read() -> None:
    end = "2026-09-21"
    stale = (
        date.fromisoformat(end) - timedelta(days=STABLE_POOL_GATE_LOOKBACK_DAYS)
    ).isoformat()
    history = {stale: (dict(GOOD), dict(CLIMATE))}
    gate = _gate(_days(end, 3), BAD, history)

    assert stale not in gate["window_event_dates"]
    assert gate["window_day_count"] == 3
    assert gate["pass"] is False


def test_the_newest_scoring_of_a_day_overrides_an_older_one() -> None:
    days = _days("2026-09-21", 3)
    # History scored the own days well; this evaluation scores them badly.
    history = {day: (dict(GOOD), dict(CLIMATE)) for day in days}
    gate = _gate(days, BAD, history)

    assert gate["pass"] is False
    assert gate["window_learned_metrics"]["crps"] == pytest.approx(BAD["crps"])


def test_the_breakage_floor_rejects_a_seat_that_just_broke() -> None:
    """The guard: a winning record cannot carry a seat whose newest holdout
    scores the log score more than half again climatology's (charter 211
    moved the floor from CRPS x2 to NLL x1.5 at the same ejection rate)."""

    days = _days("2026-09-21", 10)
    history = {day: (dict(GOOD), dict(CLIMATE)) for day in days[:7]}
    broken = {"brier": 0.85, "crps": 0.90, "nll": 3.02}
    gate = _gate(days[7:], broken, history)

    # The record alone would still admit it: 7 x 1.5 + 3 x 3.02 < 10 x 2.0.
    assert gate["window_learned_metrics"]["nll"] < CLIMATE["nll"]
    assert gate["pass"] is False
    assert gate["path"] == "REJECTED_BREAKAGE_FLOOR"
    assert gate["breakage_floor"]["hit"] is True
    assert gate["breakage_floor"]["own_relative_nll_loss"] == pytest.approx(0.51)
    # CRPS is on the record as a diagnostic and did not decide this.
    assert gate["breakage_floor"]["own_relative_crps_loss"] == pytest.approx(-0.1)


def test_the_breakage_floor_boundary_is_exactly_half_again_climatology() -> None:
    days = _days("2026-09-21", 10)
    history = {day: (dict(GOOD), dict(CLIMATE)) for day in days[:7]}
    at_floor = {"brier": 0.80, "crps": 2.50, "nll": 3.0}
    gate = _gate(days[7:], at_floor, history)

    assert STABLE_POOL_GATE_BREAKAGE_MAX_RELATIVE_NLL_LOSS == 0.5
    assert gate["breakage_floor"]["own_relative_nll_loss"] == pytest.approx(0.5)
    assert gate["breakage_floor"]["hit"] is False
    assert gate["pass"] is True


def test_against_a_perfect_climatology_any_loss_is_a_breach() -> None:
    days = _days("2026-09-21", 10)
    zero = {"brier": 0.0, "crps": 0.0, "nll": 0.0}
    history = {day: (dict(GOOD), dict(CLIMATE)) for day in days[:7]}
    gate = _gate(days[7:], GOOD, history, own_climate=zero)

    assert gate["breakage_floor"]["own_relative_nll_loss"] is None
    assert gate["breakage_floor"]["hit"] is True
    assert gate["pass"] is False
    json.dumps(gate, allow_nan=False)


# --- the history reader, on the production DDL --------------------------------

KEY = {
    "station_id": "KSFO",
    "season_bin": "PEAK",
    "lead_bucket": "h24_48",
    "run_cycle": "00Z",
    "cycle_map": {"ECMWF_IFS_ENS_OPEN_PF50": "00Z", "NOAA_GEFS_05": "00Z"},
    "settlement_definition": "official_daily_tmax_c",
    "threshold_profile": "FORWARD_ACCUMULATION_5_TRAIN_3_HOLDOUT",
}


def _evaluation_row(conn, *, evaluated_at, days, learned, ids=None, gate=None,
                    serial=[0]):  # noqa: B006 -- a counter, on purpose
    serial[0] += 1
    ids = sorted(ids or KEY["cycle_map"])
    cycle_map = {system: KEY["cycle_map"].get(system, "00Z") for system in ids}
    metrics = {
        **learned,
        "gate": gate or {"path": "STRICT", "pass": True},
        "equal_weight_diagnostic": {
            "scheme_daily_scores": [
                {"event_date": day, "learned": dict(learned), "climate": dict(CLIMATE)}
                for day in days
            ],
        },
    }
    identity = f"{serial[0]}:{evaluated_at}:{days}"
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_oos_evaluation VALUES"
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "native-oos-evaluation-" + content_sha256(identity)[:24],
            KEY["station_id"], KEY["season_bin"], KEY["lead_bucket"], KEY["run_cycle"],
            native_ensemble_oos._json(cycle_map), content_sha256(cycle_map),
            KEY["settlement_definition"], native_ensemble_oos._json(ids),
            content_sha256(ids), "a" * 64, "PRODUCTION_NO_LIVE_OOS_PASS",
            KEY["threshold_profile"], 5, 3,
            (date.fromisoformat(days[0]) - timedelta(days=1)).isoformat(),
            days[0], days[-1], native_ensemble_oos._json(metrics),
            native_ensemble_oos._json(CLIMATE), evaluated_at,
            content_sha256({"row": identity}),
        ),
    )


def _read(conn, *, as_of, ids=None):
    ids = sorted(ids or KEY["cycle_map"])
    cycle_map = {system: KEY["cycle_map"].get(system, "00Z") for system in ids}
    return _stable_pool_gate_history(
        conn,
        station_id=KEY["station_id"],
        season_bin=KEY["season_bin"],
        lead_bucket=KEY["lead_bucket"],
        run_cycle=KEY["run_cycle"],
        run_cycle_by_system_json=native_ensemble_oos._json(cycle_map),
        run_cycle_by_system_sha256=content_sha256(cycle_map),
        settlement_definition=KEY["settlement_definition"],
        system_ids=ids,
        threshold_profile=KEY["threshold_profile"],
        as_of_utc=as_of,
    )


def test_the_history_reads_only_the_pool_record_of_the_same_key() -> None:
    conn = sqlite3.connect(":memory:")
    apply_target_ssot_schema(conn)
    # Read: the key's own pool-era evaluation.
    _evaluation_row(conn, evaluated_at="2026-09-18T06:00:00.000Z",
                    days=["2026-09-15", "2026-09-16", "2026-09-17"], learned=GOOD)
    # Not read: scored before the pool existed (a contest blend, same key).
    _evaluation_row(conn, evaluated_at="2026-09-13T06:00:00.000Z",
                    days=["2026-09-10", "2026-09-11", "2026-09-12"], learned=BAD)
    # Not read: a rollback's row, which names its membership.
    _evaluation_row(conn, evaluated_at="2026-09-19T06:00:00.000Z",
                    days=["2026-09-08", "2026-09-09", "2026-09-14"], learned=BAD,
                    gate={"path": "STRICT", "pass": True,
                          "membership_mode": "subset_contest"})
    # Not read: another set.
    _evaluation_row(conn, evaluated_at="2026-09-18T07:00:00.000Z",
                    days=["2026-09-13", "2026-09-14", "2026-09-15"], learned=BAD,
                    ids=["DWD_ICON_GLOBAL_EPS", "NOAA_GEFS_05"])
    # Not read: evaluated after the instant being built.
    _evaluation_row(conn, evaluated_at="2026-09-22T06:00:00.000Z",
                    days=["2026-09-18", "2026-09-19", "2026-09-20"], learned=BAD)

    days = _read(conn, as_of="2026-09-21T00:00:00.000Z")

    assert sorted(days) == ["2026-09-15", "2026-09-16", "2026-09-17"]
    assert all(learned == GOOD for learned, _climate in days.values())


def test_the_history_takes_each_day_from_its_newest_scoring() -> None:
    conn = sqlite3.connect(":memory:")
    apply_target_ssot_schema(conn)
    _evaluation_row(conn, evaluated_at="2026-09-18T06:00:00.000Z",
                    days=["2026-09-15", "2026-09-16", "2026-09-17"], learned=BAD)
    _evaluation_row(conn, evaluated_at="2026-09-19T06:00:00.000Z",
                    days=["2026-09-16", "2026-09-17", "2026-09-18"], learned=GOOD)

    days = _read(conn, as_of="2026-09-21T00:00:00.000Z")

    assert days["2026-09-15"][0] == BAD
    assert days["2026-09-16"][0] == GOOD
    assert days["2026-09-18"][0] == GOOD


# --- wired end to end: build, persist, rebuild ---------------------------------

def _pool_whole(report):
    whole = sorted({
        system
        for candidate in report["cross_candidates"]
        for system in candidate["selected_system_ids"]
    })
    return [
        candidate for candidate in report["cross_candidates"]
        if sorted(candidate["selected_system_ids"]) == whole
        and candidate["republisher_fallback_used"] is False
    ]


def test_the_build_certifies_a_pool_seat_on_its_persisted_record(
    store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native_ensemble_oos, "STABLE_POOL_GATE_HISTORY_EPOCH_UTC",
        "2026-06-01T00:00:00Z",
    )
    first_at = "2026-07-01T00:00:00Z"
    report = build_native_ensemble_oos(
        store, station_id="TEST", as_of_utc=first_at,
        membership_mode="stable_pool",
    )
    whole = _pool_whole(report)
    assert whole
    candidate = whole[0]
    gate = candidate["oos_gate"]
    assert gate["rule"] == STABLE_POOL_GATE_RULE
    assert gate["window_day_count"] == len(
        {row["event_date"] for row in candidate["predictions"]}
    )
    assert gate["own_holdout_gate"] == _cross_oos_gate(
        candidate["oos_metrics"], candidate["climatology_baseline_metrics"]
    )
    assert candidate["state"] == "PRODUCTION_NO_LIVE_OOS_PASS"
    persist_native_ensemble_oos_evaluations(
        store, report, evaluated_at_utc=first_at,
    )
    row = store.execute(
        "SELECT * FROM calibration_cross_native_ensemble_oos_evaluation "
        "WHERE ensemble_system_ids_json=? AND season_bin=? "
        "ORDER BY evaluation_id LIMIT 1",
        (native_ensemble_oos._json(sorted(candidate["selected_system_ids"])),
         candidate["season_bin"]),
    ).fetchone()
    columns = [
        column[1] for column in store.execute(
            "PRAGMA table_info(calibration_cross_native_ensemble_oos_evaluation)"
        )
    ]
    persisted = dict(zip(columns, row))
    metrics = json.loads(persisted["oos_metrics_json"])
    # The per-day scores the window reads back are the gate's own numbers.
    daily = metrics["equal_weight_diagnostic"]["scheme_daily_scores"]
    for metric in ("brier", "crps", "nll"):
        assert sum(d["learned"][metric] for d in daily) / len(daily) == (
            pytest.approx(candidate["oos_metrics"][metric], abs=1e-12)
        )
        assert sum(d["climate"][metric] for d in daily) / len(daily) == (
            pytest.approx(candidate["climatology_baseline_metrics"][metric], abs=1e-12)
        )
    assert metrics["gate"]["rule"] == STABLE_POOL_GATE_RULE
    assert metrics["gate"]["membership_mode"] == "stable_pool"

    # An older evaluation of the same key, persisted exactly as production
    # writes it but scoring the earlier days that fill the window badly: the
    # rebuilt seat keeps its passing holdout and loses its record.
    start = date.fromisoformat(str(persisted["holdout_start_date"]))
    room = STABLE_POOL_GATE_WINDOW_DAYS - len(daily)
    assert room > 0
    older = [
        (start - timedelta(days=offset)).isoformat()
        for offset in range(room, 0, -1)
    ]
    terrible = {"brier": 5.0, "crps": 50.0, "nll": 20.0}
    history_metrics = {
        **{k: metrics[k] for k in ("brier", "crps", "nll")},
        "gate": {"path": "STRICT", "pass": True},
        "equal_weight_diagnostic": {
            "scheme_daily_scores": [
                {"event_date": day, "learned": terrible,
                 "climate": {"brier": 0.8, "crps": 1.0, "nll": 2.0}}
                for day in older
            ],
        },
    }
    forged = dict(persisted)
    forged.update({
        "evaluation_id": "native-oos-evaluation-" + "f" * 24,
        "holdout_start_date": older[0],
        "holdout_end_date": older[-1],
        "train_end_date": (date.fromisoformat(older[0]) - timedelta(days=1)).isoformat(),
        "oos_metrics_json": native_ensemble_oos._json(history_metrics),
        "evaluated_at_utc": "2026-06-30T00:00:00Z",
        "content_sha256": "e" * 64,
    })
    store.execute(
        "INSERT INTO calibration_cross_native_ensemble_oos_evaluation("
        + ",".join(forged) + ") VALUES(" + ",".join("?" * len(forged)) + ")",
        tuple(forged.values()),
    )
    rebuilt = [
        c for c in _pool_whole(build_native_ensemble_oos(
            store, station_id="TEST", as_of_utc="2026-07-01T01:00:00Z",
            membership_mode="stable_pool",
        ))
        if c["season_bin"] == candidate["season_bin"]
        and c["run_cycle_by_system"] == candidate["run_cycle_by_system"]
    ]
    assert rebuilt
    gate = rebuilt[0]["oos_gate"]
    assert gate["own_holdout_gate"]["pass"] is True
    assert gate["window_event_dates"] == sorted(
        older + [row["event_date"] for row in daily]
    )
    assert rebuilt[0]["state"] == "OOS_REJECTED"


def test_the_contest_rule_keeps_the_three_day_gate(store) -> None:
    report = build_native_ensemble_oos(
        store, station_id="TEST", as_of_utc="2026-07-01T00:00:00Z",
        membership_mode="subset_contest",
    )
    assert report["cross_candidates"]
    for candidate in report["cross_candidates"]:
        gate = candidate["oos_gate"]
        assert gate["membership_mode"] == "subset_contest"
        assert "rule" not in gate
        assert gate["pass"] is _cross_oos_gate(
            candidate["oos_metrics"], candidate["climatology_baseline_metrics"]
        )["pass"]


def test_charter_134_names_the_window_gate() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(
        encoding="utf-8"
    )
    clause = charter.split("\n134. ", 1)[1].split("\n135. ", 1)[0]
    assert "（七）" in clause
    assert STABLE_POOL_GATE_RULE in clause
    assert f"{STABLE_POOL_GATE_WINDOW_DAYS} 个" in clause
    assert "两倍" in clause
    # Charter 211: the floor and the strict path read the log score.
    assert "第 211 条" in clause and "LogScore" in clause
    assert "裁决人：用户" in clause
    # No bump of its own: it rides charter 198's (coordinator's ruling).
    assert "随第 198 条的 P0 v9 / P0-M v11 / P1 v19 一起生效" in clause
