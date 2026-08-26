---
document_id: basket-phase-policy-versions-v2
version: 2.0.12
status: TARGET_PRODUCTION_NO_LIVE_RUNNING
live_order_authorized: false
production_mutation: false
source_window_utc: 2026-07-10T12:43:15.138Z/2026-07-17T11:02:42.076Z
orders_submitted: 0
writes_production_db: false
writes_target_no_live_ssot: true
pdf_modified: false
---

# 最近7天源账户统一下单公式：策略版本 × 本地阶段

> 本合同受`bayes_basket_target_architecture_v1.md`冻结生产宪章约束；cash phase先服务RKPK单站纵向切片。45站扩展门是RKPK真实纵向链`MATERIALIZED＋RUNNING＋确定性回绕`，不是forward OOS；OOS只控制参数、权重及live晋级。

## 1. 最核心结论

> 2026-07-28 cash authority修订：`production_no_live`与正式晋级分离。显式no-live authority可在forward OOS前运行并写target no-live SSOT，持续形成decision/training fact；`FORMAL_PROMOTED`仍由内容寻址manifest与OOS证据唯一决定，只控制参数/权重和未来live晋级。45站扩展不等待forward OOS，但必须先证明RKPK真实纵向链已物化、resident正在消费且故障后可确定性回绕。

> 2026-07-29 实现状态（06:24Z）：自有cash-phase forward/no-live writer已接入唯一resident cadence。完整Maker bundle合同已真实物化：RKPK同一P1/WS周期对37℃、38℃、39℃原子生成3条Maker intent，并以单一decision/training fact绑定总预留；两轮总预留2.26668/2.26408 USDC均与各腿名义金额之和精确一致。报价变化会内容寻址接替并释放旧bundle；无正完整bundle时整篮HOLD，不退化成先买单桶。订单事件、执行回执、gateway写和live授权均为0。forward scorecard不足只阻断参数/权重及live晋级。

> 2026-07-29 07:26Z执行合同收口：跨事件共享资本同样以完整bundle为最小单位，不再暴露单腿allocator；每个事件整包入选或整包HOLD。真实RKPK bundle为38℃/39℃/40℃三腿，预留2.65290 USDC等于各腿名义金额总和。确定性部分/全成交触发实际资本结算和全篮重算；未知、迟到、冲突或FOK部分成交会取消安全prepared兄弟腿并保持未知资本预留，cycle进入`reconcile_required`。当前production closure PASS，196个跨域相关测试文件共1689项回归通过，零live与零gateway边界不变。

> 2026-07-28 生命周期补充：正式label先写主SSOT，再以独立keyset投影；单条poison不阻断后续合法证据但该轮仍fail closed；旧payload只有在最新settlement label、内嵌正式scorecard/attribution receipt、保留期和零下游依赖全部成立后才可tombstone清理。仅有SHA、合成scorecard或未结算样本一律不得删除或晋级。

> 2026-07-29 真实forward物化：主SSOT现有3条互异`fact_id/decision_scope_key`，最新fact属于当前唯一prepared decision；旧两条绑定历史blocked decision，只保留append-only前向审计，不得冒充当前候选。重复物化和target重启均零追加，当前label仍为0且必须等待同日正式truth。它们只形成no-live训练事实，不授权订单，也不改变forward OOS对参数、权重及live晋级的阻断。

最近7天45个城市、10,949笔精确签名Maker YES BUY订单表明，源账户不是用“概率或edge直接算出每笔金额和份数”，而是依次做四个不同决策：

```text
候选支持集/库存缺口决定完整Maker bundle包含哪些桶
→ 当时市场路径与价值上限决定挂什么价格
→ 全局策略版本 × 站点本地时间阶段决定标准现金片段
→ 现金片段 ÷ 挂价机械地产生份数
```

所以统一的是决策结构，不是所有城市永远使用同一个固定金额。

## 2. 已识别的版本—阶段表

| 策略版本 | 公开有效边界 | `HEAVY` | `STANDARD` | `TAIL` |
|---|---|---:|---:|---:|
| `source_clip_clock_v20260714_16` | 最后观测至2026-07-16 16:49:21.069Z | 15.00 | 9.05 | 2.22 |
| `source_clip_clock_v20260716_17_partial` | 首次观测自2026-07-16 17:00:09.074Z | 未观测，fail closed | 10.77 | 0.25 |

新旧版本之间有10.800分钟无订单间隔，因此私有配置的精确切换秒不可识别；系统只能把“最后旧观察”与“最早新观察”作为两个事实边界，中间区间必须拒绝猜测。

## 3. 阶段公式

令`D`为市场对应站点的本地事件日，`t_local`由站点IANA时区换算：

```text
phase(t) =
  HEAVY     , t_local < D-1 19:54
  STANDARD  , D-1 19:54 <= t_local < D 09:58
  TAIL      , t_local >= D 09:58

phase_next = max(phase_current, phase(t))
C_base = T[explicit_policy_version, phase_next]
```

`phase_next`只进不退；版本必须显式提供，未知版本、版本有效期空洞以及版本中未观测的阶段一律fail closed。

新版证据不是单点巧合：221笔`10.77`订单全部落在站点本地`STANDARD`，25笔`0.25`订单全部落在`TAIL`；13个同时观察到两阶段的事件中，`10.77→0.25`以本地`D 09:58`预测的Precision/Recall/F1均为1.000，而错误使用UTC的F1为0。

## 4. 可直接执行的统一公式

### 4.1 选桶

```text
G_b(t) = max(q*_b(t) - q_b(t), 0)
b* = argmax_{b in S(t)} U_b(G_b, eligibility_b, value_gate_b)
```

- `S(t)`是当前唯一SSOT阶段P0/P0-M/P1产生并经证据闸门确认的候选支持集；TAF只可在上游经逐样本scope绑定、与正式NWP组合exact join并按holdout残差相关折扣重复信息的OOS artifact形成有界P0-M修订。校准后TX是第一层确认/修正，云量、降水、风场是同一proof内的物理二次确认；现金阶段不得读取或重新解释原始TAF。
- `q*_b(t)`是系统自己的目标仓位，不得伪装成已识别的源账户私有目标。
- `G_b(t)`是“库存缺口”：目标还要多少份减去当前已有多少份。它决定本周期完整Maker bundle必须包含哪些桶及各腿规模；不允许按优先级先提交其中一桶。只有全部正缺口桶各有一条合规Maker腿且总资本可原子预留时，bundle才激活。

执行提交合同固定为：`完整bundle联合DeltaBasketScore>epsilon → 同周期多Maker intent＋总资本预留＋decision＋training fact原子提交`。各腿可独立部分成交，但任何成交、撤销、未知或迟到回报都使旧bundle失效并触发全篮重算。紧迫FOK只允许在Maker确认撤销后处理单桶剩余缺口，禁止与Maker bundle并存。
- 最近7天公开代理中，等份/等成本缺口只能达到Top-1 40.58%、Top-3 82.74%，说明缺口方向有效但私有目标向量仍不可见。

### 4.2 挂价

```text
r_b(t) = tick_down(min(market_path_price_b(t), reservation_ceiling_b(t)))
```

- 10,942笔带参考价的Maker订单中，挂价与当时市场参考价Spearman为0.967，67.46%挂在参考价下方，中位折让0.006 USDC。
- 新版246笔模板订单的对应Spearman仍为0.845、参考价覆盖100%、中位折让0.005 USDC。
- 因此市场盘口路径决定实际价位，概率/edge更像准入与保留价上限，不能用来直接生成某个精确tick。

### 4.3 单笔现金

```text
C_order,b(t) = min(
  T[policy_version, phase(t)],
  r_b(t) × G_b(t),
  remaining_budget(t),
  executable_depth_cash_b(t)
)
```

其中第一个参数是版本化的标准现金片段；目标缺口、剩余预算和可成交深度只负责向下裁剪，不能反过来把标准模板解释成概率、edge或累计库存的连续函数。

### 4.4 份数

```text
q_order,b(t) = C_order,b(t) / r_b(t)
```

最近7天原始订单的最大恒等误差为`2.27e-13`，新版阶段样本为`2.84e-14`；因此“买多少份”不是第五个独立决策，同一现金模板下价格越低，机械获得的份数越多。

## 5. 概率、价格、金额、份数各自负责什么

| 变量 | 主要职责 | 不负责什么 |
|---|---|---|
| P0/P1概率与偏置 | 支持集、目标仓位和价值上限 | 不直接生成精确挂价或固定现金片段 |
| 库存缺口 | 完整Maker bundle腿集合及各腿正缺口 | 不允许决定“先买一桶”，也不单独决定每腿金额 |
| 市场盘口路径 | 实际挂价tick与可成交深度 | 不独立决定目标桶 |
| 策略版本×本地阶段 | 标准单笔现金片段 | 不决定选桶与方向 |
| 现金片段÷挂价 | 最终订单份数 | 不构成新的策略判断 |

## 6. 动态调仓的底层驱动

每个新checkpoint重新计算`S(t)`、`q*(t)`、`G(t)`、保留价与深度：概率或新证据改变支持集/目标仓位，成交改变当前库存，盘口改变挂价与可成交深度，本地时钟改变现金阶段；只有这些输入使某桶仍有正缺口且价格不过价值上限时才继续加仓，否则停止该桶。

这意味着动态调仓不是“哪个桶edge最高就全仓追哪个桶”，也不是单纯彩票；它追求的是在已选支持集内，以不过保留价的被动订单，分阶段把库存推进到显式目标，同时限制单次现金暴露和尾盘风险。

## 7. 证据边界

- 这是公开行为复刻公式，不等于源账户私有代码或其选择`10.77/0.25`数值的因果解释。
- 签名成交订单不包含取消、未成交和从未挂出的私有订单，也无法完全识别`q*`。
- 新版`HEAVY`没有样本，任何默认值都会是编造，当前纯函数必须拒绝该阶段。
- 本研究未接订单gateway、未写生产数据库、未修改PDF，也未触碰现有跟单策略生产线。

## 8. 复现入口

```bash
python3 analyze_recent_7d_clip_policy_versions.py
python3 validate_recent_7d_clip_policy_versions.py
```

- 新版行为分析：19/19通过。
- 新旧版本纯函数、跨时区边界、有效期空洞、未知重仓阶段及逐单回放：27/27通过。
