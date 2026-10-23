"""Convergence confidence (user 2026-10-23 「大概什么时候温度开始靠近并收敛，趋于真值，
给我一个信心指标，用100分表示，并前端显示！」)."""

from bayes_basket_target import estimate_hit_readings as E


def _day(truth: str, hours: dict[int, dict[str, str | None]]) -> dict:
    curve = {str(h): hours.get(h, {}) for h in E.CURVE_HOURS}
    return {"truth": truth, "curve": curve, "rank": {"19": 0, "20": 1, "21": 2, "22": 3}}


def test_agreement_counts_the_voters_present_and_ties_go_to_the_market() -> None:
    a = E.agreement({"F": "20", "D0": "21", "MOS": "21", "P1": "20", "market": "20", "member_bayes": "21"})
    assert (a["bucket"], a["agree"], a["voters"], a["bin"]) == ("20", 3, 6, "half")
    a = E.agreement({"D0": "21", "P1": "21", "market": "21", "MOS": None})
    assert (a["agree"], a["voters"], a["bin"]) == (3, 3, "all")
    assert E.agreement({"D0": "21", "P1": "21"}) is None  # fewer than three voters


def test_the_table_scores_each_hour_and_agreement_and_shrinks_thin_cells() -> None:
    all_on = {m: "21" for m in E.CONVERGE_METHODS}
    split = {"F": "19", "D0": "20", "MOS": "21", "P1": "22", "market": "21", "member_bayes": "20"}
    cache = {f"S{i}|2026-10-20": _day("21", {15: all_on, 9: split}) for i in range(10)}
    conv = E._convergence(cache)
    cell = conv["confidence"]["15"]
    assert cell["n"] == 10 and cell["hit"] == 1.0 and cell["bins"]["all"]["score"] == 100
    s = E.confidence_score(conv["confidence"], 15, all_on)
    assert s["score"] == 100 and s["n"] == 10 and s["bin"] == "all"
    # No table cell for the hour -> no score, not a guess.
    assert E.confidence_score(conv["confidence"], 3, all_on) is None


def test_the_lock_hour_is_the_first_hour_from_which_a_method_stays_on_the_truth() -> None:
    hours = {h: {"market": "21" if h >= 12 else "20"} for h in E.CURVE_HOURS}
    hours[14] = {"market": "22"}
    conv = E._convergence({"S|2026-10-20": _day("21", hours)})
    assert conv["lock"]["market"]["median_hour"] == 15
    assert conv["by_hour"]["market"]["11"]["bucket_err"] == 1.0
