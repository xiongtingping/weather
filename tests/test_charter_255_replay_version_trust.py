"""Charter 255: old replays and old records are not to be fully trusted,
above all across probability version upgrades (user ruling 2026-10-07)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _clause() -> str:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    return next(line for line in charter.splitlines() if line.startswith("255. "))


def test_the_clause_is_written_and_not_a_placeholder() -> None:
    clause = _clause()
    assert "占号" not in clause
    assert "旧回放不可全信，特别是概率版本的升级" in clause
    for part in ("（二）版本与口径必须写明", "（三）按版本分段", "（四）能作依据的只有两种", "（五）升版后复读", "（六）判例"):
        assert part in clause
    assert "posterior_version" in clause and "严格 as-of" in clause


def test_the_rules_it_puts_under_review_are_named() -> None:
    clause = _clause()
    for number in ("第 249", "251", "254"):
        assert number in clause


def test_agents_carries_the_rule() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "宪章 255" in agents and "现行段服务记录" in agents
