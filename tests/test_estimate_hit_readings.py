from bayes_basket_target import estimate_hit_readings as E


def test_summarize_counts_hits_and_pairs_against_the_market():
    cache = {
        "ZUUU|2026-10-20": {"truth": "20°C", "snaps": {"13": {"P1": "20°C", "market": "21°C", "D0": "20°C"}}},
        "ZUUU|2026-10-21": {"truth": "22°C", "snaps": {"13": {"P1": "21°C", "market": "22°C"}}},
        "EGLC|2026-10-21": {"truth": None, "snaps": {"13": {"P1": "15°C"}}},
    }
    s = E._summarize(cache)
    p1 = s["fleet"]["13"]["P1"]
    assert (p1["n"], p1["hits"], p1["pair_n"], p1["pair_hits"], p1["pair_market_hits"]) == (2, 1, 2, 1, 1)
    assert s["fleet"]["13"]["market"]["pair_n"] == 0
    assert s["stations"]["ZUUU"]["13"]["D0"]["hits"] == 1
    assert s["fleet"]["10"]["P1"]["n"] == 0
    assert s["days"] == ["2026-10-20", "2026-10-21"]


def test_wrf_pick_is_the_newest_issue_out_by_then(monkeypatch):
    """WRF re-runs on every new scan (user 2026-10-25 「有新数据就应该跑」): at any moment the
    pick is the newest issue whose ready time has passed, the frozen 11:10 issue before that."""

    import sqlite3  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_probability_envelope (station_id, event_date, state, stage, decision_at_utc, probabilities_json)")
    conn.execute("CREATE TABLE basket_official_ws_snapshot (station_id, event_date, state, observed_at, buckets_json)")
    monkeypatch.setattr(E, "_label_of", lambda ladder, v: None if v is None else f"{v:g}")
    wrf = {"2026-10-25": [{"ready_utc": "2026-10-25T03:27:26Z", "wrf_sat_tmax_with_obs_c": 25},
                          {"ready_utc": "2026-10-25T05:20:00Z", "wrf_sat_tmax_with_obs_c": 24}]}
    zone = ZoneInfo("Asia/Shanghai")

    def pick(hour, minute=0):
        return E.picks_at(conn, "ZUUU", "2026-10-25", datetime(2026, 10, 25, hour, minute, tzinfo=zone), None,
                          f=None, wrf=wrf, d0={}, mb={}, mos=None)["WRF"]

    assert (pick(11), pick(12), pick(13), pick(14)) == (None, "25", "25", "24")
    assert E.picks_at(conn, "EGLC", "2026-10-25", datetime(2026, 10, 25, 14, tzinfo=zone), None,
                      f=None, wrf=wrf, d0={}, mb={}, mos=None)["WRF"] is None
