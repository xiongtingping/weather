# Polymarket Auto Buyer（本地）

## 目标
- 生产执行为“纯 YES 篮子 + 官方 METAR/SPECI 硬 NO”：篮子只消费已物化的 P0/P0-M/P1 概率与 regime，硬 NO 只消费官方报文硬下界。

## 工程原则（必须遵守）
- 根因优先、零技术债务：先全链路分析再给长期方案与最小改动。
- 生产仅保留三个同一域服务：`reported_trajectory_no`、`market_ws`、`clob_order_service`；当前按熊先生要求迁移到 system 域，禁止 user/system 双活。
- UI、monitor、Bayes、模型扫描、市场 REST 刷新、任务队列和行情历史 tick 写入均停用。
- 正式实时 truth 仍为 AMSC/AWC 双泳道：AMSC 负责中国站点，AWC 负责排除中国站点；对中国站点以 AMSC 官方整点 `:00` METAR 为正式主来源（AMSC 发布更快），实时 truth 禁止交叉 fallback。AMSC 半点 METAR 与为中国站点查询的 AWC 整点 METAR默认只能进入独立 `reference-only` 证据；仅历史校正缺口可按熊先生本次授权显式标记 `TEMPORARY_AWC_CALIBRATION_LABEL` 进入 `production_no_live` OOS/calibration，不得覆盖AMSC对象、进入实时truth、P1或硬NO，后续取得AMSC历史后必须重建替换。
- Open-Meteo ICON/GEM/CMA 均为显式 `aggregator_republisher`：可参与带 lineage 的历史/forward calibration candidate，但不得伪造 native publication/arrival、进入 native arrival index 或硬 NO；只有独立严格 OOS 生成并激活的至少两模型正式组合才能进入 P0，同模型存在官方 native 证据时优先 native。
- 回应使用中文并称呼“熊先生”。
- 默认聚焦熊先生当前明确要求；不复述历史问题作为背景噪声。
- 主线任务默认自动推进到可验证闭环，不在普通中间确认点停住。
- 熊先生已明确主线默认无需逐步授权；仅在扩大任务范围或不可逆外部变更时停下说明。
- 默认直接实施并验证，不以 review-only/仅建议作为交付；明确退役的旧代码直接删除，不保留兼容分叉。
- `ΔBasketScore v2` forward OOS 与 `production_no_live`、RKPK 单站闭环和 45 站扩展并行，未完成不得阻断上述生产 no-live 主链；它只阻断参数/权重 artifact 的 `FORMAL_PROMOTED` 与 live 晋级。P0 模型组合自身要求的离线严格 OOS artifact 不受此规则放宽。
- 概率阶段唯一口径为：NWP生成P0；校准后TAF TX先确认/有限修正，云量、降水和风场再做物理一致性二次确认，二者原子生成P0-M；METAR/SPECI生成P1。禁止P0-F/P0-F^r分支。
- 硬 NO lane 不得读取概率、先验、似然、TAF、NWP、regime 或其他天气字段；篮子 lane 仅可读取已物化且带 lineage 的 P0/P0-M/P1、regime 和官方 CLOB WS 证据，缺证据即 fail closed。
- 外置盘仅用于非关键、无用数据的异步冷归档；禁止作为生产数据库备份、回滚、切换、readiness 或服务启停依赖，任何外置盘异常不得影响 AMSC/METAR/SPECI、SSOT 或交易。
- 需要熊先生执行交互 sudo 时，必须同步给出可复制的完整指令。
- 每次阶段性收尾时自动说明“下一个主线任务”。
- 若熊先生已否定某个根因假设，不得再次作为当前根因。
- 若本文件需要新增/修订：保持精简（≤100 行）。

## 事实与执行口径（硬规则）
- 前端不是生产消费者，不得为了展示增加热路径读写。
- 执行阻断原因唯一来源：`auto_trade_decisions`。
- `reported_trajectory_observations`、`station_day_bounds`、decision、intent 和资本预留必须原子提交。
- 重复报文内存去重且零写入；轮询诊断无新报文时最多每来源每 60 秒一次。
- 所有 target 采集、解码、测试和证据构建必须资源有界：进程 RSS、响应/单次累计/临时落盘设硬上限，采用流式处理并保留最低剩余磁盘；任一超限或资源探针不可用立即 fail closed。
- 只保留参与 target lineage、解码或审计的最小原始对象/回执；临时拼接物、失败残片、重复副本和无引用旧输出不得继续保存。
- 盘口只允许官方 CLOB WebSocket；目标 NO token 独立原子快照，不写 `event_market_ticks`。
- 下单只允许本机唯一 gateway；写锁外取盘口，篮子优先使用内容寻址的 post-only GTC Maker，紧迫收口只允许在确认撤销 Maker 后使用有价格上限的单桶 FOK；每次成交后重算目标库存与利润曲线，硬 NO 使用 FAK，不经 outbox 秒级轮询。

## Basket + Reported Trajectory NO（当前唯一生产策略）
- `reported_trajectory_no` 服务同时承载 `weather_basket_plus_hard_no_v2` 与 `reported_trajectory_no_v1`；不得启动第四个生产服务。
- 篮子按已物化后验、方向侧覆盖价值、全情景利润曲线、集中度、预算和真实 WS 深度求唯一目标库存，不设固定桶数、4→3→2或阶段内等份。未成交 Maker 不计入已实现地板但必须计入 confirmed pending；每次成交后重算目标库存、净成本和利润曲线。默认只增不减；显式角色调仓时，仅允许卖出 S/B/L/C 桶中恢复 `M>S>A>B>F` 所需的超额份额，M/A/F 桶不得卖出、不得用卖出控制预算。卖出必须使用官方 YES bid 逐档深度并在成交后重算净成本和地板，未知成交立即 reconcile_required。边缘桶允许作为中心。
- 篮子中心只能来自已物化 P1；P1 缺失时使用最新P0-M，TAF未到时才使用P0；regime只作方向一致性闸门，不得平移概率中心。
- 篮子风险层固定区分 `underreach_risk` 与 `upside_breakthrough_risk`：P1 主桶 `M` 未达成时，按 bucket contract 的 `predecessor(M)`、`predecessor²(M)` 生成下方尾部；官方实况形成新高 `R` 后，才按 `successor(R)`、`successor²(R)` 生成上行突破桶。`P1=30℃、当前已报最高温=25℃` 到 `29℃/28℃` 仅是说明性示例，绝对温度、桶宽和边界不得硬编码；风险层不得修改 P1。
- 赔率优化必须按官方 CLOB WS 逐档 `all_in_yes_ask` 做篮子级增量覆盖价值；`ShortfallCoverage` 和 `BreakthroughCoverage` 只影响排序、预算和选桶，不是新概率。硬事实排除、lineage、证据新鲜度、盘口深度、预算及立即执行/已成交部分的 `sum(all_in_yes_ask)<1` 任一不满足即 fail closed；未成交 staged GTC 继续遵守既定独立挂单规则。
- 极端赔率只允许用于重塑自有结算利润曲线：新增 `x` 份桶 `b`、全包价为 `a_b` 时，命中 `b` 的情景增益为 `x×(1-a_b)`，未命中情景成本为 `x×a_b`；必须以 `ΔBasketScore>0`、尾部覆盖价值、P1/lineage、集中度、预算和真实 WS 深度共同决定，禁止按 `1/a_b` 高赔率或低价单独重仓。`reservation_value` 不是概率，不能改写 P0/P1、hard NO 或生产状态。
- 未达成或突破风险最多向任一方向扩展两层；两类风险同时升高时降低集中度并收紧证据/盘口门，不得用无限扩桶替代证据。动态选桶必须服从正库存缺口、Maker保留价/等待门、紧迫FOK上限、成交后重算和`reconcile_required`。
- 旧生产线标记 `019f7a84-53ec-7183-9c5f-7f9cfa62e1f0` 已按熊先生指令退役，不再作为保护或运行依赖；system 域三个共用服务只承载 target `production_no_live` 主线。
- 篮子 Maker 允许确定性部分成交并按实际成交重算；撤单未确认、成交结果不明或迟到回报立即进入 `reconcile_required`。紧迫FOK单桶全成或零成，失败后禁止猜测重发，必须先重算当前SSOT。
- 新报文仅在推进站点当日已报最高温时触发；同温和旧温不交易。
- 硬 NO 只买最近一个严格低于硬下界、因此已数学排除的桶的 NO；硬 NO lane 禁止买 YES。若同桶已有 YES，必须先用 FAK 全部卖出并确认成交，部分成交或结果不明时禁止买 NO。
- 市价语义实现为 `FAK BUY NO @ 0.99`，只吃 `<=0.99` 的可执行卖盘，禁止裸 market order。
- AMSC/AWC 共用一个资金池，不设来源或市场配额；单笔最多 50 USDC，按 `<=0.99` 深度成交。
- 入场成交后立即为同一 NO token 挂 `GTC SELL @ 0.99`；退出成交后释放资本账本。
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
- 受控切换：`python3 scripts/cutover_reported_trajectory_no_production.py --confirm-live-reported-trajectory-no-v1`
- system 域迁移：`sudo bash scripts/install_reported_trajectory_no_system_services.sh`
- 只读验收：`python3 scripts/reported_trajectory_no_production_acceptance.py`
- 单次来源诊断：`python3 scripts/reported_trajectory_no_daemon.py --once amsc`

## 关键词触发：订单分析
- 触发关键词：`订单分析` / `复盘调优` / `策略体检` / `失败特征` / `共同特征` / `参数建议`
- 默认范围：近 3 天（ET），按市场×策略版本分层
- 输出：成功/失败共同特征、参数建议、影响评估（信号量/胜率/赔率）
- 落地顺序：先只读分析报告 → 熊先生确认 → 再执行参数写入与重启
