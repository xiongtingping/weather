---
status: research
authority: L2
owner: weather-system
effective_from: 2026-07-13
activation_state: pending-development
implementation_authority: none
depends_on:
  - ../constitution/SYSTEM_CHARTER.md
  - ../adr/ADR-0001-PRODUCTION-PROBABILITY-MODEL-IDENTITY.md
  - ../adr/ADR-0002-TARGET-ARCHITECTURE-SHADOW-AUTHORITY.md
  - ../architecture/TARGET_ARCHITECTURE_V2.md
  - ../contracts/SSOT.md
  - ../contracts/SOURCE_ROLES.md
supersedes: []
last_verified_commit: not-implemented
---

# 物理状态空间 Bayes 最高温预测升级方案

> **状态：待开发升级（Pending Development）。**
>
> 本文是最高温概率内核的独立研究与开发蓝图，不是 current、deployed 或 production cutover 事实，不改变当前生产 posterior、decision、order、artifact 或参数。实施前必须形成 accepted ADR；生产切换必须另有 accepted cutover ADR。切换后只允许一个生产概率主模型，旧模型不得作为 fallback。

> **2026-07-15 时序裁决：** [ADR-0003](../adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md) 已批准轻量八状态 production successor v6。凡本文与 ADR-0003 在 `Update 后再次 post-state transition`、Predict 是否直接改变最高温边际、当前报文是否可进入 Predict、NWP 小时曲线是否再次形成 likelihood 等方面冲突，均以 ADR-0003 为准。本文较重的全物理 RBPF 路线继续保留为后续研究，不是 v6 第一阶段实现合同；v6 不建立长期并行 shadow 主线。

## 0. 模型宪章（首页摘要）

> **不以温度外推温度，而是推断产生温度的物理状态；温度是这些状态的强观测结果，最高温是未来物理路径经过冻结报告算子后的派生目标。**

唯一因果主线：

```text
四川盆地/站点/季节气候先验
→ NWP、TAF/AMD等弱预测提供未来物理强迫与多情景
→ METAR/SPECI温度、露点、云、雨、风和气压联合更新隐含物理状态
→ 从状态posterior生成时间相关的升温、降温、冷池、恢复与重新升温路径
→ 在冻结AMSC计划槽应用报告、编码、质控与缺报算子
→ 得到物理报文最高温P(X)
→ 经独立X→S bridge得到结算与市场桶P(B)
→ 原子提交唯一posterior SSOT
```

不可违反的设计约束：

1. **先验单入口**：四川盆地与大气候背景只建立一次层级先验；当日NWP、TAF和实况不得再次复制同一base rate。
2. **预测与事实分层**：NWP、TAF及TAF AMD是带偏差和相关性的弱预测；METAR/SPECI是canonical强观测；AMD替换受影响预测窗口，不与旧TAF独立相乘。
3. **状态先于结果**：云、雨、露点、风、气压和温度先更新净加热、湿润、混合、平流、冷池与天气状态，不直接拼装多个桶级LR修正最高温。
4. **温度动量降位**：温度轨迹和动量只表示实际净加热是否兑现，是联合观测innovation或诊断，不是独立外推器，也不形成第二份likelihood。
5. **可辨识优先**：AMSC与弱driver不能区分的`H/C_eff/A/E/D`必须合并为可辨识净倾向并保留不确定性，禁止输出虚假精确通量或使用自由Efficiency吸收误差。
6. **路径先于峰值**：必须传播天气转换、driver误差、过程噪声和参数不确定性，保留合理多峰；见顶、Termination和重新升温只能由未来路径事件积分产生。
7. **物理与结算解耦**：`X`始终是冻结AMSC合同定义的物理报文最高温；结算源只通过`X→S` bridge和最终标签进入，市场价格、订单、持仓与P&L不得进入天气模型。
8. **一证据一次、唯一主模型**：同一canonical报文及其派生量只能在一个联合composer中消费一次；Shadow胜出并获accepted cutover前当前模型仍是唯一production，切换后删除旧入口且禁止fallback。
9. **多假设竞争**：露点下降、云散、能见度改善、风转、压变或温度突升都不得绑定单一物理结论；必须让干混合、冷暖平流、云辐射、降水冷池和报告误差等候选解释在同一posterior中竞争。
10. **不确定性可行动**：输出不仅说明“最高温可能是多少”，还必须说明当前主要情景、主要不确定性来源，以及下一条计划报文中最能区分情景的字段与结果区间；这些解释只能从同一posterior派生，不得形成第二套打分模型。

## 1. 结论

保留现有 Bayes 宪章、AMSC报文最高温目标、后验 SSOT、hard mask、事件幂等、原子事务和交易链路；将概率内核从“直接修正最高温桶”升级为“先推断能量收支、边界层热响应、动力输送和天气状态，再生成潜在温度路径，最后经过AMSC发报/编码算子投影到报文最高温桶”。

最终唯一主线：

```text
气候/盆地先验 + 弱预测未来强迫
                    ↓
净辐射与地表能量分配 Rn = H + LE + G + Storage
                    ↓
边界层有效热容量 + 平流/卷入/下沉/冷池
                    ↓
AMSC canonical 实况连续校正物理状态
                    ↓
潜在近地面温度路径分布
                    ↓
冻结AMSC发报时刻与编码算子
                    ↓
AMSC报文最高温后验 P(X)
        ↓
X→S bridge + 冻结市场合同
        ↓
市场桶后验 P(B)
        ↓
唯一 posterior SSOT
        ↓
Decision Core → Transactional Outbox → CLOB
```

本次是“外围合同不变、概率内核升级”，不是重建第二条概率链。

## 2. 目标、变量与边界

### 2.1 物理目标

```text
X = ZUUU 目标站点日的 AMSC primary METAR 报文最高温
```

定义：

```text
T̃(τ) = 机场近地面潜在连续温度过程
K_C = 冻结合同C规定的AMSC primary METAR计划发报槽集合
Y_k = 第k个计划槽经观测、质控和报文编码后形成的温度值
```

```math
Y_k=\mathcal{O}_C\left(\widetilde T(\tau_k),\epsilon_k,encoding_k,quality_k\right),\quad k\in K_C
```

```math
X=\max_{k\in K_C}Y_k
```

因此，连续潜在温度峰值：

```math
X^*=\max_\tau\widetilde T(\tau)
```

只能作为内部物理诊断，不能替代合同目标 `X`。模型必须先生成潜在温度路径，再通过冻结的AMSC计划槽、报文编码、质控与缺失语义得到 `P(X)`；不得把连续曲线裸最大值冒充AMSC报文最高温。

### 2.2 市场目标

```text
S = 市场 resolution source 最终采用的结算标量
B = 冻结市场合同定义的结算桶
C = 来源、日期、单位、舍入和桶边界合同
J = 由同一physical path按冻结算子派生的天气型/空间bridge条件摘要
```

```math
p(S\mid e,C)=\iint p(S\mid X,J,C)p(X,J\mid e)dX\,dJ
```

```math
p(B\mid e,C)=\sum_S p(B\mid S,C)p(S\mid e,C)
```

物理最高温 `X` 与市场结算 `S/B` 必须解耦。`X→S bridge`、市场 finality、样本量、strict-as-of 和 OOS 不得阻断 AMSC 入库、物理 X live 更新或当前已批准 artifact 运行；它们只约束候选 artifact 晋级和市场级校准。

### 2.3 架构边界

- 概率层只预测物理状态、未来温度路径、`P(X)` 与 `P(B)`。
- 交易层只能读取唯一公开 `PosteriorSSOTFact`；它可引用内部posterior checkpoint，但不得直接读取内部状态后重新解释天气证据或复算概率。
- UI 只读 SSOT 快照，不得形成第二口径。
- 市场价格、订单、持仓和 P&L 不得进入天气概率模型。
- 物理状态 checkpoint 是内部推理状态，不是第二个概率 SSOT。

## 3. 为什么需要升级

### 3.1 当前方案的准确定位

当前方案不是“纯温度动量外推”，而是：

> **物理证据条件化的结果空间Bayes模型。**

其主要表达方式是：

```text
先验与弱预测最高温分布
+ AMSC当前温度、温度轨迹与动量
+ 云、雨、露点、风等物理证据
→ 直接修正剩余升温、降温与最高温桶
```

概念公式为：

```math
P_t(X)
\propto
P_{t^-}(X)
\exp\left[s_\psi\left(X;T_{1:t},cloud_t,rain_t,dewpoint_t,wind_t\right)\right]
```

其中 `sψ` 是联合强观测score或经校准的增量log-LR。当前方案已经使用物理证据；当云、雨、露点或风向先于温度变化到达时，它也能够在温度真正减速之前更新最高温后验。因此，不能把当前方案描述成“必须等待温度变化后才反应”。

温度动量只是当前联合强观测的一部分，不是当前方案的全部定义。

### 3.2 新方案的准确定位

新方案是：

> **物理证据条件化的状态空间Bayes模型。**

它并没有首次引入物理证据，而是改变物理证据的作用位置：

```text
当前方案：物理证据 → 直接修正最高温结果
新方案：  物理证据 → 修正物理状态 → 生成未来路径 → 投影最高温结果
```

两套方案可以读取相同的温度、云、雨、露点、风和预测源。核心差异不是“有没有物理证据”，而是状态、时序和条件独立结构是否显式建模。

| 维度 | 当前结果空间Bayes | 目标状态空间Bayes |
|---|---|---|
| 物理证据 | 直接贡献桶级score/log-LR | 更新固定`T̃/q/G_net/W/C_cp/R`状态并重加权物理driver分解 |
| 温度动量 | 联合强观测特征 | 净加热率的观测投影 |
| 时间记忆 | 特征窗口、规则或剩余升温分布隐式表达 | posterior checkpoint和状态转移显式继承 |
| 未来路径 | 隐含在最高温/剩余升温分布 | 显式生成潜在路径并经报告算子投影 |
| 重新升温 | 需要特征或专门规则表达 | 由天气状态转换和未来路径自然产生 |
| 计算与校准 | 更简单，直接围绕X训练 | 更复杂，状态、路径和X都需验证 |

### 3.3 升级解决的结构问题

当前方案能够工作，而且在数据较少、隐状态不可辨识或物理参数尚未校准时，可能比复杂状态模型更稳。升级不是因为当前方案“没有物理”，而是因为结果空间映射把物理过程压缩得较早，难以显式回答：

1. 未来还能输入多少有效能量；
2. 这些能量有多少转化为近地面气温；
3. 暖平流、下沉和边界层混合贡献多少；
4. 升温或降温何时终止；
5. 终止后是否可能重新升温；
6. 当前预测源分歧对应哪些独立天气情景。

结果空间模型还必须额外防范：同一条报文派生出的温度、动量、轨迹和云雨在多个score中重复计入；跨事件物理状态只能通过人工窗口保持；短时云遮、降水冷池与重新放晴需要专门交互项才能区别。

状态空间模型的目标是将这些时序和物理关系放进一个联合composer，而不是简单增加更多特征或更多LR。

### 3.4 第一性原理约束

最高温的底层不只是辐射，而是“净辐射和地表能量如何分配为感热”与“边界层有效热容量、平流、卷入、下沉、蒸发冷却和天气状态如何共同改变近地面温度”的联合结果。

第一性原理要求进一步校正三个抽象：

1. `Energy`不是单一辐射值，而是净辐射及其在感热、潜热、地表/土壤热通量和储热之间的守恒分配；
2. `Efficiency`不能是无约束自由乘数，它是感热份额、边界层有效热容量、地表—空气耦合和湿度状态的可解释合成；
3. `Termination`不是与Energy并列相乘的第三因子，而是云雨、太阳高度、平流、冷池和边界层状态发生转换后，“未来计划槽是否还能超过当前报文最高值”的派生概率。

温度动量继续保留，但其定位从“主要外推器”调整为“实际净加热率的实时观测投影”。它用于反推模型能量和输送是否兑现，不能作为与温度、云雨独立相乘的第二份事实。

### 3.5 新方案不自动更优

第一性原理结构更完整不等于统计预测必然更准。当前结果空间模型具有明确优势：

- 直接围绕最终 `X` 校准；
- 参数更少、可辨识性更强；
- 运算更快；
- 小样本和冷启动阶段可能更稳定；
- 如果联合score正确学习物理证据交互和相关性，同样能够提前反应。

状态空间模型的潜在优势是显式时序、物理一致性、多路径和重新升温；其风险是隐状态不可辨识、输入误差传播、参数复杂、错误物理结构和校准成本更高。

因此，第一性原理只决定候选模型的结构方向，不能直接授予production权威。替换条件必须是：在相同as-of证据、相同目标和相同hard mask下，新模型通过Shadow证明同时改善或满足冻结门槛中的最高温准确率、概率校准、正确反应领先时间、稳定性和运行时延。物理解释更漂亮本身不构成晋级证据。

### 3.6 当前、目标与三类简化参照

必须把“当前真实方案”“目标候选方案”和“用于解释或消融的简化框架”分开。**当前方案与目标方案都有物理证据；它们不是‘非物理模型与物理模型’的区别，而是‘结果空间Bayes与状态空间Bayes’的区别。**

| 路线 | 核心哲学 | 证据如何作用 | 优势 | 根本局限 | 本方案定位 |
|---|---|---|---|---|---|
| **当前：物理证据条件化结果空间Bayes** | 直接围绕最终最高温结果校准 | 温度、轨迹、动量、云、雨、露点、风等物理证据联合修正剩余升降温与 `P(X)` | 目标直接、参数较少、反应快、小样本更稳；物理证据可在温度变化前提前更新 | 物理机制主要压缩在联合score和结果分布中，隐状态、路径分支与重新升温不够显式 | 当前真实production建模哲学；绝不是纯温度动量外推 |
| **目标：物理证据条件化状态空间Bayes** | 先推断可辨识低维物理状态，再生成未来路径和目标 | 使用同类物理证据更新`T̃/q/G_net/W/C_cp/R`并重加权`Ψ`机制解释，再投影 `P(X)` | 显式处理时序、多峰、机制不确定性、见顶和重新升温 | 参数更多、校准和可辨识性要求更高 | 唯一目标候选实现；必须经Shadow胜出后才可晋级 |
| 纯温度动量外推 | 最近结果具有短时惯性 | 只用最近温度斜率外推未来温度 | 极快、参数少、适合消融与故障诊断 | 无法使用云雨露点风等证据解释未来机制 | 仅为消融基线；不代表当前系统 |
| 辐射单驱动 | 太阳与净辐射是白天局地加热的主要源头 | 由辐射变化直接推导温度变化 | 能在云量和太阳高度变化时提前反应 | 最高温并非由辐射单独决定；忽略能量分配、有效热容量、平流、卷入和地表湿润 | 仅为物理模块/消融基线，不是当前或目标完整模型 |
| `Energy–Efficiency–Termination` | 将未来升降温拆成驱动力、响应效率和终止机制 | 用三个概念组织温度及云雨露点风等物理证据 | 易沟通、便于列举证据与做模块审计 | 若机械相乘，会产生自由参数、证据重叠、二次计数和二元终止假设 | 保留为概念分解与诊断语言，不作为独立概率公式 |

所以，升级不是给当前方案“补上物理证据”。当前方案已经有物理证据，而且能够用云雨露点风等先行信号提前修正最高温。升级只改变联合建模的位置：

```text
当前：同一组物理证据 → 联合结果空间score/likelihood → P(X)
目标：同一组物理证据 → 联合状态likelihood → P(Z,U) → 未来路径 → P(X)
```

这也意味着目标方案不能靠“使用了物理证据”证明优于当前方案；只能证明显式状态与路径结构在相同物理证据条件下带来了额外准确率、校准度或正确反应速度。

目标方案继承当前方案对温度及云雨露点风等物理证据的联合使用，并吸收三类简化参照中的有效部分，但不继承其错误简化：

```text
温度动量 → 作为实际净加热率的观测投影
辐射       → 作为外部强迫与地表能量账本入口
Energy     → 由净辐射、感热、平流、卷入等联合定义
Efficiency → 由能量分配和有效热容量派生的诊断量
Termination→ 由未来状态路径积分得到的事件概率
最终实现   → 一个联合状态空间Bayes composer
```

### 3.7 目标设计宪章

1. **目标先行**：最终目标始终是冻结AMSC计划槽上的报文最高温 `X`，不是连续潜在峰值、模型格点峰值或市场结算值。
2. **守恒先行**：辐射必须先进入能量账本；任何“效率”都不能绕过能量分配和有效热容量凭空改变温度。
3. **时序因果**：预测源约束未来强迫与路径，AMSC观测校正当前状态，未来路径产生 `P(X)`；不得反向用当天未完成结果校正当天参数。
4. **一证据一次**：同一报文派生出的温度、动量、轨迹和天气现象必须在一个联合观测模型中消费，不能拆成多个独立LR重复累计。
5. **保留分歧**：不同预测源代表的合理天气情景必须以多峰或混合状态保留；置信度下降由联合信息量和情景权重承担，不能靠超宽核制造虚假中间温度。
6. **偏差与分歧分离**：偏差校正移动每个源在给定条件下的候选峰，条件残差决定峰内宽度，源间相关性和情景概率决定峰权；偏差校正本身不得强制合峰。
7. **可辨识约束**：递推状态只保留固定可辨识坐标；H/C_eff/A/E/D等不可分机制只能作为带区间的条件诊断，不能用自由状态吸收全部误差。
8. **可证伪晋级**：新结构只有在相同证据Shadow中改善冻结指标才可替换生产模型；第一性原理合理性是结构约束，不是上线许可。
9. **唯一SSOT**：物理状态是内部递推状态，唯一对外概率真相仍是原子提交的posterior checkpoint；UI和交易层不得建立第二composer。

## 4. 最终 Bayes 模型

### 4.1 变量层级

```text
C      = 季节、站点、四川盆地、地表与冻结目标合同上下文
F_A    = as-of时刻可用的弱预测源集合及availability mask A
U_t    = 弱预测提供的未来物理driver情景：辐射、通量、PBL、云雨、背景风温湿场与空间梯度
Θ      = 冻结慢参数：站点/季节偏差、状态耦合、转移、过程噪声与观测噪声
Z_t    = 唯一递推的低维动态物理状态
O_k    = 第k条AMSC canonical observation
Y_k    = 冻结计划槽上的AMSC报文温度
X      = 所有合同槽Y_k的最大值
J      = 由同一physical path按冻结算子派生的bridge条件摘要；只含天气型、风、云雨和空间不确定性，不是递推状态
S      = 结算源最终结果
B      = 冻结市场合同温度桶
```

唯一在线状态固定为：

```math
Z_t=(\widetilde T_t,q_t,G_{net,t},W_t,C_{cp,t},R_t)
```

| 状态 | 定义 |
|---|---|
| `T̃_t` | 机场近地面潜在连续温度 |
| `q_t` | 机场近地面潜在水汽状态，以canonical比湿表示；露点通过观测算子约束它 |
| `G_net,t` | 已实现的净温度倾向，统一吸收当前证据可辨识的局地加热、平流、混合与冷却合量 |
| `W_t` | 地表湿润、蒸发潜力与降水后记忆状态 |
| `C_cp,t` | 非负的降水冷池/蒸发冷却瞬变记忆；描述冷却影响及恢复，不等于精确冷池强度 |
| `R_t` | 晴热、云遮、降水冷池、平流转换等天气状态 |

`R_t`是`Z_t`中的离散坐标；公式中为展示切换结构而单列`R_t`时，不代表存在第二份状态。所有checkpoint必须使用同一组六个坐标，不允许按可用源切换`full/reduced/net_tendency`三套状态维度，也不允许在日内临时增删坐标。

详细物理分解统一记为非递推诊断量：

```math
\Psi_t=(R_{n,t},T_{s,t},H_t,LE_t,G_t,\Delta S_t,C_{eff,t},Q_{H,t},A_t,E_t,D_t)
```

`Ψ_t`只能由同一弱driver情景、冻结慢参数和`Z_t`条件化得到 `P(Ψ_t|Z_t,U_t,Θ,C)`；它不进入checkpoint状态向量、不拥有独立latest、不接受AMSC对各分量的自由反演。AMSC主要直接识别`T̃/q/G_net/R`，云雨与前期降水对`W/C_cp`提供软约束；`H/C_eff/A/E/D`只能随driver情景重加权后作为带不确定性的解释性分解。

`Efficiency`不设置为状态或自由乘数。需要诊断时可输出表观感热份额`η_H=H/Rn`和热响应`Q_H=H/C_eff`，但必须同时展示条件区间、driver lineage与能量闭合项；不得把诊断量重新送入composer。

### 4.2 地表能量守恒与温度倾向

地表能量账本：

```math
R_{n,t}=SW_{\downarrow,t}-SW_{\uparrow,t}+LW_{\downarrow,t}-LW_{\uparrow,t}
```

```math
R_{n,t}=H_t+LE_t+G_t+\Delta S_t
```

允许显式能量闭合误差：

```math
R_{n,t}=H_t+LE_t+G_t+\Delta S_t+\varepsilon_{closure,t}
```

`ε_closure`必须是零中心、有界尺度并可审计的模型误差；不能通过自由均值长期制造或消灭能量。

边界层/近地面温度倾向的最小结构：

```math
G_{driver,t}=\mathbb{E}[Q_{H,t}+A_t+E_t-D_t\mid Z_t,U_t,\Theta,C],\qquad Q_{H,t}=H_t/C_{eff,t}
```

```math
\frac{d\widetilde T_t}{dt}=G_{net,t}+\varepsilon_{T,t}
```

`G_driver`是`P(Ψ|Z,U,Θ,C)`给出的物理driver合量；其中`D`已条件化`W/C_cp`，不得再把同一冷却记忆重复相加。`G_net`是唯一递推、可被AMSC轨迹识别的已实现净倾向。状态转移用`G_driver`约束`G_net`，而不是把`Q_H/A/E/D`各自设成可自由补偿的在线状态。局地辐射通过能量分配和有效热容量影响`G_driver`；冷暖平流、卷入和下沉可在局地辐射较弱时继续贡献。

模型不要求每个通量都有机场实测值。缺测通量由弱预测和层级先验形成带不确定性的`Ψ`诊断；温度轨迹只更新`G_net`及driver情景权重，不能把一条净趋势伪拆成多份精确通量。

### 4.3 联合Bayes因子分解

最终联合模型：

```math
\begin{aligned}
&P(\Theta,U_{0:H},Z_{0:H},X,S,B\mid F_A,O_{1:k},C)\\
&\propto P_{climate}(\Theta,U_{0:H},Z_0\mid C)\\
&\quad\cdot L_W(F_A\mid U_{0:H},Z_{0:H},\Theta,C,A)\\
&\quad\cdot\prod_j P(Z_{j+1}\mid Z_j,U_j,\Theta,C)\\
&\quad\cdot\prod_{i\le k}P(O_i\mid Z_{\tau_i},O_{<i},C)\\
&\quad\cdot P_C(X\mid Z_{0:H},O_{1:k})\\
&\quad\cdot P(S\mid X,\mathcal J_C(Z_{0:H},U_{0:H}),C_{settle})P(B\mid S,C_{market})
\end{aligned}
```

`P_C(X|...)`是冻结AMSC计划槽、编码、质控、active revision与缺报语义组成的报告算子，不是另一个可训练桶模型。`J=𝒥_C(Z,U)`只是在每条physical path上确定性派生的bridge条件摘要，不进入checkpoint状态。`P(S|X,J)`只属于独立bridge；`P(B|S)`只属于市场合同投影。三层不得反向更新`Z/U/Θ`。

#### 4.3.1 天气型条件化的 `X→S` bridge

`X→S`不得退化为固定站点差、固定修约或全季节共用的误差核。若市场resolution source与AMSC目标的站点、采样、报告或终局规则不同，bridge必须显式表达：

```math
P(S\mid X,J,C_{settle})
=\int P(S\mid X,\Delta_{station},\Gamma_{report},J,C_{settle})
P(\Delta_{station},\Gamma_{report}\mid X,J,C_{settle})
d\Delta\,d\Gamma
```

其中：

- `Δ_station`表示AMSC目标与resolution source之间的条件站点/空间差，不是可回写物理状态的在线bias；
- `Γ_report`表示resolution source自身的采样、精度、修约、缺失、revision与finality过程；
- `C_settle`只包含冻结市场合同、站点属性和土地利用/城市热岛等慢背景；`J`只包含由同一physical path向前派生的天气型、风、云雨和空间不确定性摘要；二者均不得包含市场价格、订单、P&L或未来标签；
- 晴空弱风、多云降雨、干湿平流和局地对流可拥有不同的`Δ_station`条件分布；局地对流应扩大空间差不确定性，而不是拍脑袋移动固定均值；
- bridge只做`X→S`向前边缘化。结算源日内观测和`settlement_actual`不得反向校正`Z/U/Θ`、AMSC报告算子或物理`P(X)`。

bridge的条件特征、最小样本量、开放尾部、缺失pattern、strict-as-of和OOS门槛必须冻结为独立artifact。条件样本不足时`P(S/B)`写明确NA，`P(X)`照常提交。

运行时不从头重复相乘，而是维护上一个已归一化checkpoint并做一次事件级预测—校正：

```math
p_t^-(Z_t,U,\Theta)
=\int P(Z_t\mid Z_{t^-},U,\Theta,C)
p_{t^-}^+(Z_{t^-},U,\Theta)dZ_{t^-}
```

```math
p_t^+(Z_t,U,\Theta)
=Normalize\left[P(O_t\mid Z_t,O_{<t},C)\,p_t^-(Z_t,U,\Theta)\right]
```

```math
P_t(X)=\int P_C(X\mid Z_{t:H},O_{1:t})
P(Z_{t:H},U,\Theta\mid F_A,O_{1:t},C)dZ\,dU\,d\Theta
```

新NWP run、TAF或TAF AMD到达时，先构造截至该as-of唯一active weak revision chain。受替换的旧revision从对应有效窗口退出，通过从绑定的pre-revision checkpoint重算或受审计density-ratio更新形成新driver generation；禁止把旧预测和其AMD/新run当独立证据继续相乘。

气候先验只能有一个入口：若由 `P0(Θ,U,Z0|C)`诱导出 `P0(X)`，不得再额外乘一份独立气候 `P0(X)`；若工程上保留直接桶先验，则弱预测和物理模型必须以相对共同先验的density ratio/Bayes factor进入，禁止base-rate双计。

### 4.4 弱预测源的数学角色

弱预测不是事实。它们为未来强迫和温度路径提供带偏差、残差和相关性的观测/专家情景：

```text
NWP驱动变量 → U_t情景
NWP/TAF温度轨迹 → U_t中的路径/天气情景约束
WU/TWC/直接最高温 → X专家分布，经共同先验校正后进入
```

直接最高温预测不得绕过AMSC报告算子强行钉住`X`；它必须通过逐源历史偏差、目标口径差异和source-combination校准后作为弱证据。TAF AMD只替换原TAF受影响窗口，不制造第二个forecast expert。D0 AMSC实况只在联合状态更新中重分配天气情景概率，不回写当天源能力参数。

### 4.5 AMSC观测与温度动量

AMSC温度、露点、云、雨、风和气压共同形成：

```math
P(O_k\mid Z_{\tau_k},O_{<k},C)
```

温度动量和轨迹是 `O_{1:k}` 的确定性派生摘要，用于识别实际温度倾向与模型预期净加热率的偏差。它们不是额外独立观测，不能在原始报文likelihood之外再无条件相乘一次。

### 4.6 报告算子、最高温投影与hard mask

对每条潜在未来路径，在冻结AMSC计划槽上应用相同报告算子：

```math
Y_j=\mathcal O_C(\widetilde T_{\tau_j},\epsilon_j,encoding_j,quality_j)
```

```math
X=\max_{j\in K_C}Y_j
```

```math
P_t(X\in b)
=Normalize\left[
H_t(b)
\int
\mathbf 1\left\{\max_{j\in K_C}\mathcal O_C(\widetilde T_{\tau_j})\in b\right\}
P(\Theta,U,Z\mid F_A,O_{1:t},C)d\Theta\,dU\,dZ
\right]
```

`H_t`只处理确定事实。例如已经有一条合同内primary METAR报告33℃，最终报文最高温低于33℃的状态必须归零。未来降雨、预计见顶、太阳高度下降或“物理上很难达到”都属于概率路径权重，不能冒充hard mask。

### 4.7 Termination、见顶与重新升温

先固定当前已观测合同槽最高值和未来合同槽最高值：

```math
M_k=\max_{i\le k,\,i\in K_C}Y_i
```

```math
F_k=\max_{j>k,\,j\in K_C}Y_j
```

必须分别输出：

```math
P(no\_future\_exceedance)=P(F_k\le M_k)
```

```math
P(strict\_peak\_already)=P(F_k<M_k)
```

```math
P(future\_tie)=P(F_k=M_k),\qquad P(new\_high)=P(F_k>M_k)
```

`no_future_exceedance`表示最终最高温不会再提高，但未来温度仍可能下降、回升并再次等于当前最高值。重新升温必须进一步区分“温度回升”“回升后再次等于最高值”和“回升后创新高”。Termination不是独立乘法项；天气状态 `R_t` 只用于生成未来路径，所有见顶、终止和重新升温概率均由路径事件积分产生。

### 4.8 log-LR的最终定位

唯一联合composer直接维护物理状态并产出posterior。事件级log-LR只作为可审计派生量：

```math
\log LR_t(b)=\log P_t(X=b)-\log P_{t^-}(X=b)+constant
```

不得在composer已生成新posterior后把同一log-LR再乘一次，否则会重复计入同一条报文。

### 4.9 在线Bayes的推荐实现

上述联合分布通常无法解析积分。面向“保留多峰、报文到订单p95不超过3秒”的工程约束，推荐实现为：

> **分层切换状态空间模型 + Rao–Blackwellized切换粒子滤波（RBPF）或等价的受控Gaussian-sum滤波。**

其中：

- 离散部分表示天气regime、预测情景、源组合和过程分支；
- 连续部分只表示固定`T̃/q/G_net/W/C_cp`状态；感热、有效热容量、平流和卷入留在driver条件诊断`Ψ`；
- 粒子或混合分量保留低温云雨峰、高温晴热峰及重新升温路径；
- 每个离散情景内的近似线性连续状态可用有界EKF/UKF或小型Gaussian update解析更新，避免所有维度都靠大量粒子采样；
- 过程模型、观测模型和报告算子均来自同一个冻结artifact。

每个canonical observation的在线步骤固定为：

```text
1. 读取上一个已归一化PhysicalStateCheckpoint
2. 按冻结driver generation传播各情景/粒子至报文as-of
3. 对整条AMSC canonical observation计算一次联合观测likelihood
4. 更新并归一化情景权重
5. 仅在有效样本量不足时执行保峰重采样/合并
6. 将每个情景前推至剩余AMSC计划槽
7. 经过同版本报告算子得到路径级X
8. 聚合、hard mask、归一化并原子提交唯一posterior checkpoint
```

权重更新与最高温投影为：

```math
w_k^{(i)}
\propto
w_{k^-}^{(i)}
P(O_k\mid Z_k^{(i)},R_k^{(i)},C)
```

```math
P(X\in b\mid F_A,O_{1:k},C)
\approx
\sum_i w_k^{(i)}
P\left(\max_{j\in K_C}Y_j^{(i)}\in b\right)
```

这里没有声称RBPF是理论上唯一“最优”算法；它是在当前多峰、时延和可审计约束下的首选工程近似。单一Gaussian EKF/EnKF容易把多个天气情景压成一个中间峰；未经约束的全维粒子滤波又可能在3秒链路中产生粒子退化和不可控时延。最终算法选择必须由确定性回放、有效样本量、峰保真度、校准度和p99性能共同决定。

### 4.10 弱预测的联合误差与偏差校正

弱预测源不能被当成彼此独立的LR。对源 `s` 的轨迹或最高温预测，采用条件偏差与联合残差模型：

```math
F_s
=g_s(U_{0:H},Z_{0:H},C)
+b_s(C,lead,regime,target\_definition)
+\epsilon_s
```

```math
\boldsymbol\epsilon_A
\sim
Q_A(C,lead,regime,source\_availability\_mask)
```

`Q_A`包含当前可用源组合的逐源残差尺度、源间相关性和组合级校准参数。实现必须满足：

- 每个exact source set或可回退的分层组合都有独立校准、误差和相关性参数；缺源作为显式输入，不通过静默重归一化伪造相同置信度；
- 偏差校正只移动源在当前季节、天气型、时效和目标口径下的候选分布，不把相互分离的合理天气情景强制合并；
- 峰内宽度来自该源的条件残差，源间分歧由混合峰及其权重表达，两者不能共用一个3–4℃宽核；
- 同一provider、同一模式家族或共享初边界条件的来源必须通过 `Q_A` 折扣相关信息；
- 同一源的驱动变量、温度轨迹和直接最高温要进入联合误差结构，不能被当成三份独立证据；
- 直接最高温专家分布必须相对共同先验形成density ratio，或在统一生成模型内消费，禁止再乘一次气候base rate；
- D0实况只重分配当天情景权重，绝不在线改写该源的长期bias、skill或相关性参数。

因此，偏差校正与多峰没有冲突：前者回答“每个源在当前条件下通常偏多少”，后者保留“校正后仍然存在的多个合理天气情景”。只有校正后的峰在条件残差尺度内高度重叠，且联合误差结构支持同一情景时，才允许自然合并。

### 4.11 AMSC联合观测模型

`O_k`不是若干可独立相乘的字段LR，而是一条带编码、质控和字段相关性的联合观测：

```math
P(O_k\mid Z_{\tau_k},R_{\tau_k},C)
=P(Y_k,D_k,Cloud_k,Wx_k,Wind_k,Pressure_k,Visibility_k\mid Z_{\tau_k},R_{\tau_k},C)
```

观测层至少固定以下语义：

- 温度和露点使用与报文编码、量化区间、修约和质控一致的likelihood，不能把整数报文当作无误差连续真值；
- 风向使用圆周分布，风速、阵风和风向突变按条件联合建模；
- 云量、云底、天气现象和降水使用适合其编码的类别、有序类别或区间likelihood，不能伪造不存在的连续雨量；
- 气压、能见度和露点可用于约束天气状态与气团变化，但必须通过联合条件结构避免重复解释同一云雨事件；
- 温度动量和温度轨迹由 `O_{1:k}` 派生，只用于改善联合状态识别或审计，不在原始观测likelihood之后再乘一次；
- correction、乱序、重复和缺失必须先由canonical层确定，状态滤波只消费一次规范事件；
- 非合同计划槽的SPECI可更新物理状态；但若冻结合同只认计划槽，它不能直接成为 `X` 的hard lower bound。

### 4.12 离线学习与在线运行边界

离线层可以使用分层Bayes、EM、变分推断、MCMC、粒子平滑或其组合，学习：

- 逐源条件bias、残差尺度、相关性和缺源组合参数；
- 状态转移、regime transition、过程噪声和有界慢参数；
- AMSC观测噪声、编码与报告算子参数；
- 参数posterior或经压缩的代表性参数集合。

在线层只能加载已经批准、签名并冻结的artifact：

- 不执行在线训练、MCMC或长期skill更新；
- 不因新增一条final X直接修改production参数；
- 只执行状态传播、联合观测更新、未来路径投影和posterior原子提交；
- 每日final X与事件级标签只生成新候选artifact，经strict-as-of回放和Shadow验证后再晋级；
- 训练样本量、OOS和`X→S bridge`不足不得阻断当前已批准artifact处理AMSC live事件。

这使“快速反应”和“稳健学习”解耦：在线滤波在每条报文到达时立即修正当天状态；慢速离线校准只决定未来artifact版本，不进入3秒交易热路径。

### 4.13 明确不选的实现路线

以下路线不得成为目标production概率内核：

- 在结果空间继续叠加一条独立物理LR链，再与状态模型posterior相乘；
- 把 `Energy × Efficiency × Termination` 当作三个无约束标量机械相乘；
- 用辐射单变量或温度动量单变量直接决定最高温posterior；
- 用单一超宽Gaussian核表示预测源分歧，或用单Gaussian滤波强制压平合理多峰；
- 在报文热路径运行全量NWP/PDE模拟、在线MCMC、在线重训或网络取数；
- 让黑箱神经网络直接成为无法审计的posterior权威；神经网络最多可作为有界、版本化、可消融的残差模型、转移模型或proposal；
- 同时运行两个production composer并在运行时择优、fallback或混合；
- 让交易层、UI或市场价格反向改写天气状态与 `P(X)`。

### 4.14 P0类型与接口合同（待开发）

本节只固定目标Shadow的开发接口，不声称现有production已实现这些类型。实现必须复用accepted架构的canonical identity、序贯时序与Transaction B边界，不得建立一套平行事实身份。

#### 4.14.1 唯一目标身份

```text
TargetIdentity = physical_weather_day_v1:{station}:{local_date}
```

其中 `local_date` 由冻结站点/目标合同决定；市场slug、市场是否存在、bridge状态和订单均不得进入物理目标身份。每个target只能有一条线性checkpoint链：

```text
P0(initial) → W1(weak_revision?) → P1(canonical_event_1) → W2(weak_revision?) → ... → Pn
```

`W`不推进canonical sequence，`P`精确推进一次；二者都必须成为同一线性latest链的唯一后继。同一前驱checkpoint出现两个不同后继、同一canonical sequence绑定不兼容trigger、或重试payload哈希漂移时必须fail closed，禁止自动选一支继续。

#### 4.14.2 初始化输入合同

```text
PhysicalBayesInitializationInputV1
  target_identity
  decision_as_of
  prior_context_snapshot_id + prior_artifact_id + sha256
  weak_forecast_snapshot_id + weak_artifact_id + sha256
  source_availability_mask + exact_source_set
  physical_driver_generation_id + sha256
  active_state_model_artifact_id + sha256
  reporting_operator_version + sha256
  resolution_contract_id + sha256
  hard_mask_version
```

约束：

- `physical_driver_generation`是同一weak forecast snapshot的可重放派生物，不是另一份可重复累计的弱证据；
- 初始化不接收D0 METAR/SPECI、温度轨迹、动量、市场价格、策略、订单或旧模型posterior；
- P0在同一温度格和目标定义上联合形成 `P_0(Z,U,X)`；不得先产出一个桶posterior，再额外叠加物理状态先验；
- registry只允许一个active state-model artifact；caller不得通过payload指定或替换artifact；
- bridge未闭合仍必须形成物理P0-X，`P(S)`、`P(B)`及交易投影写明确NA。

#### 4.14.3 CanonicalObservationEvent最小输入

```text
CanonicalObservationEventV1
  canonical_event_id
  target_identity
  canonical_sequence
  station
  report_type                 # METAR | SPECI；correction由revision/supersedes表达
  report_valid_at
  first_actionable_seen_at    # T1
  canonicalized_at            # 不得替代T1
  selected_source_fact_id
  raw_object_id + raw_sha256
  supersedes_event_id?        # correction语义
  quality_state
  is_contract_report_slot
  observation_payload
    reported_temperature
    reported_dewpoint
    cloud_layers
    weather_phenomena
    wind_direction_speed_gust
    pressure
    visibility
    field_na_reasons
```

canonical层负责来源选择、解析、质控、去重、乱序和correction关系；Bayes层不得重新抓取AMSC/AWC、重新选择报文或修改canonical事实。`is_contract_report_slot=false`的事件可以更新 `Z`，但不能直接抬高合同 `X` 的hard lower bound。

#### 4.14.4 WeakForecastRevisionEvent最小输入

```text
WeakForecastRevisionEventV1
  weak_revision_event_id
  target_identity
  source_id/provider
  revision_type               # NWP_RUN | TAF | TAF_AMD | WEAK_TARGET_EXPERT
  issued_at
  first_available_at
  valid_windows[]
  supersedes_revision_ids[]
  raw_object_ids[] + sha256[]
  decoder/artifact IDs + sha256
  source_availability_mask + exact_source_set
  active_weak_revision_chain_sha256
  weak_payload
    physical_driver_fields
    path_or_regime_fields
    direct_target_fields
    field_na_reasons
```

弱预测revision不进入`CanonicalObservationEvent`，不产生`JointObservationLikelihood`，也不改变`observed_contract_max`。TAF AMD及同源新run必须按`supersedes/valid_windows`重建唯一active weak revision chain；未受影响窗口继续继承旧revision，受影响窗口不得同时保留旧、新两份概率质量。更新只能生成新的driver generation与对应checkpoint投影，不能伪装成METAR/SPECI强观测。

#### 4.14.5 状态posterior合同

`PhysicalStatePosteriorV1`不是一组点估计，而是有界多峰分布：

```text
PhysicalStatePosteriorV1
  representation = rbpf_mixture | gaussian_sum
  components[]
    component_id
    normalized_weight
    regime_state
    forcing_scenario_id
    continuous_state_mean
      latent_temperature
      specific_humidity
      net_temperature_tendency
      surface_wetness_memory
      cold_pool_memory
    continuous_state_covariance
    bounded_parameter_draw_id
  effective_sample_size
  normalization_error
  uncertainty_breakdown
  physical_decomposition_diagnostics?   # Ψ条件分布摘要，non_recursive=true
```

连续状态字段必须精确对应`(T̃,q,G_net,W,C_cp)`，离散字段必须精确对应`R`；禁止增加`H/C_eff/A/E/D/Φ`等第二组递推坐标。所有分量权重之和必须在冻结容差内等于1；协方差、参数边界、状态单位和缺失语义由artifact schema固定。只保存均值而丢弃分量、regime或协方差，不构成合法checkpoint。`physical_decomposition_diagnostics`只可从当前分量、driver和artifact重新计算，不参与下一checkpoint递推。

canonical单位固定为：`T̃=°C`、`q=kg kg⁻¹`、`G_net=°C h⁻¹`、`W∈[0,1]`、`C_cp=°C h⁻¹`等效非负冷却记忆、`R=closed enum`。内部数值实现可使用SI或变换空间，但序列化前必须经同版本codec还原为上述canonical语义。

#### 4.14.6 纯函数接口与时序

```text
initialize_target(input)
  -> InitialPhysicalStateCheckpoint

apply_weak_forecast_revision(previous_checkpoint, weak_revision_event)
  -> RevisedDriverCheckpointAtSameCanonicalSequence

predict_to_event(previous_checkpoint, event.report_valid_at)
  -> PreEventPredictiveState Z_t^-

build_observation_likelihood(Z_t^-, event, frozen_artifact)
  -> JointObservationLikelihood

condition_pre_event_state(Z_t^-, joint_likelihood)
  -> ConditionedState

advance_post_event_state(conditioned_state, event)
  -> PostEventState Z_t^+

project_future_paths(Z_t^+, frozen_driver_generation, reporting_operator)
  -> RawPhysicalTargetProjection P_raw(X)

apply_target_hard_condition(P_raw(X), active_contract_history, hard_mask)
  -> PhysicalTargetProjection P(X)

build_transaction_b_candidate(...)
  -> ProbabilityCommitCandidate
```

硬时序为：

```text
Z_t^- = Transition(Z_{t-1}^+, Δt)
likelihood_t = ObservationModel(o_t | Z_t^-)
conditioned_t = Normalize(Z_t^- × likelihood_t)
Z_t^+ = PostStateTransition(conditioned_t, o_t)
P_raw,t(X) = Project(Z_t^+, future_forcing, reporting_operator)
P_t(X) = Normalize(H_t(X) × P_raw,t(X))
```

当前事件的 `o_t` 只能在likelihood计算完成后进入 `Z_t^+`。不得先把当前温度写进轨迹/动量状态，再用包含自身的轨迹对同一 `o_t` 评分。`JointObservationLikelihood`与 `PostEventState`必须分离并分别持久化哈希，保证无自确认、可重放和可审计。

弱预测revision走独立纯函数边界：它保持canonical sequence与observed contract history不变，只替换active weak revision chain、driver generation和未来投影。若revision有效窗口早于latest checkpoint并会改变历史状态传播，必须从其生效时刻前的checkpoint按原canonical active chain重放；不得把revision当当前时刻观测直接乘入latest state。

#### 4.14.7 PhysicalStateCheckpoint合同

```text
PhysicalStateCheckpointV1
  checkpoint_id
  checkpoint_kind             # initial | incremental | weak_revision | replay
  target_identity
  canonical_sequence
  consumed_event_id?
  previous_checkpoint_id?
  previous_checkpoint_sha256?
  pre_event_state_sha256?
  observation_likelihood_sha256?
  post_event_state
  post_event_state_sha256
  physical_x_distribution
  latent_continuous_peak_distribution
  observed_contract_max
  reporting_operator_version + sha256
  prior/weak/driver/state_model artifact lineage
  source_availability_mask
  hard_mask_version
  market_projection_state
  market_projection_na_reason?
  input_freshness
  inference_started_at
  inference_completed_at
  checkpoint_sha256
```

`physical_x_distribution`是唯一供SSOT发布的物理目标概率；`post_event_state`只是下一事件的内部前驱，不建立独立public latest pointer。checkpoint必须能从P0和canonical事件序列确定性重放；replay checkpoint不得参与live latest计数或伪装新增事件。

弱预测revision形成的checkpoint使用`checkpoint_kind=weak_revision`，保持`canonical_sequence/consumed_event_id/observed_contract_max`不变，并额外绑定`weak_revision_event_id`与`active_weak_revision_chain_sha256`。同一canonical sequence允许因合法weak revision产生线性后继，但不得出现两个并行latest分叉。

#### 4.14.8 ProbabilityCommitCandidate与原子边界

推理、路径生成和序列化在数据库事务外完成。提交时只允许短事务CAS验证并写入Transaction B：

```text
ProbabilityCommitCandidateV1
  expected_previous_checkpoint_id + sha256
  expected_latest_fact_id + sha256
  expected_canonical_sequence
  trigger_kind                 # canonical_observation | weak_forecast_revision
  canonical_event_id?
  weak_revision_event_id?
  active_artifact_ids + sha256
  freshness_proof
  trigger_first_available_at   # canonical T1或weak revision first_available_at
  execution_deadline
  StrongObservationUpdate?     # 仅canonical_observation
  WeakForecastRevisionUpdate?  # 仅weak_forecast_revision
  PhysicalStateCheckpoint
  PosteriorSSOTFact
  RealizationState?            # 仅bridge closed时
  PosteriorCommitOutbox?       # 仅存在合法市场投影时
  PosteriorOutboxExecutionContract?
```

Transaction B必须整体提交或整体回滚：

```text
StrongObservationUpdate | WeakForecastRevisionUpdate
+ PhysicalStateCheckpoint
+ event_fact_history PosteriorSSOTFact
+ event_fact_latest pointer
+ RealizationState（如适用）
+ posterior_commit_outbox（如适用）
+ posterior_outbox_execution_contract（如适用）
```

`auto_trade_decisions`、position、risk reservation、intent和order不属于Transaction B；它们由后续Decision Core/Transaction C消费已提交posterior outbox后另行原子处理。bridge未闭合或市场投影不可用时，Transaction B仍提交physical-only `P(X)` 与明确NA，但不得创建交易outbox。由此同时保证概率SSOT不被市场阻断、交易不消费不存在的 `P(B)`。

#### 4.14.9 幂等键与禁止旁路

- 初始化幂等键：`target_identity + initialization_lineage_sha256`；
- 增量更新幂等键：`target_identity + canonical_event_id + previous_checkpoint_sha256 + model_artifact_sha256`；
- weak revision幂等键：`target_identity + weak_revision_event_id + previous_checkpoint_sha256 + active_weak_revision_chain_sha256 + model_artifact_sha256`；
- 同键同payload返回既有结果；同键异payload必须fail closed；
- latest pointer只由Transaction B推进，Shadow使用独立数据库和独立pointer；
- UI、Decision Core和交易层只读 `PosteriorSSOTFact`，不得读取内部粒子后自行聚合；
- 任一状态模型异常只阻断该次posterior mutation与交易，不能回滚或阻断已经完成的canonical AMSC事实入库。

## 5. 模块宪章

### 5.1 Prior

职责：

- 提供季节、气候、站点和四川盆地背景；
- 提供慢参数、初始物理状态、天气状态和未来强迫的气候分布；
- 由上述状态自然诱导日最高温基础分布，或在工程上明确选择一份共同桶先验；
- 不读取当天市场、订单、预测结果或未来观测。

禁止：

- 将当天预测重复计入先验；
- 将当天 AMSC 实况重复计入先验；
- 同时乘“状态诱导气候先验”和“独立最高温气候先验”；
- 用气候背景覆盖 D0 硬事实。

### 5.2 Unified Weak Forecast Model

ECMWF、ICON、GFS、JMA、TAF、WU、TWC全部是预测源，不是事实源。唯一弱预测主模型必须显式支持不同可用源组合，并把 source availability mask 作为模型输入。

固定处理顺序：

```text
逐源、分季节/天气型/时效偏差校正
→ 逐源条件残差分布
→ provider/source 相关性折扣
→ 独立天气情景聚类
→ 保留合理多峰
→ 联合信息量与置信度
```

硬规则：

- 偏差校正负责移动各源候选峰，不能强制生成单峰；
- 源内核宽只表达该源条件残差，不能承担源间分歧；
- 源间分歧通过多峰、联合熵和 information scale 表达；
- 峰是否合并由校正后距离、残差尺度、密度重叠和相关性自然决定；
- 缺源降低置信度与仓位能力，不阻断 AMSC 入库或物理 posterior；
- 同源派生或高度相关来源不得重复累计信息量。
- 逐源偏差校正必须区分“模式连续最高温/格点温度”和“AMSC计划槽报文最高温”的目标口径差异；
- NWP驱动变量、NWP温度轨迹和同源直接最高温不能在不建联合误差结构时重复累计。

### 5.3 Strong Observation Composer

AMSC canonical observation 是 D0 实况主线。输入至少包括：

- 当前温度与已观测日最高温；
- 露点与湿度代理；
- 云量、云底与云层演变；
- 降水和天气现象；
- 风向、风速与突变；
- 由同一事件历史确定性派生的温度轨迹、温度动量和变化率。

强观测只更新固定`T̃/q/G_net/W/C_cp/R`状态和driver情景权重，再生成潜在温度路径，通过AMSC报告算子形成最高温后验。感热分配、有效热容量、平流/卷入等只作为随情景重加权的`Ψ`诊断，不接受独立递推。温度、动量、轨迹、云雨等由同一报文派生的证据必须在一个composer内联合消费一次，禁止拆成多个相关LR重复相乘。

### 5.4 D0 软仲裁

D0 实况不回写当天弱源的基础 bias 或历史能力权重。它更新的是各天气情景产生当前观测的条件概率：

```text
弱预测保留低温峰/高温峰
→ AMSC轨迹与物理证据到达
→ 计算各情景对当前观测的解释能力
→ 情景后验权重变化
→ 最终最高温分布逐步收敛
```

这属于强观测 Bayes 更新，不能在弱层和强层各执行一次。日终 final X 才用于校正未来日期的源能力、偏差和相关性参数。

### 5.5 Hard Mask

Hard Mask只接受确定、可重放事实：

- 已观测最高温下界；
- 冻结温度桶边界；
- 报文去重、乱序与 correction 语义；
- 明确无效输入的隔离结果。

概率判断不得硬裁剪。

### 5.6 Posterior SSOT

唯一公开、可交易的 `PosteriorSSOTFact` 至少包含：

- `posterior_checkpoint_id`
- `model_artifact_id`
- `as_of`
- `observation_event_id`
- `source_generation_id`
- `source_availability_mask`
- `physical_x_distribution`
- `latent_continuous_peak_distribution`
- `reporting_operator_version`
- `market_bucket_distribution` 或明确 `market_projection_na_reason`
- `observed_max`
- `net_heating_rate_distribution`
- `surface_energy_partition_state`
- `effective_heat_capacity_state`
- `advection_entrainment_state`
- `remaining_reported_rise_distribution`
- `future_reported_peak_margin_distribution`
- `latent_temperature_change_distribution`
- `next_hour_temperature_change_distribution`
- `no_future_exceedance_probability`
- `strict_peak_already_occurred_probability`
- `future_tie_probability`
- `new_high_probability`
- `thermal_rebound_probability`
- `new_high_after_rebound_probability`
- `peak_time_distribution`
- `termination_probability` + `termination_definition_version`
- `physical_x_reportability_state` + `physical_x_na_reason`
- `weather_regime_state_distribution`
- `coherent_scenario_family_distribution` + `top_scenario_summaries`
- `uncertainty_driver_ranking`
- `next_observation_discrimination` 或明确 `next_observation_discrimination_na_reason`
- `hard_mask_version`
- `input_freshness`
- `calibration_version`
- 完整 input lineage 与 artifact SHA

UI和交易层只能读取该 `PosteriorSSOTFact`；`PhysicalStateCheckpoint`只服务内部递推、审计与重放，不得成为第二个public latest或被下游重新聚合。

## 6. 第一性原理物理模块

### 6.1 确定性外部边界

- 太阳高度角、方位角、日出日落和剩余日照；
- 冻结AMSC primary METAR计划槽；
- 站点经纬度、海拔和合同日边界；
- 目标单位、报文编码和质控规则。

这些量可确定性计算，但不能直接决定实际地表净辐射或最高温。

### 6.2 辐射与地表能量账本

- `SW↓/SW↑/LW↓/LW↑`形成净辐射 `Rn`；
- 云量、云层、降水和水汽影响短波与长波；
- `Rn`必须守恒分配到感热 `H`、潜热 `LE`、地表/土壤热通量 `G` 和储热 `ΔS`；
- 前期降雨、土壤湿度和地表湿润状态控制潜热分流；
- 没有机场实测辐射或通量时，使用NWP弱预测和气候先验形成`Ψ`条件分布；温度轨迹只更新`G_net`并重加权driver情景，显式保留机制不确定性。

NWP辐射、云雨和通量始终属于弱预测，不能升级为AMSC事实。

### 6.3 边界层热响应

- 感热 `H`是局地升温的直接能量通道；
- 有效混合深度 `h_eff`决定同样感热作用下的温度响应幅度；
- 地表—空气温差、稳定度、湍流和风速决定耦合；
- 四川盆地逆温、静稳和通风改变有效热容量与混合效率；
- `Efficiency`只能作为这些物理量的诊断摘要，不能成为无边界误差吸收器。

### 6.4 动力输送

- 冷暖平流；
- 下沉增温；
- 边界层顶部卷入与暖空气下混；
- 风向切变、冷池和局地通风；
- 未来一小时动力状态变化。

平流不能只由站点单点风场推断。至少需要冻结的周边温度/湿度/风场网格，通过 `-V·∇T` 等空间派生量估计，并把网格分辨率和插值误差纳入artifact。

### 6.5 天气状态转换

采用状态转换或 hazard：

```text
晴热加热 ↔ 云遮减热 ↔ 降水/冷池 ↔ 平流转换 ↔ 再放晴恢复
```

证据包括太阳高度变化、云层增厚、降水、冷池、风向突变、冷空气进入、暖平流结束、对流触发和实况轨迹持续偏离预期。天气状态改变未来强迫和温度倾向；“已经见顶”由未来报文路径积分产生，不是该状态的同义词。

### 6.6 观测与报告过程

- AMSC温度校正潜在`T̃_t`和`G_net`；
- 露点约束`q`并软约束`W/C_cp/R`；
- 云和天气现象更新`W/C_cp/R`及相关driver情景权重；
- 风向、风速和气压变化更新`R`、`G_net`及平流/通风driver情景权重；
- 温度动量约束 `dT̃/dt`，但不作为第二条独立事实；
- 冻结报告算子将潜在路径投影到计划槽报文温度和最终 `X`。

### 6.7 不确定性必须分解

模型至少分别表达：

- `forecast_source_uncertainty`：不同弱源及未来强迫分歧；
- `energy_partition_uncertainty`：感热、潜热、土壤和储热分配；
- `effective_heat_capacity_uncertainty`：混合深度和耦合；
- `dynamics_uncertainty`：平流、卷入、下沉与冷池；
- `regime_transition_uncertainty`：云雨和天气状态转换；
- `process_noise`：模型未解释的温度演化；
- `observation_reporting_uncertainty`：采样、编码、质控和计划槽缺失。

不得把上述不确定性全部塞进一个超宽温度核或一个自由 `Efficiency` 参数。

### 6.8 P1物理变量角色枚举

所有输入只能属于以下一种角色；角色是schema字段并进入artifact身份，不能由调用方临时解释：

| 角色 | 定义 | 是否事实 | 是否可形成hard mask | 示例 |
|---|---|---:|---:|---|
| `deterministic_boundary` | 由站点、时间和冻结合同确定的边界 | 否 | 仅合同边界可 | 太阳位置、计划槽、站点海拔 |
| `prior_context` | 日前慢背景和气候分布 | 否 | 否 | 季节、盆地气候、地表先验 |
| `weak_driver_forecast` | 对未来物理强迫/状态的预测 | 否 | 否 | NWP辐射、PBL、平流场、云雨 |
| `weak_path_forecast` | 对未来温度路径的预测 | 否 | 否 | NWP 2m温度轨迹 |
| `weak_target_expert` | 对最高温或天气情景的预测 | 否 | 否 | NWP日高温、TAF、WU、TWC |
| `canonical_observation` | AMSC canonical报文中的实时物理观测 | 是 | 仅合同内温度槽可 | 温度、露点、云、雨、风、气压 |
| `derived_observation` | 从同一canonical历史确定性派生 | 派生事实摘要 | 否 | 温度动量、轨迹、压变、风向转变 |
| `confirmation_or_substitute` | 对同一物理报文的确认或canonical替代候选 | confirmation不是第二份事实；被选为substitute后成为该唯一canonical事件的来源 | 仅最终canonical合同槽温度可 | AWC对AMSC的确认/替代 |
| `evaluation_label` | 日终或事件后仅用于训练评价 | 终局事实 | 不进入当日推断 | closed AMSC physical X、下一槽温度 |
| `settlement_layer_observation` | exact结算源在合同授权下的日内/终局观测 | 仅在S层为事实 | 只可约束S，不可改变物理X hard mask | 结算源日内值、active final revision |
| `market_projection_only` | 物理X到S/B的结算映射 | 市场合同/终局 | 不改变物理hard mask | bridge、市场桶边界 |
| `market_or_execution_only` | 行情、仓位与执行事实 | 非天气证据 | 否 | 价格、订单、持仓、P&L |

同一值不得同时以两种证据角色进入composer。例如相对湿度若由同一报文温度和露点计算，只能作为 `derived_observation` 帮助参数化联合likelihood，不能再作为第三个独立观测LR。

`TAF AMD`、新NWP run和其他revision首先是revision-control事件，其有效payload仍保持原`weak_driver_forecast/weak_path_forecast/weak_target_expert`证据角色；“revision”本身不增加信息量。

### 6.9 Canonical物理变量字典

#### 6.9.1 单位和符号宪章

- 温度与温度增量：canonical存储使用摄氏度 `°C`；物理差分中 `1°C` 与 `1K` 等价，绝对热力学公式需要开尔文时必须显式转换；
- 温度倾向：内部使用 `K s⁻¹`，审计/UI可派生 `°C h⁻¹`；
- 辐射与通量：`W m⁻²`；`SW↓/LW↓`向下为正，`SW↑/LW↑`向上为正；
- 净辐射：`Rn=SW↓−SW↑+LW↓−LW↑`，地表净获得能量为正；
- `H/LE`以地表向大气输出为正，`G`以地表向土壤输出为正，`ΔS`以地表系统储能增加为正，因此保持 `Rn=H+LE+G+ΔS`；
- 风：分量 `m s⁻¹`，方向为真北顺时针角度 `[0,360)`；静风和变向风使用明确类别，不能伪造0°；
- 气压：内部 `Pa`，展示可派生 `hPa`；
- 高度：`m AGL`与 `m MSL`必须分列，禁止混用；
- 云量：比例 `[0,1]`，报文类别到比例的映射属于观测artifact，不伪装精确连续真值；
- 降水：累计 `mm`必须绑定区间，率使用 `mm h⁻¹`；瞬时、区间平均和累计不得同列混用；
- 土壤湿度：优先体积含水率 `m³ m⁻³`；若provider只给层积分或指数，保留原语义并经adapter显式转换或写NA。

单位、方向、累计重置、插值和缺测规则任一变化都生成新变量合同版本和artifact SHA。

#### 6.9.2 确定性边界变量

| Canonical变量 | 单位 | 时间/空间语义 | 作用 | 缺失处理 |
|---|---|---|---|---|
| `solar_zenith_angle` | degree | 事件时刻/站点 | 短波上界、日照阶段 | 站点/时间无效则该target fail closed |
| `solar_azimuth_angle` | degree | 事件时刻/站点 | 地形/云影诊断 | 同上 |
| `sunrise/sunset` | UTC+local | station-day | 剩余日照和路径边界 | 同上 |
| `clear_sky_shortwave_envelope` | W m⁻² | 模型时间步/站点 | 仅为理论短波边界，不是实际辐射观测 | 允许由冻结算法计算；版本缺失则driver NA |
| `station_lat/lon/elevation` | degree/m MSL | 固定站点 | 几何、气压和地形上下文 | 身份不完整则target无效 |
| `contract_report_slots` | timestamp[] | station-day | 报告算子和物理X定义 | 不完整则不得生成可交易P(X) |

太阳几何与由它计算的晴空短波包络属于同一确定性家族，不能当成两份独立证据提高置信度。

#### 6.9.3 AMSC canonical观测变量

| Canonical变量 | 单位/编码 | 观测语义 | 更新的状态 | hard能力 |
|---|---|---|---|---|
| `reported_temperature` | °C+编码精度 | 报文valid time的站点温度 | `T̃`、净温度倾向、报告误差 | 仅合同计划槽形成X下界 |
| `reported_dewpoint` | °C+编码精度 | 同报文水汽状态 | 湿润/蒸发潜力、气团状态 | 无 |
| `wind_direction/speed/gust` | degree/m s⁻¹ | 同报文近地风 | 平流regime、通风、混合 | 无 |
| `cloud_layers` | amount/base m | 同报文云层类别与云底 | 辐射regime、对流/降水状态 | 无 |
| `weather_phenomena` | categorical | 当前天气现象 | 降水、冷池、天气状态转换 | 无 |
| `visibility` | m或区间 | 当前能见度 | 降水/雾/气团状态 | 无 |
| `station_or_qnh_pressure` | Pa+pressure_type | 报文字段原语义 | 压变、气团/天气状态 | 无 |
| `report_type/revision/supersedes/valid_time` | categorical/time | METAR或SPECI类型；correction由revision与supersedes关系表达 | 报告算子、时序 | 决定是否合同槽 |

AMSC没有直接观测净辐射、感热、潜热、PBL高度、土壤湿度或空间温度梯度。上述量只能由弱预测、层级先验与固定状态posterior条件化形成`Ψ`诊断分布，禁止把诊断结果标成AMSC事实或写入递推状态。

#### 6.9.4 官方NWP弱驱动变量

以下是目标canonical变量集合，不表示每个provider当前都已交付全部字段。ECMWF、ICON、GFS、JMA各自通过独立adapter映射，逐字段写availability、原始变量名、level、step type、valid interval、单位和raw object SHA。

| Canonical变量组 | 变量 | 单位 | 时间语义 | 空间语义 | 物理用途 |
|---|---|---|---|---|---|
| 近地温湿 | `t2m`, `d2m`/specific humidity | °C/°C或kg kg⁻¹ | instantaneous | 站点插值+周边窗口 | 路径、气团、蒸发潜力 |
| 近地风压 | `u10`, `v10`, surface/MSL pressure | m s⁻¹/Pa | instantaneous | 站点插值+周边窗口 | 通风、风转、天气状态 |
| 云 | total/low/mid/high cloud fraction | 0–1 | instant或interval average | 站点插值+周边窗口 | 短波衰减、长波、regime |
| 降水 | total/convective precipitation | mm或mm h⁻¹ | accumulated或interval average | 站点插值+周边窗口 | 潜热、湿润、冷池、终止 |
| 辐射 | `SW↓`, `SW↑`或albedo, `LW↓`, `LW↑`或surface net radiation | W m⁻²/J m⁻² | instant/average/accumulated需显式解码 | 站点插值+周边窗口 | `Rn`与未来Energy |
| 地表通量 | sensible/latent heat flux, ground heat flux | W m⁻²/J m⁻² | average/accumulated需显式解码 | 站点插值 | 能量分配与升温效率 |
| 土壤/地表 | skin temperature, soil temperature, soil moisture各层 | °C/m³ m⁻³ | instantaneous | 站点插值+地表类型 | 储热、湿润记忆、潜热分流 |
| 边界层 | PBL height, friction velocity或可用湍流代理 | m AGL/m s⁻¹ | instantaneous | 站点插值 | 有效热容量和混合 |
| 压力层 | temperature, humidity, `u/v`, geopotential at selected levels | °C/kg kg⁻¹/m s⁻¹/m | instantaneous | 冻结周边3D窗口 | 平流、稳定度、卷入、下沉 |
| 垂直运动 | omega/vertical velocity | Pa s⁻¹或m s⁻¹ | instantaneous | 冻结周边窗口 | 下沉增温、对流抬升 |
| 对流状态 | CAPE/CIN等 | J kg⁻¹ | instantaneous/interval | 站点插值+周边窗口 | regime hazard，不直接决定X |

若provider只给surface net shortwave或net radiation，不得凭字段名拆成不存在的四个分量；允许直接进入带来源语义的 `Rn`观测方程，但必须与其他辐射分量互斥，防止重复组成同一能量。

#### 6.9.5 周边空间派生变量

| 派生量 | 最小输入 | Canonical表达 | 约束 |
|---|---|---|---|
| 水平温度平流 | 同层 `u/v` + 周边温度网格 | `-V·∇T`，K s⁻¹ | 网格窗口、投影、梯度算法冻结 |
| 水汽平流 | 同层 `u/v` + 湿度网格 | `-V·∇q` | 不以单点风向猜测 |
| 低层稳定度 | 地面/2m与925/850hPa温度/位温 | K或K m⁻¹ | 地形下压力层无效时明确NA |
| 风向切变 | 10m与压力层/高度层风 | m s⁻¹或degree | 圆周角处理，不能直接相减359°与1° |
| 温度/湿度梯度 | 冻结周边窗口 | K km⁻¹/(kg kg⁻¹) km⁻¹ | 插值误差进入不确定性 |
| 冷池代理 | 降水、温湿差、阵风、周边温度 | 有界latent feature | 只能作为联合状态证据，不冒充实测冷池强度 |

站点nearest grid值不能同时作为“站点温度路径”和周边窗口中心被独立计权；空间派生量必须与其母网格在同源联合误差模型中消费。

#### 6.9.6 TAF、WU、TWC变量边界

| 来源 | 允许的weak变量 | 禁止解释 |
|---|---|---|
| TAF | 有效窗口内风、能见度、天气、云、变化组；存在时可保留TX/TN原文语义 | 不是实况；不是辐射、热通量或PBL观测；变化组不能伪造精确逐小时路径 |
| WU | 获批独立feed提供的温度路径/最高温、云雨风等预测字段 | 不得作为AMSC事实、官方NWP或市场最终S之外的结算事实 |
| TWC | 获批独立feed提供的温度路径/最高温、云雨风等预测字段 | 不得作为AMSC事实、官方NWP或最终标签 |

这些来源的字段即便与NWP同名，仍是各自source family的弱预测；必须进入联合误差/相关性模型，不得按变量名合并为事实。

### 6.10 时间、空间与provider适配合同

每个NWP字段adapter输出：

```text
provider_id
model_id
run_time
valid_time_start
valid_time_end
step_type                 # instantaneous | average | accumulation | max/min
accumulation_reset_id?
native_variable_name
native_level/type
native_unit
canonical_variable_name
canonical_unit
conversion_version
grid_id/projection
station_interpolation_version
spatial_window_contract
raw_object_id + sha256
decoder_id + version
quality_state
field_availability
field_na_reason?
```

硬规则：

1. 累计量必须先按同run连续step差分，再除以真实区间长度；跨run、跨reset或缺前一步时不得猜值；
2. 区间平均量必须保留valid interval，不能冒充结束时刻瞬时值；
3. 日最大/区间最大温度不能进入逐小时温度轨迹；
4. 压力层位于站点地形以下或被provider mask时写NA，不能向下外推；
5. nearest、bilinear、elevation-adjusted或周边窗口必须由artifact冻结，运行时不能择优取最接近实况的方法；
6. 同一provider不同分辨率/ensemble/control属于同一source family，除非artifact显式建模其联合误差，不得当成独立来源；
7. provider run更新只形成新immutable generation，绝不覆盖已经被某canonical事件绑定的as-of generation；
8. 热路径只读预热generation，不执行下载、GRIB解码、重网格或空间梯度大计算。

### 6.11 数据可得性与缺失降级矩阵

Open-Meteo 的字段覆盖、现场非空探测、状态角色映射与 research/production 边界，见 [OPEN_METEO_PHYSICAL_DRIVER_COVERAGE_AUDIT.md](OPEN_METEO_PHYSICAL_DRIVER_COVERAGE_AUDIT.md)。该审计只证明研究 transport 的字段可得性，不改变本节的官方直连合同。

下表描述本方案签署时的合同状态，不替代运行时generation事实。实际事件是否可用只能由对应raw、adapter和availability mask证明。

| 数据族 | 合同渠道 | 当前合同状态 | 可进入的角色 | 缺失时的正确行为 |
|---|---|---|---|---|
| 太阳几何/站点合同 | 本地冻结计算与AMSC计划槽合同 | 可确定性计算 | deterministic boundary | 身份/算法缺失时该target不可计算；不使用网络fallback |
| AMSC METAR/SPECI | AMSC官方raw+canonical链 | primary事实源 | canonical observation/hard候选 | 报文缺失则无该次更新；事实链继续等下一event，不伪造 |
| AWC METAR/SPECI | AWC官方链 | confirmation/substitute | `confirmation_or_substitute` | confirmation不制造第二likelihood；仅在canonical政策选为substitute时成为该唯一事件来源 |
| ECMWF | ECMWF Open Data/授权交付 | 官方collector已验证；strict arrival与扩展物理字段需逐项证明 | weak forecast | 字段/源写missing mask；不回退Open-Meteo/Windy/旧run |
| ICON | DWD Open Data | 官方collector已验证；扩展物理字段需逐项接入 | weak forecast | 同上 |
| GFS | NOAA/NOMADS | 官方collector已验证；扩展物理字段需逐项接入 | weak forecast | 同上 |
| JMA | JMA官方/批准授权交付 | 未配置授权交付时明确缺失 | weak forecast | 保持JMA missing；不猜URL、不用第三方替代 |
| TAF | 独立TAF原文与发布身份 | 独立弱预测源 | weak target/regime/path | 缺失只改变exact source set |
| WU/TWC预测 | 各自获批独立feed | 逐feed合同验证 | weak target/regime/path | 缺失只改变exact source set，不相互替代 |
| 历史气候/盆地 | 获批历史与气候artifact | prior-only | prior context | 回退到已批准更粗层先验或target fail closed；不进入D0似然 |
| final physical X | closed AMSC station-day | 日终评价标签 | evaluation label | 缺失只阻止训练/评价，不阻断当天live |
| X→S→B bridge | final X+S+冻结市场合同 | 独立慢通道 | market projection only | `P(X)`继续，`P(S/B)`和交易投影NA |

“官方collector已验证”只证明现有collector路径，不证明上表全部物理变量已经接入、时间语义正确或具备strict-as-of训练资格。每个新增变量必须单独完成raw→decode→unit/time/space adapter→generation→回放验收。

### 6.12 缺失降级与fail-closed边界

```text
字段缺失
→ 写field availability/NA reason
→ 选择artifact已批准的missingness pattern
→ 对缺失状态边缘化并增加对应不确定性
→ 仍生成P(X)
```

只有以下情况阻断该次posterior mutation：

- 当前exact source set或field-missing pattern没有获批surface；
- raw、时间、单位、空间、decoder或generation血缘无效；
- 唯一状态模型artifact不可用或schema不兼容；
- 必需目标合同、报告算子或前驱checkpoint无效；
- 数值归一、状态边界或CAS失败。

这些阻断均不得回滚AMSC raw/canonical事实。禁止的降级包括：借旧run补缺、用其他provider同名字段替换、恢复Open-Meteo、以气候均值伪装实时辐射、把缺失数值填0、或静默缩窄posterior。

目标artifact获得production资格前，必须覆盖source universe内所有声明为可运行的source-set和字段missingness pattern，包括获批的empty-weak中性信息surface。因此正式运行后，普通预测源或非关键物理字段缺失本身只能降低信息量，不能成为posterior停摆原因；“未批准pattern”只能说明artifact覆盖不完整或输入schema异常，应在晋级前暴露并修复。

### 6.13 重复计数边界

| 可能重复的证据 | 唯一正确消费方式 |
|---|---|
| 同源NWP的2m温度、最高温、辐射、云雨和热通量 | 作为一个source vector进入联合误差模型，不拆成独立source LR |
| NWP站点格点与由同网格派生的平流/梯度 | 绑定同一driver generation和母网格协方差 |
| AMSC温度、轨迹、动量和加速度 | 一条canonical历史的联合观测模型；派生项不再乘独立LR |
| AMSC云雨与由其解释的温度减速 | 联合条件模型，不能把“云雨”与“已减速”假定独立 |
| AMSC与AWC相同物理报文 | AMSC selected；AWC confirmation/substitute，不形成双观测 |
| 温度+露点与派生相对湿度 | 相对湿度为派生摘要，不形成第三观测 |
| 太阳高度与晴空短波包络 | 同一确定性家族，只定义边界，不增加随机信息量 |
| TAF与其可能共享的NWP背景 | 保留TAF独立source身份，但在source covariance中折扣相关性 |
| prior中的季节天气频率与弱模型中的同一base rate | 只保留一个prior入口，弱专家使用density ratio/生成模型 |
| `Rn`与组成它的四个辐射分量 | 同一事件只能消费组成分量或已声明的net值，不能两者相加 |
| `LE`潜热分流与 `D`蒸发/冷池降温 | `LE`只表达地表能量分配；`D`只表达未被该分配解释的大气蒸发冷却/降水冷池，并在联合协方差中去重 |
| `D`冷却诊断与`C_cp`冷池记忆 | `D`必须条件化`C_cp`后形成`G_driver`；`G_net`转移不得再把`C_cp`作为第二个加性冷却项 |

任何新变量接入前必须先填写“母数据、派生关系、与现有证据的条件相关性、唯一消费位置”；无法回答时不得进入联合composer。

### 6.14 P2状态可观测性与可辨识性

#### 6.14.1 可观测性分级

| 唯一状态坐标 | AMSC直接约束 | 弱driver约束 | 可辨识结论 |
|---|---|---|---|
| `T̃_t` | 高：报文温度经编码/报告误差直接约束 | NWP温度路径 | 独立连续状态 |
| `q_t` | 中高：露点与温度联合约束 | NWP温湿路径、降水和PBL | 独立连续状态；不得再由其派生RH形成第三likelihood |
| `G_net,t` | 中高：相邻canonical温度、有效时间与联合天气innovation约束 | `G_driver=𝔼[Q_H+A+E−D|Z,U,Θ,C]`及NWP温度倾向 | 唯一递推净倾向；不拆成多份自由机制状态 |
| `W_t` | 中低：前期降雨、露点、云雨历史软约束 | 土壤湿度、潜热、辐射、风和降水 | 有界记忆状态，不声称实测土壤湿度 |
| `C_cp,t` | 中低：降水、阵风、压变、温湿突变和恢复轨迹软约束 | 降水、冷池代理与周边冷异常 | 非负瞬变记忆，不声称精确冷池强度 |
| `R_t` | 中：云层、天气现象、能见度、风转和innovation联合约束 | 未来云雨、环流与PBL情景 | 离散概率状态，不硬选dominant regime |
| `Ψ_t`物理分解 | AMSC不直接分量识别 | NWP辐射、通量、PBL、空间/垂直场 | 非递推条件诊断，只随`Z/U/Θ`重加权 |

核心结论：**AMSC能识别温度、水汽、已实现净倾向及天气/湿润/冷池状态，却通常不能唯一解释净倾向中究竟多少来自`H/C_eff/A/E/D`。** 唯一状态只保存可由当前证据持续递推的合量；机制不确定性留在`P(Ψ|Z,U,Θ,C)`，不得用多个自由状态制造解释性幻觉。

#### 6.14.2 固定状态维度与缺源语义

所有初始化、增量、weak revision与replay checkpoint均固定：

```text
state_schema = identifiable_physical_state_v1
continuous   = (T̃, q, G_net, W, C_cp)
discrete     = (R)
```

旧`full_physical/reduced_physical/net_tendency`动态维度方案退役。缺少辐射、PBL或空间场时，不删除坐标、不切换模型、不重置posterior；只扩大相关driver/过程不确定性，并使`physical_decomposition_diagnostics.state=partial|unavailable`且提供`na_reason`。某个固定坐标长期不可辨识或对一步路径与`P(X)`无增量价值，候选artifact不得晋级；禁止在live中临时裁掉该坐标掩盖设计失败。

checkpoint必须记录固定`state_schema`、各坐标信息量、有效信息秩、posterior相关矩阵和`Ψ`诊断可用性；不得再输出`state_detail_level`或“升维/降维”状态机。

### 6.15 状态约束、参数化与P0先验

#### 6.15.1 不可违反的结构约束

- `q≥0`并满足冻结温湿物理支持；温度、露点和饱和约束通过联合分布处理，不用后处理硬改观测；
- `W∈[0,1]`；内部以artifact冻结的epsilon-interior映射进入logit空间，避免0/1无穷值；
- `C_cp≥0`，以softplus或截断分布参数化；
- `G_net`允许正、负与再次转正，使用有界重尾支持，不能被硬裁为“只剩升温”；
- regime概率和情景权重非负且归一；
- 协方差矩阵必须半正定；
- `Ψ`中的`C_eff>0`，`H/LE/G/ΔS`允许随昼夜和稳定度改变符号，但必须满足冻结诊断合同；
- 能量闭合误差为零中心、有限尺度，不得承担长期系统偏差；
- 气候范围、NWP范围和历史极值只形成软prior，不得替代已观测最高温hard condition，也不得硬裁掉尚未观测但物理上可能的极端桶。

数值上限、下限和变换尺度不在本文拍脑袋写死。它们由station/season训练集、物理可行域和OOS审计形成冻结artifact；任何边界变化都产生新artifact SHA。

#### 6.15.2 P0层级先验

```math
P_0(Z_0,U_{0:H},\Theta\mid C,F_A)
\propto
P_{climate}(Z_0,U,\Theta\mid station,season,basin)
L_W(F_A\mid Z_0,U,\Theta,C)
```

P0必须：

1. 从站点—季节—盆地层级prior初始化慢参数和regime概率；
2. 用冻结弱预测情景更新未来driver、温度路径和状态，而不是把弱预测当事实钉死；
3. 对无可用driver的状态保留宽而有界的层级prior；
4. 只消费一次气候base rate；
5. 不读取D0报文、市场、订单、final X或评价标签；
6. 同时产出状态分布和由其投影的P0 `P(X)`，禁止两个独立初始化composer。

#### 6.15.3 四川盆地背景的静态先验与当日强迫分界

盆地—大尺度背景设计必须拆清“长期base rate”和“当日弱证据”，避免把同一环流信息既写入prior又作为NWP再次更新：

```text
C_static:
  站点/海拔/地形位置、土地利用、日序与季节窗
  四川盆地逆温、低云、静稳/通风、晴热和对流等历史regime频率

C_antecedent（严格as-of）:
  前期降水、地表湿润/土壤湿度代理、连续阴晴与冷池恢复记忆

F_A / U（当日弱预测）:
  850/700 hPa温湿场、槽脊/副高/冷空气背景
  当日辐射、云雨、风、PBL、空间梯度和对流时窗
```

初始化必须是天气型混合而非单峰气候核：

```math
P_{climate}(Z_0,U,\Theta\mid C)
=\sum_r \pi_r(C_{static},C_{antecedent})
P(Z_0,U,\Theta\mid R_0=r,C)
```

这里的`π_r`只表示历史条件天气型频率。当天850/700 hPa场、TAF或NWP识别出的副高、槽脊、冷空气和对流背景只能通过`L_W(F_A|...)`更新这些混合分量，不能复制进`π_r`。前期降水若已作为`C_antecedent`初始化`W/C_cp`，同一累计量不得又作为独立弱LR；新发生的当日降水则按canonical观测或as-of driver正常更新。

每个初始化混合分量至少保存`regime_family、weight、Z_0 distribution、U path family、lineage`。高温/低温合理mode必须保留到弱预测或强观测提供足够证据后再收缩，禁止在P0阶段先平均成一个不存在的中间天气世界。

### 6.16 切换状态转移方程

#### 6.16.1 不规则时间步

对相邻canonical事件或未来积分节点：

```math
\Delta t_k=t_{k+1}-t_k>0
```

所有漂移和过程噪声都按真实 `Δt` 缩放。`Δt≤0`必须按duplicate/out-of-order/correction合同处理，不能以最小正时间偷偷推进状态。长间隔按artifact冻结的最大物理子步切分，子步算法和顺序进入semantic hash。

#### 6.16.2 Regime转移

dominant regime候选至少包括：

```text
clear_heating
cloud_attenuated
precipitation_cold_pool
warm_advection_or_mixing
cold_advection
post_rain_recovery
evening_stable_decay
```

连续物理项仍可同时存在，regime只表示当前主导结构。转移为：

```math
P(R_{k+1}=r'\mid R_k=r,Z_k^+,U_{k:k+1},O_{<k+1},C)
=Softmax\left(\alpha_{r,r'}+\beta_{r,r'}^Tf_k\right)
```

`f_k`只使用前一post-state、截至当时已见历史和as-of未来driver。当前新事件 `O_{k+1}`不进入pre-event regime预测，只通过随后联合观测likelihood重分配regime概率。

“见顶”“Termination”和“重新升温”不是regime标签。它们始终由未来计划槽路径积分产生。

#### 6.16.3 连续状态转移

driver条件合量：

```math
G_{driver,k}=\mathbb{E}[Q_{H,k}+A_k+E_k-D_k\mid Z_k,U_k,\Theta,C]
```

```math
\widetilde T_{k+1}^{-}
=\widetilde T_k^{+}
+\int_{t_k}^{t_{k+1}}G_{net}(s)ds
+\epsilon_{T,k}
```

```math
G_{net,k+1}^{-}
=\rho_G(R_k,\Delta t)G_{net,k}^{+}
+[1-\rho_G]m_G(G_{driver,k},R_k,C)
+\epsilon_{G,k}
```

```math
q_{k+1}^{-}
=\rho_q(R_k,\Delta t)q_k^{+}
+[1-\rho_q]m_q(U_k,W_k,C_{cp,k},R_k,C)
+\epsilon_{q,k}
```

湿润记忆：

```math
logit(W_{k+1}^{-})
=\rho_W(R_k,\Delta t)\,logit(W_k^{+})
+\Delta t\,[g_{rain}(U_k,R_k)-g_{dry}(Rn,wind,VPD,U_k)]
+\epsilon_{W,k}
```

冷池/恢复记忆：

```math
c_{cp,k}=Softplus^{-1}(C_{cp,k})
```

```math
c_{cp,k+1}^{-}
=\rho_{cp}(R_k,\Delta t)c_{cp,k}^{+}
+m_{cp}(precip,evaporation,gust,cold\_anomaly,W_k,U_k,R_k)
+\epsilon_{cp,k}
```

```math
C_{cp,k+1}^{-}=Softplus(c_{cp,k+1}^{-})
```

其中`VPD`由`T̃/q`及driver条件得到。上述`m_*`是有界、版本化、可消融的转移函数，可以是分层回归、样条或受约束小模型；不得在运行时任意换成黑箱函数。`O_{k+1}`不得进入这些pre-event转移函数，只能在随后联合观测likelihood中更新`Z_{k+1}^{-}`。

#### 6.16.4 物理分解诊断（非状态）

```math
R_{n,k}=H_k+LE_k+G_k+\Delta S_k+\epsilon_{closure,k}
```

`Ψ`必须联合建模，而不是对H、LE、G、ΔS分别加独立噪声后再强行归一。只有在该interval的物理合同明确四个canonical通量项均非负时，才可使用simplex/log-ratio参数化能量份额；任何通量可能换向时都必须使用带符号的受约束联合分布。参数化选择由太阳几何、regime和通量语义共同决定并属于同一artifact。

NWP已经提供net radiation时，与由同源分量重复推导的net radiation互斥消费；NWP已经提供H/LE等通量时，它们作为带偏差weak driver约束`Ψ`，不成为精确能量真值。`Ψ`变化只能通过driver generation、`Z`posterior重加权或新artifact发生，不能作为独立状态转移写入checkpoint。

### 6.17 过程噪声与相关结构

过程噪声不是独立对角“加宽旋钮”。冻结artifact至少包含：

```text
Q_process(R, season, local_time, Δt, availability_pattern)
```

要求：

- `T̃/q/G_net/W/C_cp`使用冻结块协方差；`G_net`与`W/C_cp`、`T̃`与`q`必须允许条件相关；
- 噪声随 `Δt`和regime缩放，不能每条报文固定加同样方差；
- 对流、冷池和锋面转换可使用预注册重尾创新或regime mixture，但不能用一个全局超宽Gaussian覆盖所有天气；
- process noise、weak-driver error、`Ψ`诊断不确定性和AMSC observation error必须分开估计；
- 连续多次innovation同方向偏离时，优先改变regime/driver情景权重或形成候选artifact诊断，不能在线扩大噪声掩盖系统偏差；
- 每个checkpoint输出各不确定性贡献及posterior相关性审计。

### 6.18 固定状态可辨识性守卫与诊断发布规则

离线训练和每次Shadow回放必须计算：

- 局部/后验有效信息秩；
- 状态posterior相关矩阵和条件数；
- `T̃/q/G_net/W/C_cp/R`逐坐标信息增益；
- prior到posterior方差收缩；
- 状态对一步温度、未来路径和P(X)的敏感度；
- 状态参数跨fold、季节和天气型稳定性。

阈值必须在evaluation artifact中冻结。若某固定坐标长期等于prior、与其他坐标病态补偿、跨fold不稳定或移除后对一步路径与`P(X)`无可验证损失，则整个候选artifact不通过晋级门；live不得靠动态删状态继续运行。

`H/C_eff/A/E/D/Φ`的可辨识性审计只决定`Ψ`诊断能否发布：充分时发布带区间条件分解，不足时发布`partial/unavailable + na_reason`。禁止给不可辨识分量保留看似精确的posterior mean、用强正则钉到NWP后称为实况反演，或用自由Efficiency吸收全部误差。

### 6.19 Post-event更新与未来路径

当前事件处理顺序保持：

```text
pre-event transition Z_t^-
→ joint P(O_t|Z_t^-)
→ condition state/regime weights
→ post-event memory Z_t^+
→ future transition paths
→ reporting operator
→ P(X)
```

AMSC温度、露点、云雨风共同更新状态与regime权重。温度动量用于约束 `G_net`与预期物理driver是否兑现，但不单独再乘LR。未来每条路径必须允许正、负和再次转正的温度倾向，因此自然表达升温、降温、见顶与重新升温。

路径生成不得把最可能regime硬选为唯一未来；必须对regime转换、driver误差、过程噪声和报告误差共同积分。`P(X)`由所有未来合同计划槽报告值的最大值投影得到，而不是从 `G_net`或Termination概率直接查表。

### 6.20 P3强观测联合likelihood宪章

强观测输入只有已经完成来源选择、解析、质控、去重和revision关系的 `CanonicalObservationEvent`。P3不得重新抓取网页、重新选择AMSC/AWC、重新解释报文文本或把预测源升级为事实。

对事件`O_t`和pre-event状态分量`Z_t^{-(i)}`（其中已含离散`R_t`坐标）：

```math
\ell_t^{(i)}
=\log P(O_t\mid Z_t^{-(i)},O_{<t},C,artifact)
```

```math
w_t^{(i)}\propto w_{t^-}^{(i)}\exp(\ell_t^{(i)})
```

强观测模型直接给状态/情景分量评分，不先制造一份独立最高温posterior再乘入 `P(X)`。桶级log-LR只能在完整状态更新和路径投影后作为审计派生量。

同一事件的字段必须联合消费一次。允许输出逐字段/逐因子的审计贡献，但所有贡献及其依赖修正共同构成一个 `joint_log_likelihood`；下游不得挑选其中几项再次相乘。

### 6.21 编码感知的观测模型

令 `V_t` 表示事件时刻未编码的物理观测量，`O_t`是报文编码结果：

```math
P(O_t\mid Z_t^-)
=\int P(O_t\mid V_t,encoding,quality)
P(V_t\mid Z_t^-,O_{<t},C)dV_t
```

报文解析器必须保存字段编码精度、修约/截断规则、单位、censoring、缺失原因和原始token血缘。观测模型不能把整数报文值当成无限精度连续温度。

#### 6.21.1 温度与露点

若字段值为`y`、编码步长为`δ_enc`，且合同定义为最近值修约，则对应区间为：

```math
I(y,\delta_{enc})=[y-\delta_{enc}/2,y+\delta_{enc}/2)
```

温度likelihood使用区间概率：

```math
P(Y_t=y\mid Z_t^-)
=\int_{I(y,\delta_{enc})}p(v\mid \widetilde T_t^-,\sigma_{temp},quality)dv
```

若原始报文提供更高精度组、截断而非修约、负温编码或字段专用规则，必须由encoding artifact选择对应区间，不能统一套用±0.5°C。露点使用同类编码模型，并与温度通过联合温湿残差/物理约束建模，避免把同一气团偏差当两份独立信息。

温度与露点的有效物理关系由联合分布处理；不能简单硬裁 `dewpoint≤temperature` 后忽略传感/编码误差，也不能由两者派生相对湿度后再乘第三个likelihood。

#### 6.21.2 风

- 定向风使用圆周分布或二维 `u/v` 观测模型，禁止对359°和1°做普通线性差；
- 风速/阵风使用非负、可重尾分布；阵风是条件字段，不是第二个平均风速；
- `VRB`、静风和方向缺失是不同编码状态，不能分别伪造为0°；
- 当前风向突变可帮助重分配平流/冷池regime，但必须与风速、气压和温度innovation联合消费。

#### 6.21.3 云层、天气现象与能见度

- FEW/SCT/BKN/OVC/VV等云量为类别/有序类别观测，云底为带编码精度或区间的高度观测；
- 多层云是一个结构化集合，不得只取“最厚一层”后丢失其余信息，也不得把每层当独立天气事件重复增权；
- `-RA/RA/+RA`、TS、SH、BR、FG等天气token按强度、descriptor和phenomenon组成结构化类别likelihood；
- 报文降水现象不等于精确 `mm h⁻¹`，只能约束降水/冷池regime及强度区间；
- 能见度、CAVOK和上限/下限编码按censored likelihood处理；
- “天气组缺失”只有在报文编码合同明确表示无显著天气时才可作为负证据；sensor missing、解析失败和未报告必须写不同NA reason并边缘化。

云、天气现象、能见度、露点和降水通常高度相关。实现可采用共享latent regime的条件因子图、多变量类别head或联合校准score；禁止把“低云”“下雨”“低能见度”“温度减速”四项假定独立后机械相乘。

#### 6.21.4 气压

- 保留QNH、altimeter或station pressure原始类型，先经冻结adapter转换到canonical Pa；
- 单次气压约束气团/天气状态，压变由 `O_{<t}`历史与当前字段确定性派生；
- 当前压变是当前气压字段的重表达，不能与当前气压作为两份独立观测；
- 单站气压和风转只能软约束平流，不能替代周边温度梯度直接宣称冷暖平流真值。

### 6.22 联合依赖结构与innovation

可审计的条件分解示意为：

```math
P(O_t\mid Z_t^-,O_{<t})
=P(T_t,Dew_t\mid Z_t^-)
\cdot P(Wind_t,Pressure_t\mid T_t,Dew_t,Z_t^-)
\cdot P(Cloud_t,Wx_t,Visibility_t\mid T_t,Dew_t,Wind_t,Pressure_t,Z_t^-)
```

这是一个联合条件分解，不授权三个团队分别产出可相乘LR。因子顺序、依赖边和校准参数属于同一个observation artifact；更换结构必须生成新artifact。

连续观测innovation：

```math
\nu_t=y_t-E[y_t\mid Z_t^-]
```

```math
S_t=Var(y_t\mid Z_t^-)+R_{obs,t}
```

应输出标准化innovation、NIS/robust surprise、类别log score和posterior predictive percentile，用于校准与异常诊断。它们不是额外证据。

有效但极端的AMSC事件不能因为与NWP或当前posterior冲突就被删除。身份和解析有效时，应由预注册的重尾观测误差、regime切换和多情景posterior吸收；只有canonical QC明确无效的字段才可隔离。持续同向innovation进入离线artifact诊断，不允许在线扩大噪声、修改bias或降低AMSC事实地位。

#### 6.22.1 物理多假设竞争合同

单字段变化通常不能唯一识别物理原因。联合composer必须为同一观测保留互斥或部分重叠的候选解释，通过其他字段、历史轨迹、周边空间场和未来driver重分配情景权重，而不是把人工经验写成确定性加减分：

| 观测型 | 必须竞争的候选解释 | 主要区分证据 | 禁止捷径 |
|---|---|---|---|
| 露点下降 | 晴空干混合/边界层增深、干暖平流、干冷空气进入、阵风冷池或传感/编码波动 | 温度innovation、风转与空间温湿梯度、压变、云雨、阵风 | “露点下降必然支持升温” |
| `BKN010→SCT020→FEW030`一类低云抬升消散 | 湿层消散和辐射恢复、仅云底抬升但上层云仍遮挡、干空气平流 | 多层云全集、太阳几何、能见度、温露差、NWP辐射 | 把云量代码直接映射固定升温分数 |
| `FEW/SCT→BKN CB/TCU`或对流云增长 | 对流触发和辐射截断、尚未降水的积云发展、局地云层掠过 | CB/TCU、云底与层次变化、雷雨token、阵风、压升、周边对流driver | “SCT固定负面”或见CB就把Termination硬置1 |
| `BR/FG/HZ`消退、能见度改善 | 近地湿层消散、风力通风、干空气进入、降水结束 | 云量是否同步减少、露点与风、太阳几何、气溶胶/天气token | “能见度转好等于净辐射大增” |
| `RA/SHRA/TSRA`、阵风和压变 | 降水蒸发冷却、雷暴冷池、锋面/气团转换、短时报告异常 | 温湿突变、风向风速、压变、雷暴/CB、后续恢复轨迹 | 只给温度路径减一个固定值 |
| 风转或气压升降 | 冷暖平流、热低压日变化、冷池外流、局地山谷风或系统切换 | 周边`∇T/∇q`、多层背景风、温湿innovation、云雨 | 仅凭单站风压宣称冷暖平流真值 |
| 短时温度快速跃升 | 云隙辐射恢复、混合层跃迁/暖空气下混、暖平流、编码/采样差异 | 云层前后变化、露点、风和PBL driver、下一报文兑现程度 | 按当前斜率线性外推到日终 |

上述表只是结构约束，不是规则引擎。实际权重必须来自冻结联合likelihood和状态转移artifact。SPECI因经常伴随阈值跨越而可能具有较高信息量，但不得预设统一“高于METAR”的常数权重；信息量由其具体字段组合、编码质量和各情景下的预测概率决定。

每次更新应审计“哪些候选解释因该事件增权/减权”，但只允许输出来自同一posterior的情景权重变化；不得另外建立一套人工因果分数再修改`P(X)`。

### 6.23 温度轨迹、动量与当前事件边界

事件评分时必须严格分开：

```text
PreEventHistory  = Trajectory(O_<t)
CurrentEvent     = O_t
PostEventHistory = Trajectory(O_<=t)
```

允许在联合likelihood内部计算：

```text
current_step_delta = reported_temperature_t - previous_reported_temperature
current_step_rate  = current_step_delta / valid_time_delta
cloud/wind/pressure current-step change
```

这些量是当前事件相对pre-event历史的坐标变换，用于计算 `P(O_t|Z_t^-)`；它们没有增加新信息，不能在绝对温度likelihood之外再乘一次独立动量likelihood。评分完成后，`O_t`才进入PostEventHistory供下一事件使用。

同理，60/120分钟背景动量来自 `O_<t`，可作为pre-event state摘要；含当前值的60/120分钟动量只能作为当前联合innovation或post-state摘要，不能预先写入 `Z_t^-`。

### 6.24 字段质量、缺失与异常

每个字段必须属于：

```text
valid
censored_or_interval
not_reported_by_design
sensor_or_source_missing
parse_invalid
quality_conflict
not_applicable
```

处理规则：

- `valid/censored_or_interval`进入对应likelihood；
- `not_reported_by_design`是否含负信息由编码artifact决定；
- `sensor_or_source_missing`和`not_applicable`被边缘化并写NA reason；
- `parse_invalid/quality_conflict`隔离该字段，其他有效字段仍可更新状态；
- 事件身份、valid time、revision、raw hash或canonical选择无效时，整条事件不得更新posterior；
- 温度字段缺失时仍可由云雨风等更新状态，但不能生成新的observed-max hard condition；
- 不得以0、前值、AWC同名字段或NWP预测静默填充AMSC缺字段。

观测字段缺失模式必须进入observation artifact。缺失只减少本事件信息，不得自动把posterior变窄。

### 6.25 各类报文与来源的唯一更新合同

每个输入事件只能命中下表一行，并只更新该行授权的概率层：

| 输入/事件 | 唯一角色 | 允许更新 | 明确禁止 | revision/重放语义 |
|---|---|---|---|---|
| 四川盆地/站点/季节历史 | `prior_context` | 仅初始化`P_climate(Θ,U,Z_0|C)` | 不读取当日预测、实况或final X | 新prior只能形成新artifact，日内不改写 |
| 新NWP run/revision | `weak_driver_forecast` | active weak chain、`U`情景与其权重、未来`P(X)`投影 | 不生成`JointObservationLikelihood`，不改observed max，不冒充事实 | 按run/valid window替换active generation；相关旧run不得继续独立相乘 |
| TAF首报 | `weak_path_forecast/weak_target_expert` | TAF有效窗口内的`U/R`未来情景；存在TX/TN时作为经校准弱目标证据 | 不更新当前`Z`，不形成hard mask，不把BECMG/TEMPO伪造为精确逐小时实况 | 同一issuance只消费一次 |
| TAF AMD | `weak_path_forecast/weak_target_expert`（revision=AMD） | 仅替换原TAF受影响valid windows并生成新driver generation | 不与被替换TAF叠加，不按AMSC强观测更新 | 从active revision chain重算或使用受审计density ratio |
| 冻结合同槽AMSC primary METAR | `canonical_observation`（contract slot） | 一次联合likelihood更新`Z`；active温度同时固定进入合同历史并形成`X≥observed_contract_max` | 不拆字段成多份LR，不把计划槽身份变成额外概率权重 | correction按active revision chain重放 |
| 非合同槽METAR或SPECI | `canonical_observation` | 一次联合likelihood更新`Z`与driver/regime情景权重 | 不直接抬高合同`X` hard lower bound | 迟到/更正按valid time插入后重放 |
| METAR/SPECI COR | `canonical_observation`（replacement revision） | 替换active revision chain中被supersede事件并从前驱checkpoint重放；若为合同槽则重算observed max | 不得在错误posterior上增乘新likelihood，也不得用likelihood相除“撤销” | append-only保留旧raw/fact，latest只认active revision |
| AWC同报文确认 | `confirmation` | 只补血缘与质量审计 | 不产生第二likelihood | 仅canonical政策选为substitute时成为该唯一事件来源 |
| 获批WU/TWC/结算源预测 | `weak_target_expert`或`weak_path_forecast` | 只在冻结source-role与联合误差模型内更新弱情景 | 不作为AMSC事实，不直接写`settlement_actual` | 新revision按其自身active chain替换，不与旧版重复累计 |
| 结算源日内非final观测 | `settlement_layer_observation` | 仅在accepted bridge合同授权时更新`P(S|X,...)` | 不反向更新`Z/U/Θ`或物理`P(X)` | correction/finality由结算源合同处理 |
| `settlement_actual` | `settlement_layer_observation`（final） | 闭合`S`并在日终后形成bridge、校准和评价标签 | 不参与当日物理live滤波，不回写当日参数 | 仅解析完成的active final revision有效 |
| 市场价格、订单、持仓、P&L | `market_or_execution_only` | 只服务决策、执行与复盘 | 永不进入天气prior、状态、路径或报告算子 | 不适用 |

冻结计划槽中的AMSC primary METAR无论位于整点还是半点，均为primary候选；实际合同槽只由冻结AMSC计划表决定，不能假设全天固定48槽。合同槽温度进入观测likelihood和确定性hard condition是两个不同语义：前者更新`Z`，后者执行目标定义，不是两次概率证据。

唯一层级所有权为：

```text
climate context → P0
weak revision   → U/weak scenario
canonical report→ Z
report operator → X
settlement source→ S
market contract → B
```

任何输入若无法唯一归类，必须写`role_conflict`并fail closed，禁止由调用方临时选择更有利的更新层。

### 6.26 Correction、迟到报文与重放语义

correction仍保留原 `report_type=METAR|SPECI`，以更高revision和 `supersedes_event_id`追加，不覆盖旧raw或旧canonical历史。

若correction修改已经消费的事件：

```text
1. 找到被修正事件之前的最后合法checkpoint
2. 构造active canonical revision chain
3. 用corrected revision替换被supersede revision的有效位置
4. 按canonical顺序重放其后的所有active事件
5. 比较增量重建与全量重放semantic hash
6. CAS原子提交新的latest checkpoint/fact
```

禁止将corrected报文likelihood直接乘在错误旧报文posterior之上，也禁止用likelihood相除近似撤销旧更新。若旧报文曾形成错误高温hard lower bound，重放必须从active revision chain重新计算observed contract max；hard condition可以被合法correction修正，但历史fact保持不可变可审计。

首次到达但 `report_valid_at`早于当前latest state time的合法迟到报文也不得用负 `Δt`直接更新。它必须作为out-of-sequence measurement，从其valid time之前的checkpoint插入active event chain并重放后续事件；若所需前驱、driver generation或事件血缘不完整，则该次posterior mutation fail closed，但canonical事实仍保留。

correction或迟到重放若超出3秒交易deadline，仍应提交正确physical posterior并把本次execution资格标记为NA；不得为了赶交易沿用已知错误事实或把旧报文冒充当前时刻观测。交易deadline仍从该事件真实 `first_actionable_seen_at`起算，不能因重放重置T1。

### 6.27 JointObservationLikelihood输出合同

```text
JointObservationLikelihoodV1
  likelihood_id
  canonical_observation_event_id
  target_identity
  previous_checkpoint_id + sha256
  pre_event_state_semantic_sha256
  pre_event_history_sha256
  observation_payload_sha256
  observation_artifact_id + sha256
  usable_fields[]
  marginalized_fields[] + na_reason
  isolated_fields[] + quality_reason
  component_log_score_audit[]
  dependency_correction_audit
  joint_log_likelihood_by_state_component[]
  joint_log_likelihood_semantic_sha256
  innovation_diagnostics
  deterministic_hard_condition
  report_slot_role
  source_selection_role
  started_at/completed_at
```

`component_log_score_audit`只服务解释，不能被下游重新组合。`deterministic_hard_condition`与联合likelihood分列；前者只表达合同逻辑。输出不包含post-event trajectory或 `Z_t^+`，避免观测模型和状态更新互相污染。

### 6.28 强观测训练与校准边界

训练目标至少包括：下一合同槽温度、下一事件温度增量、未来1小时路径、剩余最高温、见顶和重新升温；不能只用日终X反推所有字段likelihood。

训练/评价必须：

- 按station-day分组，避免同日事件当作独立日夸大样本；
- 保留报文原始编码和字段缺失模式；
- 使用严格as-of前向样本决定artifact晋级，历史回补只服务明确的auxiliary研究；
- 分report type、计划槽角色、季节、时段、天气regime和source selection role校准；
- 检验连续innovation覆盖、类别reliability、NIS、事件 `ΔCRPS`及错误转向；
- 通过leave-field-group-out消融验证云雨、露点、风和气压是否提供独立增量，而非重复温度趋势；
- 冻结重尾比例、观测噪声、依赖结构和缺失语义，online只滤波不学习。

样本、OOS或某字段校准不足只阻止新observation artifact晋级，不阻断AMSC canonical事实入库或当前已批准模型live。

### 6.29 P4未来路径生成合同

路径生成输入只能是：

```text
PostEventState Z_t^+
frozen physical_driver_generation
state-model artifact
reporting-operator artifact
active canonical contract-slot history
remaining contract slots
```

每个posterior情景/粒子必须生成一条时间相关的未来路径：

```math
Z_{t+1:H}^{(i,l)}
\sim P(\cdot\mid Z_t^{+(i)},U_{t:H},C)
```

其中`Z`已经包含`T̃/q/G_net/W/C_cp/R`，`i`是当前posterior分量，`l`是该分量内的未来过程/driver样本。路径权重继承当前posterior权重和条件路径概率。

#### 6.29.1 一条粒子代表一个自洽的“当天世界”

粒子不是把各时次温度边缘分布拼接起来，也不是为`Rn/H/LE/A/E/D`各自创建可自由漂移的状态。一条路径样本至少绑定：

```text
current Z_t sample
+ one coherent weak-driver U_t:H path
+ one regime transition path R_t:H
+ process innovations with frozen cross-time/cross-field dependence
+ conditional Ψ diagnostics（非递推、不可单独改权）
+ one reporting-process realization for future contract slots
= one coherent day-world path and its X*/X outcome
```

允许给路径添加仅用于解释和分层评价的`scenario_family`，例如：

- `clear_or_dry_mixing_high`：云少或低云消散，感热/混合路径持续，高温mode保留；
- `cumulus_limited`：午后积云增长，辐射减弱但不必然出现冷池；
- `convective_cutoff`：对流、降水、阵风和冷池路径使升温窗口提前终止；
- `advection_or_entrainment_boost`：在局地辐射一般时由暖平流、卷入或下沉维持升温；
- `post_rain_recovery`：降水冷却后再放晴，存在回升、追平或创新高分支。

这些family由冻结的完整路径分类器确定，不新增`Z`坐标、不替代`R`、不参与第二次加权，也不能把所有天气强行塞入上述五类；不适用路径进入版本化`other`并保留原始质量。路径重采样、裁剪和受控合并必须在合理mode仍高于冻结最小质量时保留其代表样本，避免高权重单峰复制后把低概率但真实可行的高温、对流或恢复分支永久消灭。

硬规则：

- 同一路径的未来温度、云雨regime、通量和动力状态必须保持时间相关性；禁止对每个报告槽独立抽取边缘温度后拼接；
- 时间网格必须包含所有剩余合同计划槽，并可插入artifact冻结的物理积分子步；
- 路径必须允许温度倾向为正、为负及由负再次转正；
- regime转换、weak-driver误差、process noise和慢参数不确定性都必须进入路径；
- 不能只传播posterior mean或最可能regime；
- 路径裁剪、合并和尾部压缩必须保留各合理mode及其质量，不能为提速删除低概率高温/低温尾；
- 市场价格、目标桶价格、持仓和交易策略不得影响路径数量、温度范围或采样权重。

路径数、每分量最低路径数、子步、尾部保留和收敛阈值属于artifact。若路径预算不足以使 `P(X)`在冻结误差范围内稳定，该次projection fail closed，不能输出噪声概率。

### 6.30 AMSC报告算子

对未来计划槽 `j`：

```math
V_j=\widetilde T_{\tau_j}+b_{report,day}+\epsilon_{report,j}
```

```math
Y_j=Encode_C(V_j,encoding_j,quality_j)
```

其中 `b_report,day`表示需要时保留的日内共享报告/采样偏差，`ε_report,j`表示槽级误差；二者的分布、相关性和是否启用由报告算子artifact校准。不得对所有槽使用相互独立的±0.5°C噪声。

报告算子必须冻结：

- station、resolution-local-date与计划槽版本；
- 每个槽的valid time、合同资格和时区；
- 温度单位、编码精度、修约/截断/负温规则；
- 计划METAR、非计划METAR、SPECI和correction语义；
- quality、缺报、重复和active revision规则；
- 共享/槽级报告误差结构；
- 连续潜在温度到离散报文温度的完整映射。

已经观测的active合同槽 `Y_i`作为固定事实直接放入每条路径，不能重新采样。未来槽才经过报告算子生成。correction重放后只使用active revision值。

### 6.31 报告可得性与缺报

未来槽是否形成有效合同报文记为 `A_j∈{0,1}`。缺报不是0°C、前值或插值温度：

```math
X=\max\left(\{Y_i:i\le t,A_i=1,i\in K_C\}\cup\{Y_j:j>t,A_j=1,j\in K_C\}\right)
```

但最终物理标签是否允许在缺槽情况下定义，由冻结target contract决定。若合同要求完整coverage：

- live阶段同时输出 `P(X|target_reportable)`与 `target_reportability_probability/state`；
- 未来潜在缺报只影响reportability，不作为天气降温证据；
- required past slot已不可恢复缺失时，`physical_x_reportability_state`按合同转为NA/conditional，不能伪造完整X；
- AMSC事实和潜在温度路径仍可继续更新，训练标签缺失不能阻断live状态滤波；
- bridge和交易是否可继续必须由独立accepted市场投影合同决定，不能由P4自行猜测。

缺报概率模型属于运行/报告过程，不得与温度过程共享一个自由噪声项。

### 6.32 最高温路径投影

令active已观测合同槽集合为 `K_obs`、未来合同槽集合为 `K_future`。每条路径：

```math
X^{(i,l)}
=\max\left(
\max_{j\in K_{obs}}Y_j,
\max_{j\in K_{future}}Y_j^{(i,l)}
\right)
```

离散最高温分布：

```math
P(X=x)
\approx
\sum_i w_t^{(i)}
\sum_l \omega_{l\mid i}\mathbf1\{X^{(i,l)}=x\}
```

必须在离散AMSC报文温度支持上归一，不得先取连续峰值均值再修约成一个桶。`P(X*)`和 `P(X)`分别保存；前者是连续潜在峰值诊断，后者才是物理目标SSOT。

所有开放尾部必须保留明确质量；不允许因当前市场只挂牌有限桶就截断物理温度支持。市场桶通过后续 `X→S→B`边缘化得到。

### 6.33 升温、降温、见顶与重新升温输出

路径层至少派生：

```text
next_slot_reported_change_distribution        # 有符号
next_hour_latent_temperature_change_distribution # 有符号
future_reported_peak_margin = F_t - M_t       # 有符号
remaining_reported_rise = max(0, F_t - M_t)   # 非负
future_cooling_depth_distribution             # 当前后最低温回落
no_future_exceedance_probability              # F_t <= M_t
strict_peak_already_occurred_probability       # F_t < M_t
future_tie_probability                         # F_t = M_t
new_high_probability                           # F_t > M_t
```

这里 `F_t`是未来合同槽最高报文温度，`M_t`是active已观测合同槽最高值。`future_reported_peak_margin<0`明确表达未来报文预计低于当前最高值；系统因此不仅预测“还能升多少”，也预测后续会降多少。

热力回升事件由artifact冻结的幅度、持续时间和槽间隔定义：

```text
thermal_rebound:
  路径先出现满足阈值的下降，再出现满足阈值的回升

tie_after_rebound:
  thermal_rebound 且未来报文重新等于M_t

new_high_after_rebound:
  thermal_rebound 且未来报文超过M_t
```

不能把任意一个小幅上跳都叫重新升温，也不能把“回升”直接等同“创新高”。

当已经没有剩余合同槽时，固定边界为：`no_future_exceedance=1`、`strict_peak_already_occurred=1`、`future_tie=0`、`new_high=0`、`remaining_reported_rise=0`；`future_reported_peak_margin`因无未来槽写NA而不是伪造0。存在未来槽且target reportable时，必须满足 `strict_peak_already + future_tie + new_high = 1`。

### 6.34 Termination与峰值时刻

Termination必须有版本化路径定义，例如：未来连续一定物理时长净倾向不再为正、且在剩余合同槽中没有新高；具体持续时间和容忍阈值在evaluation artifact中冻结。

必须分别输出：

- `heating_termination_time_distribution`；
- `no_future_exceedance_probability`；
- `first_peak_slot_distribution`；
- `last_peak_slot_distribution`；
- `future_tie_probability`；
- `new_high_after_rebound_probability`。

当多个槽达到同一最终最高温时，first/last peak分别记录，禁止用未声明tie-break只保存一个峰时刻。太阳落山、降雨、云层增厚或一次负动量只能改变路径概率，不能单条规则把Termination设为1。

### 6.35 Hard condition顺序

正确顺序：

```text
联合强观测更新状态
→ 生成时间相关未来路径
→ 固定active历史合同槽并应用未来报告算子
→ 计算路径级X
→ 应用确定性hard condition
→ 归一化并生成PosteriorSSOTFact
```

最佳实现是“构造即满足”：每条路径直接包含active已观测合同槽，所以天然有 `X≥M_t`。最终hard mask仍作为防御性不变量检查。

hard condition只包括：

- active revision chain中的已观测合同槽最高值；
- 冻结目标单位、离散支持和桶边界；
- 目标合同明确规定的报告资格；
- 已确认的invalid/correction/supersedes结果。

不得hard mask：预计降雨、预计见顶、太阳高度下降、模型一致认为不可能、低概率尾部、市场极端桶或交易安全偏好。

若hard condition后总质量为0，说明状态、报告算子、目标合同或实现发生矛盾；必须fail closed并保留诊断，禁止回退均匀分布、旧posterior或偷偷放宽hard事实。

### 6.36 路径采样确定性与收敛

- 随机种子绑定target、checkpoint、artifact和driver generation；
- 分量、路径、regime、报告槽和温度支持使用稳定排序；
- 同一ReplayBundle必须得到相同semantic `P(X)`哈希；
- 2×2四臂Shadow中语义相同的driver/过程随机量尽量使用common random numbers，降低Monte Carlo比较噪声；
- 路径数翻倍、子步减半和尾部预算扩展必须进行收敛敏感度测试；
- `P(X)`、CRPS和关键派生概率的Monte Carlo误差必须低于冻结容忍度；
- ESS或mode覆盖不足时可增加路径/重新分配预算，但不能在报文热路径无限迭代；超过冻结预算则该次projection fail closed。

### 6.37 三秒热路径实现

报文到达前预热：

- 剩余计划槽和太阳几何；
- weak-driver情景及其时间相关采样；
- 报告算子查表/编码边界；
- 状态转移参数、regime矩阵和确定性随机流；
- 温度支持、市场bridge（若可用）及序列化模板。

报文到达后只执行：状态权重更新、小规模条件路径传播、报告算子、X聚合、hard check和本地Transaction B。禁止同步网络、GRIB解码、模型训练、全日NWP重算或市场查询进入P4。

性能预算继续服从：状态滤波≤300ms、路径与X投影≤500ms、Transaction B≤100ms的目标；这些是待验证预算，不是降低路径精度的授权。若无法同时满足收敛和时延，模型不得晋级，应优化表示、预计算和向量化，而不是静默减少尾部。

### 6.38 PosteriorSSOTFact输出合同

P4提交的公开概率事实至少包含：

```text
identity:
  posterior_run_id/checkpoint_id
  target_identity/as_of/canonical_event_id
  previous_checkpoint_id + sha256

artifacts:
  state_model/observation/driver/reporting_operator/hard_mask IDs + sha256

physical target:
  physical_x_distribution
  physical_x_distribution_sha256
  latent_continuous_peak_distribution
  observed_contract_max
  active_contract_slot_ids + sha256
  physical_x_reportability_state/na_reason

path summaries:
  next_slot_reported_change_distribution
  next_hour_latent_temperature_change_distribution
  future_reported_peak_margin_distribution
  remaining_reported_rise_distribution
  future_cooling_depth_distribution
  no_future_exceedance_probability
  strict_peak_already_occurred_probability
  future_tie_probability
  new_high_probability
  thermal_rebound_probability
  tie_after_rebound_probability
  new_high_after_rebound_probability
  first_peak_slot_distribution
  last_peak_slot_distribution
  heating_termination_time_distribution

state diagnostics:
  state_schema = identifiable_physical_state_v1
  state_coordinate_information
  weather_regime_distribution
  net_tendency_distribution
  physical_decomposition_diagnostics_state/na_reason
  uncertainty_breakdown
  ESS/normalization/projection convergence

scenario diagnostics:
  coherent_scenario_family_distribution
  top_scenario_summaries
  uncertainty_driver_ranking
  next_observation_discrimination/na_reason

market projection:
  bridge_artifact_id + sha256
  bridge_condition_summary_sha256
  settlement_distribution + market_bucket_distribution
  or explicit market_projection_na_reason

lineage/freshness/timing:
  source/driver availability
  complete input lineage
  T1/inference/commit timestamps
```

公开fact只保存可消费分布和审计摘要，不暴露原始粒子供UI/交易自行聚合。若market bridge不可用，仍提交physical-only fact；若physical `P(X)`本身无效，则不得创建一份只有市场概率的fact。

所有不适用、不可定义或缺失的分布/概率必须带对应 `*_na_reason`。例如终局没有未来合同槽时，`future_reported_peak_margin_distribution`写NA reason，而不能用0分布伪装存在未来温度边际。

### 6.39 路径与最高温校准

P4必须分别评价：

- 下一槽报文温度和有符号增量；
- 未来1小时潜在温度变化；
- `future_reported_peak_margin`和remaining rise；
- 离散physical X的MAE、CRPS、NLL、Brier与coverage；
- no-future-exceedance/new-high/tie概率校准；
- first/last peak slot与Termination时刻；
- thermal rebound与new-high-after-rebound；
- 报告算子编码分布和reportability；
- 路径联合覆盖、时间相关性和mode保持；
- Monte Carlo收敛误差与运行时延。

只校准最终X不足以证明路径正确。候选必须同时通过一步预测、路径事件和最终最高温三层评价；否则可能以错误物理路径碰巧命中同一个最高温桶。

### 6.40 主导情景、不确定性来源与下一报文信息价值

“当前主导情景、最大不确定性来源、下一条报文最能区分什么”正式纳入SSOT，但只作为同一posterior的结构化派生诊断：

```text
ScenarioSummaryV1
  scenario_family
  posterior_mass
  conditional_X_distribution
  expected_peak_slot_distribution
  evidence_increased_by[]
  evidence_decreased_by[]
  physical_decomposition_summary/na_reason

UncertaintyDriverRankV1
  driver_family            # forecast/partition/heat_capacity/dynamics/regime/process/reporting/bridge
  contribution_metric      # frozen variance/entropy attribution definition
  contribution
  rank

NextObservationDiscriminationV1
  candidate_valid_time
  field_group              # temp_dew/wind_pressure/cloud_weather_visibility
  predictive_outcome_bins
  scenario_conditional_probabilities
  expected_information_gain
  expected_X_entropy_reduction
  human_readable_template_id
```

主导情景只允许表达为分布和top-K摘要，禁止把最大权重情景硬选为唯一事实。`top_scenario_summaries`的质量之和若小于1，剩余质量必须明确聚合为`other_mass`，不能静默删除。

令`𝓘_t=(C,F_A,O_{1:t})`表示当前严格as-of可见信息。对下一计划槽候选观测`O_{next}`，信息价值定义为：

```math
EIG(O_{next})
=\mathbb E_{o\sim P(O_{next}\mid \mathcal I_t)}
\left[
KL\left(
P(X,R_{next}\mid \mathcal I_t,o)
\,\|\,
P(X,R_{next}\mid \mathcal I_t)
\right)
\right]
```

实现使用同一状态转移、联合观测模型和报告算子做有限个预注册outcome bin的反事实更新；不得调用第二模型或用人工规则估计EIG。输出可以结构化表达例如“若下一槽仍少云且露点下降，高温干混合情景将增权；若出现BKN CB/TSRA和阵风压升，对流截断情景将增权”，但自然语言只能由`template_id + structured bins`渲染，不能反向进入posterior。

`next_observation_discrimination`必须满足：

- 只使用当前as-of可见信息，不读取下一报文、日终标签或当前之后的文件mtime；
- 只评价下一计划槽及已知可能字段，不预测一条SPECI必然出现；SPECI实际到达后按canonical合同正常更新；
- 不改变轮询频率、字段权重、hard mask、交易门槛或路径采样质量；
- 在OOS中检验预测outcome bin频率、情景区分方向和实际熵下降；失准只阻止该诊断artifact晋级，不得影响物理`P(X)`。

`uncertainty_driver_ranking`必须基于冻结归因方法和同一posterior的受控重采样/方差或熵分解。相关不确定性不得各自声称100%贡献；无法稳定分解时写`unavailable + na_reason`，不得输出看似精确的百分比。

## 7. 事件级运行链

每条 AMSC canonical event 只允许一次有效更新：

```text
1. 接收、解析、质控、去重
2. append CanonicalObservationEvent
3. 读取上一个 PhysicalStateCheckpoint
4. 传播冻结未来强迫和天气状态情景
5. 用AMSC联合观测更新固定`T̃/q/G_net/W/C_cp/R`状态并重加权driver情景
6. 生成未来潜在温度路径分布
7. 在冻结AMSC计划槽应用报告算子并投影 P(X)
8. 应用 hard mask
9. bridge 可用时投影 P(B)，否则写明确 NA 原因
10. Transaction B原子提交 StrongUpdate + checkpoint + PosteriorSSOTFact + latest + 可选posterior outbox
11. Decision Core消费已提交outbox，并由Transaction C原子提交decision、risk reservation与execution intent
12. 幂等提交 CLOB 并回写订单结果
```

AMSC入库和物理 posterior 不得等待预测源同步抓取、训练、bridge、市场 finality、行情或下单。

### 7.1 每日四阶段运行语义

| 阶段 | 合法输入 | 唯一动作 | 必须输出 |
|---|---|---|---|
| 清晨/P0 | `C_static + C_antecedent`、截至as-of的active NWP/TAF revision | 建立盆地—站点多天气型先验并生成首批自洽日路径 | `P0(Z,U)`、`P0(X*/X)`、regime/情景混合、主要不确定性 |
| 弱预测修订 | 新NWP run、TAF、TAF AMD/COR或获批弱源revision | 替换active valid windows，重建唯一driver generation | 新weak checkpoint、路径和`P(X)`；不得伪装成实况 |
| METAR/SPECI事件 | 单一active canonical report及其pre-event history | 联合更新固定`Z`和情景权重，推演到日终并经报告算子投影 | 新posterior SSOT、情景变化、下一报文信息价值 |
| 合同日闭合后 | final physical X、有效`settlement_actual`及完整revision/finality | 只形成评价、bridge和未来artifact训练样本 | 评分、校准、归因和候选artifact；不得回写当天live posterior |

午后接近最高温时，不允许因为剩余窗口缩短而切换成温度线性外推器。当前合同最高温hard lower bound会自然增强约束，但未来路径仍必须积分剩余辐射、云量/对流转换、平流、冷池、恢复、报告槽和缺报概率。

四阶段共享同一目标身份、artifact家族和checkpoint血缘，不得各自建立一份最高温概率再在末端相乘。

## 8. 三秒链路设计

### 8.1 SLO语义与唯一时钟

“3秒完成下单”固定定义为：从本机首次获得一条完整AMSC候选payload，到本机收到CLOB对该订单的accepted响应。definitive rejected只是一次提交尝试的确定失败终态，不算完成下单。仅写入本地intent、把任务放进队列、发出HTTP字节或收到不确定超时，也都不算完成下单。

```text
T1  first_actionable_seen_at
    本机接收器首次拥有完整AMSC候选payload，在解析/质控前立即使用monotonic clock打点
T2  canonical_committed_at
    raw归档、解析、质控、去重与CanonicalObservationEvent不可变提交完成
T3  posterior_committed_at
    Transaction B提交唯一PosteriorSSOTFact及可选posterior outbox
T4  execution_intent_committed_at
    Transaction C提交auto_trade_decision、risk reservation与execution intent
T5  clob_definitive_response_at
    本机收到CLOB accepted或definitive rejected响应

D_event = T1 + 3000ms
```

主目标为：

```text
eligible actionable AMSC event：accepted_at − T1 p95 ≤ 3000ms
```

每个eligible事件只有在deadline内收到accepted才算SLO成功；definitive reject、deadline miss、ambiguous outcome和进程丢失均计为失败，不能因为不存在accepted latency而从成功率分母删除。另行报告所有accepted请求的端到端p50/p95/p99，以及所有请求的time-to-terminal分布。

accepted-within-deadline成功率门槛必须在前向结果不可见时写入evaluation/cutover ADR；p95达标但成功率不达标不能晋级，成功率达标但accepted p95超过3秒同样不能晋级。

必须同时报告：

- `T2−T1` canonical事实时延；
- `T3−T2` 概率时延；
- `T4−T3` 决策与intent时延；
- `T5−T4` 外部请求到确定终态响应时延；
- `T3−T1` posterior freshness；
- `T5−T1` 端到端time-to-terminal；
- `accepted_at−T1` 真实完成下单时延；
- deadline miss、definitive reject、ambiguous outcome与未触发交易数量。

`report_valid_at`、provider发布时间、数据库mtime和worker取件时间不得替代T1。同一canonical事件的retry、replay、进程重启和队列迁移不得重置T1。合法superseding correction是一个新canonical事件，使用它自身真实的first-actionable T1；其内部重放不得改用被替代事件的T1，也不得在重放完成后重新起算。wall clock用于审计显示，deadline判断必须使用同一boot identity下可比较的monotonic elapsed time；跨进程同时保存wall/monotonic映射和boot identity。

### 8.2 SLO分母与非交易事件

为了避免通过删掉慢样本伪造SLO，事件必须先进入下列互斥终态之一：

```text
not_actionable_fact
physical_only_no_market_projection
strategy_no_action
execution_eligible_completed
execution_eligible_deadline_missed
execution_eligible_definitive_rejected
execution_eligible_ambiguous_outcome
posterior_failed_closed
```

端到端3秒SLO的主分母是：T1时live开启、target/market/凭证/批准策略均已预部署，该事件形成合法市场posterior，并且当前策略产生action或除deadline已过外本应产生action的全部事件。不得因结果慢、CLOB拒单、进程崩溃或deadline miss事后移出分母。市场不存在、bridge未闭合或策略本来不动作的事件不进入订单SLO，但仍进入canonical与posterior SLO。

另外固定两个独立SLO：

1. **事实SLO**：所有身份有效的actionable AMSC事件都必须完成T2，不受模型、市场或交易阻断；
2. **概率SLO**：所有当前active artifact能够合法消费的canonical事件都必须计入T3时延，失败也作为fail-closed失败率报告。

### 8.3 工程预算

预算按p95目标冻结；总和必须给外部网络保留明确余量：

| 阶段 | 计时 | p95目标预算 |
|---|---:|---:|
| 接收分派、raw归档、解析、质控、去重 | `T1→T2` | ≤100ms |
| 物理状态传播与联合观测更新 | T2后内部span | ≤300ms |
| 路径生成、报告算子、`P(X)`与市场投影 | T2后内部span | ≤500ms |
| Transaction B概率SSOT原子提交 | commit span | ≤100ms |
| Decision Core与Transaction C | `T3→T4` | ≤100ms |
| 行情快照验证、签名、网络和CLOB响应 | `T4→T5` | ≤1900ms |

内部预算总和是端到端预算分配，不表示各阶段可以顺序耗尽上限。p50/p95/p99、最大值和超时率都必须报告；任何阶段节省的时间只能增加剩余deadline，不能允许后续阶段忽略 `D_event`。

### 8.4 Resident热路径

唯一生产概率消费者必须是由 `system/com.weather.monitor.daemon` 托管的resident worker。开市前或目标激活前完成：

- active model artifact、calibrator、报告算子、hard mask和数值合同的SHA校验与内存加载；
- 每个target的P0/最新checkpoint、弱预测情景、物理driver generation和未来时间网格预热；
- 太阳几何、固定计划槽、市场合同、token identity和bridge映射预计算；
- CLOB客户端、认证材料、签名器和连接池预初始化；
- 只读行情流订阅及freshness监控；
- 固定大小数组、粒子/混合分量buffer和序列化buffer预分配；
- 数据库连接、prepared statement和busy/lock预算预热。

热路径禁止：

- 网络抓取NWP、GRIB解码、全日轨迹重建或provider同步查询；
- 训练、MCMC、超参数搜索、calibrator选择或production参数改写；
- 动态安装依赖、加载未校验artifact或读取 `latest.json` 研究指针；
- generic task queue、无界线程池、无界内存分配或同步UI刷新；
- 因CPU压力临时减少粒子、裁掉尾部、合并峰或切换旧模型。

粒子数、混合分量、子步、线程数、BLAS行为、内存上限和退化判定全部属于artifact运行合同。若冻结预算无法满足准确率、峰保真和时延，候选不得晋级。

### 8.5 EventExecutionEnvelope

T2后为每条事件冻结一次执行包：

```text
EventExecutionEnvelopeV1
  target_identity
  canonical_event_id + canonical_sequence + event_sha256
  T1 + D_event + clock_boot_identity
  expected_previous_checkpoint_id + sha256
  expected_latest_fact_id + sha256
  active_model_manifest_id + sha256
  weak/driver/reporting/hard-mask lineage + sha256
  source_availability_mask
  market/bridge/strategy artifact IDs + sha256
  execution_idempotency_root
```

天气推理不得绑定易变行情。Transaction B提交posterior后，Decision Core基于它派生：

```text
DecisionExecutionEnvelopeV1
  parent_event_execution_envelope_sha256
  posterior_fact_id + sha256
  latest_fact/sequence proof
  strategy_artifact_id + sha256
  quote_snapshot_id + observed_at + freshness_state + sha256
  position/risk snapshot identity
  T1 + D_event
  decision/order idempotency keys
```

推理、posterior、decision和order必须继承同一个event root，但行情只进入decision child，不进入天气 `P(X)`。行情刷新可以重新派生尚未提交的decision envelope，不能反向触发posterior重算；一旦Transaction C提交intent，该intent绑定的行情和风险快照不可改写。中途发现artifact、checkpoint、event active revision、bridge或市场合同发生漂移时，禁止混合新旧输入继续提交；必须以新身份重新计算，deadline仍不重置。

### 8.6 顺序、并发与背压

- 同一target的canonical事件严格按sequence单写者处理；不同target可在冻结资源配额内并行；
- canonical事实入库与概率推理解耦，模型积压不能阻塞T2；
- resident mailbox必须有界、按T1/deadline和canonical sequence调度，不得无界堆积；
- 同一target到达更新事件时，尚未提交的旧事件交易资格立即stale，但旧事件不能从canonical历史删除；
- 已提交Transaction B但尚未进入Transaction C的旧outbox，必须在C内复验latest fact/sequence，过期则写明确decision阻断原因；
- correction和迟到报文必须正确重放active chain；若越过deadline，仍生成正确physical posterior，但不生成晚到交易；
- 不允许跳过中间canonical状态后直接用最新事件拼接checkpoint；可在内存合并计算，但每一步语义状态和事件消费身份必须可重放。

过载时优先级固定为：

```text
AMSC事实完整性
→ canonical顺序与posterior正确性
→ 交易deadline
→ Shadow/诊断/配对/离线任务
```

不能为了满足3秒SLO牺牲事实、概率正确性或使用旧posterior下单。

### 8.7 Transaction B短原子事务

所有推理、路径传播、报告投影、hard condition、序列化和哈希在事务外完成。Transaction B只执行：

1. 重验数据库/运行域哨兵与active runtime manifest；
2. CAS校验canonical event仍为active、sequence连续、前驱checkpoint和latest fact一致；
3. 重验candidate payload SHA、概率归一、hard invariant、artifact lineage和freshness；
4. append `StrongObservationUpdate` 与 `PhysicalStateCheckpoint`；
5. append `event_fact_history.PosteriorSSOTFact`；
6. bridge/市场投影合法时append posterior outbox及execution contract；
7. CAS推进唯一 `event_fact_latest`；
8. 整体commit。

任一insert、constraint、CAS、latest推进或commit失败，必须零部分可见。同键同payload返回既有提交；同键异payload fail closed。Transaction B不能等待网络、行情、签名、Decision Core或CLOB，也不能写decision、risk、position、intent或order。

如果deadline在Transaction B前已经耗尽：

- 仍提交正确physical `P(X)`；
- market projection合法时append带 `execution_eligibility=deadline_expired` 的decision-audit outbox，由Transaction C在 `auto_trade_decisions` 写入 `execution_deadline_expired_before_posterior`，但不得创建risk reservation或execution intent；
- market projection本身不合法时只提交physical-only SSOT及既有market projection NA，不伪造交易候选；
- 不得重置T1、沿用旧市场posterior或为追赶deadline跳过hard/lineage校验。

### 8.8 Transaction C决策与intent原子事务

只有已提交posterior outbox能够进入可执行决策，并在Transaction C内一次完成：

```text
claim posterior outbox
+ verify latest posterior/event sequence/artifact/deadline
+ evaluate accepted strategy contract
+ append auto_trade_decisions
+ reserve risk/capital
+ append immutable execution intent
+ mark outbox decision state
```

`auto_trade_decisions`是执行阻断原因唯一来源。Transaction C必须：

- 复验当前fact仍是该target最新可交易fact，避免动态主YES/次YES/安全NO使用过期posterior；
- 只读取 `PosteriorSSOTFact`、冻结策略artifact、当前持仓/资金和合格行情快照，不读取内部粒子自行复算；
- 不使用旧EV/Edge/LCB gate，不在性能层引入第二套策略语义；
- 在事务外完成昂贵计算，在短事务内做CAS、额度预留与append；
- 对同一decision幂等键只产生一个reservation和一个intent；
- deadline不足以完成签名与网络提交时写明确skip，不创建注定过期的intent。

Transaction C失败不得回滚Transaction B，也不得反写posterior。重试必须先复验latest fact、reservation和deadline。

若在Transaction B之前发生artifact失效、概率fail closed或其他根本无法形成posterior outbox的故障，事件协调器只能append不可变：

```text
ExecutionIneligibilityNoticeV1
  canonical_event_id + sequence + T1/deadline
  failure_family + closed reason code
  artifact/input lineage available at failure
  notice_idempotency_key + sha256
```

该notice仍由同一个Decision Core消费，且只允许幂等append一条no-action `auto_trade_decisions`记录；禁止创建risk reservation、intent、order或伪造posterior。这样执行阻断原因仍只有一个SSOT writer/表，而概率层不获得决策写权。可执行路径仍严格只来源于已提交posterior outbox。

### 8.9 CLOB提交、幂等与不确定结果

订单幂等根至少绑定：

```text
target + posterior_fact_id + decision_id + strategy_artifact_id
+ token_id + side + limit_price + size + order_purpose
```

若CLOB支持client order ID，必须使用其稳定映射；若不支持，仍需本地execution fingerprint与订单查询/reconcile证明不会盲目重复。

执行状态固定为：

```text
intent_committed
request_started
accepted
definitive_rejected
ambiguous_timeout
reconciled_accepted
reconciled_not_found
```

规则：

- T5只取accepted或definitive rejected响应；HTTP timeout、断链、未知响应不得伪装rejected或成功；
- ambiguous timeout后禁止盲目重发；先按订单身份查询/reconcile；
- deadline已过后只允许reconcile既有请求，不允许新发或重新发订单；
- reconcile发现已成交/已挂单时回写真实状态，不因本地超时重复占用仓位；
- definitive reject必须释放对应reservation并记录交易账本原因；
- 订单响应回写属于执行账本，不进入或改写概率SSOT。

### 8.10 Deadline与fail-closed

每个阶段入口都计算：

```text
remaining_ms = D_event − monotonic_now
```

阶段只有在冻结的worst-case remaining budget内可完成时才继续交易分支。deadline耗尽后的唯一合法行为是：保持AMSC事实与正确posterior、写明确 `auto_trade_decisions`/execution状态、停止创建新外部副作用。禁止：

- 使用上一报文posterior、旧行情或旧decision赶单；
- 降低模型精度、跳过hard condition、概率校验、风控或签名校验；
- 通过修改T1、排除慢样本或把本地intent当作订单完成来美化SLO；
- 自动切换旧模型、旧策略或第二条概率链。

### 8.11 故障处置矩阵

| 故障 | 事实层 | 概率层 | 交易层 |
|---|---|---|---|
| AMSC payload身份有效但字段无效 | raw/canonical质控留痕 | 按观测质量合同边缘化或fail closed | 无合法posterior则不交易 |
| active artifact/SHA/manifest失效 | 继续T2 | fail closed，不fallback | 写模型不可用阻断 |
| resident worker冷启动/未预热 | 继续T2 | 不加载未验artifact；恢复后按序重放 | deadline过期不补单 |
| Transaction B CAS冲突 | 不受影响 | 读取新前驱后重算；deadline不重置 | 未形成新fact前不交易 |
| Transaction B部分写故障 | 不受影响 | 整笔回滚、零可见 | 无outbox、无交易 |
| Transaction C CAS/额度冲突 | 不受影响 | 已提交fact保持 | 重读latest/risk；失败写唯一阻断原因 |
| DB busy超预算 | 继续事实独立append | 当前事件fail closed或正确重试 | 不等待到deadline外下单 |
| quote stale/行情流断开 | 不受影响 | posterior仍提交 | 明确行情不可用，不同步抓取旁路 |
| CLOB definitive reject | 不受影响 | 不反写 | 释放reservation、记录reject |
| CLOB ambiguous timeout | 不受影响 | 不反写 | 标记uncertain并reconcile，禁止盲重发 |
| correction/replay超时 | active chain保留 | 最终提交正确posterior | 原deadline失效，不补历史订单 |
| 进程在intent后崩溃 | 已提交事实保留 | 已提交fact保留 | 启动后先reconcile intent，再决定是否仍可首次提交 |

### 8.12 可观测性与SLO证据

每个event必须产生统一trace identity，至少记录：

```text
canonical_event_id / sequence / target
T1..T5 + deadline + clock identity
queue_wait / parse / inference / path / projection / B-commit / C-commit / sign / network spans
artifact/checkpoint/fact/decision/intent/order IDs + sha256
particle/component count, ESS, convergence, CPU, RSS
quote age, retry/reconcile count
terminal state + na/skip/failure reason
```

SLO报告必须：

- 同时报p50/p95/p99、样本数、成功率和各终态数量；
- 按天气型、报文类型、correction、target、source-set、模型detail level和冷/热状态分层；
- 把崩溃、超时、CAS冲突和ambiguous outcome保留在对应分母；
- 使用事件时间窗口，不按请求完成时间筛掉跨窗慢请求；
- 将Shadow模拟时延和真实CLOB端到端时延分开，Shadow不得证明真实下单SLO；
- 报告生成代码、查询、数据快照和结果全部内容寻址。

### 8.13 性能与故障注入验收

晋级前至少运行：

1. 冻结真实F1逐事件回放，测量warm resident的概率时延；
2. F2前向Shadow，测量真实到达、队列、缺源与资源争用；
3. production-equivalent无真实订单压测，使用冻结CLOB响应仿真覆盖快、慢、拒绝、断链和ambiguous timeout；
4. 冷启动、artifact损坏、driver缺失、数据库busy、CAS fork、worker crash和进程重启注入；
5. 报文burst、整点/半点相邻、SPECI、correction和跨target并发；
6. Transaction B每个append/CAS/commit点与Transaction C每个claim/reservation/intent点的故障注入；
7. 网络请求发出前、发出后未响应、收到响应未落账三个crash窗口的reconcile测试；
8. 固定粒子/混合预算下的峰保真、尾部、ESS和Monte Carlo收敛测试。

不得仅在空载开发机或合成单事件上宣称达标。性能优化前后必须在同一ReplayBundle、artifact、硬件/进程配额和数值合同下证明 `P(X)`语义等价；改变粒子、阈值、数值近似或calibrator即形成新artifact并重新走准确率与Shadow验证。

### 8.14 原子切换合同

切换前置条件：

- accepted model/evaluation ADR与accepted production cutover ADR；
- 候选artifact、schema、报告算子、calibrator、hard mask和runtime二进制SHA全部冻结；
- strict-as-of/OOS、前向Shadow、确定性回放、非干扰、原子事务、故障注入和性能门全部通过；
- 当天切换时的新模型checkpoint已从canonical active event chain重建并与候选artifact绑定；
- UI、Decision Core和交易仍只识别同一 `PosteriorSSOTFact`合同，不需要第二读取路径。

切换过程：

```text
1. 选择冻结canonical sequence边界N
2. 停止为旧模型分配>N的新概率任务；AMSC事实入库继续
3. 等待旧模型≤N的Transaction B完成或明确隔离
4. 用新artifact重放到N并验证checkpoint/fact候选、hard invariant与lineage
5. 单事务CAS替换RuntimeModelManifestV1：
     old active identity → new active identity
     start_sequence = N+1
     bootstrap_checkpoint = new checkpoint at N
     fallback_identity = null
6. 新resident从N+1成为唯一production消费者
7. 删除旧模型可执行路由、注册入口和兼容分叉；历史artifact仅保留不可执行审计身份
```

任何跨边界事件只能被一个production model消费。manifest切换与新checkpoint身份必须共同CAS；不允许先改代码后改pointer、双写一段时间或按流量比例运行两套production。

### 8.15 中止、向前修复与禁止旧fallback

- 第5步manifest事务提交前发生失败，切换视为**未发生**：旧模型继续是唯一production；这叫pre-cutover abort，不是rollback；
- 第5步提交后，新模型已成为唯一production。发现缺陷时立即停止其交易副作用并fail closed，AMSC事实继续入库；
- post-cutover禁止自动或手工把旧模型当fallback重新激活，也禁止保留隐藏环境变量/路由开关；
- 恢复只能通过修复代码或形成新的已批准artifact，再执行一次新的原子向前切换；
- schema/数据修复必须以追加迁移和可重放事实为基础，不能回写历史posterior冒充原结果；
- 切换日志必须记录操作者、ADR、old/new identity、边界N、CAS结果、验证哈希和旧入口删除证明。

### 8.16 本节边界

本节只固定目标架构、SLO、事务和切换合同，不重新定义交易选桶、金额、价格或退出策略。Decision Core继续服从accepted交易合同；性能层不得借“提速”引入旧EV/Edge、旧repricing、旧stop-loss或任何第二策略口径。

上述全部预算和流程均是待开发、待压测、待accepted ADR的目标，不是当前已经达到的production证据。

## 9. 训练、校正与 live 解耦

四条数据通道独立：

```text
物理标签通道：AMSC active revision day → 事件级标签 + final physical X
弱预测通道：forecast revision as-of → 未来AMSC轨迹/final X标签 → 弱源/driver校正
物理模型通道：event prefix + as-of drivers → 后续轨迹/final X标签 → transition/observation/report训练
市场桥通道：final X + S + frozen market contract → bridge校正与市场级验证
```

未来AMSC轨迹和final X在弱预测通道中只能作为训练标签，不能回灌成该revision的输入特征。市场桥通道不能向前三条物理通道提供标签、参数或启用门。

物理状态模型不能只用每日一个final X标签训练。仅凭final X无法辨识“辐射偏差、感热分配、有效热容量、平流和冷池”分别造成了什么影响。事件级训练至少需要同时构造：

- 下一计划槽报文温度与温度增量；
- 从当前事件到日终的后续报文轨迹；
- 剩余报文最高温与剩余升温幅度；
- 当前报文是否已经成为最终 `X`；
- 降温后是否在未来计划槽重新超过当前最高值；
- 日终final physical X。

隐状态不要求拥有不可得的“真值标签”，但必须通过能量守恒、参数边界、状态平滑、事件级预测评分和特征消融约束可辨识性，禁止一个自由参数吸收所有误差。

固定规则：

- strict-as-of只约束训练、OOS和候选artifact可信度，不是live gate；
- 样本量、bridge和市场finality不得阻断AMSC入库与已批准模型运行；
- 当前已批准artifact持续live，候选artifact每日追加样本并shadow比较；
- 候选必须通过预先冻结的校准、准确率、消融和时延门槛后才能晋级；
- 晋级必须原子切换唯一主模型，旧模型随即退役，不保留运行时fallback。

### 9.1 训练样本身份

所有样本都必须绑定：

```text
station_day_identity
sample_id + schema_version
feature_as_of
feature_cutoff
label_available_at
raw/canonical/driver input IDs + sha256[]
active_revision_chain_sha256
target_definition_sha256
reporting_operator_sha256
eligibility_tier
eligibility_reasons[]
materialized_at
sample_sha256
```

`feature_as_of`决定可见输入，`label_available_at`只说明标签何时闭合。两者必须分列；materialization时刻、文件mtime、provider自报时间和日终下载时刻都不能替代feature as-of。

同一station-day可以产生多条事件样本，但独立统计单位仍是station-day。revision、correction和迟到报文必须先形成active revision chain，再物化标签；不能把已supersede旧值与corrected值同时当真。

### 9.2 五类训练样本

#### 9.2.1 `PhysicalStationDayLabelV1`

```text
station_day_identity
active_contract_slot_values[]
coverage/reportability state
final_physical_x
first/last peak slots
tie slots[]
label_na_reasons[]
```

该标签每日独立由AMSC通道形成，不等待市场、WU、bridge或订单。coverage不满足冻结合同时，final X写NA，不用插值或其他来源补齐。

#### 9.2.2 `WeakForecastRevisionSampleV1`

```text
source_id/provider/run/revision
forecast_feature_as_of
source_availability_mask/exact_source_set
raw weak temperature/driver/path payload
forecast-only context
future active AMSC trajectory label
final physical X label
```

输入context只允许季节、时效、天气预测型、站点和当时预测源组合。D0已见AMSC温度、动量、云雨、露点、final X和市场信息不得进入弱源bias、skill或相关性特征。

#### 9.2.3 `PhysicalTransitionSampleV1`

```text
target/event identity
pre-event canonical prefix O_<=t
post-event checkpoint/state representation
as-of physical driver generation
next canonical event/next contract slot labels
future 1h active trajectory labels
remaining-day active trajectory/final X labels
```

该样本训练固定`T̃/q/G_net/W/C_cp/R`状态传播和regime转移。隐状态没有伪造的“真值列”；`Ψ`中的H、C_eff、A、E等只通过weak driver、能量约束和多步预测形成条件诊断，不作为递推状态或监督真值。

#### 9.2.4 `ObservationCalibrationSampleV1`

```text
pre-event state predictive distribution
pre-event history O_<t
current canonical payload + encoding/quality
source selection/report slot role
innovation and post-event predictive checks
```

该样本训练 `P(O_t|Z_t^-)`，不能把含当前事件的post-state当输入。AMSC/AWC confirmation pairs可在独立研究层帮助估计报告差异，但live仍只消费一个canonical事件。

#### 9.2.5 `ReportingAndPosteriorSampleV1`

```text
latent/path predictive distribution
future plan-slot active reports + encoding metadata
reportability/coverage label
projected raw P(X)
final physical X
path event labels
```

该样本训练/验证报告误差、路径事件和端到端 `P(X)`校准。连续潜在峰 `X*`没有独立高频真值时只能作为latent诊断，禁止用AMSC报文X冒充 `X*` 标签。

### 9.3 事件级标签

每个合法event prefix至少派生：

| 标签 | 定义 |
|---|---|
| `next_event_temperature` | 下一active canonical事件报文温度 |
| `next_contract_slot_temperature` | 下一合同计划槽active报文温度 |
| `next_event/slot_delta` | 相对当前报文的有符号变化 |
| `future_1h_path` | 当前as-of后1小时active报文序列 |
| `future_reported_peak_margin` | 未来合同槽最高值−当前observed contract max |
| `remaining_reported_rise` | `max(0, future_peak_margin)` |
| `future_cooling_depth` | 当前后未来最低温相对当前值的回落 |
| `no_future_exceedance` | 未来合同槽最高值≤当前observed max |
| `strict_peak_already` | 未来合同槽最高值<当前observed max |
| `future_tie/new_high` | 未来等于/超过当前observed max |
| `thermal_rebound` | 按冻结幅度/持续时间定义的先降后升 |
| `new_high_after_rebound` | rebound后超过当前observed max |
| `first/last_peak_slot` | final X首次/最后出现的计划槽 |
| `final_physical_x` | 完整active合同槽报文最高值 |

标签计算代码、tie规则、rebound阈值、终局无未来槽边界和NA语义必须版本化。标签规则变化会产生新dataset identity，禁止同名覆盖。

### 9.4 泄漏与数据切分

硬规则：

1. 任何样本输入只能使用 `feature_as_of`时已存在且血缘闭合的数据；
2. final X、后续报文、correction终局和市场finality只存在于label域；
3. 同一station-day的所有revision和事件必须处于同一train/calibration/OOS split；
4. split按时间前向排列，禁止随机把同一天早报文放训练、晚报文放测试；
5. source bias state对event day `d`只能使用 `<d`的closed日；
6. 参数选择、loss weight、阈值和calibrator只使用train/calibration，不读取最终OOS/前向窗口；
7. 2×2四臂使用相同station-day split、标签和评价代码；
8. 历史回补缺真实first-actionable时间时只能进入明确auxiliary研究，不能升级为strict-as-of训练/OOS证据；
9. market S/B、价格、成交、PnL和交易结果不得进入天气模型训练。

### 9.5 分模块训练目标

#### 9.5.1 弱预测联合模型

训练：

- source×season×horizon×weather forecast regime条件bias；
- 逐源路径/最高温残差分布；
- exact source-set联合协方差与相关性；
- source availability/missingness pattern的信息量；
- 多峰天气情景权重。

先对逐源中心做冻结层级偏差校正，再用校正后残差估计source-set误差与相关性。不能一边移动中心，一边继续使用未校正残差的协方差。

#### 9.5.2 状态转移模型

以一步和多步预测边缘likelihood训练：

```math
\mathcal L_{transition}
=\lambda_1NLL(Y_{t+1})
+\lambda_2CRPS(\Delta T_{1h})
+\lambda_3EnergyScore(Path_{t:H})
+\lambda_4CRPS(X)
+\lambda_5\mathcal P_{physics}
+\lambda_6\mathcal P_{identifiability}
```

其中 `P_physics`约束能量闭合、状态边界和合法转移；`P_identifiability`惩罚无信息自由状态、病态相关和跨fold不稳定。物理penalty不能通过极大权重把状态钉死到NWP；所有λ必须预注册并通过train/calibration选择。

隐状态可用分层EM、变分推断、MCMC或particle smoothing离线估计，但OOS评分必须积分参数/状态不确定性，不能用事后平滑状态冒充当时filter结果。

#### 9.5.3 强观测模型

优化联合观测边缘log likelihood、连续innovation覆盖、类别reliability及事件后一步预测。温度、露点、云雨风压的依赖结构共同训练；逐字段loss只用于诊断/稳定训练，最终必须检验joint likelihood校准。

#### 9.5.4 报告算子

编码、修约、计划槽和revision规则尽量由合同确定，不从final X自由拟合。共享/槽级报告误差和reportability模型使用真实未来报文与缺报标签校准；不得用市场S替代AMSC报告标签。

#### 9.5.5 端到端P(X)

最终组合以离散 `P(X)`的CRPS、NLL、Brier、MAE和coverage训练/选择。final X loss只校正最终分布和共享参数，不能单独给每个隐状态贴物理真值。

### 9.6 多任务loss边界

训练必须同时保存每项未加权loss和总loss。禁止只汇报一个可任意调λ的总分。

优先级：

```text
硬物理/身份不变量
→ 概率合法与校准
→ 下一槽/1h路径
→ final physical X
→ 见顶/回升等事件
→ 隐状态解释性诊断
```

这不表示final X不重要，而是防止模型用非法概率、未来泄漏或错误路径换取偶然的最高温命中。最终晋级仍以冻结评分卡综合判断。

不同天气型、时段和source-set的loss不得按样本条数直接支配训练；需要按station-day、天气型和关键尾部分层采样/加权，并保存实际有效日数。权重只能由训练合同冻结，不能根据OOS表现事后调节。

### 9.7 概率校准

校准顺序：

```text
raw state/path posterior
→ raw discrete P(X)
→ ordered-temperature calibration
→ deterministic hard condition
→ normalize
→ PosteriorSSOTFact
```

允许使用交叉拟合的CDF/ordinal/isotonic或其他有序概率calibrator，但必须：

- 在独立calibration window拟合；
- 保持温度顺序、归一和开放尾部；
- 不用超宽核抹平真实多峰；
- 不将已观测最高温以下质量重新引入最终posterior；
- 对source-set、season、time/regime做层级收缩，样本不足回退到预注册更粗层；
- 不读取市场价格、S/B或交易结果；
- 与base model共同形成单一artifact身份，online不得二次选择calibrator。

校准前后必须同时报告CRPS、NLL、Brier、reliability、coverage、mode-collapse和尾部表现。只改善总体ECE但损坏高低尾最高温的calibrator不得晋级。

### 9.8 训练不确定性与参数posterior

候选artifact必须保存参数不确定性，而不是只保存最佳点估计：

- 层级bias和source covariance posterior/代表性draw；
- transition/regime参数posterior；
- observation/report noise posterior；
- calibration参数不确定性；
- 各参数跨fold稳定性和可辨识性。

online可加载经压缩的固定代表性参数集合，不运行MCMC。压缩前后 `P(X)`、路径和校准误差必须在冻结容忍度内等价。

### 9.9 Dataset与artifact内容寻址

```text
dataset_manifest:
  schema/code SHA
  raw/canonical/driver input SHA集合
  sample counts + independent station-day counts
  date/weather/source-set coverage
  eligibility/NA/quarantine counts
  split identities
  label contract SHA

model_artifact:
  training dataset/split SHA
  feature/target/state schemas
  source-set/missingness surfaces
  transition/observation/reporting parameters
  calibrator
  loss contract and selected hyperparameters
  deterministic numeric contract
  training/calibration/OOS reports
  code/environment SHA
  activation authority/state
```

数据、标签、代码、变量合同、loss、split、calibrator或数值算法任一变化都产生新SHA。`latest.json`只能是研究指针，不能成为production active pointer。

### 9.10 Walk-forward与OOS

评价至少包含：

- expanding/rolling-origin walk-forward；
- 按station-day聚类的bootstrap置信区间；
- season、天气型、时段、source-set、分歧强度和尾部分层；
- 当前结果空间模型与2×2四臂相同数据对照；
- 一步、路径、final X和事件概率多层评分；
- 校准前后、参数压缩前后和缺源降级前后对比。

rolling-origin产生多个fold artifact，只用于稳定性证据，不能把多个事后最优模型拼成一个production身份。最终候选必须是单一冻结artifact，在独立OOS和前向Shadow中验证。

### 9.11 日常学习与候选生命周期

```text
daily append immutable samples
→ dataset candidate
→ offline train
→ calibration
→ strict-as-of/OOS report
→ shadow candidate
→ forward Shadow
→ accepted promotion ADR
→ atomic single-model cutover
```

每日新样本不得原地修改当前production参数。可以按冻结调度产生新候选，但没有新数据、覆盖不足或OOS失败时只保持候选pending/rejected；当前批准artifact继续live。

候选状态至少为：

```text
draft
training_complete
calibration_complete
oos_evaluated
shadow_candidate
shadow_rejected
promotion_approved
retired
```

只有accepted promotion ADR和runtime manifest原子切换才能改变production active identity。

### 9.12 Live非阻断矩阵

| 离线/市场状态 | 对当前已批准物理live的影响 | 对新候选的影响 |
|---|---|---|
| 当日final X尚未闭合 | 无 | 当日标签pending |
| 某日coverage不完整 | 无 | final-X样本NA；可保留合法一步样本 |
| strict-as-of样本不足 | 无 | 阻止对应候选晋级 |
| OOS失败或未完成 | 无 | 候选rejected/pending |
| 新物理driver历史不足 | 无 | 对应Arm C/D证据不足 |
| X→S→B bridge未闭合 | `P(X)`继续 | 市场投影/OOS pending |
| 市场不存在或未final | `P(X)`继续 | 市场级标签pending |
| 训练任务失败 | 无 | 记录失败，不更换active artifact |
| registry当前active artifact有效 | 正常运行 | 与候选状态无关 |
| 当前active artifact结构/身份失效 | canonical事实继续；posterior/交易fail closed | 不是用旧模型fallback的理由 |

结论：**strict-as-of、OOS、样本量、final X、bridge和市场finality都是学习/晋级状态，不是每条AMSC live事件的交易阻断条件。** live只校验当前已批准artifact、当前输入血缘、freshness、前驱状态、概率合法性和执行deadline。

## 10. 不影响旧方案的 Shadow 隔离方案

### 10.1 核心原则

Shadow不得嵌入旧模型的同步调用链。正确拓扑是“同一份不可变证据，两套独立消费者；旧方案继续拥有唯一生产权，新方案只写隔离shadow”：

```text
AMSC Raw / CanonicalObservationEvent
        ├── 旧生产消费者
        │       └── production posterior SSOT
        │               └── Decision Core / Order
        │
        └── 新物理模型 shadow consumer
                ├── isolated PhysicalStateCheckpoint
                ├── isolated shadow posterior
                └── read-only comparison report
```

禁止形成以下调用：

```text
旧生产模型 → 同步调用新模型 → 等待返回 → 提交production posterior
```

Shadow必须满足：共享事实但不共享可变状态；共享目标但不共享写权限；独立失败、独立存储、零交易权限。旧生产链永远不得 `await shadow`。

### 10.2 权威隔离

Shadow artifact和runtime必须固定声明：

```text
activation_state = shadow_only
production_authority = false
writes_archive_db = false
writes_public_posterior = false
writes_auto_trade_decisions = false
places_orders = false
```

旧方案在cutover前继续独占：

- production posterior写入权；
- `auto_trade_decisions`写入权；
- production outbox和订单触发权；
- UI概率SSOT；
- production latest pointer。

Shadow概率不得进入 `/api/ui_snapshot`、`/api/auto-trade-criteria`、Decision Core、production outbox或CLOB client。研究读取面若后续获批，必须显式标注 `SHADOW / NON-AUTHORITATIVE / NO-TRADE`。

### 10.3 输入与as-of隔离

Shadow通过独立只读cursor消费既有不可变事件，不修改生产消费者offset。每次更新只允许读取：

- 当前 `source_observation_id` 与 `canonical_event_id`；
- 原始对象ref/SHA、`first_actionable_seen_at` 和valid time；
- 该时刻已经冻结且可用的弱源generation；
- 该时刻已经冻结且可用的物理驱动generation；
- 上一个合法Shadow状态checkpoint。

报文到达后新抓到的NWP不得倒填为该报文时已经可见的输入。某类物理驱动缺失时，Shadow必须记录availability mask并继续降级推理；不得等待同步抓取、借用后续generation或阻断旧生产模型。

旧模型和新Shadow公平比较时必须共享：

- 相同物理目标 `X`；
- 相同温度桶和hard mask合同；
- 相同canonical observation；
- 相同证据截止时刻；
- 相同final physical X标签。

### 10.4 状态与写入隔离

Shadow使用独立SQLite文件和独立cursor，例如：

```text
data/shadow/physical_state_space_bayes_v1.sqlite
```

该库只能保存：

- Shadow消费cursor；
- PhysicalStateCheckpoint；
- Shadow posterior和路径摘要；
- artifact、输入lineage和availability mask；
- 推理时延、错误和对比记录。

Shadow严禁写入：

- `data/archive.db`；
- production posterior表或latest pointer；
- `auto_trade_decisions`；
- production outbox；
- order lifecycle；
- bot或monitor生产信号文件。

幂等身份至少绑定：

```text
shadow_model_artifact_id
+ canonical_event_id
+ previous_shadow_checkpoint_id
+ source_generation_sha
+ physical_driver_generation_sha
```

同一身份只能产生一次有效Shadow checkpoint；重放必须得到相同结果。

### 10.5 物理驱动generation隔离

新增辐射、云雨、热通量、土壤、边界层和周边网格时，不能直接改变旧生产generation的字段与完整性语义。必须形成独立、带父血缘的派生generation：

```text
existing approved weak generation
        ├── 旧模型继续原样读取
        └── parent lineage
                 ↓
physical_driver_generation_v1
```

`physical_driver_generation_v1`至少绑定：

- 原始GRIB object ref/SHA；
- provider/model/run/valid-time；
- decoder和变量合同版本；
- 辐射、云雨、热通量、土壤和PBL变量；
- 周边网格窗口与平流派生规则；
- 瞬时、累计、区间平均语义及单位；
- availability mask和缺失原因。

增加GRIB变量、改变空间窗口或更改累计量解码规则均属于新artifact身份，不得按同名字段复用旧校准参数。

### 10.6 进程与资源隔离

不得新增第二个LaunchDaemon。Shadow child/resident worker只能由唯一 `system/com.weather.monitor.daemon` 托管，并满足：

- 独立进程、内存和SQLite；
- 不持有production archive写锁；
- 不在报文热路径执行网络请求；
- 有独立CPU、内存、队列和单次推理预算；
- Shadow失败只写隔离错误记录，不向旧链路抛异常；
- Shadow积压时按canonical顺序追赶，不阻塞后续AMSC事实入库；
- 提供只停止Shadow的独立kill switch，不重启、不改变旧模型。

建议的独立开关：

```text
PHYSICAL_STATE_SPACE_SHADOW_ENABLED=0|1
```

### 10.7 事件级Shadow时序

```text
T1  完整AMSC候选payload首次到达并打first_actionable_seen_at
T2  不可变raw归档与CanonicalObservationEvent形成
    ├── 旧生产消费者按原路径立即运行
    └── Shadow只读cursor异步发现该event

S1  冻结event as-of输入和availability mask
S2  读取上一个PhysicalStateCheckpoint
S3  读取当时已存在的physical_driver_generation
S4  更新固定`T̃/q/G_net/W/C_cp/R`状态并重加权driver情景
S5  生成未来潜在温度路径
S6  应用同版本AMSC报告算子，投影Shadow P(X)并执行hard mask
S7  写入隔离Shadow checkpoint
S8  按canonical_event_id与旧posterior配对评估
```

Shadow延迟、缺源、异常或停机均不得改变旧模型结果、时延和订单。

### 10.8 分阶段Shadow

1. **S0 离线确定性回放**：使用既有AMSC事件验证状态转移、幂等、hard mask、无未来穿透和无重复计数，不启动常驻进程。
2. **S1 现有数据实时Shadow**：只使用已存在的AMSC、太阳几何、温度轨迹、当前弱预测和压力层特征，先验证状态空间骨架。
3. **S2 扩展物理驱动**：依次引入辐射、云雨、热通量、土壤、PBL和周边网格；每组特征独立消融，禁止一次性混入后无法归因。
4. **S3 Resident时延Shadow**：预热情景和轨迹，测量 `T1→shadow checkpoint`，只能模拟decision，不连接CLOB。
5. **S4 候选晋级评估**：以strict-as-of回放和前向Shadow共同检验准确率、校准度、反应领先时间、稳定性与时延。

Strict-as-of和OOS只决定新模型能否晋级，不构成旧模型live gate。

### 10.9 新旧配对与比较合同

每条配对记录至少包含：

```text
event_date
canonical_event_id
as_of
old_model_artifact_id
new_model_artifact_id
old_posterior_checkpoint_id
new_shadow_checkpoint_id
old_input_lineage
new_input_lineage
final_physical_x
```

必须比较：

- 最高温MAE、CRPS、NLL、Brier与桶概率校准；
- 峰值时间、见顶概率、重新升温和终止识别；
- 云雨、风向和能量突变后的正确反应领先时间；
- 单条报文翻转、错误见顶和反复横跳次数；
- 缺源时的置信度退化；
- 推理p50/p95/p99与资源占用。

“反应更早”必须同时被final X和后续轨迹证明方向正确；更早但误报更多不能算提升。

比较必须分成两层，禁止把“新增数据源收益”和“状态空间结构收益”混在一起：

1. **同证据对照**：旧模型与新模型只使用相同as-of输入，判断状态空间结构本身是否改善；
2. **增量驱动消融**：在新模型中逐组加入辐射、热通量、PBL和周边网格，判断每类新数据的独立贡献和副作用。

若新模型只在增加额外数据后改善，结论只能是“新增数据组合有效”，不能宣称状态空间结构本身优于当前模型。若当前结果空间模型在冻结主指标上更优，则继续保持其唯一production权威，新模型不得因第一性原理叙事而晋级。

### 10.10 Shadow非干扰验收与切换边界

启用Shadow前必须以可失败审计证明：

- Shadow开/关不改变production posterior；
- Shadow开/关不改变`auto_trade_decisions`、outbox和订单数量；
- Shadow崩溃、超时或数据缺失不增加旧链路时延；
- Shadow数据库锁不涉及`archive.db`；
- Shadow网络或解码失败不传播到生产消费者；
- Shadow积压不阻塞AMSC后续报文；
- Shadow输出不存在production latest pointer或交易读取路径；
- 停止Shadow后旧模型继续正常运行。

晋级时禁止流量分配式双生产。切换前后身份必须是：

```text
cutover前：旧模型 = 唯一production；新模型 = 纯Shadow
cutover后：新模型 = 唯一production；旧模型 = 退出生产
```

生产切换必须经过accepted model ADR和accepted cutover ADR，原子替换runtime manifest；切换后删除旧模型生产入口和兼容分叉，不设置运行时自动fallback。

### 10.11 P0-S独立Shadow数据库合同

P0-S的建议默认路径为：

```text
data/shadow/physical_state_space_shadow.db
```

路径只是待开发默认值；真正身份由数据库内manifest决定，不能靠文件名判断。数据库必须带现有accepted隔离哨兵：

```text
v2_shadow_runtime_manifest.activation_state = isolated_shadow_only
```

所有writer在连接后和提交前都必须重验哨兵，并硬拒绝 `data/archive.db`、符号链接到production archive、无哨兵数据库或哨兵状态漂移。P0-S不得持有production archive写连接，只能以只读pinned snapshot读取canonical事件和旧posterior用于配对。

#### 10.11.1 复用的核心表语义

为保证未来可迁移和与现有accepted架构一致，P0-S复用以下表的身份与核心语义，不另造同义表：

| 表 | P0-S用途 | 硬约束 |
|---|---|---|
| `canonical_observation_events` | 不可变镜像输入 | event id、payload hash和T1必须与来源一致；只追加 |
| `canonical_observation_event_sources` | 来源血缘镜像 | 不重新选择primary/substitute |
| `strong_observation_updates` | 保存本事件联合观测更新摘要 | 一事件/一前驱/一artifact最多一条 |
| `posterior_checkpoints` | 保存P0及增量posterior索引 | 线性前驱、sequence和artifact CAS |
| `event_fact_history` | 隔离Shadow的PosteriorSSOTFact历史 | 不得被production UI或交易读取 |
| `event_fact_latest` | 隔离Shadow的唯一latest pointer | 只在Shadow Transaction B-S推进 |

这些同名表存在于独立数据库不代表第二production SSOT；它们必须由隔离manifest、独立路径、零生产读者和零交易权限共同证明为Shadow。

#### 10.11.2 P0-S新增表

新增表均使用 `_shadow` 后缀并只存在于隔离数据库：

```text
physical_state_model_artifacts_shadow
  artifact_id PK
  artifact_sha256 UNIQUE
  schema_version
  state_schema_json + sha256
  transition_schema_json + sha256
  observation_schema_json + sha256
  reporting_operator_schema_json + sha256
  numeric_determinism_contract_json + sha256
  state                      # candidate | shadow_approved | retired
  created_at

physical_state_model_activations_shadow
  target_scope
  activation_generation
  artifact_id + artifact_sha256
  activated_at
  retired_at?
  PRIMARY KEY(target_scope, activation_generation)
  UNIQUE active artifact per target_scope

physical_driver_generations_shadow
  driver_generation_id PK
  target_identity
  decision_as_of
  weak_forecast_snapshot_id + sha256
  source_availability_mask
  exact_source_set_json
  generation_json + sha256
  created_at

physical_driver_generation_members_shadow
  driver_generation_id
  source_id
  source_revision_id
  raw_object_id + sha256
  valid_time_start/end
  variable_contract_version
  member_json + sha256
  PRIMARY KEY(driver_generation_id, source_id, source_revision_id)

physical_observation_likelihoods_shadow
  likelihood_id PK
  strong_observation_update_id UNIQUE
  canonical_observation_event_id
  previous_checkpoint_id + sha256
  pre_event_state_sha256
  observation_payload_sha256
  likelihood_json + semantic_sha256
  normalizer_log_constant
  artifact_id + sha256
  created_at

physical_state_checkpoint_payloads_shadow
  posterior_checkpoint_id PK
  representation
  component_count
  component_payload_json
  semantic_state_sha256
  raw_numeric_sha256
  effective_sample_size
  normalization_error
  uncertainty_breakdown_json
  projection_diagnostics_json
  created_at

physical_state_shadow_cursors
  source_stream_id PK
  last_canonical_sequence
  last_canonical_event_id
  last_source_snapshot_sha256
  cursor_generation
  updated_at

physical_state_shadow_pairings
  canonical_observation_event_id
  current_production_posterior_id + sha256
  physical_shadow_checkpoint_id + sha256
  evidence_parity_state
  extra_driver_set_json
  paired_at
  PRIMARY KEY(canonical_observation_event_id, physical_shadow_checkpoint_id)

physical_state_shadow_latency_packets
  packet_id PK
  canonical_observation_event_id
  T1_first_actionable_seen_at
  T2_shadow_event_append_at
  T3_shadow_checkpoint_commit_at
  elapsed_ms
  artifact_id + sha256
  state
  created_at
```

`component_payload_json`必须使用artifact定义的有界类型schema；不得成为任意JSON垃圾桶。每个JSON列必须有对应schema版本和内容哈希，未知字段、单位不匹配、NaN/Infinity或缺少NA原因都必须在事务前拒绝。

#### 10.11.3 明确禁止的P0-S表和权限

P0-S第一阶段不得创建或写入：

- `auto_trade_decisions`；
- position、capital、risk reservation；
- `posterior_execution_intents`、order或fill表；
- 可被生产Decision Core领取的outbox；
- production `event_fact_latest`或任何UI缓存；
- 独立网络抓取队列、scheduler或第二个LaunchDaemon。

S3只允许在隔离库记录“若进入Decision Core时是否仍在3秒deadline内”的模拟资格，不生成真实decision/intent，也不连接CLOB。

#### 10.11.4 两个隔离事务

```text
Shadow Transaction A-S：
  append canonical event mirror
  + append source relations
  + CAS advance shadow cursor

Shadow Transaction B-S：
  append physical_observation_likelihoods_shadow
  + append strong_observation_updates
  + append posterior_checkpoints
  + append physical_state_checkpoint_payloads_shadow
  + append event_fact_history PosteriorSSOTFact
  + CAS advance isolated event_fact_latest
```

配对和延迟包可在B-S提交后追加，失败不能回滚posterior，但该事件不得计入配对评估或SLO样本。推理不得在B-S事务内运行；事务内只做哨兵复验、前驱/sequence/artifact CAS和短写入。任何一步失败必须使B-S零行可见。

### 10.12 确定性内存回放合同

确定性回放的目标不是“结果大致相近”，而是证明相同输入、artifact和数值合同产生相同的语义状态与 `P(X)`。

#### 10.12.1 冻结回放包

```text
PhysicalStateReplayBundleV1
  target_identity
  P0 initialization input + sha256
  ordered canonical events[] + per-event sha256
  weak forecast snapshot + sha256
  physical driver generation + sha256
  model artifact + sha256
  reporting operator + sha256
  hard mask version
  numeric determinism contract
  expected final X?           # 仅评价使用，不进入推断
```

事件排序唯一为 `canonical_sequence`；`valid_time`、`first_actionable_seen_at`或文件顺序不得在并列时自行重排。回放只能读取事件as-of时已经冻结的driver generation，禁止访问当前网络、当前系统时间、最新文件或日终final X。

#### 10.12.2 数值确定性

- 随机种子固定为 `SHA256(target_identity + artifact_sha256 + P0_sha256)`，每事件子种子再绑定 `canonical_event_id + previous_checkpoint_sha256`；
- 粒子、regime、source和温度桶使用冻结稳定排序；
- 重采样算法、触发阈值和保峰下限属于artifact身份；
- 并行归约顺序固定，不允许线程调度改变权重求和；
- 概率和状态量在计算中保持足够精度，哈希前按artifact定义的单位与quantization canonicalize；
- 时间戳、运行耗时、PID和数据库rowid不得进入语义哈希；
- `semantic_state_sha256`用于跨重放等价，`raw_numeric_sha256`只用于同构建诊断；
- 跨平台无法保证原始浮点逐bit一致时，仍必须保证canonical quantized semantic hash一致，且quantization尺度不得由调用方临时修改。

#### 10.12.3 必须通过的回放矩阵

| 测试 | 操作 | 必须证明 |
|---|---|---|
| P0重复构建 | 相同初始化包运行两次 | P0语义哈希与 `P(X)` 完全一致 |
| 单事件retry | 同event、前驱和artifact执行两次 | 第二次幂等返回既有结果，零新增行 |
| 同键漂移 | 修改payload任一字段但复用幂等键 | fail closed，latest不推进 |
| 增量/全量等价 | 逐事件增量运行，再从P0全量回放 | 每一步及最终state/`P(X)`语义哈希一致 |
| 无自确认 | 对比评分前后trajectory state | 当前 `o_t`不出现在自身pre-event特征中 |
| 重复报文 | 输入同canonical event两次 | likelihood、checkpoint和fact均不重复 |
| 乱序报文 | 先输入更高sequence再输入较低sequence | 较低sequence隔离，不能改写链 |
| correction | 输入合法superseding event | 新增一次明确更新，保留旧事件血缘，不原地覆盖 |
| hard lower bound | 合同计划槽已报33℃ | 所有 `X<33℃` 质量严格为0并重新归一 |
| 非计划SPECI | SPECI温度高于当前计划槽最高值 | 可更新状态，但不直接抬高合同hard lower bound |
| 多峰保留 | 输入分离的低温/高温天气情景 | 无新判别证据时不得塌成虚假中间单峰 |
| 物理证据收敛 | 依次加入云雨、动量和风向事件 | 情景权重按联合likelihood变化，不回写弱源skill |
| 缺源组合 | 移除一个弱源并显式更新mask | 仍产出 `P(X)`，不确定性上升或写明不升原因 |
| bridge消融 | 同一物理输入下切换bridge open/closed | `P(X)`与state哈希不变，仅市场投影变化 |
| 未来穿透 | 在回放目录放入更晚forecast/final X | 结果不变，证明未读取未来数据 |
| 故障注入 | 在B-S每个insert/latest CAS前抛错 | 整笔零可见；重试后只提交一次 |
| Shadow非干扰 | Shadow关闭、崩溃、积压和缺源 | production posterior、decision、订单和时延不变 |

#### 10.12.4 回放通过口径

回放报告必须内容寻址，并绑定：

```text
replay_bundle_sha256
model_code_sha256
schema_code_sha256
artifact_sha256
test_code_sha256
per_event_incremental_semantic_sha256[]
per_event_full_replay_semantic_sha256[]
physical_x_distribution_sha256[]
fault_injection_results[]
```

代码、schema、artifact、测试或真实事件链任一变化，旧报告立即stale。合成事件可以证明算法性质，但不能替代真实canonical事件的增量/全量等价报告，也不能关闭生产晋级门。

### 10.13 S0回放输入输出与测试夹具

#### 10.13.1 最小ReplayBundle示例

以下仅为schema示例，所有ID、哈希和数值均为非生产占位：

```json
{
  "schema": "physical_state_replay_bundle_v1",
  "target_identity": "physical_weather_day_v1:ZUUU:YYYY-MM-DD",
  "initialization": {
    "decision_as_of": "YYYY-MM-DDT00:00:00Z",
    "prior_context_id": "prior_example",
    "prior_artifact_sha256": "sha256:...",
    "weak_forecast_snapshot_id": "weak_example",
    "weak_artifact_sha256": "sha256:...",
    "source_availability_mask": {
      "ECMWF": true,
      "ICON": true,
      "GFS": true,
      "JMA": false,
      "TAF": true,
      "WU": false,
      "TWC": false
    },
    "physical_driver_generation_id": "driver_example",
    "physical_driver_generation_sha256": "sha256:...",
    "model_artifact_id": "state_model_example",
    "model_artifact_sha256": "sha256:...",
    "reporting_operator_version": "amsc_contract_operator_example",
    "reporting_operator_sha256": "sha256:...",
    "hard_mask_version": "observed_contract_max_example"
  },
  "canonical_events": [
    {
      "canonical_sequence": 1,
      "canonical_observation_event_id": "event_example_001",
      "event_sha256": "sha256:...",
      "report_valid_at": "YYYY-MM-DDT01:00:00Z",
      "first_actionable_seen_at": "YYYY-MM-DDT01:00:01Z",
      "is_contract_report_slot": true
    }
  ],
  "numeric_determinism_contract": {
    "seed_derivation": "sha256_target_artifact_p0_event_v1",
    "component_sort": "regime_scenario_component_id_v1",
    "resampling": "deterministic_systematic_mode_preserving_v1",
    "semantic_quantization_version": "physical_state_semantic_quantization_v1"
  },
  "evaluation_only": {
    "final_physical_x": null,
    "market_settlement_s": null
  }
}
```

`evaluation_only`在运行推断时必须被类型层完全剥离；即使字段存在，composer也无法访问。测试必须证明填入、删除或修改final X/S不会改变任何checkpoint。

#### 10.13.2 最小逐事件输出示例

```json
{
  "schema": "physical_state_replay_event_result_v1",
  "canonical_observation_event_id": "event_example_001",
  "previous_checkpoint_sha256": "sha256:...",
  "pre_event_state_sha256": "sha256:...",
  "joint_observation_likelihood_sha256": "sha256:...",
  "post_event_state_semantic_sha256": "sha256:...",
  "physical_x_distribution": {
    "31": 0.00,
    "32": 0.10,
    "33": 0.24,
    "34": 0.41,
    "35": 0.19,
    "36": 0.06
  },
  "physical_x_distribution_sha256": "sha256:...",
  "observed_contract_max": 32.0,
  "mode_summary": [
    {"mode_id": "cloud_rain_low", "weight": 0.34},
    {"mode_id": "clear_hot_high", "weight": 0.66}
  ],
  "effective_sample_size": 48.2,
  "normalization_error": 0.0,
  "market_projection_state": "na",
  "market_projection_na_reason": "x_to_s_to_b_bridge_unavailable"
}
```

示例概率不构成任何真实天气判断或校准参数。正式golden输出必须由冻结fixture、artifact和实现生成，并以内容寻址文件保存，不能手工抄入测试。

#### 10.13.3 三层夹具

| 层级 | 数据 | 用途 | 能关闭的门 |
|---|---|---|---|
| F0合成物理夹具 | 人工构造、边界明确的事件序列 | 验证数学不变量、模式、hard mask、SPECI、correction和故障注入 | 仅算法正确性 |
| F1冻结真实历史夹具 | 真实canonical AMSC事件及当时可见预测快照 | 验证as-of、增量/全量等价、真实字段编码和历史误差 | 回放与离线性能 |
| F2前向Shadow夹具 | Shadow启动后真实到达、不可回填的事件 | 验证非干扰、真实时延、缺源和运行稳定性 | 前向运行/SLO；仍不自动授权生产 |

F0不能证明真实准确率，F1不能单独证明前向时延，F2样本不足不能阻断当前production live，但会阻止新模型晋级。

#### 10.13.4 F0必备物理场景

1. **晴热持续升温**：净加热和动量均支持高温峰，高温峰权重应上升；
2. **低云/降水终止**：温度尚未明显下降，但云雨和冷池证据已到达，高温峰应先概率下降；
3. **降温而非只预测升温**：冷平流/降水后未来一小时增量分布应允许显著负值；
4. **短时云遮后重新放晴**：模型不得第一次降温就永久判定见顶，必须保留重新升温路径；
5. **盆地逆温与混合开启**：相同辐射下，不同有效混合深度产生不同温度响应；
6. **暖平流弱辐射升温**：太阳辐射较弱时仍可因动力输送升温，防止辐射单因子化；
7. **高辐射湿地表低效率**：净辐射较强但潜热分流大，防止自由Efficiency伪解释；
8. **已观测最高温hard mask**：合同槽已报值严格裁掉更低最高温桶；
9. **非合同SPECI高温**：更新状态但不直接改变合同hard lower bound；
10. **多源强分歧**：校正后低温/高温情景仍分离时保持多峰，不制造中间峰；
11. **缺源组合**：显式mask改变信息量但不阻断 `P(X)`；
12. **合法correction与重复报文**：correction追加更新，重复事件幂等。

每个场景同时运行当前结果空间模型和目标状态空间模型不一定有意义；F0主要验证目标模型的结构不变量。模型优劣只能在F1/F2相同真实证据上评价。

### 10.14 Shadow公平对照与晋级评分卡

#### 10.14.1 2×2四个实验臂

```text
Arm A：当前批准结果空间Bayes
       当前as-of证据全集

Arm B：目标状态空间Bayes——同证据臂
       与Arm A完全相同的温度、轨迹、动量、云雨露点风和弱预测

Arm C：当前结果空间Bayes——扩展驱动臂
       Arm A + 辐射/热通量/PBL/周边网格等新增as-of物理驱动

Arm D：目标状态空间Bayes——扩展驱动臂
       Arm B + 与Arm C完全相同的新增as-of物理驱动
```

解释规则：

- `B−A`只表示状态空间结构、时序和报告算子在当前证据集上的净收益；
- `C−A`表示新增物理驱动在当前结果空间结构中的边际收益；
- `D−B`表示新增物理驱动在目标状态空间结构中的边际收益；
- `D−C`表示状态空间结构在扩展证据集上的净收益；
- `D−A`是目标最终组合的总收益，但不能单独证明收益来自结构还是新增数据；
- 交互效应 `(D−B)−(C−A)`回答状态空间结构是否更善于利用新增物理驱动；
- 四个臂必须绑定同一target、canonical event、hard mask、目标 `X`、报告槽和评价标签；
- Arm B缺少Arm A已有的任一物理字段时，该配对事件不得进入结构优劣结论；
- Arm C与Arm D的新增driver集合、as-of和缺失mask必须完全相同；
- Arm C必须是预注册、联合消费新增driver的结果空间扩展，而不是为故意做弱对照而只加几个独立LR；
- 若当前结果空间结构确实无法合法表达某新增driver，则该driver的“结构必要性交互效应”记为 `not_identifiable`，不得把Arm C缺失当作Arm D自动胜出；
- 四臂使用相同训练日、标签、as-of边界、校准窗口和预先冻结的调参预算，测试/前向窗口对所有臂保持盲态；
- 新增驱动必须逐组消融，不能一次性加入后只报告总收益。

每条配对记录必须生成 `evidence_parity_manifest`：

```text
target_identity
canonical_event_id
evaluation_as_of
arm_a/b/c/d_artifact_ids + sha256[]
training_dataset_sha256
calibration_window_sha256
hyperparameter_budget_contract_sha256
shared_evidence_ids + sha256[]
arm_a_only_evidence[]          # 必须为空才可进入B−A
arm_b_only_evidence[]          # 必须为空才可进入B−A
arm_c_incremental_driver_ids + sha256[]
arm_d_incremental_driver_ids + sha256[]  # 必须与Arm C完全相同
target_definition_sha256
reporting_slot_contract_sha256
hard_mask_input_sha256
final_physical_x
```

#### 10.14.2 评分维度

| 维度 | 主指标 | 辅助诊断 | 解释 |
|---|---|---|---|
| 最高温点预测 | posterior mean/median MAE、RMSE | exact bucket accuracy、±1桶 accuracy | 回答最终最高温是否更准 |
| 概率质量 | CRPS、NLL/log score | Brier、reliability、PIT/rank histogram | 防止只提高命中却概率失真 |
| 事件更新质量 | `ΔCRPS_t`、`ΔNLL_t` | posterior mass向final X移动量 | 判断每次报文更新方向是否正确 |
| 未来路径 | 下一槽温度/增量误差 | 1h升降温、剩余升温分布 | 判断状态是否真的可预测而非只拟合final X |
| 见顶/重新升温 | peak与reheating Brier | 提前量、漏报、误报 | 同时覆盖终止和再启动 |
| 多峰 | mode-collapse rate | 峰数、峰距、峰权稳定性 | 防止宽核或单Gaussian制造中间值 |
| 反应速度 | 正确反应领先时间 | 首次持续正确转向事件序号 | 只奖励最终被后续轨迹/最终X证明正确的提前反应 |
| 运行速度 | `T1→T3` p50/p95/p99 | filter/project/commit分段耗时 | 判断能否给交易保留时间 |
| 稳定与安全 | 崩溃率、重放等价率 | ESS、归一误差、hard mask违规 | 属于硬门而非可权衡分数 |

事件更新指标定义为：

```math
\Delta CRPS_t
=CRPS(P_t,X_{final})-CRPS(P_{t^-},X_{final})
```

`ΔCRPS_t<0`表示该事件更新使预测更接近最终X；`ΔCRPS_t>0`表示错误方向或过度更新。该标签只用于日后评价，不得进入当天推断。

“正确反应领先时间”必须区分：

1. **计算时延**：从 `T1=first_actionable_seen_at` 到Shadow checkpoint提交；
2. **信息领先**：模型首次持续把posterior向最终正确方向移动，相对当前模型提前多少；
3. **温度确认领先**：云雨、风、辐射等证据触发正确更新，相对下一次明确温度斜率变化提前多少。

一次偶发跳动不算持续反应。默认需要连续两个checkpoint保持改善，或保持到当天终局；具体规则必须随评分artifact冻结。

#### 10.14.3 分层与置信区间

所有指标至少按以下维度分层：

- 季节；
- 晴热、云遮、降水、冷池、平流转换、重新升温等天气型；
- D0当地时段与距预计峰值时间；
- exact source set与缺源mask；
- 当前预测源分歧强度；
- 已观测最高温所在桶；
- 计划METAR与非计划SPECI；
- 数据完整度和物理驱动可用性。

不得只汇报总体平均。晋级结论必须使用按station-day聚类的bootstrap置信区间，避免同一天多条报文被当成独立样本夸大显著性。样本量不足的层写 `insufficient_evidence`，不能以零差异或自动通过处理。

#### 10.14.4 硬门、主门与观察项

**硬门——任一失败即不得晋级：**

- hard mask、概率归一、幂等、无自确认、无未来穿透全部100%通过；
- 增量/全量semantic replay等价全部通过；
- Shadow非干扰、隔离哨兵、零production写入和零交易权限全部通过；
- 不存在第二public latest、第二composer或运行时旧模型fallback；
- `T1→T3`满足冻结预算，且端到端模拟仍给3秒下单链保留足够预算；
- 任一关键分层不存在灾难性概率失真或系统性hard事实违规。

**主门——比较预测能力：**

- Arm B相对Arm A必须在冻结主指标组合上达到非劣，并至少在CRPS、NLL、最高温MAE或正确反应领先时间中的一项形成有置信区间支持的实质改善；
- 改善不能仅来自少数极端日，同时在主要季节、天气型和源组合中出现普遍退化；
- Arm D若晋级为最终候选，必须同时报告 `B−A`、`C−A`、`D−B`、`D−C`、`D−A`及交互效应，证明结构与新增驱动各自的贡献；
- 若Arm C优于Arm D，说明“当前结果空间模型＋新增驱动”可能是更优方向，不能因既定重构叙事强推状态空间模型；
- 点预测改善不能以严重概率失校准、错误见顶增加或多峰塌缩换取。

**观察项——不单独阻断当前production live：**

- strict-as-of训练覆盖、OOS天数和新物理driver历史样本量；
- `X→S→B bridge`覆盖；
- 市场级交易结果。

这些观察项可以阻止新artifact或新市场投影晋级，但不能阻断当前已批准模型处理AMSC事实和物理 `P(X)`。

#### 10.14.5 阈值冻结原则

本研究文档不凭经验任意写死“提升3%即可上线”等数字。正式Shadow开始前，必须在不知道前向测试结果的情况下，由accepted evaluation ADR一次性冻结：

```text
主指标与优先级
非劣容忍度
最小实质改善幅度
bootstrap方法与置信水平
关键分层和最低有效样本量
错误见顶/错误转向上限
多峰塌缩上限
T1→T3与各阶段时延预算
缺失数据和insufficient_evidence处理
停止、延长Shadow和拒绝晋级规则
```

冻结后不得根据结果挑选更有利指标、删掉失败天气型或移动阈值。阈值变更必须产生新evaluation artifact并重新开始独立前向窗口，防止用测试集反复调门槛。

### 10.15 P0可实施Schema/API迁移包

本节把P0从概念合同收敛为可直接拆任务、编码和验收的Shadow实施规格。它仍是待开发设计，不授权创建production writer、启动resident worker、改变当前posterior或接入交易。

#### 10.15.1 P0交付目标与停止线

P0只交付五件事：

1. 唯一的类型/schema/单位/枚举/哈希合同；
2. 最小隔离Shadow数据库及forward-only迁移；
3. 无数据库、无网络、无时钟读取的纯函数API边界；
4. 内容寻址ReplayBundle、确定性runner和F0夹具；
5. schema、CAS、幂等、故障注入与非干扰审计。

P0不得交付：

- 可被production或UI读取的第二latest pointer；
- live resident消费、Decision Core、risk、intent、order或CLOB连接；
- 用占位物理参数生成并冒充候选的posterior；
- 为通过测试而回退到当前结果空间模型；
- 运行时兼容新旧两套state schema的分支路由。

P0结束时允许S0离线回放和隔离数据库结构测试；没有P1–P4真实artifact时，不得启动前向状态空间Shadow。

#### 10.15.2 现有代码锚点与复用边界

| 当前文件 | P0处理 | 原因 |
|---|---|---|
| `canonical_observation_runtime.py` / `canonical_observation_writer.py` | 只复用既有canonical身份和只读事件结果 | Bayes层不得重新选择AMSC/AWC或重解析事实 |
| `sequential_bayes_schema_contract.py` | 抽取canonical核心DDL为中性共享合同；不得复制两份DDL | 现文件混合了canonical、posterior和交易期表语义 |
| `p10_shadow_runtime_schema.py` | **禁止作为P0-S initializer调用** | 它会创建decision、intent、prepared-cycle等P10表，超出P0最小权限 |
| `initial_posterior_checkpoint_shadow.py` | 只参考现有P0身份、幂等和血缘；目标state payload另建强类型 | 当前P0仍服务结果空间联合格，不代表目标物理状态schema |
| `sequential_bayes_filter_shadow_runtime.py` | 仅作为Arm A/current结构对照 | 目标composer不得import、fallback或包装旧filter形成混合内核 |
| `sequential_bayes_shadow_writer.py` / `sequential_bayes_public_ssot_shadow_writer.py` | 只参考CAS与append模式；目标使用独立repository | 防止目标checkpoint被旧字段和旧public writer语义约束 |
| `p2_canonical_amsc_forward_writer.py`及mirror | 只读消费既有canonical append结果 | 新Shadow不得增加第二production canonical writer |

抽取共享canonical DDL时必须先证明schema文本、列、约束和既有测试哈希不变；当前消费者继续使用同一个中性合同。目标P0-S只创建canonical镜像与物理状态表，不顺带创建任何交易表。

#### 10.15.3 目标Python包结构

新代码集中到单一包，禁止继续向仓库根目录堆叠互相import的runtime文件：

```text
physical_state_bayes/
  contracts/
    schema_ids.py
    enums.py
    units.py
    identities.py
    canonical_codec.py
    initialization.py
    observation.py
    state.py
    projection.py
    checkpoint.py
    errors.py
  core/
    initialize.py
    transition.py
    observation_likelihood.py
    condition.py
    post_event.py
    future_paths.py
    reporting_operator.py
    project_x.py
    invariants.py
  artifacts/
    manifest.py
    registry.py
    validator.py
  persistence/
    shared_canonical_schema.py
    shadow_migrations.py
    shadow_repository.py
    transaction_as.py
    transaction_bs.py
  replay/
    bundle.py
    runner.py
    semantic_hash.py
    report.py
  adapters/
    canonical_event_reader.py
    prior_reader.py
    weak_snapshot_reader.py
    driver_generation_reader.py
  shadow/
    cursor.py
    pairing.py
    latency.py
```

依赖方向只允许：

```text
contracts ← core ← replay
contracts ← artifacts
contracts ← persistence
contracts/core ← adapters ← shadow orchestration
```

`core`不得import SQLite、HTTP、filesystem、market、策略、UI、当前filter或wall clock；`persistence`不得实现Bayes；`adapters`只能把现有不可变证据转换为强类型输入；`shadow`只能编排，不能重算另一份概率逻辑。

CLI只做薄入口：

```text
scripts/plan_physical_state_shadow_schema.py       # 默认只读plan
scripts/apply_physical_state_shadow_schema.py      # 显式隔离apply
scripts/build_physical_state_replay_bundle.py      # 只读构包
scripts/run_physical_state_replay.py               # 内存回放
scripts/audit_physical_state_shadow.py              # 只读验收
```

#### 10.15.4 强类型与canonical编码

核心边界禁止裸 `Mapping[str, Any]` 穿透。adapter完成解析后必须形成不可变强类型；可选字段使用显式 `Present(value) | Missing(reason) | Invalid(reason)`，不能用 `None/0/空字符串` 混合表达。

所有携带证据、状态或结果的公共类型必须按适用性含：

```text
schema_id
schema_version
identity fields
as_of / valid interval
canonical units
lineage IDs + sha256
quality / NA reasons
payload_sha256
```

canonical编码固定：

- UTF-8、稳定key排序、无多余空白；
- 禁止NaN、Infinity、隐式字符串转数值和本地时区日期；
- 时间统一带UTC offset，并另存目标local day合同；
- 状态计算使用artifact冻结精度，语义哈希前按字段单位和quantization合同规范化；
- 概率向量按冻结温度桶/组件顺序编码；
- unknown字段、unknown enum、单位漂移和schema版本不匹配一律fail closed；
- DB rowid、materialized_at、PID、耗时和日志字段不得进入semantic hash。

身份不得由调用方任意传入后直接信任。建议固定：

```text
artifact_id   = prefix + SHA256(canonical artifact manifest)
checkpoint_id = prefix + SHA256(target + sequence + previous hash + event/artifact/payload hashes)
likelihood_id = prefix + SHA256(event + pre-state + observation artifact)
fact_id       = prefix + SHA256(checkpoint + physical P(X) + target/reporting contract)
bundle_id     = prefix + SHA256(replay manifest + ordered member hashes)
```

#### 10.15.5 纯函数API

P0冻结接口，不提前冻结P2具体数值算法：

```text
initialize_target(
  InitializationEnvelope,
  FrozenModelArtifact
) -> Accepted[InitialCheckpointCandidate] | Blocked[ClosedReason]

predict_to_event(
  PreviousCheckpoint,
  EventTime,
  FrozenDriverSlice,
  FrozenModelArtifact
) -> Accepted[PreEventPredictiveState] | Blocked[ClosedReason]

score_observation(
  PreEventPredictiveState,
  CanonicalObservationEvent,
  FrozenObservationArtifact
) -> Accepted[JointObservationLikelihood] | Blocked[ClosedReason]

condition_and_advance(
  PreEventPredictiveState,
  JointObservationLikelihood,
  DeterministicHardCondition,
  CanonicalObservationEvent
) -> Accepted[PostEventState] | Blocked[ClosedReason]

project_physical_target(
  PostEventState,
  FrozenFutureDrivers,
  FrozenReportingOperator,
  PathBudget
) -> Accepted[PhysicalTargetProjection] | Blocked[ClosedReason]

build_checkpoint_candidate(...) -> Accepted[PhysicalStateCheckpointV1] | Blocked[ClosedReason]
build_transaction_bs_candidate(...) -> Accepted[ShadowCommitCandidateV1] | Blocked[ClosedReason]
```

API约束：

- 所有时间、随机种子、artifact和driver由参数显式注入；函数内部不能读“当前时间”或“latest文件”；
- expected缺失/质量问题返回closed reason，不用异常模拟正常NA；程序错误、schema破坏和存储故障才抛异常并fail closed；
- Accepted结果必须自带input/output语义哈希、invariant report和数值诊断；
- Blocked不得携带可被误当成有效posterior的旧值、均匀分布或fallback结果；
- 纯函数运行不得写DB、文件、日志事实或全局缓存；审计记录由调用者根据返回值append。

#### 10.15.6 Schema迁移序列

迁移只作用于新的隔离数据库generation，按以下顺序forward-only：

```text
M000 shadow manifest + schema migration ledger + isolation sentinel
M010 shared canonical event/source mirror tables
M020 physical model artifact + activation registry
M030 driver generation + generation members
M040 observation likelihood + checkpoint index + typed state payload
M050 isolated event_fact_history/latest + B-S commit tables
M060 cursor + pairing + latency + replay report tables
M070 indexes, foreign-key validation and final schema attestation
```

每个migration必须包含：

```text
migration_id / ordinal / schema_from / schema_to
DDL sha256 / code sha256
requires_manifest_state = isolated_shadow_only
precondition query set
postcondition query set
applied_at / apply_result_sha256
```

规则：

- plan默认只读；apply必须显式指定隔离DB路径和expected current generation；
- 打开连接与commit前两次重验realpath、device/inode、manifest、schema generation和禁止目标列表；
- 硬拒绝 `data/archive.db`、`facts.sqlite`、符号链接、无哨兵库、production URI及任何ATTACH旁路；
- `PRAGMA foreign_keys=ON`，表优先使用SQLite `STRICT`和显式CHECK；运行SQLite不支持冻结能力时fail closed；
- migration在单事务内执行，任一postcondition失败整体回滚；
- migration只允许CREATE/ADD/INDEX，不允许DROP、RENAME覆盖、UPDATE历史行或DELETE；
- schema发生不兼容变化时创建新隔离generation并从canonical事实重放，禁止在runtime维护双schema兼容分支；
- failed generation标记quarantined，禁止自动删除或自动回退；新generation通过审计后才可成为Shadow active。

#### 10.15.7 最小数据库不变量

除10.11已列字段外，P0迁移必须用约束/索引落实：

- `canonical_observation_event_id`与source mirror幂等唯一；
- 每个target、artifact、canonical sequence最多一个checkpoint后继；
- `previous_checkpoint_id`必须同target且sequence严格相邻；
- initial checkpoint没有event/previous，incremental checkpoint必须同时拥有；
- likelihood唯一键绑定event、pre-state、artifact和observation payload hash；
- checkpoint payload SHA、semantic state SHA和索引行一一对应；
- `event_fact_latest`只能指向同target已提交history fact；
- activation registry每个target scope最多一个Shadow active artifact；
- cursor推进必须CAS `last_sequence + generation`，不允许回退；
- pairing/latency失败不能改变checkpoint或latest；
- 所有JSON列都有schema ID、SHA和事务前强校验，不作为无约束字段垃圾桶。

SQLite无法用单表CHECK表达的跨表不变量必须由repository事务前验证、事务内CAS和提交后只读audit三层共同证明；禁止依赖“调用方通常会正确”。

#### 10.15.8 Repository与事务API

```text
ShadowRepository.open_verified(path, expected_generation)
  -> VerifiedShadowConnection

read_source_snapshot(read_only_archive_uri, source_snapshot_id)
  -> PinnedCanonicalSnapshot

append_transaction_as(VerifiedShadowConnection, CanonicalMirrorBatch)
  -> IdempotentAppendResult

load_linear_predecessor(VerifiedShadowConnection, target, expected_sequence)
  -> PreviousCheckpoint | Missing | ForkBlocked

commit_transaction_bs(VerifiedShadowConnection, ShadowCommitCandidateV1)
  -> Committed | ExistingSamePayload | CasConflict | Blocked
```

`VerifiedShadowConnection`不得暴露任意SQL给core/shadow orchestration；写入只能通过封闭repository方法。A-S和B-S分别使用短事务，不跨production/Shadow数据库建立分布式事务，也不使用ATTACH。来源读取使用pinned read-only snapshot；目标写入使用隔离连接。

#### 10.15.9 Replay工具与夹具

P0 runner只接受完整 `PhysicalStateReplayBundleV1`，输出：

```text
bundle verification report
per-event Accepted/Blocked result
incremental semantic hashes
full-replay semantic hashes
invariant/fork/future-leak report
optional in-memory B-S transaction trace
```

测试用算法必须标记 `test_fixture_only=true`，其artifact ID命名空间与runtime候选隔离，registry拒绝激活。没有真实P1–P4 artifact时，fixture只能证明接口、哈希、时序和事务性质，不能生成准确率、校准或晋级证据。

F0夹具文件不得手工保存“期望posterior数字”作为真理；golden由冻结fixture artifact首次生成，经人工审查后内容寻址，随后测试同时验证算法不变量和golden漂移。若算法合同有意变化，必须生成新fixture/artifact/version，不能覆盖旧golden。

#### 10.15.10 实施工作包与依赖

```text
WP0-A 共享canonical合同抽取 + schema漂移测试
  ↓
WP0-B 强类型contracts + canonical codec + identity/hash
  ├──→ WP0-C 最小Shadow migration plan/apply/audit
  └──→ WP0-D 纯函数protocol + test-only fixture artifact
              ↓
WP0-E ReplayBundle/runner + 增量/全量等价
              ↓
WP0-F A-S/B-S repository + CAS/故障注入
              ↓
WP0-G 非干扰、权限、未来穿透和内容寻址验收包
```

每个工作包必须先有失败测试，再实施，再运行目标测试与全量相关回归；不得为了加快进度把WP0-A跳过并复制canonical DDL，也不得在WP0-D用旧filter临时填充目标API。

#### 10.15.11 P0退出门

P0完成必须同时满足：

- canonical共享合同只有一份实现，现有消费者schema/hash零漂移；
- 新包import graph符合单向依赖，core无DB/network/market/current-filter依赖；
- 所有public schema、enum、单位、NA和closed reason冻结并内容寻址；
- plan模式零写入，apply只对有效isolated-shadow generation生效；
- migration重复apply零变化，同ID不同DDL SHA fail closed；
- production archive、facts.sqlite、symlink和ATTACH旁路全部被测试拒绝；
- ReplayBundle填入/修改future final X、S、市场价格不改变任何推断hash；
- P0重复、retry、同键漂移、fork、乱序、correction和增量/全量等价通过；
- B-S每个写入/CAS/commit故障点零部分可见；
- P0数据库不存在decision、risk、intent、order、fill、production outbox和UI reader；
- 没有runtime候选artifact时系统明确 `not_implemented/not_activatable`，不生成伪posterior；
- 文档、schema、代码、migration、测试和审计报告SHA形成一个可复核P0 evidence packet。

P0通过只允许开始P1/P2实现，不自动授权S1前向Shadow；S1仍需真实artifact、隔离审计和单独accepted Shadow启动边界。

## 11. 退役清单

目标模型切换后必须删除：

- 温度动量单独外推最高温的主逻辑；
- 温度、动量、轨迹、云雨分别独立重复乘LR；
- 用无证据宽核抹平预测源多峰；
- 模型与TAF双峰旧特判；
- 单条报文导致某预测源基础权重翻转；
- D0实况回写当天弱源bias或历史能力；
- 缺一个预测源导致整个posterior停摆；
- cumulative强观测结果被当作increment再次相乘；
- 将连续潜在温度裸最大值直接当作AMSC报文最高温 `X`；
- 将 `Energy × Efficiency × Termination` 实现为三个无约束标量机械相乘；
- 用自由Efficiency或单一超宽过程噪声吸收所有物理误差；
- 将Termination、见顶或重新升温由单条启发式规则直接指定；
- 同时乘状态诱导气候先验与独立最高温气候先验；
- 训练样本、bridge、OOS阻断live；
- 旧概率模型production fallback；
- UI、交易层或其他服务重新计算posterior。

## 12. 待开发计划

### P0：合同与重复计数审计

- [ ] 形成accepted ADR，授权本方案的shadow实现边界；
- [ ] 从 `sequential_bayes_schema_contract.py` 抽取唯一中性canonical DDL合同，并以schema/hash回归证明现有消费者零漂移；
- [ ] 建立 `physical_state_bayes/` 单一包及 `contracts→core→replay/adapters→shadow` 单向依赖审计，禁止core依赖DB、网络、市场或当前filter；
- [ ] 固定 `T̃/Y/X/Z/P(X)/P(B)`、AMSC计划槽、报告算子与状态转换合同；
- [ ] 固定 `TargetIdentity`、线性checkpoint链和fork fail-closed规则；
- [ ] 固定 InitializationInput、CanonicalObservationEvent、PhysicalStatePosterior与PhysicalStateCheckpoint schema；
- [ ] 固定强类型Present/Missing/Invalid、closed reason enum、canonical单位、时间、数值、概率向量和unknown-field拒绝合同；
- [ ] 实现canonical codec及artifact/checkpoint/likelihood/fact/bundle内容寻址身份，禁止rowid、时间戳和运行诊断进入semantic hash；
- [ ] 固定 `predict→likelihood→condition→post-state→project` 纯函数接口及 `Z_t^-/Z_t^+` 时序；
- [ ] 纯函数显式注入时间、随机种子、artifact和driver，expected缺失返回Blocked且不得携带旧posterior或fallback结果；
- [ ] 固定ProbabilityCommitCandidate、Transaction B CAS和Transaction C跨层边界；
- [ ] 选择唯一气候先验入口并证明不存在base-rate双计；
- [ ] 固定`C_static/C_antecedent/F_A/U`归属，证明850/700 hPa环流、前期降水和土壤湿度不会跨prior/weak层重复消费；
- [ ] 枚举同一AMSC事件当前所有派生证据路径；
- [ ] 证明不存在absolute posterior和incremental LR双计；
- [ ] 将现有 `sequential_bayes_filter_shadow_runtime.py` 固定为Arm A对照，目标core禁止import、包装或fallback；
- [ ] 无真实P1–P4 artifact时只允许运行non-activatable test fixture，禁止生成伪候选posterior。

### P0-S：Shadow隔离基础

- [ ] 建立带 `isolated_shadow_only` 哨兵的独立数据库，并硬拒绝production archive及符号链接旁路；
- [ ] 建立M000–M070 forward-only最小migration，禁止调用会创建交易表的 `p10_shadow_runtime_schema.py` initializer；
- [ ] plan默认零写，apply显式绑定DB realpath、device/inode、manifest与expected generation，连接和commit前双重验权；
- [ ] 硬拒绝 `archive.db`、`facts.sqlite`、symlink、ATTACH、无哨兵和production URI旁路；
- [ ] 复用canonical/update/checkpoint/fact核心表语义，建立物理artifact、driver、likelihood、state payload、cursor、pairing和latency扩展表；
- [ ] 对JSON payload执行schema/version/SHA/单位/有限数校验，unknown字段和缺NA reason事务前fail closed；
- [ ] 实现A-S事件镜像事务与B-S概率事实事务，证明任一故障均零部分提交；
- [ ] 通过封闭 `VerifiedShadowConnection/ShadowRepository` 写入，core和orchestrator不得获得任意SQL写权限；
- [ ] 固定cursor、event、previous checkpoint、artifact、payload和latest pointer的幂等/CAS身份；
- [ ] 实现CanonicalObservationEvent只读tail consumer；
- [ ] 固定Shadow零生产权限与可失败审计；
- [ ] 实现独立physical_driver_generation及父血缘；
- [ ] 实现Shadow资源预算、kill switch和故障隔离；
- [ ] 证明Shadow开关不改变production posterior、decision、outbox、订单和时延；
- [ ] 建立旧posterior与新Shadow按canonical event配对的评估记录；
- [ ] 建立内容寻址ReplayBundle、确定性随机种子、稳定排序和semantic hash合同；
- [ ] 通过P0重复、retry、同键漂移、无自确认、乱序/correction、多峰、hard mask、bridge消融和未来穿透回放；
- [ ] 通过每事件增量/全量semantic state与 `P(X)` 哈希等价；
- [ ] 生成绑定代码、schema、migration、artifact、测试和真实事件链的内容寻址回放报告；
- [ ] 生成P0 evidence packet，并证明隔离库不存在decision、risk、intent、order、fill、production outbox或UI reader；
- [ ] P0退出仅授权进入P1/P2开发，不自动启动S1前向Shadow。

### P1：弱预测情景层

- [ ] 固定物理变量角色enum、canonical名称、单位、符号、时间/空间语义和NA合同；
- [ ] 为ECMWF/ICON/GFS/JMA逐字段实现独立raw→decoder→canonical adapter与availability lineage；
- [ ] 冻结累计/平均/瞬时/区间极值解码、累计reset和跨run禁止规则；
- [ ] 冻结站点插值、周边网格、压力层/高度层、地形mask和梯度算法；
- [ ] 为TAF/TAF AMD/WU/TWC固定允许字段、active revision chain与“不得解释为辐射/通量/PBL事实”的边界；
- [ ] 生成逐generation字段级availability mask与明确NA reason，禁止0填充和旧run补缺；
- [ ] 覆盖获批source universe的全部运行missingness pattern，包括empty-weak中性信息surface；
- [ ] 实现逐源条件偏差校正；
- [ ] 分离源内残差与源间分歧；
- [ ] 实现source availability mask与exact source set校准；
- [ ] 建立逐源观测方程与source-combination联合残差协方差 `Q_A`；
- [ ] 实现provider/source相关性折扣；
- [ ] 实现多峰情景混合及峰形回归验收；
- [ ] 实现四川盆地条件天气型混合先验，并保留`regime_family/weight/Z0/U path/lineage`而非先平均成单峰；
- [ ] 证明偏差校正只移动条件峰、残差只控制峰内宽度且不会机械合峰；
- [ ] 区分模式连续最高温与AMSC报文最高温的逐源口径偏差；
- [ ] 增加辐射、云、雨、风、湿度、热通量和周边网格等可用弱驱动轨迹；
- [ ] 建立NWP驱动、NWP温度轨迹和同源直接最高温的联合误差/去重合同；
- [ ] 为每个新增变量登记母数据、派生关系、条件相关性和唯一composer消费位置。

### P2：物理状态层

- [ ] 固定唯一状态`Z=(T̃,q,G_net,W,C_cp,R)`的schema、单位、边界、变换和固定维度checkpoint合同；
- [ ] 删除`full_physical/reduced_physical/net_tendency`动态状态分辨率与`state_detail_level`状态机；
- [ ] 固定AMSC直接约束`T̃/q/G_net/R`并软约束`W/C_cp`、不能凭空分离H/C_eff/A/E/D的可观测性合同；
- [ ] 实现净辐射与 `Rn=H+LE+G+ΔS` 能量账本；
- [ ] 将`Ψ=(Rn,Ts,H,LE,G,ΔS,C_eff,Q_H,A,E,D)`实现为由`Z/U/Θ`条件化、不可递推的诊断分布；
- [ ] 实现地表湿润`W`、水汽`q`与冷池/恢复记忆`C_cp`状态；
- [ ] 使用NWP辐射、PBL、热通量及周边空间/垂直场构造`G_driver=𝔼[Q_H+A+E−D|Z,U,Θ,C]`弱driver合量；
- [ ] 实现`G_net`受`G_driver`约束但由AMSC innovation更新的唯一已实现净倾向转移；
- [ ] 实现天气regime transition/hazard，并从未来路径派生Termination；
- [ ] 固定离散天气情景/状态与连续物理状态的分层切换结构；
- [ ] 固定自洽day-world粒子语义及版本化`scenario_family`路径分类器，证明其不新增状态、不二次改权；
- [ ] 实现按真实Δt缩放、固定子步和稳定语义哈希的连续状态积分；
- [ ] 实现带相关块结构和regime条件的process covariance，禁止全局对角宽噪声；
- [ ] 实现`Ψ`能量closure误差零中心审计及昼夜通量符号/参数化切换；
- [ ] 实现固定状态有效信息秩、后验相关、条件数、方差收缩和敏感度可辨识性审计；
- [ ] 实现`Ψ`诊断`available/partial/unavailable + na_reason`发布合同，禁止缺源时改变状态维度；
- [ ] 分离forecast、energy partition、heat capacity、dynamics、regime、process和reporting不确定性；
- [ ] 实现PhysicalStateCheckpoint的幂等读写与重放。

### P3：强观测联合composer

- [ ] 统一消费AMSC温度、露点、云、雨、风；
- [ ] 实现编码/修约/截断感知的温度与露点区间likelihood；
- [ ] 实现风向圆周或u/v、风速/阵风、VRB与静风的联合观测模型；
- [ ] 实现多层云、天气token、降水强度类别、能见度/CAVOK的结构化censored likelihood；
- [ ] 实现pressure type转换、气压/压变去重和单点平流软约束；
- [ ] 固定字段质量enum、缺失边缘化、字段隔离和事件级阻断边界；
- [ ] 将温度轨迹和动量定义为同一事件历史的派生观测；
- [ ] 将current-step delta/rate限制为当前温度字段的联合innovation坐标，不产生第二likelihood；
- [ ] 使用温度倾向校正实际净加热率，不把动量再次独立乘LR；
- [ ] 实现温度/露点量化、风向圆周、云雨类别及字段相关性的联合观测likelihood；
- [ ] 实现露点下降、低云消散、能见度改善、风压变化、对流突变和温度跃升的多假设竞争，禁止单字段固定加减分；
- [ ] 固定计划槽与非计划SPECI对状态更新和hard lower bound的不同语义；
- [ ] 固定AMSC整点/半点同为计划表内primary候选、AWC confirmation/substitute只消费一次的边界；
- [ ] 实现correction/迟到报文从对应valid time前checkpoint重放active event chain，禁止负Δt、叠加或likelihood相除；
- [ ] 实现JointObservationLikelihood schema、逐因子审计与joint score不可重组保护；
- [ ] 实现有效极端事件重尾更新、innovation/NIS/reliability诊断和离线校准；
- [ ] 实现事件级RBPF或通过回放证明更优的等价多峰状态滤波；
- [ ] 实现D0天气情景Bayes软仲裁；
- [ ] 输出事件级审计log-LR但不二次组合。

### P4：未来路径与最高温投影

- [ ] 输出未来一小时升降温分布；
- [ ] 输出剩余升温、见顶、终止和重新升温概率；
- [ ] 生成保持时间相关、regime相关和driver相关的日终潜在连续温度路径集合；
- [ ] 禁止逐计划槽独立抽样边缘温度，验证路径自相关和mode保持；
- [ ] 实现有效样本量监控、保峰重采样与混合分量受控合并；
- [ ] 证明每条粒子绑定自洽`Z/U/R/过程/报告`路径，`scenario_family`只做解释分组且不新增状态或二次改权；
- [ ] 实现冻结AMSC计划槽、编码、共享/槽级误差、质控、active revision和缺失语义的报告算子；
- [ ] 固定已经观测合同槽不重采样、未来槽才应用报告算子的边界；
- [ ] 实现target reportability与温度过程分离，缺报不填0/前值/插值；
- [ ] 分别输出连续潜在峰值 `P(X*)` 与AMSC报文最高温 `P(X)`；
- [ ] 输出有符号next-slot/1h变化、future peak margin、cooling depth和非负remaining rise；
- [ ] 分离strict peak、future tie、new high、thermal rebound和new-high-after-rebound；
- [ ] 实现first/last peak slot与版本化Termination time分布；
- [ ] 以“路径构造即满足”为主并执行最终hard invariant，零质量时fail closed；
- [ ] 实现确定性路径种子、common random numbers、路径预算和Monte Carlo收敛审计；
- [ ] 从同一posterior生成top-K情景、主要不确定性来源和下一计划槽`NextObservationDiscriminationV1`；
- [ ] 用联合观测模型反事实积分计算EIG，禁止规则分数、未来穿透或自然语言解释反向修改posterior；
- [ ] 应用hard condition并通过独立bridge投影市场桶；
- [ ] 原子提交唯一posterior checkpoint。

### P5：训练与验证

- [ ] 建立AMSC daily final-X独立样本通道；
- [ ] 实现 `PhysicalStationDayLabelV1`、`WeakForecastRevisionSampleV1`、`PhysicalTransitionSampleV1`、`ObservationCalibrationSampleV1` 和 `ReportingAndPosteriorSampleV1` 五类不可变样本schema；
- [ ] 为每条样本分列 `feature_as_of`、`feature_cutoff`、`label_available_at`、active revision chain、输入SHA、目标合同SHA和eligibility原因；
- [ ] 建立下一槽温度、温度增量、后续轨迹、剩余最高温、见顶和重新升温事件级标签；
- [ ] 增加有符号next-slot/1h变化、future peak margin、cooling depth、tie/new-high、first/last peak slot及版本化thermal rebound标签；
- [ ] 对coverage不完整、无下一槽和终局事件建立明确NA语义，禁止插值、前值或其他来源补齐final X；
- [ ] 建立弱预测与强物理模型的每日候选训练，禁止仅以final X拟合全部隐状态；
- [ ] 保证弱预测样本输入不含D0已见AMSC温度、动量、云雨露点、final X、市场信息或交易结果；
- [ ] 用下一事件、下一计划槽、未来1小时路径、剩余日路径和final X多任务训练状态转移，禁止以final X给`X*`、固定隐状态或`Ψ`中的H、C_eff、A、E、D伪造真值标签；
- [ ] 用严格pre-event state与 `O_<t` 训练联合观测likelihood，禁止post-state或当前事件进入自身likelihood输入；
- [ ] 独立训练/校准AMSC报告算子和reportability，禁止用市场S替代AMSC报告标签；
- [ ] 独立训练天气型条件`X→S` bridge，纳入站点/报告差及局地对流空间不确定性，证明固定站点差基线不足时才允许晋级；
- [ ] 建立strict-as-of事件级回放；
- [ ] 按station-day做前向时间切分，同一天全部forecast revision、事件和correction不得跨train/calibration/OOS；
- [ ] 冻结多任务loss、物理/可辨识性penalty与分层采样权重，并保存每项未加权loss、总loss和有效独立日数；
- [ ] 建立raw `P(X)`有序温度校准器，验证其保留合理多峰、开放尾部且不会重引入observed contract max以下质量；
- [ ] 保存参数posterior或代表性draw，并验证online压缩参数集与离线完整参数积分在冻结容忍度内等价；
- [ ] 对dataset、split、标签、代码、loss、calibrator、数值合同和model artifact做内容寻址，任一变化均产生新身份；
- [ ] 建立rolling-origin/expanding walk-forward和按station-day聚类的置信区间，禁止用事后最优fold拼接production artifact；
- [ ] 实现 `draft→training_complete→calibration_complete→oos_evaluated→shadow_candidate/rejected→promotion_approved→retired` 候选状态机，禁止每日样本原地改写production参数；
- [ ] 建立live非阻断测试矩阵，证明样本不足、final X pending、OOS未完成/失败、bridge未闭合、市场未final和训练失败都不阻断AMSC入库及当前批准artifact运行；
- [ ] 对比当前批准基线的MAE、CRPS、NLL、Brier和校准度；
- [ ] 在完全相同输入下对比结果空间Bayes与状态空间Bayes的结构净收益；
- [ ] 建立Arm A当前/当前证据、Arm B状态空间/当前证据、Arm C当前/扩展证据、Arm D状态空间/扩展证据的2×2四臂评分；
- [ ] 为每条A/B配对生成evidence parity manifest，任一单臂独有证据时不得进入结构优劣结论；
- [ ] 将纯动量、辐射单因子和E–E–T机械启发式仅作为消融基线，不赋予production候选身份；
- [ ] 分季节、天气型、时段、源组合做稳定性验证；
- [ ] 分别做能量分配、有效热容量、动力输送、天气状态和报告算子特征消融；
- [ ] 做能量守恒、参数边界、状态平滑和隐状态可辨识性审计；
- [ ] 分别校验一步温度增量、未来路径、连续潜在峰值和AMSC报文最高温；
- [ ] 校验`scenario_family`质量、top-K/other质量守恒、下一报文outcome bin频率、情景区分方向和实际熵下降；
- [ ] 衡量云雨突变和终止事件的反应领先时间；
- [ ] 分开衡量 `T1→T3` 计算时延、信息领先和温度确认领先；
- [ ] 在前向结果不可见时通过accepted evaluation ADR冻结指标、非劣界、实质改善幅度、分层和停止规则。

### P6：性能与切换

- [ ] 实现T1–T5统一时钟合同、monotonic deadline传播、wall/monotonic映射和boot identity，禁止retry/replay重置T1；
- [ ] 固定订单、概率和事实三类SLO分母及互斥终态，definitive reject、deadline miss、ambiguous outcome和进程崩溃不得从分母删除；
- [ ] 将“完成下单”限定为deadline内收到CLOB accepted，本地intent、request sent或definitive reject均不算成功；
- [ ] resident预热情景库、参数集合和报告算子，禁止报文热路径网络取数；
- [ ] 预热CLOB认证/签名/连接、只读行情流、数据库prepared statement和固定内存buffer，并实现可失败readiness gate；
- [ ] 实现内容冻结的 `EventExecutionEnvelopeV1` 与派生 `DecisionExecutionEnvelopeV1`：前者绑定天气概率血缘，后者绑定latest posterior、行情、风险、策略和订单幂等身份；
- [ ] 实现同target严格sequence单写者、跨target有界并发、有界resident mailbox和deadline-aware背压；
- [ ] 冻结粒子/混合分量预算并压测有效样本量、峰保真度与p50/p95/p99；
- [ ] 证明online路径不包含训练、MCMC或artifact参数改写；
- [ ] 证明热路径不存在NWP网络抓取、GRIB解码、generic task queue、无界线程/内存及运行时降粒子/裁尾/合峰；
- [ ] 验证概率Transaction B的整体提交/回滚、previous-checkpoint CAS和latest pointer CAS；
- [ ] 验证Transaction B前deadline耗尽时仍提交正确physical SSOT，并通过decision-audit outbox让Transaction C写唯一deadline阻断原因；
- [ ] 验证Decision Core/Transaction C只消费已提交posterior outbox且不反写概率SSOT；
- [ ] 实现pre-posterior `ExecutionIneligibilityNoticeV1`，只允许同一Decision Core写no-action `auto_trade_decisions`，绝不生成risk、intent或order；
- [ ] 验证Transaction C复验latest fact、canonical sequence、artifact、deadline、行情freshness和风险额度，过期outbox不得形成intent；
- [ ] 验证网络幂等、重试和reconcile；
- [ ] 实现CLOB accepted、definitive rejected与ambiguous timeout互斥状态机，ambiguous结果必须先reconcile且禁止盲目重发；
- [ ] 对T1–T5和所有内部span建立统一trace，按事件时间窗口输出p50/p95/p99、成功率、失败终态、资源和分层报告；
- [ ] 完成冷启动、artifact损坏、DB busy、CAS fork、burst、correction、worker crash和网络三窗口故障注入；
- [ ] 在同ReplayBundle、artifact、硬件配额和数值合同下证明性能优化前后 `P(X)`语义等价；
- [ ] 验证AMSC入库不被任何离线通道阻断；
- [ ] 达成eligible报文到CLOB accepted端到端p95≤3秒，并单独满足冻结成功率门槛；
- [ ] 形成accepted production cutover ADR；
- [ ] 实现按canonical sequence边界N的 `RuntimeModelManifestV1` 原子CAS，manifest、bootstrap checkpoint与start sequence共同切换；
- [ ] 原子晋级唯一主模型；
- [ ] 验证manifest提交前失败只产生pre-cutover abort，提交后缺陷只能fail closed并向前修复，不允许旧模型rollback/fallback；
- [ ] 删除旧概率内核及兼容分叉。

## 13. 验收合同

开发完成必须同时满足：

- [ ] 多源分歧能够保留合理多峰；
- [ ] 偏差校正不通过宽核人为抹平分歧；
- [ ] 偏差、峰内条件残差、源间分歧与联合信息量分别建模且可独立审计；
- [ ] 峰的合并与消失由校正证据和相关性决定；
- [ ] 缺源只降低信息量，不阻断物理posterior；
- [ ] canonical event/source DDL只有一个中性共享实现，当前消费者与新P0-S不存在复制后漂移的第二份合同；
- [ ] 目标 `physical_state_bayes.core` 的import闭包不包含SQLite、HTTP、market、strategy、UI或当前结果空间filter；
- [ ] public core边界不接受未校验裸Mapping，缺失/无效字段使用强类型NA并具有closed reason；
- [ ] canonical编码拒绝NaN/Infinity、unknown enum、单位漂移、隐式时区和schema版本漂移，semantic hash不含rowid/mtime/PID/耗时；
- [ ] 无真实P1–P4 artifact时P0明确不可激活，test-only fixture无法进入artifact activation registry或生成晋级证据；
- [ ] P0-S initializer不会创建或复用P10 decision/intent/order schema，且不会调用完整 `p10_shadow_runtime_schema.py` initializer；
- [ ] Shadow migration默认plan零写、apply forward-only且幂等，同migration ID不同DDL SHA、错误generation或隔离哨兵漂移均fail closed；
- [ ] production archive、facts.sqlite、符号链接、ATTACH和任意SQL writer旁路均被可失败测试阻断；
- [ ] 每个物理输入均具有唯一角色、canonical单位、符号、step type、valid interval、空间合同、raw/decoder血缘和NA reason；
- [ ] 累计辐射/降水/通量不存在跨run或跨reset误差，区间平均不冒充瞬时值；
- [ ] 普通source/字段缺失命中获批missingness pattern并扩展不确定性，不使用0、旧run、其他provider或聚合商补位；
- [ ] AMSC事实、NWP弱驱动、TAF/WU/TWC弱专家、prior和market projection角色不可互换；
- [ ] 连续潜在温度 `T̃/X*` 与AMSC计划槽报文温度 `Y/X` 目标严格分离；
- [ ] 报告算子版本化且能确定性重放计划槽、编码、质控和缺失语义；
- [ ] 地表能量分配满足 `Rn=H+LE+G+ΔS` 的结构约束；
- [ ] 唯一递推状态始终精确为`Z=(T̃,q,G_net,W,C_cp,R)`，任何source availability变化均不改变维度或schema；
- [ ] 核心温度方程固定为`dT̃/dt=G_net+ε_T`，且`G_net`转移受`G_driver=𝔼[Q_H+A+E−D|Z,U,Θ,C]`约束；
- [ ] `q≥0`、`W∈[0,1]`、`C_cp≥0`、协方差半正定和regime权重归一始终成立；
- [ ] AMSC-only证据不足时不会输出看似精确的H、C_eff、A、E或D；`Ψ`诊断必须降为`partial/unavailable`并记录原因；
- [ ] 系统不存在`state_detail_level`、动态升维/降维或缺源换模型路径；固定坐标不可辨识会阻止候选artifact晋级而非改变live状态定义；
- [ ] 过程噪声按Δt、regime和availability pattern缩放，且与weak-driver/observation error分离；
- [ ] 见顶、Termination和重新升温只由未来路径积分产生，不被regime标签硬指定；
- [ ] Efficiency不是无边界自由误差吸收器，Termination由未来路径积分产生；
- [ ] 温度动量只作为观测投影、辐射只作为强迫入口、E–E–T只作为概念诊断，三者均不形成独立production概率链；
- [ ] 气候先验只有一个入口，不存在base-rate双计；
- [ ] 四川盆地静态/前期背景与当日850/700 hPa、槽脊/副高、冷空气和对流弱预测分层明确，同一背景信息不跨层重复消费；
- [ ] 平流使用周边空间场而非单点风场伪推断；
- [ ] forecast、能量分配、有效热容量、动力、状态转换、过程和报告不确定性可分别审计；
- [ ] 已观测最高温hard condition永远成立；
- [ ] 同一canonical event最多产生一次有效状态更新；
- [ ] 当前事件始终以 `Z_t^-` 评分，`O_t`只在评分后进入 `Z_t^+`，不存在自确认；
- [ ] checkpoint链前驱、canonical sequence、event、artifact和payload哈希全部CAS闭合，任何fork fail closed；
- [ ] 同一物理证据不会在弱层和强层重复累计；
- [ ] 当前方案被准确识别为“物理证据条件化的结果空间Bayes”，不得再表述为纯温度动量外推；
- [ ] 新方案被识别为“物理证据条件化的状态空间Bayes”，不得声称其首次引入物理证据；
- [ ] 系统能同时表达升温、降温、终止和重新升温；
- [ ] 未来合同槽由时间相关联合路径生成，禁止独立边缘抽样制造虚假最高温；
- [ ] 每条未来粒子代表自洽day-world，绑定`Z/U/R/过程/报告`且`scenario_family`不新增状态、不二次加权；
- [ ] active已观测合同槽固定进入每条路径且不重采样，correction后只使用active revision；
- [ ] `P(X*)`与离散报文 `P(X)`分别保存，连续峰均值不得直接修约成单桶；
- [ ] future peak margin和next-hour change允许为负，remaining rise保持非负；
- [ ] strict peak、future tie、new high三项在有未来可报告槽时归一，终局无未来槽边界固定；
- [ ] thermal rebound、tie-after-rebound和new-high-after-rebound语义分离；
- [ ] 缺报只影响reportability，不作为降温证据，也不以0/前值/插值补齐；
- [ ] hard condition只含确定合同事实，预计降雨、见顶、太阳高度和低概率尾均不得硬裁；
- [ ] hard condition后零质量会fail closed，不回退均匀分布或旧posterior；
- [ ] 路径数、子步和尾部扩展通过Monte Carlo收敛测试且满足冻结时延预算；
- [ ] posterior概率归一且可确定性重放；
- [ ] 多峰状态滤波在确定性回放中满足概率守恒、有效样本量下限和合理峰保真；
- [ ] AMSC联合观测likelihood正确处理温度/露点量化、风向圆周、云雨类别、correction及SPECI语义；
- [ ] 当前温度、current-step动量和含当前值轨迹只贡献一次信息，pre-event history不含当前事件；
- [ ] 云、天气、能见度、露点、降水和温度减速的相关性在一个联合artifact中处理，不机械独立相乘；
- [ ] 露点下降、低云消散、能见度改善、风压变化、对流突变和温度跃升均通过多假设联合竞争解释，不存在单字段固定加减分；
- [ ] 有效但与预测强冲突的AMSC报文通过重尾innovation/regime更新消费，不因“不符合模型”被删除；
- [ ] 字段缺失/无效只按质量合同边缘化或隔离，不用0、前值、AWC或NWP静默填充；
- [ ] 合同槽温度的观测likelihood与X hard condition分列且语义不重复，非合同SPECI不抬高hard lower bound；
- [ ] AMSC计划表内整点与半点无固定事实等级差，AWC confirmation不生成第二likelihood；
- [ ] correction/迟到报文从active event chain重放，重新计算observed max，并与全量回放semantic hash一致；
- [ ] online热路径不包含网络取数、模型训练、MCMC或production参数改写；
- [ ] UI与交易只读唯一 `PosteriorSSOTFact`，内部checkpoint不存在第二个public latest或下游复算旁路；
- [ ] 情景top-K、主要不确定性与下一报文EIG均从同一posterior结构化派生，质量守恒、严格as-of且不会反向修改`P(X)`；
- [ ] `X→S` bridge按天气型、站点/空间差和resolution报告过程条件化，bridge开闭或结算观测不会改变物理状态及`P(X)`哈希；
- [ ] Transaction B只提交概率事实及可选posterior outbox，Decision Core/Transaction C不进入或反写概率事务；
- [ ] AMSC入库不受训练、市场、bridge或OOS阻断；
- [ ] `feature_as_of`与 `label_available_at`分列且均绑定不可变血缘，materialization时刻、文件mtime和provider自报时间不能冒充as-of；
- [ ] runtime feature schema在类型层无法访问后续报文、final X、correction终局、市场finality或其他未来标签；
- [ ] 同一station-day全部revision、事件和correction只能位于同一个train/calibration/OOS split；
- [ ] final X只监督可观测目标与端到端分布，不被用作连续潜在峰`X*`、固定隐状态或`Ψ`中H、C_eff、A、E、D的伪真值；
- [ ] 弱预测bias、skill、相关性和多峰权重训练不读取D0强观测、市场、价格、成交、PnL或交易结果；
- [ ] 状态转移、联合观测、报告算子和端到端 `P(X)`分别具有可审计目标，所有未加权loss、物理penalty和总loss均被保存；
- [ ] raw `P(X)`校准保持温度有序、多峰和尾部语义，hard condition后不重引入observed contract max以下概率；
- [ ] 校准、参数压缩和missingness降级前后均在相同station-day样本上比较CRPS、NLL、Brier、coverage、mode-collapse和尾部表现；
- [ ] 数据、标签、代码、变量合同、loss、split、calibrator或数值算法任一变化都会使旧dataset/artifact/report身份stale；
- [ ] online热路径不训练、不重估参数、不选择calibrator，production active artifact只能经accepted promotion ADR与runtime manifest原子切换；
- [ ] final X pending、coverage不完整、样本不足、OOS失败、bridge未闭合、市场未final或训练任务失败均不会改变当前active artifact，也不会成为AMSC live事件阻断原因；
- [ ] 新模型在strict-as-of回放中通过冻结晋级门槛；
- [ ] Shadow只读消费不可变事件且使用独立cursor、数据库和latest pointer；
- [ ] Shadow开关、崩溃、积压、缺源和网络失败均不改变旧生产链；
- [ ] Shadow数据库具备有效隔离哨兵，所有writer硬拒绝production archive、无哨兵库及路径旁路；
- [ ] Shadow第一阶段不存在decision、risk、intent、order写表或可被production领取的outbox；
- [ ] A-S/B-S故障注入证明任一写入或latest CAS失败均零部分可见；
- [ ] 相同ReplayBundle在冻结数值合同下生成相同逐事件semantic state与 `P(X)` 哈希；
- [ ] 当前事件不进入自身pre-event trajectory，合法correction追加新状态而不覆盖历史；
- [ ] Shadow输出无法进入UI生产快照、Decision Core、outbox或CLOB；
- [ ] 扩展物理驱动使用独立generation且不改变旧弱源generation语义；
- [ ] 新旧比较绑定相同event、as-of、hard mask、目标和final X；
- [ ] Shadow分别完成同证据结构对照和新增物理驱动增量消融；
- [ ] Arm A/B同证据比较具备逐事件evidence parity manifest，Arm C/D新增driver集合完全一致并报告结构、driver及交互效应；
- [ ] 晋级评分同时覆盖最高温MAE、CRPS、NLL、事件 `ΔCRPS`、路径、见顶/再升温、多峰、正确领先时间和运行时延；
- [ ] 指标阈值在前向结果不可见时冻结，变更阈值会生成新artifact并重启独立前向窗口；
- [ ] 物理结构叙事不作为晋级证据，晋级只读取冻结准确率、校准、正确领先时间、稳定性与时延门槛；
- [ ] T1在完整AMSC候选payload到达且解析/质控前打点，同事件retry/replay/重启不重置T1，correction只使用自身真实T1；
- [ ] “3秒完成下单”只在deadline内收到CLOB accepted时成立，definitive reject、ambiguous timeout、本地intent和request sent均不冒充成功；
- [ ] eligible事件的deadline miss、拒单、崩溃与不确定结果全部留在SLO成功率分母，统计窗口不按完成时间删除慢请求；
- [ ] `T2−T1`、`T3−T2`、`T4−T3`、`T5−T4`、`T3−T1`和accepted端到端时延均可由同一trace确定性重建；
- [ ] resident readiness在事件前验证artifact、checkpoint、driver、报告算子、市场合同、签名器、行情流和数据库资源，未预热时交易fail closed而事实入库继续；
- [ ] 同target严格按canonical sequence更新状态，不因背压跳过中间事件；跨target并发不会突破冻结CPU、内存或线程配额；
- [ ] `EventExecutionEnvelopeV1`绑定event、前驱、active artifact、driver、bridge、策略和deadline；行情与风险只绑定派生 `DecisionExecutionEnvelopeV1`，不会反向污染或触发天气posterior重算；
- [ ] Transaction B在每个append、CAS、latest推进和commit故障点均零部分可见，且事务内不等待网络、Decision Core或CLOB；
- [ ] Transaction C只对latest可交易fact创建一次decision、reservation和intent，所有阻断原因唯一写入 `auto_trade_decisions`；
- [ ] 没有posterior outbox时任何路径都不能创建可执行intent；pre-posterior失败仅通过幂等ineligibility notice由Decision Core记录no-action原因；
- [ ] deadline耗尽后仍可提交正确physical posterior，但不会创建新的外部订单副作用，也不会使用旧posterior/行情赶单；
- [ ] ambiguous CLOB结果不会产生盲重发或重复仓位，crash重启后先reconcile已提交intent和外部订单状态；
- [ ] 性能优化不通过减少粒子、裁掉尾部、抹平多峰、跳过hard condition或修改calibrator换取时延；
- [ ] 真实回放、前向Shadow、production-equivalent执行仿真和故障注入共同覆盖暖机、冷启动、burst、correction、CAS、DB与网络故障；
- [ ] eligible报文到CLOB accepted端到端p95≤3秒，且拒单、超时、崩溃和ambiguous结果未被排除；
- [ ] 原子cutover在canonical sequence边界保证每个事件只由一个production模型消费，runtime manifest不存在fallback identity；
- [ ] cutover前失败仅中止未提交切换；cutover后故障只允许fail closed与新的向前切换，不允许重新激活旧概率模型；
- [ ] 旧概率模型的可执行路由、registry入口、环境开关和兼容分叉均已删除，历史artifact仅保留不可执行审计身份；
- [ ] 主模型不可用时交易fail closed；
- [ ] production不存在旧模型fallback或第二条概率链。

## 14. 最终宪章

> 当前模型是已经使用温度、轨迹、动量、云雨露点和风等物理证据的结果空间Bayes，不得贬称为纯温度动量外推；目标模型的升级点是把同类证据先用于固定`T̃/q/G_net/W/C_cp/R`状态推断，再生成未来路径。预测源提供未来外部强迫和温度情景；唯一联合composer遵守地表能量守恒，利用AMSC实况持续更新可辨识状态并重加权`Ψ`中的感热分配、边界层有效热容量和动力输送解释，生成机场潜在连续温度路径，再通过冻结AMSC计划槽与报告算子投影为报文最高温和市场桶后验SSOT。温度轨迹与温度动量是净加热率的强观测投影，不是唯一预测机理；Termination和见顶是未来报文路径的派生概率，不是独立乘法因子。第一性原理结构不自动获得production权威；开发期Shadow必须旁路只读、状态隔离、零生产写权、零交易权限且独立失败，并以同证据对照和新增驱动消融证明实际收益。晋级前旧模型始终是唯一production，晋级后新模型成为唯一production并删除旧入口。本文全部内容当前均为待开发升级目标，未获accepted ADR和cutover授权前不得解释为生产事实。
>
> 数学实现只允许一个联合状态空间Bayes composer：温度动量是实际净加热率的观测投影，辐射是能量账本的外部强迫，`Energy–Efficiency–Termination`是解释与诊断框架，不是三个机械相乘的概率因子。露点下降、云散、能见度改善、风压变化、对流和温度跃升必须作为多种物理解释的联合竞争证据；每条粒子代表一条自洽day-world而不是独立边缘拼接。目标在线近似优先采用分层切换状态空间模型与RBPF/受控Gaussian-sum滤波，在一个冻结artifact内联合处理弱预测相关误差、AMSC观测likelihood、未来路径和报告算子；情景摘要、不确定性排名与下一报文信息价值只从该posterior派生，`X→S`由天气型条件bridge单向投影。任何替代算法都必须同时证明多峰保真、校准、可重放和3秒链路性能，且不得建立第二条production概率链。
