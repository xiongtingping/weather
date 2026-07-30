---
status: archived
authority: L2
owner: weather-system
effective_from: 2026-07-19
---

# ZUUU 成都 maker 挂单配平实时诊断

该工具是手工启动的只读诊断，不是生产驻留服务。它不会写
`event_market_ticks`、不会刷新市场 REST 价格、不会生成 decision/intent，也不会下单。

实时盘口只来自官方 Polymarket CLOB WebSocket。启动时从官方 CLOB market-info
读取每个 condition 的最小 tick 与费用适用范围。只有官方明确返回 `taker_only=true`
时才按 maker 费用为 0 计算；可能存在的 maker rebate 不计入收益。

每条 BUY 挂单默认在当前 best bid 上改善一个最小 tick；如果改善后会跨过 best ask，
则只加入 best bid 队列。没有 best bid 时，才尝试挂在 best ask 下一个 tick。任何报价都不得跨盘成为 taker。

监控三类完整兑付组合：

- 同一桶 `YES + NO`：每份结算兑付 1 USDC。
- 全桶 `YES`：仅在本地 registry 可证明所有桶连续、互斥且穷尽时计算，每份兑付 1 USDC。
- 全桶 `NO`：同一穷尽事件有 N 个桶时，每份兑付 `N-1` USDC。

默认每腿挂 5 股。所有腿按拟挂价格全部成交后，每份完整组合净利差至少 0.005 USDC 时标记“候选”：

```bash
.venv/bin/python scripts/monitor_zuuu_balance_arbitrage.py
```

常驻终端运行时，连接异常会 fail closed 并自动重连；默认每 300 秒主动重连一次，重新加载成都当地当日唯一活跃事件和官方费用证据。它不会被安装为 launchd 服务。

一次性验收：

```bash
.venv/bin/python scripts/monitor_zuuu_balance_arbitrage.py --once
```

自定义股数、全成后净利差阈值或输出 JSON Lines：

```bash
.venv/bin/python scripts/monitor_zuuu_balance_arbitrage.py \
  --shares 10 \
  --minimum-edge 0.01 \
  --json-lines
```

输出同时显示拟挂价、当前 best bid/best ask、报价模式和前方排队量。没有卖盘不阻止 maker BUY 挂单；
只要存在有效 best bid，仍可选择改善或加入买单队列。

“候选”只表示所有腿最终全部成交后的完整组合收益，不是已经锁定的套利。多腿挂单没有组级原子成交，
单腿成交会形成裸露风险；任何自动执行都必须另行设计双腿状态机、撤单、资本预留和 reconciliation，
本工具不授予这些权限。

概率前二桶、YES 先行敞口和 maker-only 配平的未激活参考设计见
`docs/ZUUU_TOP2_MAKER_BALANCE_STATE_MACHINE.md`。
