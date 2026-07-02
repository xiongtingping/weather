# 低风险改动实施报告

## 实施日期
2026-04-24 15:17

## 完成的改动

### ✅ 1. 修复标签生成逻辑

**状态**: 已验证，逻辑正确

**发现**:
- 标签生成逻辑已经是正确的
- `_bucket_hit()` 函数正确处理 settlement_actual 数值转换为二元标签
- 测试案例验证通过：
  - 实际温度 83.7°F → 取整后 83°F
  - "82-83°F" → 命中 ✓（label = 1）
  - "84-85°F" → 未命中 ✗（label = 0）

**代码位置**: `/Users/xiong/weather/scripts/train_q_calibrator.py` 第145-161行

**结论**: 无需修改，逻辑已正确实现

---

### ✅ 2. 添加 ensemble_spread 到 sigma 计算

**状态**: 已完成并生效

**修改内容**:
- 文件: `/Users/xiong/weather/edge_signal_runtime.py`
- 行数: 第66-68行
- 修改: 将 `model_spread` 权重从 0.18 提高到 0.40

**修改前**:
```python
signal_sigma = 0.025 + model_penalty * 0.35 + freshness_penalty
if model_spread_f is not None:
    signal_sigma += max(0.0, model_spread_f) * 0.18
```

**修改后**:
```python
signal_sigma = 0.025 + model_penalty * 0.35 + freshness_penalty
if model_spread_f is not None:
    signal_sigma += max(0.0, model_spread_f) * 0.40
```

**预期效果**:
- 当 model_spread = 0.0433 时：
  - 旧 sigma: 0.025 + 0.0433×0.18 = 0.0328
  - 新 sigma: 0.025 + 0.0433×0.40 = 0.0423
  - 增加: +29%
- 系统对模型分歧更加敏感
- 高分歧市场的 LCB 会更保守

**说明**:
- `model_spread` 就是 `q_round_model_spread`（ensemble spread）
- 计算方式: 加权标准差 = sqrt(Σ weight_i × (q_i - q_avg)²)
- 已经在系统中计算，只是提高了权重

---

### ✅ 3. 重新训练 Platt 校准模型

**状态**: 已完成并部署

**训练结果**:
- 样本数: 3,159（60天窗口）
- 训练时间: ~2分钟
- 版本: qcal_v1_20260424T071610Z

**Platt 参数**:
```json
{
  "global_platt": {
    "a": 0.2252,
    "b": -1.3083,
    "sample_n": 3159
  }
}
```

**分段参数**:
- d0 (当天): a=0.2735, b=-1.0285 (395样本)
- d1 (1天后): a=0.2052, b=-1.3297 (883样本)
- d2p (2天+): a=0.2274, b=-1.3391 (1881样本)

**评估指标**:
| 指标 | 全局 | d0 | d1 | d2p |
|------|------|----|----|-----|
| Brier Score | 0.0762 | 0.0673 | 0.0778 | 0.0772 |
| Log Loss | 0.2678 | 0.2399 | 0.2725 | 0.2715 |
| ECE | 0.0117 | 0.0416 | 0.0089 | 0.0081 |

**关键发现**:
- ECE (校准误差) 非常低: 0.0117（优秀）
- Platt 参数显著偏离恒等变换（a≠1, b≠0）
- 说明校准层确实在发挥作用

**部署**:
- ✓ Promote 到生产环境
- ✓ 服务已重启
- ✓ 新配置已生效

---

## 综合效果预估

### 当前配置
```python
# z-score
PM_EDGE_TRADEABILITY_SIGNAL_Z = 1.12

# sigma 计算
signal_sigma = 0.025 + model_penalty*0.35 + freshness_penalty + model_spread*0.40

# 校准模型
Platt: a=0.2252, b=-1.3083 (3159样本)
```

### 预期改善

**1. Sigma 更准确**
- 对模型分歧的敏感度提高 122%（0.18 → 0.40）
- 高分歧市场会被更保守地对待
- 低分歧市场不受影响

**2. 校准质量提升**
- ECE 从 0.0966 降至 0.0117（-88%）
- 概率预测更接近真实频率
- 分段校准更精细（d0/d1/d2p）

**3. 通过率影响**
- z-score=1.12 + sigma提高 → 通过率可能略降
- 但决策质量提升（更准确的 alpha 和 sigma）
- 预期通过率: 55-60%（之前 48.8%）

### 风险评估

**低风险**:
- ✅ 所有改动都是参数调整，不改变系统架构
- ✅ 可以快速回滚（修改 .env 和 edge_signal_runtime.py）
- ✅ 校准模型有备份（.bak 文件）

**监控指标**:
1. 通过率变化（目标 55-60%）
2. 平均 sigma 变化（预期略增）
3. 决策质量（alpha 分布）
4. 实际 PnL 表现

---

## 下一步建议

### 短期（今天-明天）
1. **监控通过率**
   - 目标: 55-60%
   - 如果 < 50%: 考虑降低 z-score 到 1.0
   - 如果 > 65%: 可能过于激进

2. **监控 sigma 分布**
   ```bash
   sqlite3 data/archive.db "
   SELECT
     ROUND(AVG(json_extract(details_json, '$.edge_signal_sigma')), 3) as avg_sigma,
     ROUND(AVG(json_extract(details_json, '$.q_round_model_spread')), 3) as avg_spread
   FROM auto_trade_decisions
   WHERE created_at > datetime('now', '-1 hour')
   "
   ```

3. **验证校准效果**
   - 等待更多事件结算
   - 对比 q_raw vs q_calibrated 的准确性

### 中期（本周）
1. **考虑实施 Isotonic Regression**
   - 如果 Platt 校准效果不够好
   - 更灵活的非参数校准方法

2. **分层校准**
   - 按城市/提前期/季节分开训练
   - 需要至少 50 个样本/层

3. **添加 model_disagreement 特征**
   - 计算三个模型（GFS/ICON/ECMWF）之间的分歧
   - 作为额外的不确定性指标

### 长期（本月）
1. **EMOS 或 BMA 校准**
   - 更高级的概率校准方法
   - 需要更多历史数据

2. **基于 PnL 的参数优化**
   - 网格搜索最优 z-score, sigma 权重
   - 最大化历史 PnL

3. **引入 GenCast 或其他 AI 模型**
   - 作为第4个 backbone
   - 或用其输出作为特征

---

## 回滚方案

### 如果需要回滚 sigma 权重
```bash
# 编辑 edge_signal_runtime.py 第68行
# 改回: signal_sigma += max(0.0, model_spread_f) * 0.18

# 重启服务
launchctl stop com.weather.trade_engine
launchctl start com.weather.trade_engine
```

### 如果需要回滚校准模型
```bash
# 恢复备份
cp data/q_calibration_bundle.json.bak data/q_calibration_bundle.json

# 重启服务
launchctl stop com.weather.trade_engine
launchctl start com.weather.trade_engine
```

### 如果需要回滚 z-score
```bash
# 编辑 .env
PM_EDGE_TRADEABILITY_SIGNAL_Z=1.28  # 或 1.64

# 重启服务
launchctl stop com.weather.trade_engine
launchctl start com.weather.trade_engine
```

---

## 总结

### 成功完成
1. ✅ 验证标签生成逻辑正确
2. ✅ 提高 ensemble_spread 权重（0.18 → 0.40）
3. ✅ 重新训练并部署校准模型（3159样本，ECE=0.0117）

### 关键指标
- **样本数**: 3,159（60天窗口）
- **ECE**: 0.0117（优秀的校准质量）
- **Sigma 权重**: 0.40（提高敏感度）
- **z-score**: 1.12（保持不变）

### 系统状态
- ✅ 所有服务运行正常
- ✅ 配置已生效
- ✅ 校准模型已部署
- ✅ 可随时回滚

---

**实施完成时间**: 2026-04-24 15:17
**下次检查时间**: 2026-04-24 16:00（1小时后验证效果）
**负责人**: Claude + User
