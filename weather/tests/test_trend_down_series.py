import scan_weather_market as swm


def test_trend_stats_expose_up_and_down_consistency():
    pct, down_cons, up_cons, ok = swm._trend_stats_from_series(series=[1.00, 0.95, 0.96], lookback=3)
    assert ok is True
    assert pct is not None and pct < 0
    assert down_cons == 0.5
    assert up_cons == 0.5


def test_entry_down_loose_can_block_on_net_move_only():
    pct, down_cons, up_cons, ok = swm._trend_stats_from_series(series=[1.00, 0.95, 0.96], lookback=3)
    assert ok is True
    assert pct is not None
    # net down 4% should be considered down for entry gating even if path isn't fully consistent
    assert pct <= -0.02
    # strict down would NOT fire at min_consistency=0.67
    _, _, down_strict = swm._trend_down_from_series(series=[1.00, 0.95, 0.96], lookback=3, min_move=0.02, min_consistency=0.67)
    assert down_strict is False


def test_entry_uptrend_requires_consistency():
    pct, down_cons, up_cons, ok = swm._trend_stats_from_series(series=[1.00, 1.05, 1.04], lookback=3)
    assert ok is True
    assert pct is not None and pct > 0
    assert pct >= 0.02
    assert up_cons == 0.5
    assert not (pct >= 0.02 and up_cons >= 0.67)
    pct2, _, up2, ok2 = swm._trend_stats_from_series(series=[1.00, 1.05, 1.10], lookback=3)
    assert ok2 is True
    assert pct2 is not None and pct2 >= 0.02 and up2 is not None and up2 >= 0.67
