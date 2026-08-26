---
document_id: basket-phase-state-machine-v1
policy_version: source_clip_clock_v20260714_16
status: VALIDATED_SHADOW_ONLY_NOT_LIVE
orders_submitted: 0
writes_production_db: false
pdf_modified: false
---

# BasketPhase 纯函数状态机 v1

## 1. 核心结论

源账户在当前可识别策略版本中不是按固定累计成本切换 `15→9.05→2.22`，而是按每个站点的本地民用时间切换现金阶段：

| 当前阶段 | 本地时间条件 | 下一阶段 | 单笔现金模板 |
|---|---|---|---:|
| `HEAVY` | `local < D-1 19:54` | `HEAVY` | 15.00 USDC |
| `HEAVY` | `local >= D-1 19:54` | `STANDARD` | 9.05 USDC |
| `STANDARD` | `local < D 09:58` | `STANDARD` | 9.05 USDC |
| `STANDARD` | `local >= D 09:58` | `TAIL` | 2.22 USDC |
| `TAIL` | 任意更早或更晚报文 | `TAIL` | 2.22 USDC |

其中 `D` 是市场对应站点的本地事件日期；时区必须来自带lineage的站点/IANA时区合同，禁止用UTC或机器本地时区替代。

## 2. 纯函数

```text
heavy_cutoff = LocalDateTime(D-1, 19:54, station_timezone)
tail_cutoff  = LocalDateTime(D,   09:58, station_timezone)

clock_phase =
  TAIL     if observed_local >= tail_cutoff
  STANDARD if observed_local >= heavy_cutoff
  HEAVY    otherwise

next_phase = max(current_phase, clock_phase)
clip_cash  = {HEAVY:15.00, STANDARD:9.05, TAIL:2.22}[next_phase]
```

`max`按 `HEAVY < STANDARD < TAIL` 排序，因此旧报文、时钟折返和乱序checkpoint只能保持当前状态，不能回退阶段；若首次检查已经越过两个阈值，则一次追赶到`TAIL`，但函数本身不创建订单或intent。

## 3. 输入合同

| 字段 | 类型 | 规则 |
|---|---|---|
| `policy_version` | string | 必须等于显式已验证版本；未知版本fail closed |
| `current_phase` | enum | 仅`HEAVY/STANDARD/TAIL` |
| `event_date_local` | date | 站点本地事件日期，不接受datetime冒充 |
| `observed_at_utc` | aware datetime | 必须带时区；函数转换为站点本地时间 |
| `station_timezone` | IANA timezone | 必须可解析且有lineage |
| `checkpoint_id` | non-empty string | 幂等事实标识；空值fail closed |

输出包括`clock_phase`、`next_phase`、是否迁移、两个本地阈值、现金模板及reason code；不包括选桶、目标仓位、保留价、盘口、订单或资金预留。

## 4. 数据证据

校准使用创建日2026-07-14至15，最终审计固定为创建日2026-07-16：

| 转换 | 校准继续最大时刻 | 校准切换最小时刻 | 配置阈值 | 校准P/R/F1 | 审计P/R/F1 |
|---|---|---|---|---|---|
| `15→9.05` | D-1 19:53:37.844 | D-1 19:54:32.013 | D-1 19:54 | 1.000/1.000/1.000 | 1.000/1.000/1.000 |
| `9.05→2.22` | D 09:56:55.140 | D 09:58:24.066 | D 09:58 | 1.000/1.000/1.000 | 1.000/1.000/1.000 |

45站按交错集合拆成A/B两组后，校准和最终审计仍均为F1=1.000；若错误使用UTC代替站点本地时间，最终审计F1分别降至0.526和0.160。

绝对成本+距结束+静默间隔基线的最终审计F1分别只有0.909和0.255，所以成本/库存完成度是阶段内状态与加仓约束，不是这两个现金模板转换的第一触发器。

## 5. 版本边界

- 本状态机只对`source_clip_clock_v20260714_16`有效；更早的15 USDC策略epoch不服从同一晚间阈值，不能混训。
- 2026-07-16 17:00 UTC后出现的是另一全局现金模板版本：最近7天补充回放已识别`STANDARD=10.77`、`TAIL=0.25`及相同的站点本地`D 09:58`切换，但`HEAVY`仍无样本；该部分版本只允许已识别阶段，`HEAVY`必须fail closed，详见`basket_phase_policy_versions_v2.md`。
- 阈值复现的是公开签名订单行为，不证明私有调度器为何选择19:54和09:58。

## 6. 与篮子系统的边界

`BasketPhase`只选择现金模板；完整决策仍按以下顺序解耦：

```text
P0/P1支持集与偏置 → 库存缺口选择下一桶 → 盘口路径/价值上限决定挂价
→ BasketPhase选择现金模板 → 目标缺口/预算/深度裁剪现金 → shares=cash/price
```

该纯函数当前只能进入零订单Shadow；不得连接现有跟单策略、生产数据库、订单gateway或服务启停。

## 7. 验证入口

- `python3 analyze_recent_7d_basket_phase_thresholds.py`
- `python3 validate_recent_7d_basket_phase_state_machine.py`
- 新部分版本：`python3 analyze_recent_7d_clip_policy_versions.py && python3 validate_recent_7d_clip_policy_versions.py`
- 分析校验：13/13通过。
- 纯函数边界/回放校验：33/33通过。
- 低→高→低时钟回绕、未知版本、未知时区、无时区时间、错误phase及空checkpoint均已覆盖。
