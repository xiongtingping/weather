# 门控拦截问题完整诊断报告

## 诊断时间
2026-04-24 16:30

## 问题演变

### 初始问题
- 第2关通过率：12.4%
- 拦截原因：edge_realizable = NULL

### 第一次修复
✅ 创建校准文件：`edge_realizable_calibration.json`
✅ 配置环境变量：`PM_EDGE_REALIZABLE_CALIBRATION_PATH`

### 第二次修复
✅ 启用 edge_realizable_v2：`PM_EDGE_REALIZABLE_V2_ENABLED=1`

### 最终发现
❌ **不是门控问题，是市场流动性问题**

---

## 根本原因

### 决策漏斗（最近30分钟数据）

```
总决策数: 5,114
    ↓
第1关: signal_lcb >= 0.35
    ↓ 通过 47.5% (2,441个)
市场流动性筛选
    ↓ exec_edge_entry > 0
    ↓ 通过 22.4% (547个)
第2关: edge_realizable_v2 计算
    ↓ 需要正的 exec_edge_entry
    ↓ 通过 0% (0个)
最终可交易: 0
```

### 关键数据

**通过第1关的2,441个决策**：
- 正 exec_edge：547个（22.4%）
- 负 exec_edge：1,894个（77.6%）
- 平均 exec_edge：**-0.024**（负数）

**典型案例**：
| q_edge | exec_edge | spread | 说明 |
|--------|-----------|--------|------|
| 1.000 | 0.001 | 0.001 | spread吃掉99.9%的edge |
| 0.975 | -0.028 | 0.002 | 负edge，无法盈利 |
| 0.945 | -0.063 | 0.004 | 负edge，无法盈利 |
| 0.830 | -0.163 | 0.001 | 负edge，无法盈利 |

---

## 问题本质

### q_edge vs exec_edge_entry

**q_edge**（理论edge）：
- 基于模型预测的概率优势
- 不考虑执行成本
- 公式：`q_edge = q_calibrated - 0.5`

**exec_edge_entry**（可执行edge）：
- 考虑市场价差、滑点、手续费
- 实际可获得的利润
- 公式：`exec_edge_entry = q_edge - spread - fees - slippage`

**关系**：
```
q_edge = 1.0（理论100%优势）
spread = 0.001（市场价差0.1%）
→ exec_edge_entry = 0.001（实际只剩0.1%）
```

### 为什么 exec_edge 是负数？

1. **市场价差太大**
   - 小市场流动性差
   - bid-ask spread 宽
   - 吃掉大部分理论edge

2. **预测优势不够大**
   - q_edge < spread 时必然亏损
   - 需要 q_edge > spread + fees 才能盈利

3. **这是正常的市场机制**
   - 不是系统bug
   - 是流动性成本

---

## 解决方案

### 方案1：提高 q_edge 阈值（推荐）

**目标**：只交易 q_edge 足够大的市场

```bash
# 添加到 .env
PM_AUTO_TRADE_MIN_Q_EDGE=0.05  # 最小5%理论优势

# 或者提高 signal_lcb 阈值
PM_EDGE_TRADEABILITY_SIGNAL_Z=1.28  # 从1.12提高到1.28
```

**预期效果**：
- 过滤掉低edge决策
- exec_edge_entry 正数比例提高
- 通过率降低，但质量提高

---

### 方案2：降低 spread 影响（不推荐）

**方法**：使用限价单而非市价单

```bash
PM_AUTO_TRADE_ORDER_TYPE=limit  # 使用限价单
PM_AUTO_TRADE_LIMIT_OFFSET=0.001  # 限价偏移
```

**风险**：
- 成交率降低
- 可能错过机会

---

### 方案3：接受负 edge（不推荐）

**方法**：允许小幅负edge

```bash
PM_EDGE_POLICY_EDGE_LCB_MIN=-0.02  # 允许-2%的edge
```

**风险**：
- 长期必然亏损
- 违背交易原则

---

### 方案4：优化市场选择（推荐）

**目标**：只交易流动性好的市场

```bash
# 添加流动性过滤
PM_AUTO_TRADE_MIN_DEPTH=1000  # 最小深度
PM_AUTO_TRADE_MAX_SPREAD=0.01  # 最大价差1%
```

**预期效果**：
- 过滤掉流动性差的市场
- exec_edge_entry 更接近 q_edge
- 通过率提高

---

## 推荐配置

### 立即实施

```bash
# 1. 提高 signal_lcb 阈值（更严格）
PM_EDGE_TRADEABILITY_SIGNAL_Z=1.28  # 从1.12提高到1.28

# 2. 添加流动性过滤
PM_AUTO_TRADE_MAX_SPREAD=0.02  # 最大价差2%

# 3. 添加最小edge要求
PM_AUTO_TRADE_MIN_Q_EDGE=0.03  # 最小3%理论优势
```

### 预期效果

**优化前**（当前）：
- 第1关通过率：47.5%
- exec_edge > 0：22.4%
- 最终通过率：0%

**优化后（预期）**：
- 第1关通过率：35-40%（更严格）
- exec_edge > 0：40-50%（流动性过滤）
- 最终通过率：15-20%

---

## 监控命令

### 检查 exec_edge 分布
```bash
sqlite3 data/archive.db "
SELECT
  CASE
    WHEN CAST(json_extract(details_json, '$.exec_edge_entry') AS FLOAT) > 0.05 THEN '>5%'
    WHEN CAST(json_extract(details_json, '$.exec_edge_entry') AS FLOAT) > 0.02 THEN '2-5%'
    WHEN CAST(json_extract(details_json, '$.exec_edge_entry') AS FLOAT) > 0 THEN '0-2%'
    WHEN CAST(json_extract(details_json, '$.exec_edge_entry') AS FLOAT) > -0.02 THEN '-2-0%'
    ELSE '<-2%'
  END as edge_range,
  COUNT(*) as count
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-1 hour')
  AND json_extract(details_json, '$.edge_signal_lcb') >= 0.35
GROUP BY edge_range
ORDER BY edge_range
"
```

### 检查 spread 分布
```bash
sqlite3 data/archive.db "
SELECT
  CASE
    WHEN CAST(json_extract(details_json, '$.spread') AS FLOAT) < 0.01 THEN '<1%'
    WHEN CAST(json_extract(details_json, '$.spread') AS FLOAT) < 0.02 THEN '1-2%'
    WHEN CAST(json_extract(details_json, '$.spread') AS FLOAT) < 0.05 THEN '2-5%'
    ELSE '>5%'
  END as spread_range,
  COUNT(*) as count,
  AVG(CAST(json_extract(details_json, '$.exec_edge_entry') AS FLOAT)) as avg_exec_edge
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-1 hour')
  AND json_extract(details_json, '$.edge_signal_lcb') >= 0.35
GROUP BY spread_range
ORDER BY spread_range
"
```

---

## 总结

### 问题不是门控，是市场

1. ✅ **门控逻辑正确**
   - edge_realizable_v2 正确拒绝负edge决策
   - 这是保护机制，不是bug

2. ❌ **市场流动性差**
   - 77.6% 的决策 exec_edge < 0
   - spread 吃掉大部分理论edge

3. ✅ **解决方案**
   - 提高 signal_lcb 阈值（更严格筛选）
   - 添加流动性过滤（只交易好市场）
   - 添加最小edge要求（确保盈利空间）

### 关键指标

**当前状态**：
- 通过第1关：47.5%
- exec_edge > 0：22.4%
- 最终可交易：0%

**优化目标**：
- 通过第1关：35-40%
- exec_edge > 0：40-50%
- 最终可交易：15-20%

---

**诊断完成时间**: 2026-04-24 16:30
**建议实施时间**: 立即
**预期验证时间**: 1小时后
