---
status: archived
authority: L2
owner: weather-system
effective_from: 2026-07-19
---

# ZUUU 概率前二桶 Maker 实时配平设计

## 结论

目标是事件驱动的 maker-only 双腿配平，不是同时提交两个独立挂单。每个周期先挂
YES，只有 YES 成交结果已由交易所确认后，才挂不超过已成交 YES 数量的 NO。
最终目标是 `YES 持仓 = NO 持仓`；“偏向 YES”仅指配平前的瞬时敞口方向，不能把
永久 YES 超配称为套利。

本设计当前只是无 I/O 的策略状态机参考实现，不读取密钥、不写数据库、不生成
decision/intent、不调用订单网关，也未被任何生产服务加载。生产若启用，只能嵌入
现有获授权服务并通过本机唯一 `clob_order_service`，不得新增第四个生产服务。

参考实现：`maker_pair_state_machine.py`。

## 概率前二桶

每个完整官方 CLOB WebSocket 快照同时使用 YES 和 NO 两本盘口：

```text
p_yes_mid = (YES best_bid + YES best_ask) / 2
p_yes_from_no = 1 - (NO best_bid + NO best_ask) / 2
p_market_yes = (p_yes_mid + p_yes_from_no) / 2
```

按 `p_market_yes` 降序选择正好两个桶；概率相同时优先双边总 spread 更小的桶，再按
桶名稳定排序。任一边缺 bid/ask、盘口倒挂、快照超过 2 秒或官方 tick/费用证据不全，
该桶不得参与排名。新入选桶连续 3 个 WS 版本都在前二后才允许新增 YES 风险；旧桶
掉出前二时立即撤销未成交 YES。旧桶已有成交仓位时仍可继续挂 NO 配平或退出 YES，
但不得继续增加 YES，这属于风险收敛而非第三个交易桶。

## 硬不变量

对每个桶按交易所已确认成交与仍可能成交的在途订单计算：

```text
潜在 NO 过量 = NO已成 + NO买单在途 + YES卖单在途 - YES已成 <= 0
最坏 YES 单腿 = YES已成 + YES买单在途 - NO已成 <= 5股
两个桶最坏 YES 单腿之和 <= 10股
NO买单与YES卖单不得同时在途
```

取消结果未知的订单继续按“仍可能全部成交”计入在途量。任何不变量失效、订单结果
未知、撤单结果未知或成交数量无法核对时，状态直接进入 `RECONCILE_REQUIRED`，冻结
所有新单；不得用本地预期替代交易所证据。

默认每桶只做一组 5 股，正好等于当前交易所最小委托量。完整配平后每桶最多持有
`5 YES + 5 NO`，持有到结算，不循环叠加仓位。

## 状态机

| 状态 | 含义 | 允许动作 |
|---|---|---|
| `PAUSED` | WS、费用、tick 或全局证据失效 | 只撤单和核对 |
| `IDLE` | 无成交、无在途订单 | 仅当前稳定前二桶可评估 YES |
| `YES_WORKING` | 5股 post-only YES 在途 | 等成交、必要时撤改，不挂 NO |
| `YES_EXPOSED` | 有已确认 YES，尚未达到/完成配平 | 等待最小对冲手数、挂 NO 或退出 |
| `NO_HEDGING` | 不超过已成 YES 的 post-only NO 在途 | 等成交、撤改，不新增 YES |
| `BALANCED` | YES 与 NO 各5股 | 撤销余单，持有到结算 |
| `EXITING_YES` | NO 对冲超时，撤清 NO 后 maker 卖出 YES | 只允许减小 YES |
| `YES_RESIDUAL_HOLD` | 不足5股且无法再合法 maker 配平的 YES 碎片 | 自动持有到结算，不再加仓 |
| `RECONCILE_REQUIRED` | 订单/撤单/成交结果不确定 | 冻结新单，自动核对交易所直至恢复 |

主路径：

```text
IDLE -> YES_WORKING -> YES_EXPOSED -> NO_HEDGING -> BALANCED
                         |                 |
                         +-----------> EXITING_YES
任意状态 --结果未知--> RECONCILE_REQUIRED
任意状态 --证据失效--> PAUSED
```

## 入场与配平价格

入场前必须同时存在两条非跨盘 maker 参考价和逐 token 官方费用证据：默认
`best_bid + 1 tick`；若下一 tick
会触及 ask，则只加入 best bid。只有参考价满足：

```text
YES拟挂价 + NO参考拟挂价 + 官方maker费用 <= 1 - 0.005
```

才先提交 5 股 `GTC BUY YES, post_only=true`。绝不同时预挂 5 股 NO，因为 NO 可能先成。

YES 成交满 5 股并确认余单状态后，NO 数量严格等于未配平 YES。NO 价格同时受实时
maker 价和已成交成本约束：

```text
NO挂价 <= 1 - YES成交VWAP - 当前要求净利差
```

前 15 秒要求每份至少 0.005 USDC；15–60 秒线性放宽到盈亏平衡，但默认绝不自动
接受负配平收益。60 秒仍未完成时，先撤销并确认 NO 已无成交可能，再进入
`EXITING_YES`，以 `best_ask - 1 tick` 的 post-only YES 卖单退出。maker-only 无法保证
在固定时间成交，因此“实时”表示每个 WS、订单和成交事件都立即重算，不表示原子或
定时必成。

## 部分成交

5股 YES 可能只成交不足5股。此时新挂5股 NO 会违反 YES 偏置，而小于5股的新委托
又可能低于交易所最小量。因此：

1. 保留原 YES 余单，最多等待首笔成交后 30 秒，且最坏单腿始终不超过5股。
2. 满5股后撤清任何余单并挂等量 NO。
3. 30秒仍不足5股则先撤单并确认；不足最小委托量的残余自动进入
   `YES_RESIDUAL_HOLD`，持有到结算并停止该桶后续周期。

运行时不依赖人工介入。订单结果未知时由 reconciliation 自动重试；在恢复明确交易所
终态前始终冻结新增风险。若长期无法恢复，系统保持冻结和通知，不猜测成交、不反向补单。

## 撤改单

所有替换必须执行 `cancel -> exchange确认终态/成交量 -> reconciliation -> 新幂等键提交`；
禁止先发替代单再撤旧单。

安全撤单不等待最小挂单时间：桶掉出前二、订单将跨盘、组合收益失效、WS 超过2秒、
费用/tick/partition 证据失效、敞口上限可能被突破。普通追价仅在订单至少静置1.5秒且
目标价偏移至少1个 tick 时发生；小于1 tick 不撤，避免丢失队列位置和形成撤改单风暴。

每次撤单失败、超时或返回未知状态都进入 `RECONCILE_REQUIRED`。只有唯一订单网关的
order lookup 与交易所成交明细一致后，才可继续。

## 事件与持久化边界

未来启用时，状态机应由现有服务消费以下事件并逐事件重算：官方 CLOB WS 全量盘口、
本机唯一网关的订单确认、撤单确认和成交回报。不得使用 REST 价格兜底，不写行情 tick，
不得由 heartbeat/Codex 直接下单。状态、decision、intent、资本预留与成交游标必须按
现有 SSOT 规则原子提交；执行阻断原因仍只来自 `auto_trade_decisions`。

当前生产合同仍只授权既有“纯 YES 篮子 + 官方硬 NO”策略，因此本设计在明确修改策略
合同并完成零下单演练前必须保持未激活。
