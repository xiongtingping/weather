# Polymarket Auto Buyer（本地）

## 目标
- 生产下注规则为宪章 231 的 Proper 下注线（2026-09-26 熊先生裁决「实盘上线，p＝天气模型」），现行形态由宪章 265 定（2026-10-10 裁决「改」）：回到宪章 231 标准规则——整条梯子、双边、份额∝|s|，s＝log(b/q)，b＝兑现层（宪章 254）v2 信念，含 AIFS 集合／AIFS 单成员／GEFS「最新一轮−上一轮」改动×新鲜度 exp(−h/6)（模型唯一测到的市场外分辨率；水平没有），只在 v2 拟合门准入且为当前代码所拟时读，否则等待 `belief:<状态>`；post-only 挂各腿自身买一+1、每单在场 15 分钟（`rung_horizon_seconds=900`，不越过 36h 线）、未成交重挂至成交，每次重挂复读 b；每轮按最新 P0 决策评估（`freshness="envelope_validity"`）；P0/P0-M/P1 各阶段都开、按第 260 条优先级判站日阶段，b 按阶段各自拟合过门，距结算 36h 窗口取消（10-10，宪章 231 补记一/二）；`rule=log_proper_maker_realisation_belief_v4`、`belief="realisation_b"`、`sides=["YES","NO"]`、`buckets="all"`（宪章 249 的只买众数 YES 为拟合限制版，v3 仍可选）；被对标簇封住的 NO 只卖不反手；每腿挂满看板「Proper每笔」预算 `stake_usdc`（默认 1 U，不低于交易所最小单、不足 1 U 补到 1 U，受现金封顶；宪章 252）；篮子车道已关（照常编译、记决策），L4 已下线（代码保留）。跟单线（宪章 256/257/266/268）缺省关：只跟 8 钱包簇的吃单首笔，不等比例、不跟卖出、日预算不设上限（10-09）；现行 v4（宪章 268，10-12「改」）：只跟 YES、不看我方概率与方向、每腿平注「跟单每笔」（裁决 2 U，不足 5 股补到 5 股）、按出现先后跟，NO 腿停跟；v3 三档（宪章 266：共享方向信号 `direction_signal`＝ECMWF 动量＋四特征模型，任何车道可读；大单 ×2）代码与车道文件参数保留，`rule` 改回即回拨；方向信号的物理特征来自 Open-Meteo（aggregator），只作执行选择、不进 P0/P0-M/P1，是本条显式例外；10-09「3+5」起滑点 15 美分、只吃单不挂单（30 分钟窗）；簇成交由只读快读通道每 15 秒拉进内存，本线在生产周期检查点最短 20 秒跑一次（补记三）。除宪章 231 的 NO 腿（现停）与宪章 234 远尾做空线（2026-09-28 裁决，按市场自身价格结构吃单买 NO；宪章 251 起另须在役 P0（251（六）起平移到市场中心后）给该桶的 p 低于 YES 买一，首次可发时判一次、判否整日封桶；别的车道在别的桶上已确认的挂单不挡它）外禁止自动买 NO（宪章 258：Proper 线以对标 8 钱包簇为筛子——簇在该桶 YES/NO 份额分开记、配对视为锁利、按时间轴此刻立场与本线相反即封桶撤单（立场转回即解封）、所持 YES 全部卖出（车道停用也卖），flip 遗留的 NO 若簇净站 YES 则卖出并反手买回 YES，本线不买 NO，买 NO 归跟单线（v4 起跟单线也停 NO），两线可重叠；宪章 257 跟单线 v2 可买 NO、无日预算上限——2026-10-09 裁决「可以买no。40 U 日预算改为不设置上限」）；宪章 256 跟单线 v1（等比例跟随、含跟卖）代码保留未启用。宪章 267（10-11「挂牌前把f算好。接入确定性预报源」）：挂牌前预算 F＝六家原生确定性（AIFS 单成员/IFS HRES/GFS/AIGFS/GDPS/ICON 全球，直读发布方对象、不经 Open-Meteo；ARPEGE 只采集不进 F）中位＋全队平移＋0.5×站修正，常驻每 10 分钟写 `data/runtime/listing_forecast_f_v1.json`，执行层数字、不进 P0/P0-M/P1、暂无车道读；原生四家只进 F、不进 P0 特征；宪章 263 挂牌采集遇「必需家族未发布」45 秒后重试（≤20 次）；确定性家族名单时区只读 `calibration_station_market_timezone_binding`。

## 工程原则（必须遵守）
- 单写者调度法（2026-08-29，源自一周四起饥饿/停摆事故）：SSOT 写者轮转只准放真正写 SSOT 的车道；**只读工作（渲染、报表、探针的读段）一律走独立 executor，不占写者槽**；维护类车道（sweeps/采集）在生产到期时**不得连占两个槽**（生产保底交替是最坏情形下限）；单个槽位超过 ~300 秒的成员必须拆分或移出；SSOT 与 archive 的 WAL 超 256M 且写者空闲时由 daemon 自动 TRUNCATE。新增任何托管 sweep 前先回答：它写 SSOT 吗？一轮多久？不写或超长就不进队。

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
- 未来日/forward 数据一律登记为 `observational_candidate_not_oos` 到达、接口与时序验证证据：不是 OOS、不计入任何 OOS、20 train/8 holdout 或 TAF 24 样本门，也不阻断 `production_no_live`；只有已闭日且 strict-as-of 的canonical ASOS优先/AWC后备truth（或宪章 225 的结算源修补真值）才能形成严格OOS。Open-Meteo可作为显式`aggregator_republisher`的历史同日calibration/OOS fallback，并在该历史激活存在时服务原生缺失的no-live P0；仍绝不进入native arrival、P1或formal/live authority。旧回放与旧版本服务记录按宪章 255 写明版本口径、分版本段报；只有现行段服务记录或现行链严格 as-of 重放可作现行规则依据。
- 概率阶段唯一口径为：NWP生成P0；校准后TAF TX先确认/有限修正，云量、降水和风场再做物理一致性二次确认，二者原子生成P0-M；canonical ASOS一分钟/AWC METAR-SPECI观测生成P1。禁止P0-F/P0-F^r分支。校准分两层（宪章 259）：A 分布校准评整条 P(T=k)（211/240：LogScore 主裁、CRPS、Brier、PIT），B 交易概率校准只评被交易的桶（二元 LogLoss＋可靠性图，对市场成对、只认前推），B 只评价、不进校准链、不作训练目标。P1 存在后 P0-M 当日决策不是独立信念（宪章 260）：读我方概率一律 `stage_authority.served_belief_row`（P1 > P0-M > P0，不取任一阶段最新行），宽度车道剔除被 P1 取代的 P0-M 格。F 站 ×>36h 的 P0 向量固定尾混 ε=0.10（宪章 261，`with_f_far_tail`，证书标签 `f_far_tail_mix_v1`，开关 `PM_P0_F_FAR_TAIL_MIX`）。
- 篮子 lane 仅可读取已物化且带 lineage 的 P0/P0-M/P1、regime、官方报文硬事实排除集合和官方 CLOB WS 证据，缺证据即 fail closed；篮子不得生成 BUY NO intent（NO 腿只属宪章 231 与 234）。
- 外置盘仅用于非关键、无用数据的异步冷归档；禁止作为生产数据库备份、回滚、切换、readiness 或服务启停依赖，任何外置盘异常不得影响 AWC/METAR/SPECI、SSOT 或交易。
- 禁止把日常生产操作设计为交互sudo；用户域迁移后所有常规服务控制必须由当前登录用户完成。
- 每次阶段性收尾时自动说明“下一个主线任务”。
- 若熊先生已否定某个根因假设，不得再次作为当前根因。
- 若本文件需要新增/修订：保持精简（≤100 行）。

## 事实与执行口径（硬规则）
- active model-product registry只保留官方原生全球集合成员产品：`ECMWF_IFS_ENS_OPEN_PF50 / ECMWF_AIFS_ENS_GLOBAL / DWD_ICON_GLOBAL_EPS / NOAA_GEFS_05 / ECCC_GEM_GLOBAL_ENS / NOAA_AIGEFS_025`（第六套按 2026-09-24 裁决加入，宪章 212）。正式P0的每个`station_id × lead_bucket × run_cycle` exact scope必须由其中至少两个正权重、不同`independence_family_id`系统组合生成，并分别绑定原生成员、arrival/receipt、闭日历史strict-as-of OOS和exact activation。AIFS Single及全部确定性、区域确定性、Open-Meteo republisher产品均退出active registry；历史对象只保留原lineage，不得进入P1或formal/live authority。
- 结算单位是站属性：C 市场每档 `1°C`，F 市场每档 `2°F = 1.111°C`；交易全域 48 站 = 37 个 C 站 + 11 个 F 站（F 全为美国 ASOS：`KATL/KAUS/KBKF/KDAL/KHOU/KLAX/KLGA/KMIA/KORD/KSEA/KSFO`；2026-10-04 接入 LLBG/LTFM/UUWW），香港（无结算站）与郑州/济南（挂牌缺结算时区）不入全域。桶宽只能从 `basket_target_market_registry.registry_json` 的 `lower/upper` 现算，禁止按单位硬编码常数；以 °C 计的量（σ、包络、位移、硬下界、CRPS）跨站聚合前必须已换算，以桶计的规则（±1/±2 偏移、3–4 桶窗、两层扩展）是序数规则不换算，单桶概率阈值跨单位不可比。详见宪章第 53 条。
- 前端不是生产消费者，不得为了展示增加热路径读写。
- 执行阻断原因唯一来源：`auto_trade_decisions`。
- `reported_trajectory_observations`、`station_day_bounds`、decision、intent 和资本预留必须原子提交。
- 重复报文内存去重且零写入；轮询诊断无新报文时最多每来源每 60 秒一次。
- 所有 target 采集、解码、测试和证据构建必须资源有界：进程 RSS、响应/单次累计/临时落盘设硬上限，采用流式处理并保留最低剩余磁盘；任一超限或资源探针不可用立即 fail closed。
- 只保留参与 target lineage、解码或审计的最小原始对象/回执；临时拼接物、失败残片、重复副本和无引用旧输出不得继续保存。
- 盘口只允许官方 CLOB WebSocket；目标 YES token 独立原子快照，不写 `event_market_ticks`。
- 下单只允许本机唯一 gateway；写锁外取盘口，执行族由宪章 65 裁定为 taker（2026-08-27，「整篮 EV」是执行层唯一的法），`EXECUTION_MODE` 只在 `delta_v2_runtime` 声明（纸面账本 `scripts/paper_ledger_aligned.py` 直接调生产入口，不再另立同名常量）；**迁移未完**：FOK 候选已可枚举但预留价/授权层仍拒绝 FOK action，故生产默认仍为 maker，翻转默认必须与该层修复同一个提交落地；旧口径「优先 post-only GTC Maker、紧迫收口才用 FOK」已被宪章 65 取代，`confirmed_cancel_for_fok` 握手已退役；每次成交后重算目标库存与利润曲线，不经 outbox 秒级轮询。

## 读 SSOT 前必查（命名口径，2026-08-26 全仓排查）
- **列名不承诺量纲，先 `select distinct` 再写 where。** 117 张表共用 625 个列名，同名不同义是常态而非例外。以下六条已实测确认，按名字直觉写出的查询会静默给出错误答案。
- `station_id` 不总是站：真 ICAO / 通配 `*` / 空串三义并存。`basket_delta_v2_fill_artifact` 83,132 of 86,427 行是 `*`，`basket_target_market_registry` 34,738 of 357,302 行是空串。把 delta-v2 系表按 `station_id` 内联到 `calibration_station_registry` 会静默丢掉 96% 的行；`GROUP BY station_id` 会多出一个叫 `*` 的"站"。
- `stage` 在三张 delta-v2 表里恒为 `*`（`basket_delta_v2_policy_artifact` 57/57、`basket_delta_v2_policy_activation` 21/21、`basket_delta_v2_fill_artifact` 86,427/86,427），**零行**是 `P0/P0-M/P1`。按具体 stage 过滤这三张表恒得空集，而覆盖该 stage 的行就在 `*` 里。解析走 `production_io.resolve_delta_v2_*`，不要自己写 where。
- `season_bin` 三套互不相交的词汇同时在用：`summer/winter`、`JJA`、以及宪章 62 的站点本地相位 `PEAK/DESCENT/TROUGH/ASCENT/ALLYEAR`。同一批南半球站（FACT/NZWN/SAEZ/SBGR）在三张表里分别被标 `winter`、`JJA`、`TROUGH`——`JJA` 是日历断言，恰是它们处于 `TROUGH` 时携带的标签，名字说反话。跨表 `GROUP BY season_bin` 会把同一批站日切成三个互不重叠、各自样本不足的分区。
- `lead_bucket` 两套词汇同列共存（`d0/d1/d2/d2_fallback` 与 `h00_12/h12_24/h24_48/h48_plus`，宪章 47 只认后者），且 `d0` 在 `METAR_PHYSICAL_PATH_TMAX` 与 `OFFICIAL_AIRPORT_TAF_TX` 上是第三义"无提前量可言"。`d1` 一个标签横跨三个小时桶。
- `run_cycle` 混了两个坐标轴：真模型 run（`00Z/06Z/12Z/18Z`）与日偏移（`PREVIOUS_DAY1/PREVIOUS_DAY2`），外加 `MIXED_NATIVE_CYCLES`/`AGGREGATOR_UNKNOWN_NATIVE_CYCLE` 两个哨兵。判例：`WHERE run_cycle='18Z' AND lead_bucket='d2'` 在 `calibration_historical_asof_model_run` 上返回 **0 行**，而两天外样本实际 7,762 条，全在 `(PREVIOUS_DAY2,d2)` 与 `(00Z,d2)` 里。
- `maturity_bin` 与 `lead_bucket` 取值域逐字相同但量不同（事件还有多远 vs 这个 run 有多旧），**没有任何一张表同时含这两列**，所以错误 join 会正常返回行、不报空。唯一换算点是 `production_io.derive_policy_maturity_bin`；不要在 SQL 里让两者相等。
- 正式真值是两条车道（宪章 225）：`source_lane` 为 `AWC` 或 `SETTLEMENT_SOURCE`，按修订号取最新；只写 `source_lane='AWC'` 会读到被结算源修补取代的旧值，一律用 `canonical_contract.FORMAL_TRUTH_LANES_SQL`。
- 时区只从 `calibration_station_market_timezone_binding` 或 `basket_target_market_registry` 逐站取；`calibration_station_registry.timezone_name` 是种子包原文，45 站中 33 站错标 `America/New_York`。

## 纯 YES Basket（宪章 231 起车道关闭、规则保留）
- `com.weather.weather_basket_yes` 是canonical ASOS优先/AWC后备正式观测层的纯YES篮子唯一驻留服务名；旧`com.weather.reported_trajectory_no` label与`reported_trajectory_no_v1` hard-NO策略均已退役，不得加载、决策或下单，不得作为服务别名，不得启动第四个生产服务。
- 篮子按已物化后验、方向侧覆盖价值、全情景利润曲线、集中度、预算和真实 WS 深度求唯一目标库存，不设固定桶数、4→3→2或阶段内等份。未成交 Maker 不计入已实现地板但必须计入 confirmed pending；每次成交后重算目标库存、净成本和利润曲线。默认只增不减；显式角色调仓时，仅允许卖出 S/B/L/C 桶中恢复 `M>S>A>B>F` 所需的超额份额，M/A/F 桶不得卖出、不得用卖出控制预算。卖出必须使用官方 YES bid 逐档深度并在成交后重算净成本和地板，未知成交立即 reconcile_required。边缘桶允许作为中心。
- 篮子中心只能来自已物化 P1；P1 缺失时使用最新P0-M，TAF未到时才使用P0；regime只作方向一致性闸门，不得平移概率中心。
- 篮子只在 P0 开仓建仓（开仓、加仓、补腿、TAF 进场）；P0-M/P1 只编译、记决策、持有（`basket_stage_not_buying`）。P0-M/P1 的开仓归秤砣线（秤砣线不在 P0 开仓），10-10 起 Proper 线也在 P0-M/P1 开仓（宪章 231 补记二）；同心线已停。篮子/秤砣线/同心线在看板各有独立开关（宪章 229，2026-09-26）。
- 双风险层（`underreach_risk`/`upside_breakthrough_risk`）已于 2026-09-04 按裁决退役（宪章第 29、32 条）：`_derive_dual_risk_scope` 只在 P1 标出 `predecessor(M)`/`successor(R)` 两层，结果先与概率核心取交集，只作 `value_v2` 审计读数的覆盖项输入，不进种子、席位、定注，不生成也不扩出新桶；桶关系只由 bucket contract 解析，风险层不得修改 P1。
- 赔率优化必须按官方 CLOB WS 逐档 `all_in_yes_ask` 做篮子级增量覆盖价值；`ShortfallCoverage` 和 `BreakthroughCoverage` 只影响排序、预算和选桶，不是新概率。硬事实排除、lineage、证据新鲜度、盘口深度、预算及立即执行/已成交部分的 `sum(all_in_yes_ask)<1` 任一不满足即 fail closed；未成交 staged GTC 继续遵守既定独立挂单规则。
- 极端赔率只允许用于重塑自有结算利润曲线：新增 `x` 份桶 `b`、全包价为 `a_b` 时，命中 `b` 的情景增益为 `x×(1-a_b)`，未命中情景成本为 `x×a_b`；必须以 `ΔBasketScore>0`、尾部覆盖价值、P1/lineage、集中度、预算和真实 WS 深度共同决定，禁止按 `1/a_b` 高赔率或低价单独重仓。`reservation_value` 不是概率，不能改写 P0/P1 或生产状态。
- 候选集不再按风险锚扩层（第 32 条退役，两层扩展上限一并下线）：种子只剩概率核心（第 76 条），枚举域是整条已报价梯子（第 49 条）。动态选桶必须服从正库存缺口、Maker保留价/等待门、紧迫FOK上限、成交后重算和`reconcile_required`。
- 旧生产线标记 `019f7a84-53ec-7183-9c5f-7f9cfa62e1f0` 已按熊先生指令退役，不再作为保护或运行依赖；用户域三个共用服务只承载 target `production_no_live` 主线，system 域同名服务必须为零。
- 篮子 Maker 允许确定性部分成交并按实际成交重算；撤单未确认、成交结果不明或迟到回报立即进入 `reconcile_required`。紧迫FOK单桶全成或零成，失败后禁止猜测重发，必须先重算当前SSOT。
- 新报文仅在推进站点当日已报最高温时触发；同温和旧温不交易。
- 官方报文硬下界只用于从纯 YES 篮子支持集剔除已数学排除桶；不得据此创建、补仓或退出任何 NO 仓位（Proper 下注线的 NO 腿只来自 s<0 且 p<YES 买价；远尾做空线的 NO 腿只来自市场自身价格结构，模型只作宪章 251 的过滤）。
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

## 多会话同时开工（2026-09-06 立，熊先生裁决"约定"）

同一棵树上常有多个 Claude 会话并行改动，今天三个会话各自落宪章条款时踩了三类坑，都不是任何一方的错，只是**没有见面的地方**。以下是见面的地方。

- **重启任何常驻服务只走 `scripts/restart_resident.sh "理由"`。** 覆盖三个服务（`weather_basket_yes` / `market_ws` / `clob_order_service`），`--only <label>` 可只重启一个。直接 `launchctl kickstart` 或 `targetctl.sh restart` 都不要再用。
  重启会把官方采集轮转游标归零，D+2 的 scope 排在目录约 192 位、每 10 分钟推进 20 个，**一次重启等于那个事件日八到十六分钟没人服务**。实测 2026-09-06：三个会话在 12:07／12:30／12:44／12:51Z 各自重启一次，2026-09-08 整个窗口只被判过一轮，而那一轮抽到的六个站没有一个能触发刚落地的改动。
  **它每次退出都报 `data/live_paused.flag` 的状态。** 那面旗是操作者的开关，脚本永不设置也永不清除（熊先生裁决 2026-09-06「取消暂停旗标，我自己开关」）；旗在时下单服务拒收每一笔提交，而这件事以前只能靠一笔订单被拒才发现——2026-09-06 12:07:51Z 一次下单服务重装把它置上，13:36:22Z EGLC 当天第一个完整 09-08 篮子（三腿 5.202 U，0.031 那条腿被宪章 98 抬到 33 股）三条意图全部 `SUBMIT_REFUSED`。装机路径的自动置旗已经删掉。
  脚本用 10 分钟合并窗（`PM_RESIDENT_RESTART_COOLDOWN_SEC`）：窗内的请求**记账不执行**，下一次重启（无论谁触发）一次性服务所有排队请求。`--pending` 看队列，`--force` 只在等不了时用且会留痕。**排队不是失败，退出码是 0。**
- **归属只能按文件足迹判。** 全仓共用同一个 git author（`xiongtingping`），`git log --author` 分不出人；用 `git show --stat <sha>` 看改了哪些文件。今天有一次把第三方的提交认成对方的，据此提出的改号请求整个落错人。**"正在跟我说话的人"不等于"拥有那些提交的人"。**
- **立宪章条款前先占号。** `grep -o "^[0-9]\+\. " docs/constitution/SYSTEM_CHARTER.md | sort -n | uniq -c` 查重号，**然后立刻只提交编号占位那一行**，再去写正文。并行会话之间没有别的占号机制——今天 96 与 97 各撞了一次，都是因为两边都写完整条才提交，谁也没看见对方。撞号后**不要跨会话去重写别人的引用**（那是本仓吃过亏的坑），改号由拥有那些提交的会话各自执行，或由熊先生统一裁。
- **别人的未提交文件不要碰。** 动手前 `git status --porcelain` 看一眼目标文件在不在别人手里；在，就把发现发消息过去，不要自己改。
- **共用一个 `.git/index`，所以会出现"活文件被暂存删除"的假象。** 2026-09-20/21 两个会话各自在会话起始的 `git status` 快照里看到同一个形态：`D  <文件>` 与 `?? <同一文件>` 并存（暂存区少了这个条目、盘上文件还在且未跟踪）——一个是 `bayes_basket_target/p1_tail_lane.py`（宪章 166 的 P1 尾段线，`p1_tail.enabled=true` 正在实盘跑），另一个是 `tests/test_dashboard_observation_intake_and_cycle.py`。同批还撞见 `scripts/calibration_dashboard.py` 的**暂存 blob 落后于 HEAD**（`git diff --cached` 显示把 d6f64e4f 整个回退掉），以及三个 0 字节、无进程持有的 `.git/index.lock`（14:54 / 19:22 / 20:33）。**两个文件最后都完好**（跟踪中、与 HEAD 逐字一致），没有任何删除进过提交，但排查花掉了两个会话各一轮。
  - **根因（09-23 当场复现）**：按①用临时 index `commit-tree` + `update-ref` 提交后，**共享 index 里被提交路径的条目仍是旧的**——新文件在 HEAD 有、index 没有 → `D ` + `?? `；改过的文件 index 停在旧 blob → `MM`，`git diff --cached` 恰是「把那次提交整个回退」。所以临时 index 提交后必须对**自己提交的每条路径**跑 `git update-index --add --cacheinfo 100644,$(git rev-parse HEAD:<路径>),<路径>`（等价 `git reset -q -- <路径>`），别的条目不碰；造 blob 用的 `git show HEAD:<文件>` 基线必须与 `read-tree HEAD` 在同一条命令里取（09-23 用 20 分钟前的快照造 blob，把并发会话刚提交的板块整个回退，靠 diff-tree 删除行数 697≠4 发现）。
  - **规矩**：① 任何需要临时暂存区的操作（`read-tree` / `write-tree` / `update-index` / `git rm --cached`）**必须先设 `GIT_INDEX_FILE=$(mktemp)`**，绝不写共享的 `.git/index`；② 不要在共享树上跑 `git reset`（mixed 会按目标树重写整个 index，比目标树新的文件全部变成"暂存删除"）；**还原自己改坏的文件也不要用 `git checkout`**——`git checkout -- <文件>` 是从 **index** 取内容并刷新该条目，index 陈旧时它会把旧 blob 静默写回工作区（正是本条说的陈旧 blob 场景，等于替别人回退一次），`git checkout HEAD -- <文件>` 则把 HEAD 的 blob 写进 index 条目；两者都碰共享 index。**唯一完全不碰 index 的还原方式是 `git show HEAD:<文件> > <文件>`**（2026-09-20 `清理磁盘` 会话的实战：它本打算 `git checkout --`，被 index.lock 挡下后改用这一条，事后看挡对了）；③ 提交一律路径限定（`git commit --only <路径>`，新文件先 `git add <该文件>`），别用 `git add -A`；④ 看到别人的文件是 `D  `＋`?? ` 时**不要"帮忙"恢复**——先 `git ls-files <文件>` 与 `git diff HEAD -- <文件>` 确认盘上与 HEAD 是否一致，再发消息问，绝不 `git checkout` 或 `git add` 别人的条目；⑤ 0 字节、`lsof` 空、无 git 进程、超过十分钟没动的 `.git/index.lock` 可以删，其余情况等。

## 常用运行（本地）
- 一次性用户域迁移：`bash scripts/migrate_weather_basket_yes_to_user_domain.sh --confirm-user-domain-production-no-live`
- 用户域安装/刷新：`bash scripts/targetctl.sh restart`（**仅安装用；重启常驻一律走 `scripts/restart_resident.sh`，见「多会话同时开工」**。`targetctl.sh restart` 的 stop 步骤超时返回非零，`set -e` 会让后续安装不执行，实测掉过 `market_ws` 与 `clob_order_service` 两个服务）
- 重启常驻（唯一入口，覆盖三服务）：`bash scripts/restart_resident.sh "理由" --who <会话名>`；看队列与暂停旗标 `--pending`；只重启一个 `--only clob_order_service`
- target-only概率切换：`bash scripts/cutover_bayes_basket_target_probability_evidence_no_live.sh --confirm-target-probability-evidence-production-no-live`
- 服务状态：`bash scripts/targetctl.sh status`
- 只读验收：`python3 scripts/weather_basket_yes_production_acceptance.py`
- 单次来源诊断：`python3 scripts/weather_basket_yes_daemon.py --once awc`
