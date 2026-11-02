"""A buy that fills re-reads the account and the book, as a sale does (user
2026-10-18 「篮子计算器成交后没有自动刷新」): the buy path re-read only the book,
so 已投入 and the holdings kept the pre-trade position."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = DASHBOARD.index(f"function {name}(")
    depth = 0
    for index in range(DASHBOARD.index("{", start), len(DASHBOARD)):
        depth += {"{": 1, "}": -1}.get(DASHBOARD[index], 0)
        if depth == 0:
            return DASHBOARD[start:index + 1]
    raise AssertionError(name)


def _run(event_type: str) -> list[str]:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the order path")
    reply = {"ok": True, "batch_id": "b", "results": [{"bucket": "21°C", "side": "YES", "event_type": event_type}]}
    script = (
        "const calls = [];\n"
        "const SD = {station: 'ZUUU', data: {days: [{event_date: '2026-10-18', market: {}}]}};\n"
        "const crypto = {randomUUID: () => 'b'};\n"
        "globalThis.confirm = () => true; globalThis.alert = () => {};\n"
        "const sdCalcState = () => ({mode: 'taker'});\n"
        "const sdOrderLegs = () => ({res: {ok: true, newTotal: 1, sunk: 0, total: 1},"
        " legs: [{bucket: '21°C', side: 'YES', shares: 3, limit_price: 0.33, cost: 1}]});\n"
        "const sdLocal = s => s; const sdAgo = s => s;\n"
        "const sdRefreshCalc = () => {};\n"
        "const sdLoadHoldings = k => calls.push('holdings:' + k);\n"
        "const sdRefreshBookOnce = k => calls.push('book:' + k);\n"
        f"globalThis.fetch = () => Promise.resolve({{ok: true, json: () => Promise.resolve({json.dumps(reply)})}});\n"
        + _function("sdPlaceOrderNow")
        + "\nsdPlaceOrderNow('2026-10-18');\nsetTimeout(() => console.log(JSON.stringify(calls)), 50);\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_filled_buy_reloads_the_holdings_and_the_book() -> None:
    assert _run("FILL") == ["holdings:2026-10-18", "book:2026-10-18"]


def test_a_buy_that_did_not_fill_reloads_nothing() -> None:
    assert _run("POST_ACK") == []
    assert _run("SUBMIT_REFUSED") == []
