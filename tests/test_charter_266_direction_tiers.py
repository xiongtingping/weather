"""Charter 266: the shared direction signal (ECMWF momentum and the
four-feature model) and the copy-follow lane's three stake tiers.

User rulings 2026-10-10 「落地三档」 and 「动量和四特征模型：解耦出来，其他策略比如
proper线也可以使用」.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bayes_basket_target import copy_follow_lane as lane  # noqa: E402
from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target import run_change, strategy_lanes  # noqa: E402
from tests.test_charter_256_copy_follow_lane import (  # noqa: E402
    ED, ST, TZ, USER, _book, _intents, _seed, _transport,
)
from tests.test_charter_257_copy_follow_v2 import (  # noqa: E402
    SETTINGS, W2, Cluster, _hermetic, _run, _served, _taker,
)
from tests.test_tiebreak_lane import _insert, store  # noqa: E402,F401 -- ``store`` is a fixture

assert _hermetic  # the v2 suite's autouse fixture, re-armed here

ROOT = Path(__file__).resolve().parents[1]
TRADE_AT = "2026-09-18T12:00:00Z"
V3 = {**SETTINGS, "rule": lane.RULE_V3, "stake_usdc": 3.0,
      "stake_tiers": {"momentum_agrees": 3.0, "model_agrees": 2.0, "probability": 1.0}}


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _runs(aifs=(), ifs=(), hres=(), single=()):
    """``family_runs`` rows: (had_at, run_init, value) per system."""

    def rows(values, start="2026-09-17T00:00:00Z"):
        base = _at(start)
        return [(base + timedelta(hours=6 * i + 7), (base + timedelta(hours=6 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), v)
                for i, v in enumerate(values)]

    return {"ECMWF_AIFS_ENS_GLOBAL": rows(aifs), "ECMWF_IFS_ENS_OPEN_PF50": rows(ifs),
            "ECMWF_IFS_9KM_RELAY_POINT": rows(hres), "ECMWF_AIFS_SINGLE_POINT": rows(single)}


def _patch_runs(monkeypatch, runs):
    monkeypatch.setattr(run_change, "family_runs", lambda conn, *, station_id, event_date, systems=run_change.SYSTEMS:
                        {s: list(runs.get(s, [])) for s in systems})


def _empty_physics(tmp_path):
    return ds.PhysicsCache(tmp_path / "physics.json")


def _seed_mode_snapshot(conn, *, observed="2026-09-18T11:59:00.000Z", ladder=None):
    """A committed snapshot just before the source's trade (the mode is read
    as of the trade); ``_seed`` adds the fresh one the send prices on."""

    _insert(conn, "basket_official_ws_snapshot", {
        "snapshot_id": "snap0", "market_id": "m", "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "sequence": 0, "observed_at": observed, "quote_version": "v0",
        "quote_observed_at": observed, "quote_expires_at": "2026-09-18T13:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(_book(ladder) if ladder else _book()), "state": "COMMITTED",
        "created_at": observed,
    })


# ---------------------------------------------------------------------------
# The tier rule (pure).


@pytest.mark.parametrize(("momentum", "model", "p_passes", "expected"), [
    ("agree", "disagree", False, ("momentum_agrees", None)),
    ("agree", "agree", True, ("momentum_agrees", None)),
    ("disagree", "agree", True, (None, "direction_disagrees:momentum")),
    ("quiet", "agree", False, ("model_agrees", None)),
    ("no_direction", "agree", False, ("model_agrees", None)),
    ("quiet", "disagree", True, (None, "direction_disagrees:model")),
    ("quiet", "quiet", True, ("probability", None)),
    ("no_direction", "no_direction", True, ("probability", None)),
    ("quiet", "quiet", False, (None, None)),
])
def test_three_tiers_and_disagreement_skips(momentum, model, p_passes, expected) -> None:
    assert lane.direction_stake_tier(momentum=momentum, model=model, probability_passes=p_passes) == expected


def test_the_tier_table_must_name_every_tier() -> None:
    assert lane.stake_tier_multipliers(V3) == {"momentum_agrees": 3.0, "model_agrees": 2.0, "probability": 1.0}
    for broken in ({}, {"momentum_agrees": 3.0, "model_agrees": 2.0}, {**V3["stake_tiers"], "probability": 0.0}):
        with pytest.raises(ValueError):
            lane.stake_tier_multipliers({"stake_tiers": broken})


def test_a_malformed_tier_table_stops_the_lane(store) -> None:
    with pytest.raises(ValueError, match="copy_follow_stake_tier"):
        _run(store, Cluster(), [], **{**V3, "stake_tiers": {"momentum_agrees": 3.0}})


# ---------------------------------------------------------------------------
# Momentum.


def test_momentum_sums_the_four_components_in_their_own_spreads(monkeypatch) -> None:
    _patch_runs(monkeypatch, _runs(aifs=[20.0, 20.369], ifs=[20.0, 20.383], hres=[20.0, 20.676],
                                   single=[20.0, 30.0, 20.473]))
    out = ds.momentum(None, station_id=ST, event_date=ED, at_utc="2026-09-18T12:00:00Z")
    assert set(out["components"]) == {"aifs_ens", "ifs_ens", "hres", "aifs_single"}
    # AIFS single is newest minus two runs back (20.473 - 20.0), not minus the 30.0 between.
    assert out["components"]["aifs_single"]["delta_c"] == pytest.approx(0.473)
    assert out["M"] == pytest.approx(4.0, abs=1e-3) and out["side"] == 1


def test_momentum_reads_only_runs_in_hand_and_is_quiet_under_one(monkeypatch) -> None:
    # Runs land at init + 7 h: the second AIFS run (init 06Z, had 13Z) is not in hand at 12:00Z.
    _patch_runs(monkeypatch, _runs(aifs=[20.0, 21.0]))
    assert ds.momentum(None, station_id=ST, event_date=ED, at_utc="2026-09-17T12:00:00Z")["M"] is None
    held = ds.momentum(None, station_id=ST, event_date=ED, at_utc="2026-09-17T13:00:00Z")
    assert held["M"] == pytest.approx(1.0 / 0.369, abs=1e-3) and held["side"] == 1
    _patch_runs(monkeypatch, _runs(aifs=[20.0, 19.8]))
    quiet = ds.momentum(None, station_id=ST, event_date=ED, at_utc="2026-09-18T00:00:00Z")
    assert quiet["M"] == pytest.approx(-0.2 / 0.369, abs=1e-3) and quiet["side"] == 0


def test_run_change_keeps_its_three_systems_and_refuses_unknown_ones() -> None:
    assert run_change.SYSTEMS == ("ECMWF_AIFS_ENS_GLOBAL", "ECMWF_AIFS_SINGLE_POINT", "NOAA_GEFS_05")
    assert set(ds.MOMENTUM_SYSTEMS) <= run_change.NATIVE_SYSTEMS | run_change.POINT_SYSTEMS
    with pytest.raises(ValueError, match="run_change_system_unknown"):
        run_change.family_runs(None, station_id=ST, event_date=ED, systems=("NOAA_HRRR",))


# ---------------------------------------------------------------------------
# Physics and the model.


def _payload(days: list[str], values: dict[str, list[float]], *, drop_hour: str | None = None):
    """An Open-Meteo previous-runs body: every variable x model x lag, 24
    local hours a day, the day's value constant over its hours."""

    times = [f"{d}T{h:02d}:00" for d in days for h in range(24)]
    hourly: dict = {"time": times}
    for variable in ds.PHYSICS_VARIABLES:
        for model in ds.PHYSICS_MODELS:
            for lag in (1, 2):
                series = values.get(f"{variable}|{model}", [10.0] * len(days))
                hourly[f"{variable}_previous_day{lag}_{model}"] = [
                    None if drop_hour and t == drop_hour else series[days.index(t[:10])] for t in times]
    return {"hourly": hourly}


DAYS = [f"2026-09-{d:02d}" for d in range(10, 21)]


def test_daily_means_need_all_eight_daytime_hours() -> None:
    daily = ds.daily_means(_payload(DAYS, {}, drop_hour="2026-09-12T13:00"))
    assert daily["dew_point_2m|icon_global"]["2026-09-11"] == [10.0, 10.0]
    assert daily["dew_point_2m|icon_global"]["2026-09-12"] == [None, None]
    # A night-time gap does not matter.
    night = ds.daily_means(_payload(DAYS, {}, drop_hour="2026-09-12T03:00"))
    assert night["dew_point_2m|icon_global"]["2026-09-12"] == [10.0, 10.0]


def test_the_lag_switches_at_eight_utc_on_the_eve_and_the_day_before() -> None:
    assert ds.physics_lag("2026-09-20", "2026-09-19T08:00:00Z") == 0
    assert ds.physics_lag("2026-09-20", "2026-09-19T07:59:00Z") == 1
    assert ds.physics_lag("2026-09-20", "2026-09-18T08:00:00Z") == 1
    assert ds.physics_lag("2026-09-20", "2026-09-18T07:59:00Z") is None


def test_physics_features_are_station_z_scores_with_their_signs() -> None:
    ramp = [float(i) for i in range(len(DAYS))]          # the event day (09-20) is the warmest of the window
    values = {"shortwave_radiation|icon_global": ramp, "wind_speed_10m|ecmwf_ifs025": ramp}
    for model in ds.PHYSICS_MODELS[:3]:
        values[f"dew_point_2m|{model}"] = ramp
    doc = {"fetched_at_utc": "2026-09-18T10:00:00Z", "daily": ds.daily_means(_payload(DAYS, values))}
    out = ds.physics_features(doc, event_date="2026-09-20", at_utc="2026-09-18T12:00:00Z")
    assert out["lag"] == "previous_day2" and out["icon_shortwave"] > 1.5
    # Wind is read inverted (+ = warmer): a windy day is a colder one.
    assert out["ecmwf_wind_inverse"] == pytest.approx(-out["icon_shortwave"])
    # Six models' dew point averaged: three ramps and three flat series (no spread, no z) -> the three.
    assert out["dew_point_consensus"] == pytest.approx(out["icon_shortwave"])
    too_few = {**values}
    for model in ds.PHYSICS_MODELS[1:3]:
        too_few.pop(f"dew_point_2m|{model}")
    thin = ds.physics_features({"daily": ds.daily_means(_payload(DAYS, too_few))}, event_date="2026-09-20",
                               at_utc="2026-09-18T12:00:00Z")
    assert thin["dew_point_consensus"] is None
    assert ds.physics_features(None, event_date="2026-09-20", at_utc="2026-09-18T12:00:00Z")["state"] == "no_physics"


def test_the_model_score_is_the_round_22_slope() -> None:
    feats = {"icon_shortwave": 1.0, "dew_point_consensus": 1.0, "ecmwf_wind_inverse": 1.0}
    assert ds.model_score(2.0, feats) == pytest.approx(0.056 + 0.014 - 0.045 + 0.032)
    assert ds.model_score(None, {}) == 0.0
    assert ds.MODEL_COEFFICIENTS == {"momentum_half": 0.056, "icon_shortwave": 0.014,
                                     "dew_point_consensus": -0.045, "ecmwf_wind_inverse": 0.032}


def test_the_physics_cache_fetches_due_stations_and_survives_a_restart(tmp_path) -> None:
    calls: list[str] = []

    def fetch(url):
        calls.append(url)
        if "latitude=31.1460" in url:      # ZSPD
            return _payload(DAYS, {})
        return {"error": True, "reason": "bad request"}

    cache = ds.PhysicsCache(tmp_path / "physics.json")
    receipt = cache.refresh({"ZSPD": "Asia/Shanghai", "EGLC": "Europe/London"}, now_utc="2026-09-18T12:00:00Z",
                            fetch=fetch)
    assert receipt["ok"] == 1 and receipt["failed"]["EGLC"].startswith("api:")
    assert "previous-runs-api" in calls[0] and "models=icon_global" in calls[0]
    reborn = ds.PhysicsCache(tmp_path / "physics.json")
    doc = reborn.station_doc("ZSPD")
    assert doc["lineage"] == ds.PHYSICS_LINEAGE == "aggregator_republisher" and doc["timezone"] == "Asia/Shanghai"
    # Fresh: not due again within REFRESH_SECONDS; due after it.
    assert reborn.due({"ZSPD": "Asia/Shanghai"}, now_utc="2026-09-18T13:00:00Z") == []
    assert reborn.due({"ZSPD": "Asia/Shanghai"}, now_utc="2026-09-18T15:30:00Z") == ["ZSPD"]


# ---------------------------------------------------------------------------
# The market's mode and a leg's side of it.


def test_a_legs_direction_is_its_side_of_the_mode() -> None:
    ords = {"27°C": 0, "28°C": 1, "29°C": 2}
    assert ds.leg_direction(bucket="29°C", side="YES", mode="28°C", ordinals=ords) == 1
    assert ds.leg_direction(bucket="27°C", side="YES", mode="28°C", ordinals=ords) == -1
    assert ds.leg_direction(bucket="29°C", side="NO", mode="28°C", ordinals=ords) == -1
    assert ds.leg_direction(bucket="28°C", side="YES", mode="28°C", ordinals=ords) == 0
    assert ds.agreement(1, 1) == "agree" and ds.agreement(-1, 1) == "disagree"
    assert ds.agreement(0, 1) == "quiet" and ds.agreement(1, 0) == "no_direction"


def test_the_mode_is_read_as_of_the_instant(store) -> None:
    _seed_mode_snapshot(store, ladder={"27°C": (0.18, 0.20), "28°C": (0.28, 0.32), "29°C": (0.18, 0.20)})
    _seed(store, book=_book({"27°C": (0.10, 0.12), "28°C": (0.20, 0.22), "29°C": (0.60, 0.64)}))
    before = ds.market_mode_at(store, station_id=ST, event_date=ED, at_utc=TRADE_AT)
    assert before["state"] == "ok" and before["mode"] == "28°C" and before["snapshot_id"] == "snap0"
    after = ds.market_mode_at(store, station_id=ST, event_date=ED, at_utc="2026-09-18T12:03:00Z")
    assert after["mode"] == "29°C"
    assert ds.market_mode_at(store, station_id=ST, event_date=ED, at_utc="2026-09-18T11:00:00Z")["state"] == "no_snapshot"


# ---------------------------------------------------------------------------
# The lane under rule v3.


def _v3_run(store, monkeypatch, tmp_path, runs, *, bucket="29°C", p=None, price=0.20):
    _patch_runs(monkeypatch, runs)
    _seed_mode_snapshot(store)
    _seed(store)
    _served(store, p or {"27°C": 0.25, "28°C": 0.45, "29°C": 0.30})
    sent: list = []
    receipt = lane.execute_copy_follow(
        store, transport=_transport(sent),
        now_utc="2026-09-18T12:02:30.000Z", live_submit_authorized=True,
        settings={**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **V3},
        fetch=Cluster([_taker(bucket, price=price, at=TRADE_AT)]),
        position_reader=lambda token: {"ok": True, "shares": 1e9}, ratio_cache={},
        physics=_empty_physics(tmp_path),
    )
    row = store.execute(
        f"SELECT refusal, stake_tier, stake_usdc, scaled_shares, price_cap, direction_json FROM {lane.SOURCE_TABLE}"
    ).fetchone()
    return receipt, row, sent


def test_momentum_agreeing_stakes_three_times_without_a_probability_margin(store, monkeypatch, tmp_path) -> None:
    # 29°C YES is the warm side of the 28°C mode; AIFS ENS +0.5 C -> M = +1.36 (warm).
    receipt, row, sent = _v3_run(store, monkeypatch, tmp_path, _runs(aifs=[20.0, 20.5]),
                                 p={"27°C": 0.40, "28°C": 0.45, "29°C": 0.15})
    refusal, tier, stake, shares, cap, direction = row
    assert refusal is None and tier == "momentum_agrees" and stake == pytest.approx(9.0)
    # Our p (0.15) is under the price: v2 would refuse; the direction tier's cap is price + slippage.
    assert cap == pytest.approx(0.26) and shares == pytest.approx(9.0 / 0.26, abs=1e-4)
    reading = json.loads(direction)
    assert reading["mode"]["mode"] == "28°C" and reading["leg_direction"] == 1 and reading["momentum"] == "agree"
    assert reading["signal"]["physics"]["lineage"] == "aggregator_republisher"
    assert receipt["rule"] == "copy_follow_lane_v3" and receipt["planned"] == 1
    plan = json.loads(_intents(store)[0][6])["plan"]
    assert plan["stake_tier"] == "momentum_agrees"


def test_momentum_against_skips_the_leg_even_when_p_agrees(store, monkeypatch, tmp_path) -> None:
    receipt, row, _sent = _v3_run(store, monkeypatch, tmp_path, _runs(aifs=[20.0, 19.5]),
                                  p={"27°C": 0.10, "28°C": 0.40, "29°C": 0.50})
    assert row[0] == "direction_disagrees:momentum" and row[1] is None and row[3] is None
    assert receipt["planned"] == 0


def test_the_model_agreeing_with_quiet_momentum_stakes_twice(store, monkeypatch, tmp_path) -> None:
    # M = 0.3 / 0.369 = 0.81: quiet, but the model score 0.056 * M/2 > 0 is warm.
    _receipt, row, _sent = _v3_run(store, monkeypatch, tmp_path, _runs(aifs=[20.0, 20.3]),
                                   p={"27°C": 0.40, "28°C": 0.45, "29°C": 0.15})
    assert row[0] is None and row[1] == "model_agrees" and row[2] == pytest.approx(6.0)


def test_the_model_against_with_quiet_momentum_skips(store, monkeypatch, tmp_path) -> None:
    _receipt, row, _sent = _v3_run(store, monkeypatch, tmp_path, _runs(aifs=[20.0, 19.7]),
                                   p={"27°C": 0.10, "28°C": 0.40, "29°C": 0.50})
    assert row[0] == "direction_disagrees:model"


def test_no_signal_falls_to_the_probability_tier_at_one_stake(store, monkeypatch, tmp_path) -> None:
    _receipt, row, _sent = _v3_run(store, monkeypatch, tmp_path, _runs(),
                                   p={"27°C": 0.10, "28°C": 0.40, "29°C": 0.50})
    refusal, tier, stake, _shares, cap, _direction = row
    assert refusal is None and tier == "probability" and stake == pytest.approx(3.0)
    assert cap == pytest.approx(0.26)  # min(0.20 + 0.06, 0.50 - 0.05)


def test_no_signal_and_p_short_is_v2s_own_refusal(store, monkeypatch, tmp_path) -> None:
    _receipt, row, _sent = _v3_run(store, monkeypatch, tmp_path, _runs(),
                                   p={"27°C": 0.40, "28°C": 0.45, "29°C": 0.15})
    assert row[0].startswith("model_margin_short:")


def test_a_leg_on_the_mode_has_no_direction(store, monkeypatch, tmp_path) -> None:
    # Momentum is strongly warm, but a 28°C YES sits on the mode: only the probability tier can take it.
    _receipt, row, _sent = _v3_run(store, monkeypatch, tmp_path, _runs(aifs=[20.0, 21.0]), bucket="28°C",
                                   price=0.30, p={"27°C": 0.10, "28°C": 0.50, "29°C": 0.40})
    assert row[0] is None and row[1] == "probability" and json.loads(row[5])["leg_direction"] == 0


# ---------------------------------------------------------------------------
# Wiring.


def test_the_defaults_carry_the_tier_table_and_v2_stays_selectable() -> None:
    cfg = strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS
    assert cfg["stake_tiers"] == {"momentum_agrees": 3.0, "model_agrees": 2.0, "probability": 1.0}
    assert {lane.RULE_V2, lane.RULE_V3} == set(lane.CLUSTER_RULES) and lane.RULE_V2 in lane.RULE_VERSIONS


def test_the_lanes_file_carries_v3() -> None:
    # The switch and the stake dial are the operator's; only the rule and the tier table are pinned.
    cfg = strategy_lanes.lane_settings(strategy_lanes.LANE_COPY_FOLLOW,
                                       ROOT / "config" / "trading" / "strategy_lanes_v1.json")
    assert cfg["rule"] == lane.RULE_V3
    assert cfg["stake_tiers"] == {"momentum_agrees": 3.0, "model_agrees": 2.0, "probability": 1.0}


def test_the_live_resident_keeps_the_signal_fresh_and_the_feed_follows_the_lane(monkeypatch) -> None:
    import scripts.weather_basket_yes_daemon as daemon

    # Since the station board reads the fleet snapshot (2026-10-10), every live resident keeps it.
    monkeypatch.setattr(daemon, "PRODUCTION_EXECUTION_MODE", "production_live")
    assert daemon.direction_physics_wanted() is True
    for enabled, rule in ((True, lane.RULE_V3), (False, lane.RULE_V3), (True, lane.RULE_V2)):
        monkeypatch.setattr("bayes_basket_target.strategy_lanes.lane_settings",
                            lambda name, path=None, e=enabled, r=rule: {"enabled": e, "rule": r,
                                                                         "source_users": [USER, W2]})
        # The fast feed polls the cluster under v2 and v3 alike.
        assert bool(daemon.copy_follow_feed_users()) is enabled
    monkeypatch.setattr(daemon, "PRODUCTION_EXECUTION_MODE", "production_no_live")
    assert daemon.direction_physics_wanted() is False


def test_the_resident_refresh_job_never_raises(tmp_path) -> None:
    out = ds.refresh_fleet_physics(tmp_path / "missing.db", cache=ds.PhysicsCache(tmp_path / "p.json"))
    assert out["state"] in {"zones_unreadable", "ran"}
    out = ds.refresh_fleet_signal(tmp_path / "missing.db", cache=ds.PhysicsCache(tmp_path / "p.json"),
                                  board_path=tmp_path / "board.json")
    assert out["state"] in {"zones_unreadable", "ran"}


# ---------------------------------------------------------------------------
# The fleet snapshot the station board reads (user 2026-10-10 「在前端"站点看板"的
# "辐射偏移（宪章 222）"增加各个站点的方向信息」).


def test_the_combined_side_reads_momentum_first() -> None:
    assert ds.combined_side(1, -1) == 1 and ds.combined_side(0, -1) == -1 and ds.combined_side(0, 0) == 0


def test_the_board_snapshot_covers_today_to_two_days_on(store, monkeypatch, tmp_path) -> None:
    _patch_runs(monkeypatch, _runs(aifs=[20.0, 20.5]))
    _seed_mode_snapshot(store, observed="2026-09-20T05:50:00.000Z")
    doc = ds.board_snapshot(store, now_utc="2026-09-20T06:00:00Z", physics=_empty_physics(tmp_path),
                            zones={ST: TZ})
    days = doc["stations"][ST]
    assert sorted(days) == ["2026-09-20", "2026-09-21", "2026-09-22"]
    today = days["2026-09-20"]
    assert today["momentum_side"] == 1 and today["side"] == 1
    assert today["mode"]["state"] == "ok" and today["mode"]["mode"] == "28°C"
    assert days["2026-09-21"]["mode"]["state"] == "no_snapshot"
    path = ds.write_board_snapshot(doc, tmp_path / "board.json")

    from bayes_basket_target import station_day_board as board  # noqa: PLC0415

    monkeypatch.setattr(board, "DIRECTION_BOARD_PATH", path)
    monkeypatch.setattr(board, "_DIRECTION_CACHE", {})
    read = board._direction(ST, "2026-09-20")
    assert read["side"] == 1 and read["computed_at_utc"] == "2026-09-20T06:00:00Z"
    assert board._direction(ST, "2026-09-25") is None and board._direction("XXXX", "2026-09-20") is None


def test_the_station_board_reads_the_snapshot_and_redraws_on_it() -> None:
    source = (ROOT / "bayes_basket_target" / "station_day_board.py").read_text(encoding="utf-8")
    # Read, never computed: the board does not call the signal's arithmetic.
    for name in ("direction_signal(", "momentum(", "physics_features(", "leg_reading(", "board_snapshot("):
        assert name not in source, name
    assert '"direction": _direction(station, day)' in source and 'f"dir:{DIRECTION_BOARD_PATH' in source
    dashboard = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "function sdDirection(D, R)" in dashboard and "sdDirection(day.direction, R)" in dashboard


# ---------------------------------------------------------------------------
# 补记一: big episodes are staked more (user ruling 2026-10-10 「大单增加预算」).

BIG = {"big_episode_usdc": 30.0, "big_episode_window_seconds": 60.0, "big_episode_multiplier": 2.0}


def test_the_big_episode_rule_is_validated_and_off_by_default() -> None:
    assert lane.big_episode_rule(BIG) == (30.0, 60.0, 2.0)
    cfg = strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS
    assert lane.big_episode_rule(cfg)[2] == 1.0  # the code default is off; the lane file turns it on
    for broken in ({**BIG, "big_episode_multiplier": 0.5}, {**BIG, "big_episode_usdc": -1.0},
                   {**BIG, "big_episode_window_seconds": 0.0}, {**BIG, "big_episode_multiplier": "x"}):
        with pytest.raises(ValueError, match="big_episode"):
            lane.big_episode_rule(broken)


def test_episode_usdc_counts_the_window_inclusive() -> None:
    flow = [(100, 5.0), (130, 10.0), (160, 20.0), (161, 50.0)]
    assert lane.episode_usdc(flow, start_ts=100, window_seconds=60) == 35.0
    assert lane.episode_usdc(flow, start_ts=130, window_seconds=60) == 80.0


def _big_run(store, monkeypatch, tmp_path, trades, *, now="2026-09-18T12:02:30.000Z", seed=True, sent=None):
    _patch_runs(monkeypatch, _runs(aifs=[20.0, 20.5]))
    if seed:
        _seed_mode_snapshot(store)
        _seed(store)
        _served(store, {"27°C": 0.40, "28°C": 0.45, "29°C": 0.15})
    sent = [] if sent is None else sent
    receipt = lane.execute_copy_follow(
        store, transport=_transport(sent), now_utc=now, live_submit_authorized=True,
        settings={**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **V3, **BIG, "take_only": True},
        fetch=Cluster(trades), position_reader=lambda token: {"ok": True, "shares": 1e9}, ratio_cache={},
        physics=_empty_physics(tmp_path),
    )
    return receipt, sent


def test_a_big_episode_doubles_the_stake(store, monkeypatch, tmp_path) -> None:
    # 8 U first fill, then 31.5 U by a second wallet 30 s later: 39.5 U inside 60 s -> a big episode.
    trades = [_taker("29°C", price=0.20, size=40.0, at=TRADE_AT, tx="0x1"),
              _taker("29°C", user=W2, price=0.21, size=150.0, at="2026-09-18T12:00:30Z", tx="0x2")]
    receipt, sent = _big_run(store, monkeypatch, tmp_path, trades)
    assert receipt["planned"] == 1
    plan = json.loads(_intents(store)[0][6])["plan"]
    assert plan["stake_tier"] == "momentum_agrees" and plan["size_multiplier"] == 2.0
    assert plan["episode_usdc"] == pytest.approx(39.5)
    # momentum tier 3 x 3 U = 9 U at the 0.26 cap, doubled.
    assert plan["target_shares"] == pytest.approx(2 * 9.0 / 0.26, abs=1e-3)


def test_fills_after_the_window_do_not_make_it_big(store, monkeypatch, tmp_path) -> None:
    trades = [_taker("29°C", price=0.20, size=40.0, at=TRADE_AT, tx="0x1"),
              _taker("29°C", user=W2, price=0.21, size=150.0, at="2026-09-18T12:01:30Z", tx="0x2")]
    _receipt, _sent = _big_run(store, monkeypatch, tmp_path, trades)
    plan = json.loads(_intents(store)[0][6])["plan"]
    assert plan["size_multiplier"] == 1.0 and plan["episode_usdc"] == pytest.approx(8.0)


def test_a_late_fill_on_the_tape_tops_the_leg_up(store, monkeypatch, tmp_path) -> None:
    first = _taker("29°C", price=0.20, size=40.0, at=TRADE_AT, tx="0x1")
    late = _taker("29°C", user=W2, price=0.21, size=150.0, at="2026-09-18T12:00:30Z", tx="0x2")
    sent: list = []
    _big_run(store, monkeypatch, tmp_path, [first], sent=sent)
    assert len(_intents(store)) == 1
    base = float(_intents(store)[0][5])
    # The second wallet's fill reaches the public tape a few minutes later: the episode turns big
    # inside the take window and the next pass buys the difference (on a book fresh then).
    _insert(store, "basket_official_ws_snapshot", {
        "snapshot_id": "snap2", "market_id": "m", "station_id": ST, "event_date": ED, "settlement_timezone": TZ,
        "sequence": 2, "observed_at": "2026-09-18T12:05:50.000Z", "quote_version": "v2",
        "quote_observed_at": "2026-09-18T12:05:50.000Z", "quote_expires_at": "2026-09-18T13:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0, "buckets_json": json.dumps(_book()),
        "state": "COMMITTED", "created_at": "2026-09-18T12:05:50.000Z",
    })
    _big_run(store, monkeypatch, tmp_path, [first, late], now="2026-09-18T12:06:00.000Z", seed=False, sent=sent)
    intents = _intents(store)
    refusals = store.execute(f"SELECT evaluated_at_utc, refusal, gap_shares FROM {lane.EVALUATION_TABLE}").fetchall()
    assert len(intents) == 2, refusals
    top_up = json.loads(intents[1][6])["plan"]
    assert top_up["size_multiplier"] == 2.0 and float(intents[1][5]) == pytest.approx(base, rel=0.05)


def test_the_lanes_file_turns_big_episodes_on() -> None:
    cfg = strategy_lanes.lane_settings(strategy_lanes.LANE_COPY_FOLLOW,
                                       ROOT / "config" / "trading" / "strategy_lanes_v1.json")
    assert lane.big_episode_rule(cfg) == (30.0, 60.0, 2.0)
