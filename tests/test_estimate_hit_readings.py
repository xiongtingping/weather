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
