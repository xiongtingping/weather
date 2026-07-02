# 地区+时间模型权重实施方案

## 阶段1：代码修改（地区+时间组合）

### 修改1：扩展SCENARIO_WEIGHTS

**文件**: `scan_weather_market.py`
**位置**: Line 340

**修改内容**：在现有SCENARIO_WEIGHTS字典中添加地区+时间组合的权重配置

```python
SCENARIO_WEIGHTS = {
    # ============================================
    # 美国城市（USA）
    # ============================================
    "usa_early": {
        "gfs_seamless": 0.5,      # GFS是美国NOAA模型，早期更新频繁
        "icon_seamless": 0.3,
        "ecmwf_ifs025": 0.15,
        "nws_forecast": 0.05,     # NWS早期预报不如中长期模型
    },
    "usa_mid": {
        "gfs_seamless": 0.4,
        "ecmwf_ifs025": 0.3,
        "icon_seamless": 0.2,
        "nws_forecast": 0.1,
    },
    "usa_late": {
        "nws_forecast": 0.7,      # 根据历史数据，NWS短期最准（Brier=0.0584）
        "gfs_seamless": 0.15,
        "ecmwf_ifs025": 0.1,
        "icon_seamless": 0.05,
    },
    "usa_integer": {
        "nws_forecast": 0.5,      # 整数温度，NWS精度高
        "ecmwf_ifs025": 0.3,
        "gfs_seamless": 0.2,
        "icon_seamless": 0.0,
    },

    # ============================================
    # 欧洲城市（Europe）
    # ============================================
    "europe_early": {
        "ecmwf_ifs025": 0.4,      # ECMWF是欧洲中期天气预报中心
        "icon_seamless": 0.4,     # ICON是德国气象局模型
        "gfs_seamless": 0.2,
        "nws_forecast": 0.0,      # NWS不覆盖欧洲
    },
    "europe_mid": {
        "ecmwf_ifs025": 0.6,
        "icon_seamless": 0.3,
        "gfs_seamless": 0.1,
        "nws_forecast": 0.0,
    },
    "europe_late": {
        "ecmwf_ifs025": 0.6,      # 欧洲短期仍用ECMWF
        "icon_seamless": 0.3,
        "gfs_seamless": 0.1,
        "nws_forecast": 0.0,
    },
    "europe_integer": {
        "ecmwf_ifs025": 0.6,
        "icon_seamless": 0.4,
        "gfs_seamless": 0.0,
        "nws_forecast": 0.0,
    },

    # ============================================
    # 亚洲城市（Asia）
    # ============================================
    "asia_early": {
        "ecmwf_ifs025": 0.5,
        "gfs_seamless": 0.3,
        "icon_seamless": 0.2,
        "nws_forecast": 0.0,
    },
    "asia_mid": {
        "ecmwf_ifs025": 0.6,
        "gfs_seamless": 0.3,
        "icon_seamless": 0.1,
        "nws_forecast": 0.0,
    },
    "asia_late": {
        "ecmwf_ifs025": 0.6,
        "gfs_seamless": 0.3,
        "icon_seamless": 0.1,
        "nws_forecast": 0.0,
    },
    "asia_integer": {
        "ecmwf_ifs025": 0.6,
        "gfs_seamless": 0.3,
        "icon_seamless": 0.1,
        "nws_forecast": 0.0,
    },

    # ============================================
    # 其他地区（Other/Global）
    # ============================================
    "other_early": {
        "ecmwf_ifs025": 0.4,
        "gfs_seamless": 0.3,
        "icon_seamless": 0.3,
        "nws_forecast": 0.0,
    },
    "other_mid": {
        "ecmwf_ifs025": 0.6,
        "gfs_seamless": 0.2,
        "icon_seamless": 0.2,
        "nws_forecast": 0.0,
    },
    "other_late": {
        "ecmwf_ifs025": 0.6,
        "gfs_seamless": 0.2,
        "icon_seamless": 0.2,
        "nws_forecast": 0.0,
    },
    "other_integer": {
        "ecmwf_ifs025": 0.6,
        "icon_seamless": 0.4,
        "gfs_seamless": 0.0,
        "nws_forecast": 0.0,
    },

    # ============================================
    # 默认场景（保留向后兼容）
    # ============================================
    "early": {
        "gfs_seamless": 0.45,
        "icon_seamless": 0.45,
        "ecmwf_ifs025": 0.05,
        "nws_forecast": 0.05,
    },
    "mid": {
        "ecmwf_ifs025": 0.6,
        "gfs_seamless": 0.2,
        "icon_seamless": 0.2,
        "nws_forecast": 0.0,
    },
    "late": {
        "nws_forecast": 0.6,
        "ecmwf_ifs025": 0.2,
        "gfs_seamless": 0.1,
        "icon_seamless": 0.1,
    },
    "integer": {
        "ecmwf_ifs025": 0.55,
        "icon_seamless": 0.45,
        "gfs_seamless": 0.0,
        "nws_forecast": 0.0,
    },
}
```

### 修改2：添加地区检测函数

**文件**: `scan_weather_market.py`
**位置**: 在Line 44736之前添加

```python
def _detect_region(lat: Optional[float], lon: Optional[float]) -> str:
    """
    根据经纬度判断地区

    返回值：
    - "usa": 美国本土
    - "europe": 欧洲
    - "asia": 亚洲
    - "other": 其他地区
    """
    if lat is None or lon is None:
        return "other"

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return "other"

    # 美国本土：纬度25-50，经度-125到-65
    if 25 <= lat_f <= 50 and -125 <= lon_f <= -65:
        return "usa"

    # 欧洲：纬度35-70，经度-10到40
    if 35 <= lat_f <= 70 and -10 <= lon_f <= 40:
        return "europe"

    # 亚洲东部：纬度-10到55，经度60到150
    if -10 <= lat_f <= 55 and 60 <= lon_f <= 150:
        return "asia"

    # 南美洲：纬度-60到15，经度-85到-30
    if -60 <= lat_f <= 15 and -85 <= lon_f <= -30:
        return "other"  # 可以后续扩展为"south_america"

    # 非洲：纬度-35到40，经度-20到55
    if -35 <= lat_f <= 40 and -20 <= lon_f <= 55:
        return "other"  # 可以后续扩展为"africa"

    # 大洋洲：纬度-50到0，经度110到180
    if -50 <= lat_f <= 0 and 110 <= lon_f <= 180:
        return "other"  # 可以后续扩展为"oceania"

    return "other"
```

### 修改3：修改scenario_stage函数

**文件**: `scan_weather_market.py`
**位置**: Line 44736

**原函数签名**：
```python
def scenario_stage(
    run_date: Optional[str],
    event_date: Optional[str],
    rounding_rule: Optional[str],
    market_unit: Optional[str],
    bucket_labels: Optional[list] = None,
) -> str:
```

**修改为**：
```python
def scenario_stage(
    run_date: Optional[str],
    event_date: Optional[str],
    rounding_rule: Optional[str],
    market_unit: Optional[str],
    bucket_labels: Optional[list] = None,
    lat: Optional[float] = None,  # 新增
    lon: Optional[float] = None,  # 新增
) -> str:
    """
    确定场景阶段（地区+时间组合）

    返回格式：
    - "{region}_{time_stage}": 例如 "usa_late", "europe_mid"
    - "{time_stage}": 如果无法确定地区，fallback到纯时间场景
    """
    # 1. 检测地区
    region = _detect_region(lat, lon)

    # 2. 计算距离事件的天数
    run_dt = _parse_date(run_date)
    event_dt = _parse_date(event_date)
    days_to_event = None
    if run_dt and event_dt:
        days_to_event = (event_dt - run_dt).days

    # 3. 判断是否整数温度市场
    integer_bucket = False
    if bucket_labels:
        seen = False
        all_integer = True
        for label in bucket_labels:
            parsed = _bucket_is_integer(label or "")
            if parsed is None:
                continue
            seen = True
            if not parsed:
                all_integer = False
                break
        integer_bucket = bool(seen and all_integer)

    # 4. 确定时间阶段
    if days_to_event is not None and days_to_event <= 1:
        time_stage = "late"
    elif (
        days_to_event is not None
        and 2 <= days_to_event <= 4
        and ((rounding_rule or "") == "whole_degrees" or integer_bucket)
    ):
        time_stage = "integer"
    elif days_to_event is not None and days_to_event <= 4:
        time_stage = "mid"
    else:
        time_stage = "early"

    # 5. 组合地区+时间
    if region and region != "other":
        combined = f"{region}_{time_stage}"
        # 检查是否存在该组合的权重配置
        if combined in SCENARIO_WEIGHTS:
            return combined
        # 如果没有该组合，尝试使用地区的默认配置
        region_default = f"{region}_mid"
        if region_default in SCENARIO_WEIGHTS:
            return region_default

    # 6. Fallback到纯时间场景（向后兼容）
    return time_stage
```

### 修改4：更新scenario_weights调用点

需要在所有调用`scenario_stage`的地方传入`lat`和`lon`参数。

**主要调用点**：
1. Line 15790: `scenario_override = scenario_weights(...)`
2. Line 49604: `scenario_override = scenario_weights(...)`
3. Line 51550: `scenario_override = scenario_weights(...)`
4. Line 53494: `weights = scenario_weights(...)`
5. Line 58013: `weights = scenario_weights(...)`

**修改方法**：
在每个调用点，从上下文中获取`lat`和`lon`，然后传递给函数。

例如Line 53494附近：
```python
# 原代码
weights = scenario_weights(models, run_date, date, rounding_rule, market_unit, bucket_labels)

# 修改为
lat = item.get("lat")  # 从item中获取lat
lon = item.get("lon")  # 从item中获取lon
weights = scenario_weights(models, run_date, date, rounding_rule, market_unit, bucket_labels, lat, lon)
```

### 修改5：更新scenario_weights函数签名

**文件**: `scan_weather_market.py`
**位置**: Line 44774

**原函数签名**：
```python
def scenario_weights(
    models: List[str],
    run_date: Optional[str],
    event_date: Optional[str],
    rounding_rule: Optional[str],
    market_unit: Optional[str],
    bucket_labels: Optional[list] = None,
) -> Dict[str, float]:
```

**修改为**：
```python
def scenario_weights(
    models: List[str],
    run_date: Optional[str],
    event_date: Optional[str],
    rounding_rule: Optional[str],
    market_unit: Optional[str],
    bucket_labels: Optional[list] = None,
    lat: Optional[float] = None,  # 新增
    lon: Optional[float] = None,  # 新增
) -> Dict[str, float]:
    if not models:
        return {}
    stage = scenario_stage(run_date, event_date, rounding_rule, market_unit, bucket_labels, lat, lon)  # 传递lat/lon
    base = SCENARIO_WEIGHTS.get(stage, {})
    weights = {m: float(base.get(m, 0.0)) for m in models}
    total = sum(weights.values())
    if total <= 0:
        w = 1.0 / len(models)
        return {m: w for m in models}
    return {m: (weights[m] / total) for m in models}
```

### 修改6：启用scenario权重

**文件**: `.env`

```bash
# 启用scenario权重（地区+时间组合）
export PM_SCAN_WEIGHTING="scenario"
```

## 阶段2：升级为auto模式（1-2周后）

### 目标

让系统自动学习每个城市+时间组合的最佳模型权重。

### 实施方法

#### 方法1：扩展auto模式支持地区维度

**修改compute_model_hit_weights函数**（Line 51235）：

```python
def compute_model_hit_weights(
    days: int = 7,
    end_date: Optional[str] = None,
    region: Optional[str] = None,  # 新增：按地区过滤
    time_stage: Optional[str] = None,  # 新增：按时间阶段过滤
) -> Dict[str, float]:
    # ... 现有逻辑 ...

    # 在查询event_runs时，添加地区和时间过滤
    # 需要在event_runs表中添加region和time_stage字段
    # 或者在运行时从lat/lon和event_date计算
```

#### 方法2：使用分层auto权重

```bash
# 先用scenario建立基础权重
export PM_SCAN_WEIGHTING="scenario"

# 1-2周后，积累足够数据
export PM_SCAN_WEIGHTING="auto"

# auto模式会自动覆盖scenario的权重
```

## 预期效果

### 阶段1（scenario模式）

**纽约（usa_late）**：
- NWS: 70%
- GFS: 15%
- ECMWF: 10%
- ICON: 5%
- model_spread: 从0.10降到0.03-0.04
- signal_lcb: 从-0.02提升到0.01-0.02

**阿姆斯特丹（europe_late）**：
- ECMWF: 60%
- ICON: 30%
- GFS: 10%
- NWS: 0%
- model_spread: 从0.10降到0.04-0.05
- signal_lcb: 从-0.02提升到0.00-0.01

### 阶段2（auto模式）

系统会基于实际表现自动调整，可能发现：
- 纽约周一的NWS更准 → 周一NWS权重更高
- 伦敦冬天的ECMWF更准 → 冬天ECMWF权重更高
- 等等

## 测试计划

1. **修改代码**（今天）
2. **测试scenario模式**（1-2天）
   - 观察model_spread是否降低
   - 观察signal_lcb是否提高
   - 观察通过率是否提升
3. **收集数据**（1-2周）
   - 积累各地区+时间组合的历史数据
4. **切换到auto模式**（2周后）
   - 让系统自动学习最佳权重

## 风险评估

**低风险**：
- 保留了fallback机制（纯时间场景）
- 向后兼容现有逻辑

**中风险**：
- 需要修改多个调用点
- 需要测试验证

**建议**：
- 先在测试环境验证
- 逐步rollout到生产环境
