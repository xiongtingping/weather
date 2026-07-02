# 代码约束分析报告 (2026-04-25 11:15)

## 核心发现

**代码硬编码了LCB阈值的最小值，不允许负值**

## 代码约束详情

### 1. signal_lcb_min约束

**位置**: `scan_weather_market.py:12473`

```python
lcb_global_signal_lcb_min = max(0.0, float(lcb_global_signal_lcb_min))
```

**约束**: 最小值0.0，无上限
**影响**: 即使设置`PM_LCB_GLOBAL_SIGNAL_LCB_MIN=-0.03`，也会被clamp到0.0

### 2. path_lcb_min约束

**位置**: `scan_weather_market.py`（具体行号见grep结果）

```python
lcb_global_path_lcb_min = min(1.0, max(0.0, float(lcb_global_path_lcb_min)))
```

**约束**: 范围[0.0, 1.0]
**影响**: 不允许负值

### 3. edge_lcb_min约束

**位置**: `scan_weather_market.py`

```python
lcb_global_edge_lcb_min = min(0.10, max(-0.05, float(lcb_global_edge_lcb_min)))
```

**约束**: 范围[-0.05, 0.10]
**影响**: 允许负值，可以设置为-0.05

### 4. signal_soft_band_min约束

**位置**: `scan_weather_market.py:12482-12484`

```python
lcb_global_signal_soft_band_min = min(
    1.0,
    max(0.0, float(lcb_global_signal_soft_band_min)),
)
```

**约束**: 范围[0.0, 1.0]
**影响**: soft band机制也不允许负值

## 实际影响

### 被阻止的高价值案例

根据FINAL_DIAGNOSIS_REPORT.md，4个高价值案例的signal_lcb值：
- -0.02
- -0.0003
- -0.02
- -0.01

**所有案例的signal_lcb都是负值**

### 当前配置的效果

设置`PM_LCB_GLOBAL_SIGNAL_LCB_MIN=0.0`后：
- ✅ 允许signal_lcb=0.0的案例通过
- ❌ 仍然阻止signal_lcb<0的案例（包括上述4个高价值案例）

## 解决方案

### 方案A：修改代码（根本解决）

**修改位置**: `scan_weather_market.py:12473`

```python
# 修改前
lcb_global_signal_lcb_min = max(0.0, float(lcb_global_signal_lcb_min))

# 修改后
lcb_global_signal_lcb_min = float(lcb_global_signal_lcb_min)
# 或者允许一定范围的负值
lcb_global_signal_lcb_min = max(-0.05, float(lcb_global_signal_lcb_min))
```

**优点**：
- 彻底解决问题
- 可以设置任意阈值

**缺点**：
- 需要修改代码
- 需要测试验证
- 可能影响其他逻辑

### 方案B：接受现状，观察效果

**当前配置**：
- signal_lcb_min=0.0（允许的最小值）
- path_lcb_min=0.0
- edge_lcb_min=-0.05

**预期效果**：
- 解锁signal_lcb=0.0的案例
- 仍然阻止signal_lcb<0的案例
- 通过率可能从0%提升到1-3%（取决于有多少signal_lcb=0的案例）

**优点**：
- 无需修改代码
- 风险低

**缺点**：
- 无法解锁负signal_lcb的高价值案例
- 效果有限

### 方案C：使用soft_band机制（不可行）

soft_band机制本来是为了允许低于阈值的案例通过，但：
- soft_band_min也被clamp到[0.0, 1.0]
- 无法设置负值
- 不能解决问题

## 根本矛盾

**signal_lcb vs exec_edge_score**

- signal_lcb: 信号质量的保守估计（LCB下界），考虑不确定性
- exec_edge_score: 预期收益的点估计，不考虑不确定性

**矛盾案例**：
- signal_lcb=-0.02（信号质量差，不确定性高）
- exec_edge_score=3.18（预期收益高）

**问题**：
- 如果信号质量差，为什么预期收益高？
- 是signal_lcb计算过于保守？
- 还是exec_edge_score计算有问题？

## 建议

### 短期（立即）

1. ✅ 已设置signal_lcb_min=0.0（允许的最小值）
2. ✅ 已重启服务
3. ⏭️ 观察1小时，看是否有订单生成
4. ⏭️ 统计signal_lcb=0的案例数量

### 中期（1-2天）

1. 调查signal_lcb为负的原因
2. 理解signal_lcb和exec_edge_score的关系
3. 决定是否修改代码允许负signal_lcb

### 长期（1周）

1. 如果决定允许负signal_lcb，修改代码
2. 设置合理的负值下限（如-0.05）
3. 基于实际交易数据调整阈值

## 监控指标

修改后需要监控：
1. 通过率（预期1-3%）
2. signal_lcb分布（有多少=0，有多少<0）
3. 订单生成数量
4. 实际成交情况
5. 盈亏情况

## 总结

**核心问题**：代码不允许负signal_lcb阈值，导致所有signal_lcb<0的案例被阻止

**当前方案**：设置signal_lcb_min=0.0（允许的最小值）

**预期效果**：有限（只能解锁signal_lcb=0的案例，无法解锁负值案例）

**根本解决**：修改代码，允许负signal_lcb阈值
