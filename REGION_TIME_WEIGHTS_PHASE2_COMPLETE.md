# 地区+时间模型权重系统 - 阶段2完成报告

## 实施时间
2026-04-25

## 阶段2目标

升级为auto模式，让系统自动学习每个地区+时间组合的最佳模型权重。

## 实施内容

### 1. 核心函数修改

#### 修改1: compute_model_hit_weights函数
**文件**: `scan_weather_market.py:51421`

**新增参数**:
```python
def compute_model_hit_weights(
    days: int = 7,
    end_date: Optional[str] = None,
    region: Optional[str] = None,        # 新增：地区过滤
    time_stage: Optional[str] = None,    # 新增：时间阶段过滤
) -> Dict[str, float]:
```

**新增过滤逻辑**（Line 51485-51537）:
```python
# 应用地区和时间阶段过滤
if region is not None or time_stage is not None:
    filtered_events: Dict[str, dict] = {}
    for key, r in latest_by_event.items():
        # 检查地区过滤
        if region is not None:
            lat = r.get("lat")
            lon = r.get("lon")
            detected_region = _detect_region(lat, lon)
            if detected_region != region:
                continue

        # 检查时间阶段过滤
        if time_stage is not None:
            # 从payload中获取bucket_labels
            # 调用scenario_stage计算时间阶段
            # 提取实际时间部分（去除地区前缀）
            if actual_time_stage != time_stage:
                continue

        filtered_events[key] = r
    latest_by_event = filtered_events
```

**功能说明**:
- 当`region`参数不为None时，只统计该地区的历史数据
- 当`time_stage`参数不为None时，只统计该时间阶段的历史数据
- 两个参数可以同时使用，实现精确的地区+时间组合过滤
- 基于过滤后的数据计算模型命中率权重

#### 修改2: resolve_weights函数
**文件**: `scan_weather_market.py:50678`

**新增参数**:
```python
def resolve_weights(
    models: list,
    weighting: str,
    weights_override: Optional[dict] = None,
    weight_date: Optional[str] = None,
    region: Optional[str] = None,        # 新增：地区过滤
    time_stage: Optional[str] = None,    # 新增：时间阶段过滤
) -> dict:
```

**修改auto模式逻辑**:
```python
if weighting == "auto":
    # ... 现有逻辑 ...

    # 使用地区和时间阶段过滤的权重
    recent = _normalize_weights_partial(
        compute_model_hit_weights(
            MODEL_WEIGHT_DAYS,
            end_date=weight_date,
            region=region,           # 传递地区过滤
            time_stage=time_stage,   # 传递时间阶段过滤
        ),
        models,
    )
    # ... 后续逻辑 ...
```

**功能说明**:
- 在auto模式下，调用compute_model_hit_weights时传递region和time_stage参数
- 系统会基于该地区+时间组合的历史表现自动计算最佳权重
- 例如：纽约+late阶段会基于纽约late阶段的历史数据学习权重

### 2. 调用点更新

#### 更新1: execute_auto_trades函数（Line 15900-15920）
```python
lat = result.get("lat")
lon = result.get("lon")
region = _detect_region(lat, lon)

# 计算时间阶段
run_dt = _parse_date(run_date)
event_dt = _parse_date(event_date)
days_to_event = (event_dt - run_dt).days if run_dt and event_dt else None

# 判断时间阶段（early/mid/late/integer）
# ... 逻辑 ...

weights_auto = resolve_weights(
    models, "auto", weight_date=run_date,
    region=region, time_stage=time_stage  # 传递地区和时间阶段
)
```

#### 更新2: archive_run函数（Line 49819-49855）
```python
lat = meta.get("lat")
lon = meta.get("lon")

# 计算地区和时间阶段
region = _detect_region(lat, lon)
# ... 计算time_stage逻辑 ...

weights_auto = resolve_weights(
    models, "auto", weight_date=run_date,
    region=region, time_stage=time_stage
)

weights_auto_bias = resolve_weights(
    models, "auto", weight_date=run_date,
    region=region, time_stage=time_stage
)
```

#### 更新3: compute_place_bias_stats函数（Line 51902-51917）
```python
lat = r.get("lat")
lon = r.get("lon")

# 计算地区和时间阶段
region = _detect_region(lat, lon)
# ... 计算time_stage逻辑 ...

weights_auto = resolve_weights(
    models, "auto", weight_date=r.get("run_date"),
    region=region, time_stage=time_stage
)
```

### 3. 配置更新

**当前配置**（.env）:
```bash
PM_SCAN_WEIGHTING=scenario
```

**切换到auto模式**:
```bash
PM_SCAN_WEIGHTING=auto
```

## 工作原理

### Scenario模式（阶段1）
使用预定义的模型权重配置：
```python
"usa_late": {
    "nws_forecast": 0.7,
    "gfs_seamless": 0.15,
    "ecmwf_ifs025": 0.1,
    "icon_seamless": 0.05,
}
```

### Auto模式（阶段2）
基于历史数据自动学习：
1. 系统检测当前事件的地区（usa/europe/asia/other）
2. 系统检测当前事件的时间阶段（early/mid/late/integer）
3. 调用`compute_model_hit_weights(region="usa", time_stage="late")`
4. 函数从历史数据中筛选出所有"纽约+late"的案例
5. 计算每个模型在这些案例中的命中率
6. 基于命中率生成权重（命中率高的模型权重大）
7. 应用bias惩罚（对有系统性偏差的模型降权）

### 示例流程

**场景**: 纽约市场，距离事件1天（late阶段）

**Scenario模式**:
- 返回预定义权重: NWS 70%, GFS 15%, ECMWF 10%, ICON 5%

**Auto模式**:
1. 检测地区: usa
2. 检测时间阶段: late
3. 查询历史数据: 过去7天所有"usa+late"的案例
4. 计算命中率:
   - NWS: 15/20 = 75%
   - GFS: 12/20 = 60%
   - ECMWF: 10/20 = 50%
   - ICON: 8/20 = 40%
5. 归一化权重:
   - NWS: 75/(75+60+50+40) = 33.3%
   - GFS: 60/225 = 26.7%
   - ECMWF: 50/225 = 22.2%
   - ICON: 40/225 = 17.8%
6. 应用bias惩罚（如果有系统性偏差）
7. 返回最终权重

## 优势对比

### Scenario模式（阶段1）
**优点**:
- 立即生效，无需历史数据
- 基于专家知识和历史Brier分数
- 稳定可预测

**缺点**:
- 权重固定，无法适应变化
- 无法学习特定城市的特殊规律
- 需要手动调整

### Auto模式（阶段2）
**优点**:
- 自动学习，无需手动调整
- 适应性强，能发现隐藏规律
- 持续优化，随数据积累改进

**缺点**:
- 需要足够的历史数据（至少7天）
- 初期可能不稳定
- 依赖数据质量

## 预期效果

### 短期（1-2周）
- 系统开始积累各地区+时间组合的历史数据
- Auto模式权重逐渐收敛到最优值
- 可能发现scenario模式未考虑的规律

### 中期（2-4周）
- Auto模式权重稳定
- model_spread进一步降低
- signal_lcb进一步提升
- 通过率持续改善

### 长期（1个月+）
- 系统可能发现：
  - 纽约周一的NWS更准确
  - 伦敦冬天的ECMWF更准确
  - 东京夏天的GFS更准确
  - 等等隐藏规律

## 切换方式

### 保持Scenario模式（当前）
```bash
# .env文件
PM_SCAN_WEIGHTING=scenario
```

### 切换到Auto模式
```bash
# .env文件
PM_SCAN_WEIGHTING=auto

# 重启服务
launchctl unload ~/Library/LaunchAgents/com.weather.auto_trade_entry_1m.plist
launchctl load ~/Library/LaunchAgents/com.weather.auto_trade_entry_1m.plist
```

### 回退到Equal模式（如果需要）
```bash
# .env文件
PM_SCAN_WEIGHTING=equal
```

## 监控指标

切换到auto模式后需要监控：

1. **权重分布**
   - 查询: 观察日志中auto模式计算的权重
   - 预期: 权重应该合理，不应该出现极端值（如某个模型100%）

2. **命中率**
   - 查询: `SELECT model, COUNT(*) as total, SUM(CASE WHEN pred_label = actual_label THEN 1 ELSE 0 END) as hits FROM ... GROUP BY model`
   - 预期: 命中率应该稳定或提升

3. **model_spread**
   - 查询: `SELECT AVG(q_round_model_spread) FROM event_runs WHERE run_date >= '2026-04-25'`
   - 预期: 继续降低

4. **signal_lcb**
   - 查询: `SELECT AVG(signal_lcb) FROM edge_tradeability WHERE run_date >= '2026-04-25'`
   - 预期: 继续提升

5. **通过率**
   - 查询: `SELECT COUNT(*) FILTER (WHERE gate_pass) * 100.0 / COUNT(*) FROM edge_tradeability WHERE run_date >= '2026-04-25'`
   - 预期: 继续提升

## 风险评估

### 低风险
- ✅ 保留了完整的fallback机制
- ✅ 可随时切换回scenario或equal模式
- ✅ 不影响已有功能

### 中风险
- ⚠️ 初期数据不足时权重可能不稳定
- ⚠️ 需要验证过滤逻辑正确性

### 缓解措施
- 建议先运行1-2周scenario模式，积累足够数据
- 切换到auto模式后密切监控1-2天
- 如有异常立即切换回scenario模式

## 建议

### 当前阶段（2026-04-25）
**保持scenario模式**，原因：
1. 刚刚实施阶段1，需要验证效果
2. 需要积累1-2周的历史数据
3. 确保scenario模式配置合理

### 1-2周后
**切换到auto模式**，条件：
1. ✅ Scenario模式运行稳定
2. ✅ model_spread已降低
3. ✅ signal_lcb已提升
4. ✅ 积累了足够的历史数据（每个地区+时间组合至少20个样本）

### 验证方法
切换前先查询历史数据量：
```sql
SELECT
    CASE
        WHEN lat BETWEEN 25 AND 50 AND lon BETWEEN -125 AND -65 THEN 'usa'
        WHEN lat BETWEEN 35 AND 70 AND lon BETWEEN -10 AND 40 THEN 'europe'
        WHEN lat BETWEEN -10 AND 55 AND lon BETWEEN 60 AND 150 THEN 'asia'
        ELSE 'other'
    END as region,
    CASE
        WHEN julianday(event_date) - julianday(run_date) <= 1 THEN 'late'
        WHEN julianday(event_date) - julianday(run_date) <= 4 THEN 'mid'
        ELSE 'early'
    END as time_stage,
    COUNT(*) as sample_count
FROM event_runs
WHERE run_date >= date('now', '-7 days')
  AND settlement_actual IS NOT NULL
GROUP BY region, time_stage
ORDER BY region, time_stage;
```

如果每个组合都有20+样本，可以安全切换到auto模式。

## 总结

阶段2实施完成，系统现在支持：
1. ✅ Scenario模式：使用预定义的地区+时间权重
2. ✅ Auto模式：基于历史数据自动学习地区+时间权重

两种模式可以随时切换，建议先运行scenario模式1-2周，积累数据后再切换到auto模式。

Auto模式将持续学习和优化，随着数据积累，权重会越来越准确，model_spread会越来越小，signal_lcb会越来越高，最终解锁更多高价值交易机会。
