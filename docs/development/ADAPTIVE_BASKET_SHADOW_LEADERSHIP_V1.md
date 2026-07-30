---
status: superseded
authority: L2
owner: basket-target
effective_from: 2026-07-23
---

# Adaptive Basket Shadow：桶领导权归因合同 v1

## 1. 状态

- 作用域：独立、离线、零订单 Shadow。
- 生产权限：无；不得接入现有 `4→3→2 等份 + FOK-only` 生产线。
- 实现：`adaptive_basket_shadow/leadership.py`。
- 验收：`tests/test_adaptive_basket_shadow_leadership.py` 与 `adaptive_basket_shadow_v5_acceptance.json`。

## 2. 目的

M4-13 要求把“主成本桶”和“主份额桶”从静态标签改成成交驱动的时序事实，并使每次切换都能闭合到唯一确认成交 clip；该合同不判断天气方向、概率正确性或是否应该下单。

## 3. 输入合同

`BucketFillClip` schema 为 `adaptive_bucket_fill_clip_v1`，必须包含：

| 字段 | 语义 |
|---|---|
| `clip_id` | 由其余规范字段计算的 SHA256；可省略并由纯函数生成 |
| `event_id` | 互斥最高温事件身份 |
| `bucket / ordinal` | 桶标签及事件内稳定序号 |
| `filled_at_utc / source_sequence` | UTC 成交时刻和源行确定性次序 |
| `shares / cost_usdc` | 本 clip 的确认成交份额与成本 |
| `status` | `FILLED / PENDING / UNKNOWN / CANCELLED / REJECTED` |
| `lineage` | 至少包含源文件、源文件 SHA256 和源行 ID |

未知字段被拒绝，因此盘口、后验、TAF、METAR、外部天气方向字段或目标价不能进入历史领导权计算。

## 4. 纯函数

```text
q_b(t) = Σ shares_i · 1[bucket_i=b, status_i=FILLED, time_i≤t]
k_b(t) = Σ cost_i   · 1[bucket_i=b, status_i=FILLED, time_i≤t]

share_leader(t) = argmax_b q_b(t)
cost_leader(t)  = argmax_b k_b(t)
```

平局规则固定为：更早首次成交 → 更低 `ordinal` → 桶名字典序；累计使用十进制精确运算，避免 `0.1 + 0.2` 浮点假切换。

## 5. 输出合同

| 输出 | Schema | 关键语义 |
|---|---|---|
| `BucketLeadershipSnapshot` | `adaptive_bucket_leadership_snapshot_v1` | 每个确认 clip 后的双领导桶、逐桶累计、前态 ID 和滚动 clip 根 |
| `BucketLeadershipTransition` | `adaptive_bucket_leadership_transition_v1` | 触发 clip、前后领导桶、切换布尔值、前后成本/份额差、新桶或既有桶追加 |
| `BucketLeadershipAudit` | `adaptive_bucket_leadership_audit_v1` | 全部 accepted/ignored/duplicate clip ID、快照和迁移的内容寻址总账 |

## 6. 不变量

1. 只有 `FILLED` 改变 `q_b/k_b`；pending、unknown、cancelled 和 rejected 只进入 ignored lineage。
2. 主成本桶与主份额桶分别计算，允许长期不同。
3. 每个切换必须有唯一 `triggering_clip_id`，且前后快照 ID 形成不可改写链。
4. 乱序输入先按 UTC、源序号和 clip ID 规范排序；完全重复 clip 幂等去重，同 ID 异内容拒绝。
5. 同一桶的 `ordinal` 不得在事件内变化。
6. 盘口或后验后来变化不能重写既有快照。
7. 该总账没有网络、数据库、gateway、intent 或 order 依赖。

## 7. 已覆盖边界

- 成本主桶与份额主桶相同及不同。
- 单 clip 切换、精确平局、十进制边界。
- 低→高→低→高→低多次折返和回绕。
- 乱序、重复、内容篡改、桶序漂移。
- pending/unknown/cancelled/rejected 不冒充 filled。
- 同输入重复 100 次 ID 和结果完全一致。

## 8. 证据边界

公开成交只能证明已经成交的 clip；未成交挂单、撤单前意图、私有模型和内部预算均不可识别，因此本合同解决“成交后如何归因”，不宣称还原源账户的挂单决策算法。
