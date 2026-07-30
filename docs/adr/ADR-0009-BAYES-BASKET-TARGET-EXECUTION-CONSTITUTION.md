---
status: accepted
authority: L0
owner: bayes_basket_target
effective_from: 2026-07-26
---

# ADR-0009：Bayes Basket Target 执行宪章

- 状态：Accepted（M1-11 合同与静态门）
- 生效范围：target `production_no_live` runtime closure
- Live 授权：`false`
- 关联路线：roadmap M1-11、M6-04、M6-06、M6-07、M8-04

## 决策

target 篮子执行只有一个可执行合同：

```text
目标库存 q*
→ post-only Maker
→ 确认撤销
→ 有价格上限的单桶 FOK
→ 接收成交/撤单回执
→ 以新 SSOT 重算目标库存、成本和利润曲线
```

`q*` 是当前唯一概率、库存、盘口、风险、预算和策略版本状态下的目标
库存。订单动作只表达正库存缺口，不由固定桶数、固定角色或现金阶段直接
生成。

## 执行状态合同

### 1. 目标库存与 Maker

- 先求每桶 `q*`，再求 `gap = max(q* - filled - confirmed_pending, 0)`。
- `gap <= 0` 不产生新增动作。
- 非紧迫路径只能生成 `post-only` Maker 候选；Maker 采用官方盘口、队列、
  未成交机会成本、逆向选择和显式保留价。
- Maker 确定性部分成交按实际成交份额进入新状态；未成交部分只进入
  `confirmed_pending`，不进入已实现结算地板。

### 2. Maker 到 FOK

- 只有正库存缺口、后验/lineage/盘口仍新鲜、风险/预算门通过、时间紧迫并且
  同桶 Maker 撤单已收到权威确认时，才允许进入 FOK 收口。
- FOK 必须是单桶、全成或零成、具有限价上限的候选；不得把多个桶组成全腿
  原子 FOK。
- 撤单请求不等于撤单完成。撤单未确认时不得发送 FOK。
- FOK 失败、价格越限、深度不足或回报未知时不得猜测重发，必须重新读取
  当前 SSOT 并进入 `HOLD` 或 `reconcile_required`。

### 3. 回执与重算

- 每次 Maker 成交、部分成交、撤单确认、FOK 成交或 FOK 失败都必须用新的
  官方 WS、库存和订单状态版本重算 `q*`、正缺口、净成本、保护地板和
  `DeltaBasketScore`。
- 任何未知成交、迟到成交、撤单竞态、撤单未确认或版本漂移都恒定进入
  `reconcile_required`。
- 未经对账清除，不得新挂同桶订单、转发其他桶动作或通过重试绕过状态机。

## 禁止语义

target runtime closure 禁止出现下列执行语义：

- 固定 `4→3→2` 桶数或任何固定阶段桶数序列；
- 阶段内等份、固定 `2:1` 或由现金阶段直接决定份额；
- 逐腿 FOK、全腿必成、多腿原子 FOK 或立即阶段的全腿门；
- 未确认撤单后直接进入 FOK；
- 未知成交后猜测补单；
- 成交后不重算目标库存和利润曲线。

上述词语在本 ADR、路线图和测试反例中可以作为“被禁止对象”说明，但这些
文档和测试不是 runtime closure，不能作为静态门的扫描输入。

## `production_no_live` 边界

在当前部署模式下，以下值是硬合同而非运行时建议：

```text
execution_mode = production_no_live
live_authorized = false
live_submit_authorized = false
places_orders = false
calls_order_gateway = false
orders_submitted = 0
```

静态门通过不等于获得 live 授权；live 仍需独立的 L0 宪章、人工授权和后续
切换门。任何静态门失败都必须保持 no-live，并阻断候选执行。

## 静态门范围与反误报规则

唯一入口为：

```text
scripts/bayes_basket_target_execution_constitution_audit.py
```

默认只扫描脚本中声明的 target runtime allowlist，包括 resident daemon、
production worker/compiler、M5 ledger/reconciliation 和 basket kernel。它：

- 不扫描仓库全树、ADR/其他 Markdown、`tests/`、`data/`、`output/`、旧生产线
  或源账户回放；
- 不把 Python comments、模块/类/函数 docstring 当作执行语义；
- 保留扫描 runtime string literal，因为 reason code 和 contract constant
  可能是实际状态机输入；
- 对文件数量、单文件字节数、总字节数和命中数设硬上限，超限立即 fail
  closed；
- 只读解析 AST/token，不导入 target runtime、不打开数据库、不访问网络、
  不调用 gateway。

静态门必须同时证明：目标库存和 post-only Maker 合同存在；确认撤单先于
单桶 FOK；成交后重算存在；未知/未确认状态进入 `reconcile_required`；
`production_no_live` 的 live 授权为 false；并且禁止语义在受控 runtime
范围内零命中。

## 验收

```bash
python3 scripts/bayes_basket_target_execution_constitution_audit.py
python3 -m pytest -q tests/test_bayes_basket_target_execution_constitution_audit.py
```

静态门当前发现旧 immediate-stage 全腿 FOK 代码时，预期返回非零并列出精确
文件/行号；这属于正确的 fail-closed 结果，不能通过修改 ADR、路线图或测试
反例来消除。只有 runtime closure 完成迁移并重新验收后，门才可返回 `ok=true`。
