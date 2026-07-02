# 门控模式切换：Shadow → Enforced

## 切换时间
2026-04-24 16:15

## 变更内容

### 之前（Shadow 模式）
```bash
PM_EDGE_POLICY_MODE=shadow
```
- 门控只记录拦截原因，不实际拦截
- 所有决策都会执行，即使不满足门控条件
- 用于观察和调试

### 现在（Enforced 模式）
```bash
PM_EDGE_POLICY_MODE=enforced
```
- 门控强制执行
- 不满足任何一个条件的决策会被拦截
- 生产模式

## 当前门控配置

### 第1关：Signal LCB
```bash
PM_EDGE_TRADEABILITY_SIGNAL_Z=1.12
# signal_lcb = alpha - 1.12 × sigma
# 阈值：signal_lcb >= 0.35
```

### 第2关：Policy Gate（5个子门控）

1. **signal_lcb >= 0.0**（再次检查）
2. **path_lcb >= 0.05**
   ```bash
   PM_EDGE_POLICY_PATH_LCB_MIN=0.05
   ```

3. **edge_realizable >= 0.0**（已修复）
   ```bash
   PM_EDGE_REALIZABLE_CALIBRATION_PATH=/Users/xiong/weather/data/edge_realizable_calibration.json
   PM_EDGE_REALIZABLE_ALLOW_GLOBAL_DEFAULTS=1
   ```

4. **v2_value 必须存在**
   ```bash
   PM_EDGE_POLICY_REQUIRE_V2=True（默认）
   ```

5. **exec_audit_gate_pass = True**
   ```bash
   PM_EDGE_POLICY_REQUIRE_EXEC_AUDIT=True（默认）
   ```

## 预期效果

### Shadow 模式下
- 所有通过第1关的决策都会执行
- 通过率：47.7%（仅第1关过滤）

### Enforced 模式下（当前）
- 必须通过所有门控才能执行
- 预期通过率：
  - 第1关：55-60%（z-score=1.12）
  - 第2关：50-70%（edge_realizable已修复）
  - **最终：30-40%**

## 监控命令

### 1小时后检查通过率
```bash
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total,
  SUM(CASE WHEN json_extract(details_json, '$.edge_signal_lcb') >= 0.35 THEN 1 ELSE 0 END) as gate1_pass,
  SUM(CASE WHEN json_extract(details_json, '$.edge_policy_result.pass') = 1 THEN 1 ELSE 0 END) as gate2_pass,
  ROUND(100.0 * SUM(CASE WHEN json_extract(details_json, '$.edge_signal_lcb') >= 0.35 THEN 1 ELSE 0 END) / COUNT(*), 1) as gate1_rate,
  ROUND(100.0 * SUM(CASE WHEN json_extract(details_json, '$.edge_policy_result.pass') = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) as final_rate
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-1 hour')
"
```

### 检查拦截原因分布
```bash
sqlite3 data/archive.db "
SELECT
  json_extract(details_json, '$.edge_policy_result.reasons_csv') as reasons,
  COUNT(*) as count
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-1 hour')
  AND json_extract(details_json, '$.edge_policy_result.blocked') = 1
GROUP BY reasons
ORDER BY count DESC
"
```

### 检查 edge_realizable 值
```bash
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total,
  COUNT(json_extract(details_json, '$.edge_realizable')) as has_value,
  AVG(CAST(json_extract(details_json, '$.edge_realizable') AS FLOAT)) as avg_value,
  MIN(CAST(json_extract(details_json, '$.edge_realizable') AS FLOAT)) as min_value,
  MAX(CAST(json_extract(details_json, '$.edge_realizable') AS FLOAT)) as max_value
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-1 hour')
"
```

## 风险评估

### 低风险
- ✅ edge_realizable 校准文件已部署
- ✅ 使用保守的默认概率（0.80 × 0.85 × 0.80 = 0.544）
- ✅ 可随时回滚到 shadow 模式

### 如果通过率过低（< 20%）
```bash
# 临时回滚到 shadow 模式
sed -i '' 's/PM_EDGE_POLICY_MODE=enforced/PM_EDGE_POLICY_MODE=shadow/' .env
launchctl restart com.weather.trade_engine
```

### 如果通过率过高（> 50%）
```bash
# 提高 edge_realizable 阈值
echo "PM_EDGE_POLICY_EDGE_LCB_MIN=0.02" >> .env
launchctl restart com.weather.trade_engine
```

## 总结

- ✅ 已切换到 enforced 模式
- ✅ 服务已重启
- ⏭️ 1小时后检查效果
- ⏭️ 根据数据调整阈值

---

**实施时间**: 2026-04-24 16:15
**下次检查**: 2026-04-24 17:15
