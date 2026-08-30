#!/usr/bin/env python3
"""What stands between this book and a live order, measured rather than recalled.

There is no go-live migration script in this repository, and this file is not
one.  It is the read-only preflight the migration would have to pass, written
because "B2 三重契约→go-live 迁移" was a name on a blocker list and not an
inventory: the phrase said three locks, the code has nine, and two of them are
SQL CHECK constraints on tables holding 44k rows of production history.  That
last fact is why the step is called a migration at all -- SQLite cannot alter a
CHECK in place, so those tables must be rebuilt and swapped, which is a
maintenance window and not a config edit.

Every check reads the live system: the files on disk, the schema in the SSOT,
the gateway's own health endpoint, and the exchange's geoblock probe.  Nothing
here writes, and nothing here reads a credential value -- the funds check looks
only at whether key names are present in .env, because whether the account is
funded is the operator's to state and not mine to discover.

Ownership is the column that matters.  ``code`` gates are ours to change and
each names the edit; ``operator`` gates are the user's explicit safety settings
and this script will never flip one (the 2026-08-11 ruling: 第 2、3 层是用户的
显式安全设定，不要代为翻转); ``external`` gates are outside the machine
entirely.  A green board here is a precondition for writing the migration, not
permission to trade.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SSOT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"

PASS = "PASS"
BLOCK = "BLOCK"
WARN = "WARN"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _check(
    gate: str,
    layer: int,
    owner: str,
    state: str,
    detail: str,
    remedy: str,
) -> dict[str, Any]:
    return {
        "gate": gate,
        "layer": layer,
        "owner": owner,
        "state": state,
        "detail": detail,
        "remedy": remedy,
    }


# --------------------------------------------------------------------------
# Layer 0 -- the operator's arming flag.
# --------------------------------------------------------------------------


def check_intent_flag() -> dict[str, Any]:
    intent = _json(ROOT / "config" / "trading" / "live_intent_v1.json")
    armed = intent.get("live_enabled") is True
    allowlist = intent.get("station_allowlist") or []
    return _check(
        "意向旗 live_enabled",
        0,
        "operator",
        PASS if armed else BLOCK,
        (
            f"live_enabled={intent.get('live_enabled')}"
            f" armed_by={intent.get('armed_by')}"
            f" allowlist={len(allowlist)}站"
        ),
        "看板「Live 上线准备」输入 LIVE 解锁；这是操作者的动作，脚本不代劳。",
    )


# --------------------------------------------------------------------------
# Layer 1 -- the SQL CHECK constraints.  This is the migration proper.
# --------------------------------------------------------------------------


def check_schema_constraints() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not SSOT_DB.is_file():
        return [
            _check(
                "SSOT schema CHECK",
                1,
                "code",
                WARN,
                "SSOT 库不在此机",
                "在生产机上重跑。",
            )
        ]
    conn = sqlite3.connect(f"file:{SSOT_DB}?mode=ro", uri=True, timeout=60.0)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
            " AND sql LIKE '%live_submit_authorized%'"
        ).fetchall()
        # Only the intents table gates an order.  basket_p0_preselection_plan
        # carries the same column and is out of scope: its own compiler says
        # that path "never creates an intent, reservation, order, or gateway
        # call", and its five no-live constraints describe what a forward-day
        # preselection is rather than what the lane may do.  Reporting it as a
        # blocker (as this did on 2026-08-30) points the migration at 42k rows
        # it must not touch.
        for name, sql in rows:
            flat = (sql or "").replace(" ", "")
            locked = "CHECK(live_submit_authorized=0)" in flat
            count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            on_order_path = name == "basket_target_order_intents"
            out.append(
                _check(
                    f"CHECK 约束 {name}",
                    1,
                    "code" if on_order_path else "n/a",
                    (BLOCK if locked else PASS) if on_order_path else PASS,
                    (
                        f"CHECK(live_submit_authorized=0) 生效，{count} 行历史"
                        if locked and on_order_path
                        else (
                            f"{count} 行；不在下单路上，迁移不动它"
                            if locked else f"无 CHECK 锁，{count} 行"
                        )
                    ),
                    (
                        "SQLite 不能原地改 CHECK：建新表→拷贝→换名，需停机窗口。"
                        " 这一条就是「迁移」二字的来源。"
                        " 脚本：scripts/go_live_migration.py"
                        if on_order_path
                        else "无需处置：前瞻预选按设计从不建 intent 或下单。"
                    ),
                )
            )
    finally:
        conn.close()
    return out or [
        _check(
            "SSOT schema CHECK",
            1,
            "code",
            WARN,
            "未找到带 live_submit_authorized 的表",
            "确认库路径。",
        )
    ]


# --------------------------------------------------------------------------
# Layers 2-5 -- the code contracts.
# --------------------------------------------------------------------------


def check_activation_validator() -> dict[str, Any]:
    source = _read(ROOT / "bayes_basket_target" / "activation.py")
    # Resolved once a live validator exists beside the no-live one.  The
    # no-live refusal staying in place is the point, not the blocker: both
    # lanes have to keep working, so this asks whether a live contract has a
    # validator at all -- not whether the other one was relaxed.
    has_live = "def validate_live_activation(" in source
    refuses = (
        'row.get("execution_mode") != "production_no_live"' in source
        and not has_live
    )
    falses = re.search(
        r"for key in \(\s*((?:\s*\"[a-z_]+\",\s*)+)\s*\):", source
    )
    keys = re.findall(r'"([a-z_]+)"', falses.group(1)) if falses else []
    return _check(
        "激活合约校验器",
        2,
        "code",
        BLOCK if refuses else PASS,
        (
            f"validate_target_activation 硬拒非 production_no_live，"
            f"并要求 {len(keys)} 个键恒 False：{','.join(keys[:4])}…"
            if refuses
            else "validate_live_activation 已就位（增量，未放松 no-live 校验器）"
        ),
        (
            "需新增 schema bayes_basket_target_production_live_activation_v1"
            " 与其校验器；合约 sha256 自封，改字段必须重算。"
        ),
    )


def check_runtime_config_loader() -> dict[str, Any]:
    source = _read(ROOT / "bayes_basket_target" / "runtime_config.py")
    # The loader builds its return dict from literals, so editing the JSON
    # cannot produce a live config -- that is the point worth reporting.
    # Same shape as the activation check: the no-live loader still pinning
    # its five literals is correct, so this asks whether a live loader exists.
    hardcoded = (
        '"live_submit_authorized": False,' in source
        and "def load_target_live_runtime_config(" not in source
    )
    return _check(
        "runtime config 加载器",
        3,
        "code",
        BLOCK if hardcoded else PASS,
        (
            "返回值把 5 个 live 边界写死为 False，改 JSON 无效"
            if hardcoded
            else "load_target_live_runtime_config 已就位（no-live 加载器仍钉死 5 个边界）"
        ),
        "需 live 变体加载器；勿在原文件放宽，no-live 车道还要用它。",
    )


def check_execution_mode_constant() -> dict[str, Any]:
    hits = []
    for rel in ("bayes_basket_target/production_worker.py",
                "bayes_basket_target/production.py"):
        for i, line in enumerate(_read(ROOT / rel).splitlines(), 1):
            if line.startswith("PRODUCTION_EXECUTION_MODE = "):
                hits.append(f"{rel}:{i}")
    return _check(
        "执行模式常量",
        4,
        "code",
        BLOCK if hits else PASS,
        f"production_no_live 硬编码于 {len(hits)} 处：{'; '.join(hits)}",
        "两处必须同步，分叉会让 worker 与提交路各说各话。",
    )


def check_writer_gate() -> dict[str, Any]:
    source = _read(ROOT / "bayes_basket_target" / "production_worker.py")
    gated = "target_writer_activation_not_production_no_live" in source
    return _check(
        "写手闸 open_target_writer_if_enabled",
        5,
        "code",
        BLOCK if gated else PASS,
        (
            "激活合约非 no-live 即 raise，含 network_access 必须 False"
            if gated
            else "写手闸已放开"
        ),
        "随激活合约一起改；这是 SSOT 写入的总闸。",
    )


def check_capital_policy() -> dict[str, Any]:
    policy = _json(ROOT / "config" / "trading" / "portfolio_capital_policy_v1.json")
    scope = str(policy.get("authority_scope") or "")
    falses = [
        key
        for key in ("calls_order_gateway", "places_orders",
                    "live_submit_authorized")
        if policy.get(key) is False
    ]
    blocked = scope.endswith("NO_LIVE_CANDIDATE") or bool(falses)
    return _check(
        "组合资金政策",
        5,
        "code",
        BLOCK if blocked else PASS,
        (
            f"authority_scope={scope}，{len(falses)} 个 live 边界为 False；"
            f"风险敞口 {policy.get('portfolio_risk_fraction')}"
        ),
        "迁移须把 authority_scope 升为 live 并重签；风险分数是真实敞口，先复核。",
    )


# --------------------------------------------------------------------------
# Layer 6 -- the gateway.  Note the inversion.
# --------------------------------------------------------------------------


def check_gateway_launcher() -> dict[str, Any]:
    source = _read(ROOT / "scripts" / "clob_order_service.sh")
    refuses_live = 'PM_TARGET_EXECUTION_MODE:-}" != "production_no_live"' in source
    return _check(
        "网关启动器（反向闸）",
        6,
        "code",
        BLOCK if refuses_live else PASS,
        (
            "clob_order_service.sh 在模式≠production_no_live 时 exit 78"
            "——网关拒绝以 live 模式启动，方向与其它闸相反"
            if refuses_live
            else "启动器接受 live 模式"
        ),
        "迁移必须同时改启动器，否则改完模式网关直接起不来。",
    )


def check_pause_flag() -> dict[str, Any]:
    flag = ROOT / "data" / "live_paused.flag"
    exists = flag.is_file()
    return _check(
        "网关暂停旗",
        6,
        "operator",
        BLOCK if exists else PASS,
        (
            f"{flag.name} 存在（置于 "
            f"{datetime.fromtimestamp(flag.stat().st_mtime, tz=UTC):%Y-%m-%d}），"
            "网关在签名与 POST 之前拒绝每一次 place-order"
            if exists
            else "无暂停旗"
        ),
        "操作者移除；这是最后一道人工急停，脚本不代劳。",
    )


def check_gateway_health() -> list[dict[str, Any]]:
    try:
        raw = subprocess.run(
            ["curl", "-s", "--max-time", "10",
             "http://127.0.0.1:8765/health"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        health = json.loads(raw)
    except Exception:
        return [
            _check(
                "网关 /health",
                6,
                "code",
                WARN,
                "网关无响应",
                "确认 com.weather.clob_order_service 在跑。",
            )
        ]
    region = health.get("region") or {}
    proxy_region = (health.get("proxy") or {}).get("region") or {}
    caps = health.get("capabilities") or []
    out = [
        _check(
            "网关能力",
            6,
            "code",
            PASS if "place_order_v1" in caps else BLOCK,
            f"read_only={health.get('read_only')} 能力={len(caps)} 项",
            "网关侧已具备下单能力，不是缺口。",
        ),
        # Not an ``external`` gate.  ``blocking_orders`` is computed from
        # ``polymarket.com/api/geoblock`` -- the *website's* restriction --
        # and clob_order_service.mjs:2798 turns it into a local pre-rejection
        # that never signs and never posts.  The exchange's own answer is a
        # different question, and the record separates them: the three real
        # "Trading restricted in your region" responses are all 2026-08-07,
        # from the US egress.  Every rejection since the gate landed
        # (2026-08-09, 81d119f0) is our own ``clob_region_blocked:JP``, so
        # the CLOB has not been asked from this egress even once.
        _check(
            "区域预拒闸（本地，非交易所）",
            6,
            "code",
            BLOCK if region.get("blocking_orders") else PASS,
            (
                f"网关按网站 geoblock 自我预拒："
                f"country={region.get('country') or proxy_region.get('country') or '?'}"
                f" blocked={region.get('blocked')}"
                f" 探针={region.get('error') or 'ok'}"
                f"；此闸 fail-open（探针失败即放行），"
                "且交易所本身自 2026-08-09 起从未被问过"
            ),
            (
                "旋钮 PM_CLOB_IGNORE_REGION_BLOCK=1 可关闸。"
                "⚠️ 先读 check_asos_bypass 的警告：ASOS 车道绕过暂停旗与只读位，"
                "这道闸是它唯一的阻拦。"
            ),
        ),
    ]
    return out


def check_asos_bypass() -> dict[str, Any]:
    """Who actually gets stopped by the pause flag -- and who does not.

    ``if (livePausePresent()) { if (!isAsosLive) { reject } }``: the ASOS
    argmax lane is exempt from the pause flag, and from the read-only flag
    directly above it.  So for that lane the region pre-rejection is not one
    gate among several, it is the last one.  Anybody reaching for
    PM_CLOB_IGNORE_REGION_BLOCK has to know that before they reach.
    """

    source = _read(ROOT / "scripts" / "clob_order_service.mjs")
    bypasses_pause = "if (livePausePresent()) {\n      if (!isAsosLive) {" in source
    launcher = _read(ROOT / "scripts" / "clob_order_service.sh")
    asos_live = "PM_ASOS_ARGMAX_LIVE_ENABLED=1" in launcher
    return _check(
        "ASOS 车道的暂停旗豁免",
        6,
        "operator",
        WARN if (bypasses_pause and asos_live) else PASS,
        (
            "ASOS 车道绕过暂停旗与只读位（isAsosLive），"
            "区域预拒是它唯一剩下的闸——关闸即可能真下单"
            if (bypasses_pause and asos_live)
            else "ASOS 无豁免，或未启用"
        ),
        "关区域闸之前，先决定 ASOS 车道是否也该放行；两者是同一个开关。",
    )


# --------------------------------------------------------------------------
# Layer 7 -- outside the machine.
# --------------------------------------------------------------------------


def check_geoblock_direct() -> dict[str, Any]:
    try:
        raw = subprocess.run(
            ["curl", "-s", "--max-time", "15",
             "https://polymarket.com/api/geoblock"],
            capture_output=True, text=True, timeout=25,
        ).stdout
        probe = json.loads(raw)
    except Exception:
        return _check(
            "地区探针（直连）", 7, "external", WARN,
            "探针无响应", "联网后重跑。",
        )
    # Reported, not scored: this endpoint is the website's, and the ledger
    # of real attempts shows the exchange answering differently.  Calling it
    # BLOCK is what produced the wrong verdict on 2026-08-30.
    return _check(
        "网站 geoblock 探针（仅参考）",
        7,
        "external",
        WARN if probe.get("blocked") else PASS,
        f"country={probe.get('country')} region={probe.get('region')}"
        f" blocked={probe.get('blocked')}"
        "（网站口径；不等于 CLOB 拒单）",
        "要证明交易所口径只能真发一单，那是操作者的动作。",
    )


def check_credentials_present() -> dict[str, Any]:
    """Key *names* only.  Values are the operator's and are never read."""

    env = _read(ROOT / ".env")
    names = {
        line.split("=", 1)[0].strip()
        for line in env.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    wanted = {"PRIVATE_KEY", "POLY_API_KEY", "PM_FUNDER_ADDRESS"}
    have = sorted(wanted & names)
    missing = sorted(wanted - names)
    return _check(
        "凭证存在性",
        7,
        "external",
        PASS if not missing else BLOCK,
        f"已配置键名 {have}；缺 {missing or '无'}（只看键名，不读值）",
        "资金入金与 allowance 由操作者亲手完成，我不得经手。",
    )


def check_funding_is_unstated() -> dict[str, Any]:
    return _check(
        "账户实际余额",
        7,
        "external",
        WARN,
        "本脚本不查余额：查询要动凭证，且入金是操作者的事实",
        "上线前由操作者确认余额与 venue 最低单额（B4）。",
    )


# --------------------------------------------------------------------------


def main() -> int:
    checks: list[dict[str, Any]] = [check_intent_flag()]
    checks.extend(check_schema_constraints())
    checks.append(check_activation_validator())
    checks.append(check_runtime_config_loader())
    checks.append(check_execution_mode_constant())
    checks.append(check_writer_gate())
    checks.append(check_capital_policy())
    checks.append(check_gateway_launcher())
    checks.append(check_pause_flag())
    checks.extend(check_gateway_health())
    checks.append(check_asos_bypass())
    checks.append(check_geoblock_direct())
    checks.append(check_credentials_present())
    checks.append(check_funding_is_unstated())

    checks.sort(key=lambda row: (row["layer"], row["gate"]))
    blocked = [row for row in checks if row["state"] == BLOCK]
    by_owner: dict[str, int] = {}
    for row in blocked:
        by_owner[row["owner"]] = by_owner.get(row["owner"], 0) + 1

    if "--json" in sys.argv:
        print(json.dumps(
            {
                "schema": "go_live_migration_preflight_v1",
                "migration_script_exists": False,
                "checks": checks,
                "blocked_count": len(blocked),
                "blocked_by_owner": by_owner,
            },
            ensure_ascii=False, indent=2, sort_keys=True,
        ))
        return 0

    # Pad on display width, not character count: the gate names are CJK and
    # a str.ljust on them lands the detail column in a different place on
    # every row.
    def wide(text: str, width: int) -> str:
        span = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
        return text + " " * max(1, width - span)

    print("go-live 迁移预检 — 迁移脚本本身不存在，本表是它必须先过的门\n")
    print(wide("层", 4) + wide("归属", 11) + wide("状态", 8)
          + wide("闸", 40) + "说明")
    print("-" * 116)
    for row in checks:
        print(
            wide(str(row["layer"]), 4) + wide(row["owner"], 11)
            + wide(row["state"], 8) + wide(row["gate"], 40) + row["detail"]
        )
    print("-" * 116)
    print(f"\n阻断 {len(blocked)} 项：" + "、".join(
        f"{owner} {count} 项" for owner, count in sorted(by_owner.items())
    ))
    print("\n各阻断项的处置：")
    for row in blocked:
        print(f"  [{row['owner']}] {row['gate']}\n      {row['remedy']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
