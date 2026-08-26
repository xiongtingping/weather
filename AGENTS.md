# Polymarket Auto Buyer（本地）

## 目标
- 生产执行唯一为纯 YES 篮子：篮子只消费已物化的 P0/P0-M/P1、regime 与官方 METAR/SPECI 形成的硬事实排除集合；禁止任何自动买 NO 策略。

## 工程原则（必须遵守）
- 根因优先、零技术债务：先全链路分析再给长期方案与最小改动。
- 数据治理总则：同一事实必须通过唯一 canonical schema 和唯一 lineage 进入 SSOT；字段或语义无法一一对应时立即 fail closed，禁止改名、猜测、默认补值或跨表兼容。
- 生产仅保留三个同一用户域服务：`com.weather.weather_basket_yes`、`com.weather.market_ws`、`com.weather.clob_order_service`；固定运行于`gui/$(id -u)`，禁止user/system双活。旧 `com.weather.reported_trajectory_no` label 必须退役。root-owned旧LaunchDaemon只允许在一次性迁移中以不弹密码的`sudo -n`退役；迁移后安装、切换、重启和验收不得依赖sudo。
- UI、monitor、Bayes、模型扫描、市场 REST 刷新、任务队列和行情历史 tick 写入均停用。
- 正式观测层由唯一优先级合同仲裁独立lineage：可识别美国ASOS站依次选择覆盖完整且新鲜的IEM/NCEI一分钟ASOS（220）、NOAA MADIS HF-METAR五分钟ASOS（210），否则选择官方AWC整点`:00` METAR/有效SPECI（100）；非ASOS站直接选择AWC。IEM必须标记`aggregator_republisher`，MADIS必须标记`official_republisher`；二者不得冒充METAR、AWC或native。ASOS只有绑定自身20 train+8 holdout历史exact-match P1/OOS artifact后才能进入no-live P1；两源原始事实分别保存，只有`reported_trajectory_canonical_station_day_bounds`可给出唯一赢家，禁止逐值混源取最大。AMSC及旧AMSC lineage固定为`retired_history_only`。
- Open-Meteo ICON/GEM/CMA/AIFS 等均为显式 `aggregator_republisher`：原生对象缺失、无效或未及时到达时，可作为同一站点×事件日的 no-live P0 fallback；必须保留 republisher lineage、明确 native 不可用、且绑定独立严格历史 OOS 激活。不得伪造 native publication/arrival、进入 native arrival index、进入 P1 或 formal/live authority；同模型存在官方 native 证据时始终优先 native。
- 回应使用中文并称呼“熊先生”。
- 默认聚焦熊先生当前明确要求；不复述历史问题作为背景噪声。
- 每项长任务先写清可验证终点线（Goal）；终点线未达成前不得以局部测试或单站结果宣称完成。
- 长任务必须在关键阶段强制复盘“已完成/仍阻断/下一步”，并定期清理无关上下文，避免重复抓取、重复实现或偏离四大方案。
- 主线任务默认自动推进到可验证闭环，不在普通中间确认点停住。
- 熊先生已明确主线默认无需逐步授权；仅在扩大任务范围或不可逆外部变更时停下说明。
- 默认直接实施并验证，不以 review-only/仅建议作为交付；明确退役的旧代码直接删除，不保留兼容分叉。
- `ΔBasketScore v2` 的未来日时序观察与 `production_no_live`、RKPK 单站闭环和 45 站扩展并行；未来/forward 数据不构成 OOS、硬门或晋级依据，未完成不得阻断生产 no-live 主链。参数/权重 artifact 的 `FORMAL_PROMOTED` 与 live 晋级只接受闭日、strict-as-of 历史 OOS；P0 模型组合自身要求的离线严格 OOS artifact 同样只用历史数据。
- 未来日/forward 数据一律登记为 `observational_candidate_not_oos` 到达、接口与时序验证证据：不是 OOS、不计入任何 OOS、20 train/8 holdout 或 TAF 24 样本门，也不阻断 `production_no_live`；只有已闭日且 strict-as-of 的canonical ASOS优先/AWC后备truth才能形成严格OOS。Open-Meteo可作为显式`aggregator_republisher`的历史同日calibration/OOS fallback，并在该历史激活存在时服务原生缺失的no-live P0；仍绝不进入native arrival、P1或formal/live authority。
- 概率阶段唯一口径为：NWP生成P0；校准后TAF TX先确认/有限修正，云量、降水和风场再做物理一致性二次确认，二者原子生成P0-M；canonical ASOS一分钟/AWC METAR-SPECI观测生成P1。禁止P0-F/P0-F^r分支。
- 篮子 lane 仅可读取已物化且带 lineage 的 P0/P0-M/P1、regime、官方报文硬事实排除集合和官方 CLOB WS 证据，缺证据即 fail closed；不得生成 BUY NO intent。
- 外置盘仅用于非关键、无用数据的异步冷归档；禁止作为生产数据库备份、回滚、切换、readiness 或服务启停依赖，任何外置盘异常不得影响 AWC/METAR/SPECI、SSOT 或交易。
- 禁止把日常生产操作设计为交互sudo；用户域迁移后所有常规服务控制必须由当前登录用户完成。
- 每次阶段性收尾时自动说明“下一个主线任务”。
- 若熊先生已否定某个根因假设，不得再次作为当前根因。
- 若本文件需要新增/修订：保持精简（≤100 行）。

## 事实与执行口径（硬规则）
- active model-product registry只保留官方原生全球集合成员产品：`ECMWF_IFS_ENS_OPEN_PF50 / ECMWF_AIFS_ENS_GLOBAL / DWD_ICON_GLOBAL_EPS / NOAA_GEFS_05 / ECCC_GEM_GLOBAL_ENS`。正式P0的每个`station_id × lead_bucket × run_cycle` exact scope必须由其中至少两个正权重、不同`independence_family_id`系统组合生成，并分别绑定原生成员、arrival/receipt、闭日历史strict-as-of OOS和exact activation。AIFS Single及全部确定性、区域确定性、Open-Meteo republisher产品均退出active registry；历史对象只保留原lineage，不得进入P1或formal/live authority。
- 结算单位是站属性：C 市场每档 `1°C`，F 市场每档 `2°F = 1.111°C`；交易全域 45 站 = 34 个 C 站 + 11 个 F 站（F 全为美国 ASOS：`KATL/KAUS/KBKF/KDAL/KHOU/KLAX/KLGA/KMIA/KORD/KSEA/KSFO`），另 4 个无绑定站的 C 单位城市不入全域。桶宽只能从 `basket_target_market_registry.registry_json` 的 `lower/upper` 现算，禁止按单位硬编码常数；以 °C 计的量（σ、包络、位移、硬下界、CRPS）跨站聚合前必须已换算，以桶计的规则（±1/±2 偏移、3–4 桶窗、两层扩展）是序数规则不换算，单桶概率阈值跨单位不可比。详见宪章第 53 条。
- 前端不是生产消费者，不得为了展示增加热路径读写。
- 执行阻断原因唯一来源：`auto_trade_decisions`。
- `reported_trajectory_observations`、`station_day_bounds`、decision、intent 和资本预留必须原子提交。
- 重复报文内存去重且零写入；轮询诊断无新报文时最多每来源每 60 秒一次。
- 所有 target 采集、解码、测试和证据构建必须资源有界：进程 RSS、响应/单次累计/临时落盘设硬上限，采用流式处理并保留最低剩余磁盘；任一超限或资源探针不可用立即 fail closed。
- 只保留参与 target lineage、解码或审计的最小原始对象/回执；临时拼接物、失败残片、重复副本和无引用旧输出不得继续保存。
- 盘口只允许官方 CLOB WebSocket；目标 YES token 独立原子快照，不写 `event_market_ticks`。
- 下单只允许本机唯一 gateway；写锁外取盘口，篮子优先使用内容寻址的 post-only GTC Maker，紧迫收口只允许在确认撤销 Maker 后使用有价格上限的单桶 FOK；每次成交后重算目标库存与利润曲线，不经 outbox 秒级轮询。

## 读 SSOT 前必查（命名口径，2026-08-26 全仓排查）
- **列名不承诺量纲，先 `select distinct` 再写 where。** 117 张表共用 625 个列名，同名不同义是常态而非例外。以下六条已实测确认，按名字直觉写出的查询会静默给出错误答案。
- `station_id` 不总是站：真 ICAO / 通配 `*` / 空串三义并存。`basket_delta_v2_fill_artifact` 83,132 of 86,427 行是 `*`，`basket_target_market_registry` 34,738 of 357,302 行是空串。把 delta-v2 系表按 `station_id` 内联到 `calibration_station_registry` 会静默丢掉 96% 的行；`GROUP BY station_id` 会多出一个叫 `*` 的"站"。
- `stage` 在三张 delta-v2 表里恒为 `*`（`basket_delta_v2_policy_artifact` 57/57、`basket_delta_v2_policy_activation` 21/21、`basket_delta_v2_fill_artifact` 86,427/86,427），**零行**是 `P0/P0-M/P1`。按具体 stage 过滤这三张表恒得空集，而覆盖该 stage 的行就在 `*` 里。解析走 `production_io.resolve_delta_v2_*`，不要自己写 where。
- `season_bin` 三套互不相交的词汇同时在用：`summer/winter`、`JJA`、以及宪章 62 的站点本地相位 `PEAK/DESCENT/TROUGH/ASCENT/ALLYEAR`。同一批南半球站（FACT/NZWN/SAEZ/SBGR）在三张表里分别被标 `winter`、`JJA`、`TROUGH`——`JJA` 是日历断言，恰是它们处于 `TROUGH` 时携带的标签，名字说反话。跨表 `GROUP BY season_bin` 会把同一批站日切成三个互不重叠、各自样本不足的分区。
- `lead_bucket` 两套词汇同列共存（`d0/d1/d2/d2_fallback` 与 `h00_12/h12_24/h24_48/h48_plus`，宪章 47 只认后者），且 `d0` 在 `METAR_PHYSICAL_PATH_TMAX` 与 `OFFICIAL_AIRPORT_TAF_TX` 上是第三义"无提前量可言"。`d1` 一个标签横跨三个小时桶。
- `run_cycle` 混了两个坐标轴：真模型 run（`00Z/06Z/12Z/18Z`）与日偏移（`PREVIOUS_DAY1/PREVIOUS_DAY2`），外加 `MIXED_NATIVE_CYCLES`/`AGGREGATOR_UNKNOWN_NATIVE_CYCLE` 两个哨兵。判例：`WHERE run_cycle='18Z' AND lead_bucket='d2'` 在 `calibration_historical_asof_model_run` 上返回 **0 行**，而两天外样本实际 7,762 条，全在 `(PREVIOUS_DAY2,d2)` 与 `(00Z,d2)` 里。
- `maturity_bin` 与 `lead_bucket` 取值域逐字相同但量不同（事件还有多远 vs 这个 run 有多旧），**没有任何一张表同时含这两列**，所以错误 join 会正常返回行、不报空。唯一换算点是 `production_io.derive_policy_maturity_bin`；不要在 SQL 里让两者相等。
- 时区只从 `calibration_station_market_timezone_binding` 或 `basket_target_market_registry` 逐站取；`calibration_station_registry.timezone_name` 是种子包原文，45 站中 33 站错标 `America/New_York`。

## 纯 YES Basket（当前唯一生产策略）
- `com.weather.weather_basket_yes` 是canonical ASOS优先/AWC后备正式观测层的纯YES篮子唯一驻留服务名；旧`com.weather.reported_trajectory_no` label与`reported_trajectory_no_v1` hard-NO策略均已退役，不得加载、决策或下单，不得作为服务别名，不得启动第四个生产服务。
- 篮子按已物化后验、方向侧覆盖价值、全情景利润曲线、集中度、预算和真实 WS 深度求唯一目标库存，不设固定桶数、4→3→2或阶段内等份。未成交 Maker 不计入已实现地板但必须计入 confirmed pending；每次成交后重算目标库存、净成本和利润曲线。默认只增不减；显式角色调仓时，仅允许卖出 S/B/L/C 桶中恢复 `M>S>A>B>F` 所需的超额份额，M/A/F 桶不得卖出、不得用卖出控制预算。卖出必须使用官方 YES bid 逐档深度并在成交后重算净成本和地板，未知成交立即 reconcile_required。边缘桶允许作为中心。
- 篮子中心只能来自已物化 P1；P1 缺失时使用最新P0-M，TAF未到时才使用P0；regime只作方向一致性闸门，不得平移概率中心。
- 篮子风险层固定区分 `underreach_risk` 与 `upside_breakthrough_risk`：P1 主桶 `M` 未达成时，按 bucket contract 的 `predecessor(M)`、`predecessor²(M)` 生成下方尾部；官方实况形成新高 `R` 后，才按 `successor(R)`、`successor²(R)` 生成上行突破桶。`P1=30℃、当前已报最高温=25℃` 到 `29℃/28℃` 仅是说明性示例，绝对温度、桶宽和边界不得硬编码；风险层不得修改 P1。
- 赔率优化必须按官方 CLOB WS 逐档 `all_in_yes_ask` 做篮子级增量覆盖价值；`ShortfallCoverage` 和 `BreakthroughCoverage` 只影响排序、预算和选桶，不是新概率。硬事实排除、lineage、证据新鲜度、盘口深度、预算及立即执行/已成交部分的 `sum(all_in_yes_ask)<1` 任一不满足即 fail closed；未成交 staged GTC 继续遵守既定独立挂单规则。
- 极端赔率只允许用于重塑自有结算利润曲线：新增 `x` 份桶 `b`、全包价为 `a_b` 时，命中 `b` 的情景增益为 `x×(1-a_b)`，未命中情景成本为 `x×a_b`；必须以 `ΔBasketScore>0`、尾部覆盖价值、P1/lineage、集中度、预算和真实 WS 深度共同决定，禁止按 `1/a_b` 高赔率或低价单独重仓。`reservation_value` 不是概率，不能改写 P0/P1 或生产状态。
- 未达成或突破风险最多向任一方向扩展两层；两类风险同时升高时降低集中度并收紧证据/盘口门，不得用无限扩桶替代证据。动态选桶必须服从正库存缺口、Maker保留价/等待门、紧迫FOK上限、成交后重算和`reconcile_required`。
- 旧生产线标记 `019f7a84-53ec-7183-9c5f-7f9cfa62e1f0` 已按熊先生指令退役，不再作为保护或运行依赖；用户域三个共用服务只承载 target `production_no_live` 主线，system 域同名服务必须为零。
- 篮子 Maker 允许确定性部分成交并按实际成交重算；撤单未确认、成交结果不明或迟到回报立即进入 `reconcile_required`。紧迫FOK单桶全成或零成，失败后禁止猜测重发，必须先重算当前SSOT。
- 新报文仅在推进站点当日已报最高温时触发；同温和旧温不交易。
- 官方报文硬下界只用于从纯 YES 篮子支持集剔除已数学排除桶；不得据此创建、补仓或退出任何 NO 仓位。
- 更正/修订报使站点日停牌并人工复核，禁止自动反向交易。
- 旧 Bayes/Target Realization 代码只保留离线历史，不得启动、读取或 fallback。

## 时区与统计口径
- 日口径固定 ET（`America/New_York`）。

## 日常静态配仓计算协议（熊先生指定）
- 熊先生只需提供：日期、城市、温度桶、资金量、主桶/副桶；未提供已有份额时按新篮子 `q=0` 计算。
- 该模式仅拉取官方 CLOB WebSocket 真实 YES 卖盘逐档深度；不读取或等待概率、regime、模型或天气字段，不提交订单。
- 主桶、副桶严格按熊先生输入；其余选中桶作为保底桶，默认主>副>保底，最小严格份额差 1 份。
- 默认安全垫为 0.01 USDC，资金量为上限而非强制用满；输出每桶份数、逐桶成本、总成本、各情景结算利润及每桶最低/最高利润。
- 每次计算必须复核真实深度、预算和结算地板；盘口缺失或报价过期仍阻断。立即成交的 FAK 仍要求 `sum(all_in_yes_ask)<1`，但 staged 模式的未成交 GTC 可在该合计限制之外挂出，单笔价格必须 `<1.00`；挂单未成交不计入已实现结算地板，成交后只补实际差额并重算。生产篮子主线仍遵守逐腿 FOK 约束。
- 角色调仓卖单的未成交部分不改变持仓地板；成交后以实际 bid proceeds 计入净结算成本，必须重新计算最低/最高利润。

## 常用运行（本地）
- 一次性用户域迁移：`bash scripts/migrate_weather_basket_yes_to_user_domain.sh --confirm-user-domain-production-no-live`
- 用户域安装/刷新：`bash scripts/targetctl.sh restart`
- target-only概率切换：`bash scripts/cutover_bayes_basket_target_probability_evidence_no_live.sh --confirm-target-probability-evidence-production-no-live`
- 服务状态：`bash scripts/targetctl.sh status`
- 只读验收：`python3 scripts/weather_basket_yes_production_acceptance.py`
- 单次来源诊断：`python3 scripts/weather_basket_yes_daemon.py --once awc`
