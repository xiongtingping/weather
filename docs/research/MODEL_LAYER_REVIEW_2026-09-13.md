# 模型层审查：单系统校准、融合、分布、band 表

熊先生，本次审查结论是：单系统校准的核心算式可以复现，但融合选择、OOS 验收和最终服务分布尚未构成同一条可信的验证链。确认了三项高优先级验证缺陷，以及三项分布支持集、band 轴和版本合同问题。现有 OOS PASS 不能直接解释为最终市场概率已经通过严格样本外验收。

审查终点线：逐层定位生产入口，检查训练／服务算式、时间与单位合同；覆盖 45 站实际引用；以隔离反例和现有测试验证结论。该审查终点已达成，生产修复和修复后历史 OOS 尚未完成。本次只新增审查文档、只读探针与证据，不修改生产代码、不写 SSOT、不重启服务、不提交订单。

**证据口径**

- 固定决策／创建截止：`2026-09-13T14:02:22.359983Z`，即 ET 2026-09-13 10:02:22。逐站选择该时点之前最新已提交 P0，共 45 行；43 行事件日为 09-14、2 行为 09-13。它们是服务引用样本，不是 45 个同时可交易的 scope，也不是闭日 OOS 样本。
- 采样后生产继续运行，最新行会从未来日切回当日。因此本报告固定上述截点，不把不同事件日、lead 的“最新行”变化当成模型改善。
- SSOT 使用 `mode=ro`、`query_only`、查询超时和小页缓存；执行器限制 RSS 1200 MiB、单文件 16 MiB、最低剩余盘 3 GiB。一次证据导出触及文件上限、一次无合适索引的查询触及时间上限，均停止并改为精简输出／使用已有 scope 索引；失败残片已删除，未增加生产索引。
- 仓库存在并行修改，代码定位以随附 `source_manifest.json` 的哈希和摘录为准；报告中的行号仅作导航。

**四层现状**

| 层 | 实际实现 | 固定截点的证据 |
| --- | --- | --- |
| 单系统校准 | 60 日滚动窗、5 日半衰期；按模型温度水平三分位拟合收缩后的偏差；成员偏差乘 spread factor；0.5°C 高斯核积分 | 100 个实际引用、可解析的系统 artifact 均为 `level_terciles_v1`，`reliability_weight=1`，未掺入气候样本；另有 11 个借参席位 |
| 融合 | 子集竞赛；有足够共同训练残差时使用收缩协方差权重，否则逆训练误差／成熟度权重；家族限额、consensus bridge | 34 行引用 `minimum_variance_shrunk_covariance_v1`，11 行引用逆误差权重；45 行证书均为 `FORWARD_ACCUMULATION_5_TRAIN_3_HOLDOUT` |
| 分布 | 每家成员积分到桶，融合后再施加形状与宽度变换 | 44/45 行带中心／宽度回执，宽度系数 0.75；这 44 行同时使用非恒等 band |
| band | `q_b = p_b f(cell_b) / Σ p_j f(cell_j)`；表与阶段、axis 一同标识 | P0 44 行距离轴、KSFO 1 行旧 claim 轴；实际引用 4 个表版本，不能以当前 active 表替代逐行 pin |

单系统的 spread factor 为 0.565–5.0，中位 1.369，仅 1/100 达到上限；有效训练量约 11.02–14.43，中位 13.56。97/100 个 artifact 的服务重拟合截止 09-12，3 个为 09-13（事件日是否闭合取站点时区，不能只按 UTC 日期否决）。这些统计描述证据量，不证明校准质量已经足够。

11 个借参席位均为 ECMWF IFS，从该站自身其他 run cycle 的 artifact 借用参数；涉及 CYYZ、KATL、KBKF、KDAL、KLGA、KMIA、KORD、MMMX、MPMG、SAEZ、SBGR。这是现存显式借参路径，不应误报为“45 站所有入座系统均有当前 exact scope 校准”。也不能把 5+3 no-live 证书说成 20+8 formal 晋级。服务重拟合吸收最新已闭日数据本身是已有设计，不将它单独定性为泄漏。

**R1 · 高优先级：选择、验收、服务执行三套不同的形状栈**

位置：[native_ensemble_oos.py:1785](/Users/xiong/weather/bayes_basket_target/native_ensemble_oos.py:1785)、[native_ensemble_oos.py:4610](/Users/xiong/weather/bayes_basket_target/native_ensemble_oos.py:4610)、[native_ensemble_p0.py:3008](/Users/xiong/weather/bayes_basket_target/native_ensemble_p0.py:3008)。

子集竞赛 `_select_minimum_crps_subset` 应用 band，却没有中心／宽度参数；最终 OOS `_shaped` 执行 **band → 中心／宽度**；生产编译执行 **中心／宽度 → band**。即使引用相同 table SHA，顺序也不同，pin 不能弥补这个差异。

使用当前 P0 表 `4f522947…5571b` 和当前宽度 0.75，七桶隔离反例的同一中心桶在验收顺序下为 0.359510，服务顺序下为 0.353780，差 **0.573 个百分点**。这不是收益估计，而是确定性的不等价证明。固定截点 44/45 行同时带这两个变换，缺陷具备现实适用条件。

最小长期修复是由一个 canonical 变换函数统一选择、最终评分和服务，显式传入有序变换、桶合同和参数版本；为三条调用链增加输出向量一致性测试。旧证书需要重新评分，不能只对调代码顺序后继续宣称原证书已经覆盖新算式。

**R2 · 高优先级：同一 holdout 既选组合又给选中的组合发证**

位置：[native_ensemble_oos.py:1632](/Users/xiong/weather/bayes_basket_target/native_ensemble_oos.py:1632)、[native_ensemble_oos.py:1806](/Users/xiong/weather/bayes_basket_target/native_ensemble_oos.py:1806)、[native_ensemble_oos.py:4831](/Users/xiong/weather/bayes_basket_target/native_ensemble_oos.py:4831)。

候选子集按 `common_holdout` 的 CRPS／NLL／Brier 排名，赢家随后仍在同一 `common_holdout` 上生成 `cross_metrics` 并过门。单系统参数未在 holdout 上拟合，不代表“系统成员集合”未在该集合上学习。函数注释中“这不会比单个全量组合更调 holdout”的论证不成立。

隔离反例固定训练误差、权重规则、预测向量，仅把 holdout 真值从 20 改为 24，赢家就从 A+B 改成 B+C；没有第二组外层留出样本。该反例证明选择读取了验收标签，不用于估计偏差大小。`pinned_subset` 路径若只评价固定组合，不能简单归入同等强度的子集搜索偏差；本项针对实际枚举多个候选的路径。

修复应把组合选择与最终评估分开：内层选系统／参数，外层按闭日、strict-as-of 滚动重演整个选择流程；对选择集合、训练集合、验收集合分别保存身份。只把 3 天改成 8 天不能消除这个问题。

**R3 · 中优先级：分布先落有限桶、再按桶中心收宽，会制造结构性零概率**

位置：[center_width_calibration.py:365](/Users/xiong/weather/bayes_basket_target/center_width_calibration.py:365)、[native_ensemble_p0.py:480](/Users/xiong/weather/bayes_basket_target/native_ensemble_p0.py:480)。

成员积分原本保留尾部质量；宽度层把每个桶全部当成桶中心的点质量，再向有限网格搬运。收缩后最外侧可能没有任何点能落回，概率变成精确零，即使没有传入任何观测硬排除。

隔离反例使用五成员 `[-1,3,7,11,15]`、有开放边缘的 15 桶合同，初始所有桶概率均正。宽度 0.75 后两端由约 19.967% 变成 0；把相同仿射变换施于连续高斯混合后再积分，两端各仍有约 **1.627%**。这是“有限桶上的搬运”与“连续分布的缩放”不同的证明，不主张原先 19.967% 应保留不变。

根本修复应保留连续／成员级分布完成几何变换，然后按市场真实边界积分；最终 band 若需要桶尺度可继续在积分后处理。零值来源应能区分官方硬事实、数值下溢和模型近似。不能用随意加一个概率地板替代验证。

相关覆盖缺口：OOS `_probabilities` 使用整数 °C 网格，生产使用市场真实边界及开放尾桶。标准化 °C 评分可用于模型比较，但不能自动证明同一组序数 band、桶中心搬运在 2°F 市场上等价；R1 修复应同时覆盖 34 个 C 站、11 个 F 站和开放尾桶。

**R4 · 高优先级：band 的“前向窗口”不是独立留出；历史重演也未隔离表的可知时间**

位置：[band_reliability_store.py:1605](/Users/xiong/weather/bayes_basket_target/band_reliability_store.py:1605)、[band_reliability_store.py:1296](/Users/xiong/weather/bayes_basket_target/band_reliability_store.py:1296)、[native_ensemble_oos.py:3861](/Users/xiong/weather/bayes_basket_target/native_ensemble_oos.py:3861)。

`band_lane_report(serving)` 用全部服务日估计因子、bootstrap 区间及前后半符号；`_forward_nll_delta` 随后从同一批日里拿最后 40% 评分。对真实拟合函数的隔离调用记录到：20 个拟合日中的最后 8 日，全部同时进入所谓 forward 评分。P0-M/P1 的这个门只能说明训练后段没有明显恶化，不能冒充独立样本外验证。P0 更明确跳过该门，依赖融合 OOS 再评分，因此同时受 R1/R2 影响。

此外，`build_native_ensemble_oos(as_of_utc, max_event_date)` 先无 as-of 限制读取当前 active band／宽度表，再限制历史事件。表可能已经用待评分日或更晚日拟合；`_truth` 也读取当前最新修订。对过去事件做 strict-as-of 重演时，事件日期截止并不能屏蔽这些信息。实际未来预测使用当时已知闭日拟合表是正常行为，本项针对历史证据的独立性和重演口径。

修复：前段拟因子、冻结后只在独立后段评分；最终全样本重拟合与评估参数分别留证。历史重演需要完整的参数表、truth 修订、激活／退役 as-of 解析，保存样本 ID 与时间边界；旧数据上反复选择通过的表不能计成新的独立验收。

**R5 · 中优先级：距离 band 的“峰值不动、轴自洽”前提已被在役数据反驳**

位置：[cross_consensus.py:653](/Users/xiong/weather/bayes_basket_target/cross_consensus.py:653)。

距离以变换前 argmax 为锚；拟合逐格独立，未保证峰值格的因子不小于邻格。当前 P0 表中心因子为 1、邻格为 1.047515，足以让概率接近的邻桶反超。

固定截点实际 P0 行逆回执后，可观察到三个移峰：KBKF `86–87°F → 84–85°F`；KORD `72–73°F → 74–75°F`；ZUUU `27°C → 26°C`。这些都是 band 前后峰值的变化，不是天气预测事后命中结果。

移峰本身未必错误，回执反演也仍可成立；错误是继续保证它“不动点、自带稳定轴”，或按变换后峰值给桶重新分组并当作原拟合格。应显式记录 band 的输入锚／桶序和输出锚，诊断沿原锚评分。是否强制保持峰值属于模型设计选择，需历史实验，不能临时钳位。

**R6 · 中优先级：pin 校验与历史退役语义仍有漏洞**

位置：[band_reliability_store.py:421](/Users/xiong/weather/bayes_basket_target/band_reliability_store.py:421)。

按 SHA 加载只查 SHA，不核对调用者的 stage；内存库反例能以 `stage=P0` 成功读取 P1 表。尚未观察到生产实际错绑，但解析器没有兑现“阶段不混用”的硬合同。应验证 stage、axis、因子合法性与内容哈希，而不是仅相信字符串引用。

同一 SHA 在表退役前读到五格，退役后无条件变成空因子（恒等），接口没有决策 as-of。立即让当前服务停止使用被否决表是已有设计；问题在于历史重演也被今天的退役状态改变。应将表内容与按时间解析的激活／退役事件分开，避免一个 pin 在不同审计日期代表不同算式。

**验证结果与边界**

| 验证 | 结果 |
| --- | --- |
| 第一轮校准／融合／分布／band，13 个模块 | 118 passed |
| 扩展到 P0、cohort、激活合同及 P0-M/P1 链路，10 个模块 | 114 passed，2 failed |
| 同一核心模块反序复测，发生并行源文件变动后的工作树 | 117 passed，1 failed |
| 两项集成失败单独重跑 | 两项均复现 |
| 隔离不变量探针 | 顺序差、holdout 选择依赖、band 拟合与 forward 重叠、尾部归零、峰值移动、跨 stage pin／退役均复现；恒定偏差及三分位偏差共 21 组同网格训练／服务对照一致（最大误差约 6.14×10⁻⁸，来自有限核尾截断） |

集成失败分别为 `taf_native_live_exact_scope_not_unique`，以及测试直接调用另一测试时漏传 `monkeypatch`。反序新增失败是 `test_every_serving_face_carries_the_tiers` 对 `**p0m_serving_tables,` 出现次数期待 2、当时 worker 为 1；它是源码文本断言，且工作树正在并行重构。没有把这些失败混称为已修复，也没有仅凭它们宣称线上故障。本次没有修改这些文件。

未做全历史收益重放、未更换概率参数、未发布新 artifact；所以不报告“修后提升多少收益”，也不宣称模型生产验收完成。当前 truth 表域为 AWC／`official_daily_tmax_c`／ACCEPTED，未发现逐来源最新行在同站日冲突；这不替代 ASOS/AWC canonical truth 全链路专项验收。

证据：[45 站引用及原始回执](/Users/xiong/weather/tmp/model_layer_audit_20260913/served_evidence.json)、[质量与移峰统计](/Users/xiong/weather/tmp/model_layer_audit_20260913/quality_summary.json)、[隔离反例](/Users/xiong/weather/tmp/model_layer_audit_20260913/invariant_evidence.json)、[源码版本与摘录](/Users/xiong/weather/tmp/model_layer_audit_20260913/source_manifest.json)。复现入口在同目录，测试输出分别为 `core_tests.log`、`pipeline_tests.log`、`reverse_tests.log`、`isolated_failures.log`。

**下一个主线任务**：先统一选择／验收／服务的分布算式与真实桶合同，再重建严格 as-of 的外层 OOS；随后评估连续分布变换和 band 独立验证。修复顺序应由证据可信性驱动，不能先继续调 band 因子来掩盖上游不一致。

## 处置状态（2026-09-13 晚）

| 项 | 状态 | 提交 | 生效与影响面 |
| --- | --- | --- | --- |
| R1 三套形状栈 | 已修 | 38725ea8（代码被并行会话的扫尾提交带入）、35730290（测试） | `served_shape.apply_served_table_layers` 是唯一的"中心宽度→band"组合点；编译、门、消融、等权诊断、子集赛都经由它，倾斜锚统一读加权校准中心。编译逐字节不变。改前代码上赢家 CRPS 0.056097、发证 0.057264，改后为同一个数。只量 R1：45 站 607 个 scope 中 81 个（13.3%）发证席位集合变化。15:21Z 重启生效 |
| R6 pin 合同 | 已修 | 688dc11a | 按 sha 加载校验 stage、因子合法、内容哈希（接受唯一一张 P0 claim 轴旧口径表 9963e3fe）；退役判定可带 as-of。线上 120 张表本 stage 全过、跨 stage 全拒；42 个在役 pin、31,009 件 artifact 全部可加载 |
| R4 历史重演 | 已修 | a76bde60 | `table_as_of` 按激活事件解析某一时刻在役的 band、宽度、站宽表；构建函数新增 `tables_as_of_utc`；服务集读取加上界；时间比较统一解析为时刻。前向构建不变 |
| R4 band 前向窗 | 已修 | 95a481b9 | 前向准入改为前 60% 天拟因子、后 40% 天打分，服务仍用全样本因子；gate 迁移与整表诊断同口径。旧代码会以 1.145 放进前段拟不出的格。三阶段当前都在攒证据，今天惰性，约 09-21 首次生效 |
| R2 选择即发证 | 已测量，未落地 | 4e6c5dce | 见下表。两种修法都改变 P0 入座，需裁决 |
| R5 峰值移位 | 已记录 | b1756d44 | 距离轴回执记 `anchor_bucket` 与 `served_peak_bucket`；宪章 105 加补记。09-12..13 距离轴 P0 行 6.7% 移峰。拟合车道本就按逆回的输入锚分格，不受影响 |
| R3 结构性零 | 已测量，降级为不落地（09-17 实验） | — | 见下表与「R3 实验」。1.17% 零桶落入几乎全是已退役站宽表 v1 的行；现役算术 0.13%；连续变换同 λ 下 RPS 零差 |

**R2 读数**（三个互不重叠重演起点 09-03/07/11 合并，1,148 个枚举型子集赛，45 站，站聚类 90% 区间；六个起点方向全部一致）

| 口径 | 读数 |
| --- | --- |
| 改在发证窗之前的 3 天选，赢家不同 | 79.8% |
| 发证 CRPS 相对诚实嵌套赢家的乐观幅度 | +35.3% [+30.5%, +40.9%] |
| 发证 NLL 乐观幅度 | +23.0% [+18.3%, +29.0%] |
| 整个 cohort 入座 vs 诚实嵌套赢家，CRPS | −1.0% [−3.2%, +1.4%] |
| 整个 cohort 入座 vs 诚实嵌套赢家，NLL | −1.4% [−2.9%, +0.2%] |
| 只因选择读了发证标签才过门 | 88 / 1,146 |
| 枚举型竞赛中凑得出前置 3 天选择窗的比例 | 20%–44%（按起点） |

两种修法都需要裁决：（一）取消子集赛、整个 cohort 入座——与宪章第 42 条退役时保留的"P0 组合成员资格由子集赛授予"一款（2026-08-15 裁决）直接冲突；（二）保留子集赛但改成嵌套选择——不违背该款，但 56%–80% 的枚举型竞赛凑不出选择窗，会大面积少发件。

**R3 读数**（P0 服务行，决策 09-05 起、事件日到 09-12）

| 口径 | 带宽度层 | 不带宽度层 |
| --- | --- | --- |
| 行数 | 8,153 | 1,278 |
| 含精确零桶的行 | 95.3% | 6.0% |
| 精确零桶占全部桶 | 27.2% | 0.7% |
| 结算真值落进精确零桶 | 95（1.17%） | 0 |

两组年代与 artifact 构成不完全可比。现役 λ=0.75 是在离散搬运下拟出的，改成连续变换而不重拟会让服务宽度再收窄，所以不能直接落地。

**R3 实验（2026-09-17，`tmp/r3_zero_bucket_20260917/`）**

取样：P0 服务行 7,452 条（决策 09-03..09-16，每站日每三小时块一条），宽度层输入按血统重建（成员 → 成员校准 → 桥接 → 辐射平移 → 邻格 → 尾部），再按 receipt 重放宽度层与 band，7,452/7,452 逐桶复现。`_identity_counterfactual` 不可用：记录输入的行只有 09-16 的 102 条，λ=0.75 下 7,211 行秩亏。

| 口径 | 读数 |
| --- | --- |
| 零桶落入，宽度层前 / 后 | 0.11% / 1.11% |
| 其中 receipt `station_width_scale`=0.5 的行（09-06..09-10，1,611 行，站宽表 v1 在役期） | 4.59% |
| 现役算术（pooled 0.75、scale 1，5,464 行） | **0.13%**（ZUUU 3、ZUCK 2、RCSS 1、LFPB 1） |
| scale=0.5 行 served NLL 对 identity | 2.13 对 1.56（+0.57 nat），准入门 09-06 以前向 +0.028/+0.077 放行，09-10 15:27Z 才被否决 |
| 同 λ=0.75、scale 1 行：连续变换（桶内均匀 + 开口边指数尾）对离散搬运，ΔNLL | −0.010 [−0.035, +0.007]（≥09-11 −0.019 [−0.055, +0.005]） |
| 同上，ΔRPS | −0.0004 [−0.0017, +0.0007] |
| 同上，剔除零桶行 | ΔNLL −0.002、ΔRPS −0.0003 |
| 前向重拟 λ（拟 09-03..10 / 评 09-11..16） | 离散 0.875、连续 0.85；拟 <09-07 / 评 ≥09-07：0.725 / 0.70。漂移随窗口，与变换无关 |
| leak 臂 (1−ε)·moved+ε·input、高斯核臂 h=0.25/0.5 | 与离散逐字相同 / 不优于均匀+指数尾 |

处置：不换服务算术。换连续变换是服务算术变更（版本 bump、宽度/band/λ 三车道重攒证据），买到的是 RPS 零差、NLL ≤0.02 nat 且全部来自 0.13% 的零桶行。真正的风险是合成 λ 可低到 0.375（`MIN_STATION_WIDTH_SCALE` 0.5 × 0.75）：站宽 v2 车道再次激活前，合成 λ ≥ 0.5 的硬地板待裁决。跨 09-06..09-10 的任何 P0 宽度测量必须按 receipt 的 `station_width_scale` 分组。

**顺带修复的既有红测试**：中心锚 3 条、采集就绪 2 条、AIFS 入口 3 条、历史 republisher 合同 3 条（1bc37cd7，新增 `tests/conftest.py` 统一映射夹具站 `TEST`）；`test_center_width_tiers` 的 P0-M 服务面计数（a76bde60）。生产链端到端 P1 夹具补齐 09-02 后的执行证据（7e3cef27），但仍卡在下一层经济 HOLD。

**仍红、未处理**：`test_point_forecast_scope_contracts` 两条（CMA_GRAPES_GLOBAL_POINT 无 scope 合同）与 `test_forward_taf_is_observational_and_never_enters_p0m_oos`，均属宪章 133 并行会话；生产链端到端 4 条与其包装测试，属执行层篮子目标夹具债。

**服务算术护狗**：P0/P0-M/P1 指纹已动，`--audit` 判定需等 09-14 04:20Z 各车道重拟后才能下结论，届时再决定 acknowledge 还是 bump；R1 的 13.3% 席位变化意味着不能凭"编译逐字节不变"直接 acknowledge。R4b 已随 16:17Z 那次常驻重启生效；R4a 与 R5 已登记排队，随下一次重启生效。
