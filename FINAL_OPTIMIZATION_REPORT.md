# 最终配置优化报告

## 优化时间
2026-04-24 16:45

## 问题诊断

### 1. 双重惩罚问题

**发现**：
- exec_edge 已经扣除了 spread、fees
- edge_v2 在此基础上再乘以 p_realize，再减去 hold_cost
- hold_cost = 9.6%/2天（年化1752%）过高

**结论**：
- p_realize 调整是合理的（真实成交风险）
- hold_cost 配置错误导致过度惩罚

### 2. v1 fallback 问题

**现象**：
- 当 v2 计算失败时，fallback 到 v1
- v1 不考虑成交风险，过于乐观

**风险**：
- 可能交易低质量决策

---

## 最终配置

### 1. 禁用 v1 fallback

```bash
# .env 配置
PM_EDGE_REALIZABLE_V2_ENABLED=1
PM_EDGE_REALIZABLE_V2_GATE_ENTRY=1
PM_EDGE_POLICY_REQUIRE_V2=1  ← 强制要求 v2 值存在
```

**效果**：
- 只有 v2 成功计算的决策才能通过
- 不会 fallback 到 v1
- 更严格，更安全

### 2. 优化 hold_cost

```json
{
  "hold_cost_params": {
    "mode": "linear",
    "k_per_hour": 0.00001
  }
}
```

**对比**：

| 配置 | k_per_hour | 年化成本 | 2天成本 |
|------|-----------|---------|---------|
| 旧配置 | 0.002 | 1752% | 9.6% |
| 中间配置 | 0.0001 | 87.6% | 0.48% |
| **最终配置** | 0.00001 | **8.76%** | **0.048%** |

**合理性**：
- 年化 8.76% 接近市场平均回报率
- 2天成本 0.048% 很小，不会过度惩罚

---

## hold_cost 详解

### 什么是 hold_cost？

**持仓成本** = 资金占用的机会成本

**场景**：
```
今天：投入 $100 买入合约
2天后：结算，释放资金

这2天内，$100 无法用于其他投资
```

### 为什么需要？

**机会成本**：
- 如果不做这笔交易，$100 可以：
  - 存银行：年化 5%
  - 买国债：年化 4%
  - 其他交易：可能更高

**所以持仓有成本**：
```
hold_cost = 投入金额 × 日化利率 × 天数
```

### 在 edge_v2 中的作用

```
edge_v2 = exec_edge × p_realize - hold_cost

含义：
  实际收益 = 理论收益 × 成功率 - 资金占用成本
```

### 合理值参考

| 场景 | 年化成本 | k_per_hour |
|------|---------|-----------|
| 无风险利率（国债） | 4% | 0.0000046 |
| **市场平均回报** | **8-10%** | **0.00001** |
| 高资金成本（借贷） | 20% | 0.000023 |
| 禁用（不考虑） | 0% | 0 |

**我们选择**：8.76%（接近市场平均）

---

## edge_v2 计算示例

### 配置参数

```
p_fill_entry = 0.80（入场成交率80%）
p_converge = 0.85（价格收敛率85%）
p_fill_exit = 0.80（出场成交率80%）
p_realize = 0.80 × 0.85 × 0.80 = 0.544

k_per_hour = 0.00001
2天持仓成本 = 2 × 24 × 0.00001 = 0.00048 = 0.048%
```

### 案例1：小 exec_edge

```
exec_edge = 0.001（0.1%）
edge_v2 = 0.001 × 0.544 - 0.00048 = 0.00006

结论：勉强为正，但太小，可能被其他门控拒绝
```

### 案例2：中等 exec_edge

```
exec_edge = 0.02（2%）
edge_v2 = 0.02 × 0.544 - 0.00048 = 0.0104（1.04%）

结论：正数，可交易
```

### 案例3：大 exec_edge

```
exec_edge = 0.18（18%）
edge_v2 = 0.18 × 0.544 - 0.00048 = 0.0974（9.74%）

结论：正数，保留大部分edge
```

---

## 预期效果

### 决策漏斗

```
总决策数: ~2,500/30分钟
    ↓
第1关: signal_lcb >= 0.35
    ↓ 通过 47.5%
通过: ~1,200
    ↓
市场筛选: exec_edge > 0
    ↓ 通过 22.4%
通过: ~270
    ↓
第2关: edge_v2 > 0 (新配置)
    ↓ 通过 60-70%
通过: ~180
    ↓
其他门控: path_lcb, exec_audit
    ↓ 通过 80-90%
最终可交易: ~150 (6%)
```

### 对比

| 配置 | 第2关通过率 | 最终通过率 |
|------|-----------|-----------|
| 旧配置（k=0.002） | 0% | 0% |
| 中间配置（k=0.0001） | ~10% | ~5% |
| **最终配置（k=0.00001）** | **60-70%** | **6-8%** |

---

## 监控命令

### 1. 检查 edge_v2 分布（10分钟后）

```bash
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total,
  COUNT(json_extract(details_json, '$.edge_realizable_v2')) as has_v2,
  SUM(CASE WHEN CAST(json_extract(details_json, '$.edge_realizable_v2') AS FLOAT) > 0 THEN 1 ELSE 0 END) as v2_positive,
  AVG(CAST(json_extract(details_json, '$.edge_realizable_v2') AS FLOAT)) as avg_v2
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-10 minutes')
  AND json_extract(details_json, '$.exec_edge_entry') > 0
"
```

### 2. 检查通过率

```bash
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total,
  SUM(CASE WHEN json_extract(details_json, '$.edge_signal_lcb') >= 0.35 THEN 1 ELSE 0 END) as gate1_pass,
  SUM(CASE WHEN json_extract(details_json, '$.edge_policy_result.pass') = 1 THEN 1 ELSE 0 END) as final_pass,
  ROUND(100.0 * SUM(CASE WHEN json_extract(details_json, '$.edge_policy_result.pass') = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) as pass_rate
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-10 minutes')
"
```

### 3. 检查 hold_cost 影响

```bash
sqlite3 data/archive.db "
SELECT
  ROUND(CAST(json_extract(details_json, '$.exec_edge_entry') AS FLOAT), 4) as exec_edge,
  ROUND(CAST(json_extract(details_json, '$.edge_realizable_v2') AS FLOAT), 4) as edge_v2,
  ROUND(CAST(json_extract(details_json, '$.edge_realizable_v2') AS FLOAT) / CAST(json_extract(details_json, '$.exec_edge_entry') AS FLOAT), 3) as retention_ratio
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-10 minutes')
  AND CAST(json_extract(details_json, '$.exec_edge_entry') AS FLOAT) > 0
  AND json_extract(details_json, '$.edge_realizable_v2') IS NOT NULL
LIMIT 10
"
```

**预期 retention_ratio**：
- 理想值：~0.54（p_realize = 0.544）
- 实际值：0.50-0.54（考虑 hold_cost）

---

## 回滚方案

### 如果通过率过低（< 3%）

**方案1：降低 hold_cost**
```json
{
  "k_per_hour": 0.000001  // 年化0.876%
}
```

**方案2：提高 p_realize**
```json
{
  "p_fill_entry_default": 0.85,
  "p_converge_default": 0.90,
  "p_fill_exit_default": 0.85
}
// p_realize = 0.85 × 0.90 × 0.85 = 0.650
```

### 如果通过率过高（> 15%）

**方案1：提高 hold_cost**
```json
{
  "k_per_hour": 0.00005  // 年化43.8%
}
```

**方案2：降低 p_realize**
```json
{
  "p_fill_entry_default": 0.75,
  "p_converge_default": 0.80,
  "p_fill_exit_default": 0.75
}
// p_realize = 0.75 × 0.80 × 0.75 = 0.450
```

---

## 总结

### 关键优化

1. ✅ **禁用 v1 fallback**
   - 强制使用 v2
   - 更严格，更安全

2. ✅ **优化 hold_cost**
   - 从年化1752% → 8.76%
   - 从2天9.6% → 0.048%
   - 不再过度惩罚

3. ✅ **保留 p_realize 调整**
   - 0.544 是合理的成交风险
   - 不是惩罚，是真实风险

### 预期结果

- 第1关通过率：47.5%
- 第2关通过率：60-70%（exec_edge > 0 的决策）
- **最终通过率：6-8%**

### 下一步

- ⏭️ 10分钟后检查效果
- ⏭️ 根据数据微调参数
- ⏭️ 1小时后评估 PnL

---

**优化完成时间**: 2026-04-24 16:45
**下次检查**: 2026-04-24 16:55（10分钟后）
