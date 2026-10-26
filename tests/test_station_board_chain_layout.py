"""The station-day cards stand in the relationships they have (user 2026-10-26
「把卡片改为逻辑清晰的关系」): the production chain -- the deterministic
forecasts feed P0's regression, P0 is P1's root -- with what each step adds on
its arrow, then the read-only estimates beside it."""

from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = SOURCE.index(f"function {name}(")
    return SOURCE[start:SOURCE.index("\n}\n", start)]


def test_the_day_draws_the_chain_then_the_read_only_estimates() -> None:
    day = _function("sdDay")
    assert "${sdChain(day)}" in day and "sd-six" not in SOURCE
    chain = _function("sdChain")
    # Input -> P0 -> P1, in that order, each arrow naming what the step adds.
    order = [chain.index(k) for k in ("sdDeterministic(day, true)", "p.P0", '"× 实况"', "p.P1")]
    assert order == sorted(order) and '"回归输入"' in chain
    # The read-only estimates sit under their own heading, never in the chain.
    start = chain.index("const side")
    side = chain[start:chain.index("\n", start)]
    for card in ("sdMemberBayes(day)", "sdD0(day)", "sdMos(day)", "sdWrf(day)"):
        assert card in side
    assert chain.index("旁证") > chain.index("p.P1") and "只读，不进生产" in chain


def test_in_the_chain_the_deterministic_headline_is_what_enters_p0() -> None:
    det = _function("sdDeterministic")
    assert "if(chain && ngr.f_c != null)" in det and 'tag: "P0 的输入"' in det
    assert "挂牌前 F" in det
