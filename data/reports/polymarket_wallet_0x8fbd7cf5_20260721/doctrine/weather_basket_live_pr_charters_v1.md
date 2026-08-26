---
document_id: weather-basket-live-pr-charters-v1
version: 1.40.1
effective_date: 2026-08-04
status: EXECUTION_DOCUMENTATION_ACTIVE
canonical_format: markdown
live_order_authorized: false
production_mutation: false
parent_plan: weather_basket_live_pr_milestone_execution_plan_v1.md
---

# 最高温纯 YES 篮子：18 个 PR 工作包

## 0. 用途与终点线

本文把执行计划中的每一个 PR 固化为一个独立、可交付、可验收的工作包。共18个工作包：PR01–PR17及独立数据补取工作包PR02.C；PR02.A/B、PR03.B/C/D1、PR04.C、PR10.W3g与PR15.A仍是各自主PR的内部切片，不单独计数。本文负责实施合同；当前状态仅镜像父文档第17节，二者不一致时以父文档第17节为准并fail closed。父文档负责里程碑、依赖图和总体顺序，四大方案仍是业务规范来源。

本文与父执行计划共同构成唯一 PR 文档集，不再按PR复制出18份平行状态文档。任何实施分支、issue或提交说明只能引用本工作包中的PR编号及当时输入hash，不能自行改变Goal、依赖、canonical字段、OOS门或live权限。

终点线不是“某站跑通”或“有原始数据”，而是45站的`MODEL_OOS / P0_ACTIVATION / P0_SNAPSHOT / TRUTH_OBSERVATION / TRUTH_DAY / TAF_P0M / TRAJECTORY_P1 / STRATEGY_POLICY / STRATEGY_UTILITY / MARKET_WS / RUNTIME`十一类scope均按各自canonical natural key得到唯一状态：DATA_READY → OOS_READY → ACTIVATION_READY → CONSUMABLE，或 CANDIDATE_ONLY / BLOCKED(唯一代码)。不得用model grain代替P0、truth observation/day、TAF、P1、policy/utility、WS或runtime grain。`READY`不是独立canonical状态，只能是`CONSUMABLE`的只读展示布尔值。所有可消费事实均可反查 raw receipt → canonical fact → closed AWC truth revision → evaluation → artifact → selection → activation。

其中 PR05–PR09 的 `MODEL_OOS` shared coverage row 只允许
`DATA_READY / OOS_READY / CANDIDATE_ONLY / BLOCKED`；`ACTIVATION_READY / CONSUMABLE`
是 PR10 之后的下游生命周期，不属于coverage schema。历史样本达门但缺
`single_system_artifact_sha256`时只能是`CANDIDATE_ONLY`，不得改名为`OOS_READY`。

PR01–PR16 始终 production_no_live、live pause、零 gateway 写、零订单；PR17 的代码合并不等同于解除 pause，真实 live 仍需熊先生单独授权。

### 发布解耦合同

PR04 current release 是最终十一类 manifest 汇聚/CAS 门，不是 PR10–PR16 生成领域事实的串行前置。PR04 仍为 `BLOCKED` 时，各领域只经唯一 canonical ingress 输出 `MATERIALIZED_UNPUBLISHED`、`PRODUCTION_NO_LIVE_ONLY` 或唯一 `BLOCKED` receipt；未发布对象仅可被后续 builder exact-hash 引用。它们不得标为 `CONSUMABLE`，不得进入运行时篮子、三服务、gateway 或 live。只有 PR04 验证十一类 `expected = observed`、unresolved receipt 与 CAS 后，才可派生可消费发布根。

## 1. 全 PR 共用合同

- 一个 canonical contract、一个 ingress、一个缺口账本；字段或语义不能一一对应即 fail closed，禁止改名、猜测、默认补值、跨表兼容。
- PR03只完成AWC closed-day truth第一纵切片；PR05–PR16新增的model/artifact/P0/P0-M/P1/policy/market/runtime事实仍必须逐项走同一canonical ingress，并以绕过ingress零写负测作为各PR出口，禁止领域专用第二writer。
- OOS、20 train + 8 holdout、TAF-24 与晋级只使用闭日 strict-as-of 历史 AWC truth。forward 仅 observational_candidate_not_oos，既不计样本也不反向阻断历史 OOS。
- 模型数据 native-first，已有 ECMWF、GEFS、ECCC、AIFS、DWD raw 一律复用。native 不可用/无效/迟到时才允许 Open-Meteo 以 aggregator_republisher 进入独立 no-live fallback；其永不进入 P1、native arrival 或 formal/live authority。
- AWC 是 45 站唯一正式 realtime truth、P1 与篮子硬事实来源；AMSC 仅 retired_history_only。
- 纯 YES 是唯一生产策略。hard-NO、BUY NO、旧 reported_trajectory_no 和第四生产服务不得复活。
- 所有网络采集器（模型、AWC历史/realtime、TAF、市场registry与CLOB WS）共用一个资源合同：代理固定`127.0.0.1:1082`，响应/单次累计字节、RSS、临时落盘与最低剩余磁盘均有硬上限，采用流式处理；探针不可用或任一越界立即fail closed并生成terminal receipt。只有已识别的官方EPS对象可对64MB guard做对象级有界放宽，禁止全局取消资源门。
- 每个 PR 只修改自己明确的合同面；共享 schema、canonical contract、migration、服务和 live 配置由主线统一整合。

## 2. 状态语义与当前快照

| 状态 | 含义 |
|---|---|
| PLANNED | 已定义，尚未实现 |
| AUDITED | 已发现根因与验收条件，尚未实现修复 |
| IMPLEMENTATION_RETURNED | 子代理已交回局部实现；尚未由主线审查、回绕和集成验证 |
| PARTIAL_IMPLEMENTATION_RETURNED | 仅一个明确子切片已交回；PR 的其余合同面仍未实现或未审查 |
| PARTIAL_VERIFIED_NO_LIVE | 主线已验证主要 no-live 合同，但仍存在已定位的可活化旁路或未闭合出口 |
| IN_PROGRESS | 主线正在实施；已有切片不能独立授予可消费状态 |
| VERIFIED_NO_LIVE | 主线已复核合同、边界和所列验收；仍不授予 live |
| BLOCKED | 有唯一 blocker，禁止用旁路继续 |

当前工作树是大范围用户已有脏状态，本文不把代码验证、数据晋级、服务装载或live授权混为一谈。V5→V6的103,881行与`0879d2b9…5087`指纹只作为历史迁移基线；随后V7/V8/V9/V10/V11均以逐版append schema迁移且保留旧行身份。最新V10→V11迁移前后均为103,893条既有canonical fact，逐行内容指纹保持`40c71bcc…265`；V11另由唯一ingress追加45条`market_bucket_bridge`。迁移提交前并发生成的旧V6 preview只保留为历史证据，禁止作为current。

V11 current reader保持55个登记native scope均`BLOCKED(LINEAGE_MISMATCH)`、0 native single-system artifact、0通用/native activation、0 P0；native publication/arrival证据门没有被放宽。Open-Meteo historical lane已形成CYYZ、NZWN各自的AIFS Single+ICON deterministic 20 train + 8 holdout artifact、两条cross-selection、两条train-only residual-bootstrap probability distribution artifact与两条Gen2 no-live activation，其余43站仍为`OOS_SAMPLE_SHORT`。active既有canonical fact为103,893行，另有45条V11 bridge；Gen2只授予`PRODUCTION_NO_LIVE_ONLY`历史概率artifact可引用状态，不含forward packet、TTL、native-unavailable或P0，不得使用旧native materializer写入`calibration_native_ensemble_*`，也不得宣称45站完成。PR04 current pointer仍为`35777f0d…e9c1`且保持BLOCKED。

W3d发现的概率分布缺口已由V10闭合；V11又闭合45站canonical market bridge、P0 canonical fact与`ProbabilityEnvelope(P0)`同事务投影及`P0_ACTIVATION/P0_SNAPSHOT` typed producer。三类canonical P0事实仍必须同时存在（`forward_applicability / probability_distribution_artifact / p0_snapshot`），不得只加forward+P0。CYYZ/NZWN两条distribution artifact与Gen2 exact binding已提交，但真实forward applicability和P0均仍为0；CYYZ native complete、NZWN indeterminate，均不得切入republisher P0。旧contract缺字段与typed P0 key不一致两项已由W3i闭合；PR04.D通用expected-manifest合同和集合门已实现，MODEL basis v2已形成，但其余领域basis未齐全，因此最新preview仍正确BLOCKED且current pointer未切换。两个root-owned旧LaunchDaemon plist仍待闭合。

## 3. PR 总览

| PR | 里程碑 | 工作包 | 依赖 | 当前状态 |
|---|---|---|---|---|
| PR01 | M0 | baseline 与 stale-evidence fence | 无 | VERIFIED_NO_LIVE |
| PR02 | M1 | canonical contract / AWC role / timezone | PR01 | PARTIAL_VERIFIED_NO_LIVE |
| PR02.C | M1 | 官方历史AWC truth真缺口补取与终局冻结 | PR02.A、PR03 ingress | VERIFIED_NO_LIVE |
| PR03 | M1 | 唯一 canonical ingress、封闭 legacy writer | PR02 | PARTIAL_VERIFIED_NO_LIVE |
| PR04 | M1 | 唯一 ledger、current pointer、activation 选择器 | PR03、PR02.C 的冻结合同输入；只在最终汇聚时读取 PR05–PR16 输出 | BLOCKED（最终发布门） |
| PR05 | M2 | 全球确定性五模型与区域模型 benchmark/ablation | PR03 canonical ingress 与冻结 registry/contract；不依赖 PR04 发布 | IN_PROGRESS |
| PR06 | M2 | ECMWF ENS/PF50 builder与AIFS独立产品泳道 | PR03 canonical ingress 与冻结 registry/contract；不依赖 PR04 发布 | IN_PROGRESS |
| PR07 | M2 | DWD ICON Global/EPS builder | PR03 canonical ingress 与冻结 registry/contract；不依赖 PR04 发布 | IN_PROGRESS |
| PR08 | M2 | NOAA GEFS builder | PR03 canonical ingress 与冻结 registry/contract；不依赖 PR04 发布 | IN_PROGRESS |
| PR09 | M2 | ECCC GEM/GEPS builder | PR03 canonical ingress 与冻结 registry/contract；不依赖 PR04 发布 | IN_PROGRESS |
| PR10 | M2 | 跨模型家族exact-mask、activation与P0 | PR06–PR09中至少两个独立家族的同holdout artifact READY；PR15.A immutable bucket bridge；native与republisher authority分门；其余系统须terminal | IN_PROGRESS |
| PR11 | M3 | 官方 AWC TAF exact-join 与 P0-M | PR10 的未发布历史 P0 root；不依赖 PR04 发布 | AUDITED |
| PR12 | M3 | AWC station-day / correction / closed truth | PR02.C、PR03 canonical ingress；不依赖 PR04 发布 | IN_PROGRESS |
| PR13 | M3 | trajectory artifact / activation / P1 | PR10 的未发布 P0 root、PR12；PR11 只在 P0-M 已形成时作为精确修正输入 | IN_PROGRESS |
| PR14 | M4 | utility、目标库存、纯 YES basket | PR11/PR13/PR15 的未发布 basis 或唯一 terminal；不依赖 PR04 发布 | PLANNED |
| PR15 | M4 | 官方 CLOB WS 盘口事实 | PR03 canonical ingress、冻结 market registry/bucket contract；不依赖 PR04 发布 | PLANNED |
| PR16 | M4 | 三用户域服务 no-live 闭环 | PR14、PR15 的未发布 basis/terminal；PR04 发布前只生成 runtime receipt，绝不启动服务 | PLANNED |
| PR17 | M5 | 45站总验收、formal promotion、受控 live | PR04 已发布的总 release，以及 PR05–PR16 的完整可消费交集 | PLANNED |

## 3.1 审计问题 → 唯一 PR → 退出证据

此表是本轮审计结论的唯一归属表。一个问题只能由一个主 PR 修复；其他 PR 只能消费其已验证输出，不能复制修复、改名绕过或另建兼容路径。`BLOCKED` 是正确结果，不是需要以补抓、forward 样本或单站特判消除的错误。

| 审计问题 | 唯一主 PR | 不可接受的旁路 | 该 PR 的可验证退出证据 |
|---|---|---|---|
| 基线、ledger/pointer 的 row/content/file hash 与 as-of 漂移 | PR01 → PR04 | 取最高 generation、mtime 或重抓 raw | 只读 baseline receipt；immutable ledger 与 pointer 精确同一 hash/count/as-of |
| AMSC/旧混源 role 可流入 formal truth、settlement label、forward observation 或 P1 | PR02 | 把旧 role 改名为 AWC；以当前 DB 恰好为 AWC 当作证明 | 各 formal consumer 与子查询只接受 canonical `AWC`；AMSC/旧 role 输入零写 fail closed |
| 已有AWC raw重验后仍缺完整历史station-day，且没有数据获取owner | PR02.C | 等待未来OOS、用AMSC/Open-Meteo补truth、反复审计同一批raw | 只补未保留官方AWC历史产品并扩展闭日窗；每站ready dates或内容寻址终局缺口 |
| root-owned旧monitor/watcher plist仍可被手工bootstrap | PR02 | 因当前disabled/unloaded就宣称已退役，或让PR16另建兼容控制 | 一次性迁移删除两个plist并留receipt；system/gui旧label均absent，仓库与用户域只剩三个正式服务 |
| `settlement_timezone` 未成为 observation/truth/model 的持久化、可比对 identity | PR02 | 从 registry/站点默认补值；跨表猜测时区 | 新事实 exact persisted match；旧缺字段事实 `BLOCKED`；冲突零写 |
| 临时 AMSC/AWC builder、reference-only dataset 或 `TEMPORARY_AWC_LABEL` 仍能构造 calibration 包 | PR02 | 恢复常量、改名为 AWC/replay、为通过收集而保留兼容入口 | 移除 builder/专属测试及所有入口；45站 builder 只消费正式 AWC lineage；无一一映射历史输入只保留审计 |
| public writer、legacy adapter 和首次 packet 破坏 canonical-first 原子性 | PR03 | pre-commit legacy、双写、legacy fallback 或第二 writer | 单个外层事务中 canonical + projection 同成败；首包、重放、注入失败回绕均通过 |
| legacy authority/source/run-cycle/family 不能一一映射仍被推断为 canonical | PR03 | 显式 map、默认 native 或固定 `global_deterministic` | 无逐字段证明的事实拒绝 ingress 并进入 gap ledger；无反向 legacy 造新 authority |
| d2 `00Z` / `18Z` / fallback 与 `PREVIOUS_DAY2` 混用，activation generation 不唯一 | PR04 | 改名、最大 generation 选择器或跨 lead 借 activation | 每 scope 一个 exact current pointer；无法一一映射者保留 `BLOCKED(RUN_CYCLE_MISMATCH)` |
| release required scopes与canonical typed scope registry分叉，`P0_ACTIVATION/P0_SNAPSHOT/TRUTH_OBSERVATION/STRATEGY_POLICY`未进入同一current release | PR04 | 因contract枚举了scope就声称release已支持，或由PR10另建P0 pointer | 十一类scope均有producer或唯一terminal blocker，并在同一release/current pointer原子发布 |
| 同一scope聚合出多个canonical contract hash时被折叠为空字符串 | PR04 | 以空hash继续发布builder status，或任选第一个hash | 多hash立即`BLOCKED(LINEAGE_MISMATCH)`且release零切换；唯一hash逐字保留 |
| runtime schema label、inspector与当前V11不一致 | PR03 | 把label当展示文本忽略，或降低inspector到旧版 | runtime label、inspector state、migration target逐字一致为V11；错版fail closed |
| 五个全球确定性模型曾被错误放入P0权威路径 | PR05 | 让`CalibrationActivation`授权P0，或把多个确定性模型叫原生集合 | 确定性/区域builder只输出benchmark/ablation；P0 activation=0 |
| ECMWF ENS/PF50缺owner，AIFS、ICON-EPS、GEFS、GEPS产品/成员/lineage混杂 | PR06–PR09 | 把AIFS Single当ensemble、区域模型替代全球集合、republisher冒充native | 每原生集合系统有成员、arrival、20+8和单系统artifact；AIFS/IFS独立性不重复计数 |
| 单系统builder完成但缺少跨家族exact-mask组合与P0 owner | PR10 | 单套集合物化P0、用成员数替代独立家族数，或把republisher写成native | 至少两个独立模型家族在同一holdout形成cross selection/artifact/activation；no-live与formal/native authority分门，P0可全链反查 |
| 历史activation产品与current forward产品不一致 | PR10 | 把AIFS Single改名为AIFS ENS，或把ICON deterministic改名为ICON EPS | forward receipt中的provider/product逐字段精确匹配selection与activation；错产品、错站、错日、过期均零写`FAIL_CLOSED_NO_P0` |
| native不可用只是布尔声明，缺独立官方receipt | PR10 | 把republisher transport缺arrival、输入包缺席或机械生成的`native_source_unavailable=true`当证据 | receipt精确绑定station/event/product/request/content hash/checked-at/expiry/HTTP或解析结果/原因；fresh native存在时republisher拒绝 |
| deterministic温度中心缺少桶概率分布artifact | PR10 | 用加权温度、holdout RMSE、旧native ensemble compiler或临时归一化伪造概率 | 独立artifact只用历史train cohort拟合，冻结market bucket bridge与概率合同；8条holdout只评估，缺artifact不生成P0 |
| canonical market registry/bucket bridge缺失或只传裸SHA | PR15（PR15.A） | P0自行猜桶、接受错registry/桶区间/token或用WS/REST临时映射 | 45站bridge由唯一ingress写入并逐站绑定AWC、registry snapshot、interval与token；等价重放0 insert |
| P0未exact resolve persisted bridge或typed key缺run vector/lane/family-set | PR10（PR10.W3g）→ PR04.C | 只校验64位hash、把generation/content hash当业务key、默认补缺字段 | P0物化前逐字反查bridge；typed key逐项对应canonical业务键，否则`BLOCKED(LINEAGE_MISMATCH)`且current不切换 |
| TAF 历史样本/修订与 legacy d0 root exact-join 不成立 | PR11 | 当前 truth 伪造 TAF、Open-Meteo 替代官方 TAF revision | 每 scope ≥24 historical exact sample 才 P0-M；P0 root、TAF revision、decision time 全量绑定 |
| AWC station-day、COR/AMD 审核、closed truth 与 trajectory/P1 proof 不闭合 | PR12 → PR13 | AMSC/跨源 fallback、rootless P1 或自动恢复 correction halt | 历史closed truth与realtime observation分门；当前45×JJA×4 intraday×D0的180 scopes均有唯一状态，P1有promotion+activation+proof |
| 策略 OOS 没有真实 WS 成本、成交、结算与机会成本，score rows 为零 | PR14 | 用天气概率、回放或模拟成交代替 utility OOS | target/reserve/cadence 使用真实闭日 utility；无证据仅 `CANDIDATE_ONLY` |
| 缺口账本只覆盖model grain，TAF/P1/utility/WS/runtime可能另建平行ledger | PR04 | 为truth伪造model字段，或每层建立自己的current pointer | 一个release内按`MODEL_OOS/TRUTH_DAY/TAF_P0M/TRAJECTORY_P1/STRATEGY_UTILITY/MARKET_WS/RUNTIME` typed scope统一发布 |
| 盘口来源、三服务、gateway 与受控 live 的运行边界不闭合 | PR15 → PR16 → PR17 | REST/前端盘口、第四服务、system 双活或代码合并即 live | 官方 WS 原子事实；仅三用户域服务 no-live；45站总验收和单独 live receipt |

**依赖解释（2026-08-05 发布解耦修订）**：表中的依赖是领域 builder 的输入依赖，不是 PR04 已发布 release 的串行依赖。PR01–PR03 与冻结的 PR04 contract/ledger 是共同基础；PR04 的 release/CAS 只在 PR05–PR16 全部产出 exact-hash basis、事实或唯一 terminal 后一次性执行。发布前的下游 builder 只可生成或引用 `MATERIALIZED_UNPUBLISHED / PRODUCTION_NO_LIVE_ONLY / BLOCKED`，不得消费；任何模型数据已存在但 activation root 尚未发布时，状态不得标为 `CONSUMABLE`。

## 3.2 当前实现与验证回执（不等于里程碑或 live 验收）

下列状态只说明代码合同和 no-live 边界；真实 baseline、ledger、activation 或 scope 仍可正确为 `BLOCKED`。任何代码测试通过都不自动形成数据 promotion、服务装载或 live authority。

| PR | 已验证范围 | 当前证据 | 剩余出口 |
|---|---|---|---|
| PR01 | baseline v2、direct inputs 精确 schema/content hash、exact-45、P1 audit/repair pair、DB cutoff projection、稳定 verification identity、worktree hunk fence | post-PR02.C current baseline identity=`a95ae2ef…c7e4`并保留5个真实blocker | 只随真实输入变化重建；不得因上游仍BLOCKED而重做baseline合同，也不得引用无artifact的identity |
| PR02 | 596条canonical truth与2,845条d1 residual已进入active SSOT；旧invalid事实进入append-only retired表；旧authority入口已删除/typed reject；legacy manifest退役；真实daemon AST closure已纳入production manifest | active=12,600 model+596 truth+2,845 residual+0 activation；post-PR02.C PR04 current已发布 | 两个root-owned旧plist待退役。0/45达到20+8，任何activation都必须继续阻断 |
| PR02.C | 20,946/20,946 target均有terminal state；累计新增2,075个exact raw；identity-preserving rebase保留399个旧truth并追加197个truth | final receipt SHA=`032bc5…ce9`；replay=16,945 observations/859 station-days/596 truth-ready；0 retryable blocker | `EXACT_SLOT_MISSING`与`RETENTION_WINDOW_EXPIRED`已终局冻结，不再循环重抓；本PR不承诺20+8且不生成activation/P0 |
| PR03 | canonical ingress显式outer transaction；contract V4、receipt manifest、ordinal binding、V6→V10迁移器已实现 | 真实V10已apply；迁移保持103,889行与`2f7f6bea…82b8`，随后同一ingress追加2条probability distribution和2条Gen2 activation，active为103,893行 | native arrival/manifest/binding、forward applicability和P0仍均为0；其余下游fact registry仍未闭合 |
| PR04 | single-snapshot supersession/release、typed envelope、原子root | typed P0 key、V11 snapshot、八类DB反查、expected manifest合同/loader、集合门、P0 TTL及apply围栏已验证；preview为`CANONICAL_CONTRACT_TRUTH_GAP` | `forward_applicability/p0_snapshot=0`；各领域basis manifest尚缺，coverage正确为false；current仍为`35777f0d…e9c1`且不得apply |

**执行顺序固定**：PR02.C与post-PR02.C release（已完成）→ V4/V5/V6/V7历史迁移及typed envelope首批物化（已完成）→ retained GEFS/GEPS 87,840 member candidate唯一ingress与55-scope blocker（已完成）→ V7→V8→V9真实apply（已完成）→ AIFS/ICON冻结cohort的4条republisher historical artifact、2条cross-selection和2条Gen1 activation（已完成）→ V9→V10三类fact迁移、2条distribution artifact和2条Gen2 activation（已完成）→ exact-cohort native probe + fresh forward capture + post-capture re-probe → 仅真实native unavailable时物化forward applicability与no-live P0 snapshot。selection、distribution、activation、forward/native receipt与P0必须分步验收，禁止任意跨级。一次性退役两个root-owned旧plist作为独立M1运行边界收口，不阻断M2只读artifact/selection/activation工作。

## 4. M0 — 停止循环

### PR01 — 冻结事实基线与 stale-evidence fence

**交付结果**：一个只读、WAL-safe 的 baseline manifest；它同时冻结 DB identity、canonical fact 计数、activation generation、ledger/pointer 的 file/content hash 与 as-of，并把任何漂移输出为结构化 blocker。

**必须做**：只读事务；pointer 与 ledger 的 count/file hash/content hash/as-of 精确比较；P0 实读内容地址与 TAF binding 精确比较；frozen/as-of 时钟不得为空或晚于外层 as-of；P1 pair 的 as-of 必须不晚于外层 as-of。

**明确不做**：不写 SSOT、不改 pointer、不抓网、不启动服务、不把 mtime 当作证据。

**验收/出口**：正常快照可重算；outer/inner as-of 均为严格 UTC-Z 且 `inner ≤ outer`；ledger as-of、pointer effective-as-of 与 inner 精确一致；P0 采用重算内容地址，TAF 绑定同一 P0 hash 和 frozen time；P0、TAF、native linkage/repair 四方均为 exact canonical 45-station scope。任何 duplicate scope、record/builder status 矛盾或 builder `BLOCKED` 均确定性阻断。人为改动任一绑定对象后必须 `BLOCKED`，精确字节恢复后才可回到原 verification identity；gateway/order 写入为零。PR01 代码合同已 `VERIFIED_NO_LIVE`；当前21:08Z baseline identity=`a95ae2ef6d2cd9128b78b76d0e204c22fc6dd63b08ea547866cd8ce23b35c7e4`并保留5个真实blocker。只随真实输入变化重建，不因`BLOCKED`重做PR01合同。

## 5. M1 — 一个 ingress、一个 ledger、每 scope 一个 current activation

### PR02 — Canonical contract、AWC truth role 与 timezone

**交付结果**：唯一 canonical 字段/语义验证器，正式 truth/P1 只接受 AWC；`source_role`、truth/model/observation 的 persisted `settlement_timezone` 均成为可复核 identity；residual 必须与 truth/model 的结算时区 exact match。

**必须做**：拒绝 AMSC active truth 与 P1 bridge；保留 AMSC 为 retired_history_only；正式 AWC 与历史 replay 使用不同 source-role，不能由消费权限反向改写；truth/model 新事实持久化时区，旧未绑定行 `BLOCKED`；拒绝 source、authority、run-cycle、lineage 或 timezone 的默认推断；退役临时 AMSC/AWC builder 及 reference-only/temporary-label 入口，legacy adapter 只解释原事实，不能改名成等价事实；生成内容寻址的 authority-entrypoint inventory，逐项记录 owner、读/写能力、canonical-only/retired/typed-reject 状态和验收证据。historical truth 子合同与旧CLI/P1 replay/DDL/view旁路必须全部闭合才允许 M1 出口。

historical replay 只能逐 raw 对象验证官方 URL、poll identity、raw SHA/body、站点、报文类型、valid time、温度、时区与 closed-day 后授予 `AWC_HISTORICAL_REPLAY_INGRESS`；旧 observation packet 只作 raw locator。只为证据完整的 station-day 生成新 truth/residual contract，不完整 scope 进入唯一 ledger。

**明确不做**：不做全表 destructive rebuild，不把 native_or_official_cloud_replica 改名为 native，不把 temporary/reference-only AWC/AMSC 输入改名为 AWC/replay，不切换下游 consumer。

**验收/出口**：45站 binding 唯一；active formal AWC view 无 AMSC；AMSC ingress、旧 role 伪装 AWC、时区 identity 冲突、truth/model timezone mismatch 均 fail closed。已登记TEMP/P1/NWP/AMSC monitor/SPECI/task-queue/control/install/writer入口由负测证明不存在或typed reject；legacy probability manifest固定为`RETIRED_HISTORY_ONLY`且无network entrypoint，production manifest以真实daemon为root并对约139文件执行AST closure，最近定向12 passed。只读historical解析模块不得被误删或重新获得authority。当前仓库内authority出口已闭合；剩余运行环境出口是两个root-owned旧LaunchDaemon plist必须一次性退役并留receipt。该结论不等同于认证式、全库不可伪造的provenance闭环；0/45达到20+8是另一个独立数据门。

**当前数据证据**：post-PR02.C replay v5逐对象重验得到16,945条observation、859个observed station-day，其中596个满足完整`:00`时隙与无冲突合同并已形成canonical truth；其余显式blocked，45 blocked/0 complete。manifest content SHA=`aed04642006585eaf3f87b8d5ef1352b3c54ac92edd61812628f216b15cc8c1e`。旧1,260条truth未补`source_role`，而是整体退役。596个truth-ready station-day不等于每个lead有596个OOS样本；d1实际形成569 truth×5=2,845 residual。当前正确出口仍是其余scope保持`OOS_SAMPLE_SHORT/DATA_MISSING`，不是伪造45×28完整。

### PR02.C — 官方历史 AWC truth 真缺口补取与终局冻结

**交付结果**：唯一历史truth数据获取owner。必需输入是PR02.A冻结inventory；可读取PR04既有只读缺口投影作为目标索引，但不依赖本轮PR04新release。它补未保留的官方AWC历史产品、扩展更早闭日窗，并为官方确实不可得的station-day生成内容寻址终局缺口receipt；输出再进入下一次PR04 release。

**必须做**：输入inventory绑定content SHA、target count与唯一去重键；按station × event_date × expected`:00` slot × revision目标清单获取官方AWC产品；保留URL、request/received、raw/body SHA、COR/AMD与closed-day；已有raw零重复下载；每个请求记录响应/RSS/临时盘/最低剩余磁盘边界；通过唯一canonical ingress append；每批输出truth-ready/blocked dates及HTTP、产品不存在、检索窗外、资源门等terminal diagnostic subtype。网络固定代理`127.0.0.1:1082`。

**明确不做**：不用AMSC/Open-Meteo/未来数据补truth，不把`:52`改名为`:00`，不重复抓模型或已有AWC raw，不生成residual/artifact/activation/P0。

**验收/出口**：每个`45站 × 目标日期 × expected slot/revision`只有一个terminal state；每个目标要么以`raw receipt → canonical truth → station-day diagnostic`逐对象exact绑定形成合法closed truth，要么有终局`DATA_MISSING/OOS_SAMPLE_SHORT` receipt；45站均有ready dates或唯一blocker。PR02.C不承诺20+8、不生成residual/artifact/activation/P0，只为后续builder提供terminal receipts与canonical truth。

**当前状态**：`VERIFIED_NO_LIVE`。inventory固定20,946个target，content SHA=`5807fea0b6e224d1a13580d81cdf8b60bc9d6a8f05719cf2a1c6056d37dbafb5`；最终v4 receipt content SHA=`032bc5b409da31b09cba0e87c7ceb458bdc117a1ce49c2ea26c46a2a24728ce9`，4,879 `ALREADY_RETAINED`、48 `FETCHED_OFFICIAL_RAW`、4,769 `EXACT_SLOT_MISSING_FROM_OFFICIAL_RESPONSE`、11,250 `OFFICIAL_RETENTION_WINDOW_EXPIRED`、0 retryable。v1 TLS失败及v2/v3中间重试只作历史证据。canonical append contract SHA=`c616f2dbee716af01153fb3744296197c271478a3374be493d9c03bd42a14264`，首次ingress为399 replayed + 197 inserted，幂等复跑为596 replayed + 0 inserted。

**审查入口**：

- inventory：`output/audits/awc_historical_truth_gap_inventory_45station_20260804_pr02c_v1.json`
- 最终terminal receipt：`output/audits/awc_historical_truth_gap_fetch_45station_20260804_pr02c_v4/terminal_receipt.json`
- observation evidence：`output/audits/awc_historical_truth_gap_fetch_45station_20260804_pr02c_v4/canonical_observation_evidence.json`
- replay manifest：`output/audits/historical_awc_replay_45station_20260804_post_pr02c_v5/manifest.json`
- canonical rebase receipt：`output/audits/historical_awc_truth_contract_45station_20260804_post_pr02c_v4/canonical_rebase_receipt.json`
- 定向验收：`.venv/bin/python -m pytest -q tests/test_awc_historical_gap_fetch.py`，当前回执为`10 passed`。

### PR03 — 唯一 canonical ingress 与 legacy writer 封口

**交付结果**：先使 AWC closed-day truth 只能从 canonical ingress 到同事务 typed projection；legacy 不再拥有第二写权限。其他类别先建立 writer inventory 和唯一归属，不以本 PR 扩大为一次性全域重写。

**必须做**：`settlement_truth` 先写 `CanonicalTruthFact`，再在同一 outer transaction 投影 daily truth；修订/幂等只读 canonical truth；退役 historical direct-append writer；`legacy_projection` 和 adapter 不得由旧 truth/model 反向造 canonical 或推断 authority/source/family/run-cycle；修复首次 packet 的 transaction fence。其他 direct writer 必须先登记到自己的后续 PR，未完成 canonical 化不得授予 consumable 权限。

**明确不做**：不以预提交、双写、第二数据库或 legacy fallback 绕过首包；不把 read-only audit 变成 writer；不借“统一 ingress”之名在本 PR 同时重写 market/regime/P0/P1/artifact 的全部领域。

**验收/出口**：绕过 canonical 的 public write 被拒绝；AWC truth 首个 packet、重放和合法晚到 revision 在一个 outer savepoint 内成功或完整回滚；canonical append 后/投影冲突/commit 前故障均零半写；legacy adapter 与 `legacy_projection` 的 public 输入均 typed reject，canonical/projection 零写；formal AWC projection 与 canonical truth 的 `source_role` 等字段精确匹配，AMSC/旧 role 注入零半写。被拒 scope 的唯一 ledger 登记由 PR04 承接，不得伪装 READY；读路径 mode=ro/query_only。

**当前状态**：`PARTIAL_VERIFIED_NO_LIVE`。AWC truth 第一纵切片已验证：public legacy calibration ingress 及顶层导出已退役，d1 legacy `--apply` 和 d2 legacy adapter 均 typed reject；commit fault、projection conflict 与 caller-owned transaction 边界已验证。新增 ensemble member/native arrival canonical 类型尚未完成 producer/ingress 全量集成，因此不能把第一纵切片外推为全领域唯一 ingress 已完成。PR04 承接 blocker 不属于恢复 legacy writer 的理由。

#### PR03.B — Canonical contract v2 / SSOT v4 / ensemble ingress（PR03内部工作包，不新增PR）

**根因与归属**：PR08/PR09 的 retained GEFS/GEPS 成员资产不能直接产出单系统 artifact，原因是旧数据尚未按member与arrival的独立canonical语义逐字段入库。PR03.B只提供唯一contract/SSOT/ingress基础设施；retained资产的具体adapter与artifact分别归PR08/PR09，不得各建平行成员表、arrival表或兼容ingress。

**唯一合同**：

- `CanonicalEnsembleMemberFact` 是不可变解码成员事实，natural key 为 `station_id × ensemble_system_id × event_date × run_init_utc × lead_bucket × run_cycle × member_id × extraction_version`；它保留 `raw_receipt_set_sha256` 与 `archive_query_at_utc` 的原始语义，不复制 arrival 状态。
- `CanonicalNativeArrivalFact` 是独立 append-only run/lead/object arrival 事实，至少包含 `ensemble_system_id × provider_id × run_init_utc × lead_bucket × run_cycle × native_object_id`；除`published_at_utc`、`received_at_utc`、`raw_object_sha256`外，还必须内容寻址绑定`official_publication_proof_content_sha256`、`publication_proof_type/provider_endpoint`与`local_completed_receipt_content_sha256`。publication proof必须来自官方、与该对象内容关联且语义明确；local receipt必须证明本机完整接收结束。允许显式unknown/null并保持BLOCKED；禁止从run-init、HTTP Last-Modified、archive query、mtime、URL或receipt-set SHA推断。
- `aggregator_republisher` 永远不能生成 `CanonicalNativeArrivalFact`，也不能令 `native_arrival_indexed=true`。
- contract v1 producer 不得静默继续；所有 producer 必须显式升级为v2并提供两个新顶层数组，即使为空。缺字段、旧schema或字段语义不能一一映射立即 fail closed。
- SSOT v4 只新增 `ensemble_member/native_arrival` 两类 active fact，必须保留v3迁移历史不变并无损迁移现有16,041行；真实数据库必须经preview、writer quiescence与内容哈希同一性门后才能apply。

**当前实现回执**：PR03.B基础设施已在no-live边界闭合。contract v2、V4 schema/migration、唯一member/arrival ingress、ingress-only trigger与全部现役producer v2已完成；真实V3→V4迁移前后均16,041行，行内容SHA均为`4cd298ac9e2b00f63bdf5cc666b6f7c25a583e18d689bf098bbdd608b64ef128`，迁移后preview=`PREVIEW_ALREADY_V4`，组合回归104 passed。迁移未调用order gateway、未写订单、未启动服务。

**出口与下游门**：PR03.B合并门已通过，但不自动生成member/arrival数据、single-system artifact、activation或P0。PR08/PR09现在只能将retained成员逐字段映射为canonical candidate；无法证明`raw_receipt_set_sha256`或native publication/received的scope继续fail closed。服务、live pause和订单均不在本切片授权范围。

#### PR03.C — Exact receipt manifest、ordinal binding / SSOT V6（PR03内部工作包，不新增PR）

**交付结果**：在同一canonical contract、同一ingress与同一SSOT中追加不可变的`ensemble_member_receipt_manifest`事实，并把member↔native-arrival binding改为manifest ordinal覆盖。V5的“一条member binding即可通过”语义作废；PR06–PR09的native单系统builder只能消费V6 manifest与binding闭包，禁止按共同system/run/lead/cycle猜join。

**当前已证事实**：72个retained GEFS/GEPS包只证明87,840条member candidate，不能生成任何native arrival。PF50、GEFS、GEPS、AIFS Single、ICON Global/EPS现有retained证据均无法合法生成official publication + 本机received的native arrival：PF50显式`native_publication_at_known=false`；GEFS/GEPS缺publication与原始完整received proof；AIFS/ICON把HTTP `Last-Modified`保存为publication；ICON EPS采集代码同样使用`Last-Modified`；f015缺原始capture receipt。故当前合法计数必须保持`native_arrival=0`、receipt manifest=0、binding=0、artifact=0、activation=0、P0=0。

**唯一manifest/binding合同**：

- `CanonicalEnsembleMemberReceiptManifestFact`精确引用一个member hash、原始receipt-set SHA（仅保留provenance）、连续排序且无重复的exact receipt refs，以及receipt refs set hash；manifest不能由URL、查询时刻、mtime或集合hash反推。
- 每个receipt ref固定ordinal、`native_object_id`、`raw_object_sha256`与对象内selector；ordinal必须从0连续覆盖到manifest末项，禁止缺号、越界、乱序、重复或默认补值。
- binding精确引用`ensemble_member_content_sha256`、`native_arrival_content_sha256`、`receipt_manifest_content_sha256`、`manifest_entry_ordinals`与自身`content_sha256`。同一ordinal跨binding重叠、binding端点不一致或arrival对象/hash不匹配均fail closed。
- reader仅在每个member的binding ordinal并集**精确等于**其manifest全集时承认receipt-set完整。单binding只覆盖一个ordinal时继续`LINEAGE_MISMATCH`；“每个member至少一条binding”永远不是完整性证明。
- member、manifest、arrival与binding必须在同一canonical SSOT中形成exact endpoint；provider/system/run/lead/cycle、evidence/as-of clock、object id/raw SHA均逐字匹配。两端只允许`native_official`，`aggregator_republisher`永远不能形成native arrival、native manifest/binding或formal/live authority。
- native arrival还必须精确引用内容寻址的官方publication proof与本机completed-receipt proof；缺任一个、proof类型不在provider allowlist、proof未绑定相同object/raw SHA，或仅有HTTP `Last-Modified`时均为`LINEAGE_MISMATCH`。
- arrival晚到只追加arrival、manifest与binding，不修改member；extraction version变化产生新member hash与新manifest。manifest/binding本身不授予strict OOS、P1、formal/live或P0 authority。

**V6迁移合同**：canonical contract升为V4，顶层必填`ensemble_member_receipt_manifest_facts`数组；旧packet不得由adapter默认补空。SSOT V6只在V5允许事实类型末尾追加manifest类型并收紧binding字段；若V5已有任何旧scalar binding，迁移立即fail closed，禁止复制成V6。preview零写；apply要求fresh writer-quiescence、精确confirmation、`BEGIN EXCLUSIVE`、迁移前后行数/逐type计数/逐行指纹相同、`quick_check`与foreign-key检查通过，并保留V5/V4/V3 replaced表append-only。reader只接受V6，禁止V5/V6运行时双读。

**native与republisher分门**：V6 manifest/binding是native authority所需证据，不是Open-Meteo历史fallback的伪native入口。PR06–PR09若官方历史publication/arrival proof不可得，可在同一canonical ingress中建立显式`aggregator_republisher`历史strict-OOS no-live lane；它使用republisher receipt/lineage与独立artifact，不生成native arrival/manifest/binding，不进入P1或formal/live。两个lane分别记账，不允许用republisher解除native `LINEAGE_MISMATCH`。

**验收/出口**：

1. V5 preview零写；V6 apply前后既有103,881行、逐type计数与row fingerprint=`0879d2b9…5087`完全一致；四个故障点均完整回滚且无临时表。
2. schema inspector拒绝缺/乱序/额外fact type、列/键/trigger漂移和旧scalar binding；二次preview/apply确定性返回`ALREADY_V6`。
3. 正测覆盖多receipt manifest、跨多binding完整ordinal union、arrival晚到与幂等重放；负测覆盖partial coverage、ordinal重叠/缺号/越界、dangling hash、跨provider/system/run/lead/cycle、republisher、unknown clocks与as-of后到达。
4. 真实V6 builder在无新增证据时必须仍输出55个`LINEAGE_MISMATCH`、0 artifact/activation/P0；不得为追求READY改变正确阻断。

**回滚边界**：事务提交前任何故障由migration原子回滚。提交后禁止删除/降级V6事实；停止consumer、保留V6 SSOT并fail closed，只能通过重新验证的V6前向release修复。迁移前V5快照和replaced表仅供只读审计，禁止启动为运行恢复实例或形成V5/V6双读。

**当前实现回执**：`PARTIAL_VERIFIED_NO_LIVE`（PR03整体仍未把全部领域事实收进唯一ingress；PR03.C迁移切片已完成）。真实库已在fresh writer-quiescence下原子apply V6：103,881行、legacy V5 binding=0、逐行指纹`0879d2b9…5087`不变，apply SHA=`3b02d501…c9fea`；post-apply preview v2为`PREVIEW_ALREADY_V6`、SHA=`6d72869a…82c03`，FK violation=0、WAL=0。迁移与静默组合测试30 passed。V6-only 55-scope重跑已完成并正确保持55 `LINEAGE_MISMATCH`、0 artifact/activation/P0。下一出口是republisher historical artifact的canonical contract/ingress和native builder真正READY materializer分支，而不是再次迁移、写adapter或重抓raw。

#### PR03.D — 下游 canonical fact registry（PR03内部工作包，不新增PR）

**交付结果**：在同一个canonical contract和同一个ingress中登记single-system artifact、cross-selection、cross-artifact、P0/P0-M、P1、policy、WS与runtime事实类型；每类固定字段、natural key、authority lane、owner、content hash及绕过ingress零写负测。PR06–PR16不得各建领域表、第二writer或跨表兼容。

**首个出口**：新增能如实表达republisher历史OOS的canonical artifact事实，完整绑定provider/product、source class、authority lane、settlement timezone、capture receipts/raw hashes、冻结cohort、closed AWC truth revision/hash vector、20+8、run-cycle语义和永久no-live flags。不得复用`CanonicalEnsembleMemberFact`猜补真实`run_init_utc`，不得调用会写`calibration_native_ensemble_*`的旧materializer。native链比republisher链额外要求formal promotion；republisher永不产生native arrival、formal promotion或P1 authority。

### PR04 — 唯一缺口账本、current pointer 与 activation generation

**交付结果**：一个 immutable ledger 和一个原子 pointer；每个 business scope 仅有一个 evidence-complete current activation。无法证明唯一 current 的历史 generation 只保留审计并使该 scope `BLOCKED(ACTIVATION_CURRENT_AMBIGUOUS)`，不得自动标记 `SUPERSEDED` 或选择最大 generation。

**必须做**：一个release内固定十一类typed scope：`MODEL_OOS / P0_ACTIVATION / P0_SNAPSHOT / TRUTH_OBSERVATION / TRUTH_DAY / TAF_P0M / TRAJECTORY_P1 / STRATEGY_POLICY / STRATEGY_UTILITY / MARKET_WS / RUNTIME`。`MODEL_OOS/EVENT`至少固定station、system/product、provider product、family、source class、authority lane、settlement timezone、run-cycle、run-init/vector、lead和event date；`MODEL_OOS/COHORT`再以`strict_oos_cohort_sha256`绑定train/holdout日期、逐条AWC truth revision/hash、strict-as-of clock及完整模型输入hash vector。`P0_ACTIVATION/CURRENT`稳定key只含station、lead、run-cycle/vector、authority lane、exact available-system-set hash与independence-family-set hash；generation是append-only版本属性，不进入business key。`P0_SNAPSHOT`持久化`system_run_vector_sha256`，不得用单一run-init代表多个系统；as-of/TTL是证据属性，不用于规避duplicate。任一truth revision、holdout、source、product/system、run vector、timezone或authority lane变化，旧OOS/selection/activation/P0均不可复用。

`TRUTH_OBSERVATION`与`TRUTH_DAY`分别使用observation/revision/valid-time与station-day/bounds-version；`TRAJECTORY_P1`必须含`input_probability_stage∈{P0,P0_M}`并精确绑定相应P0 activation、P0-M root及TAF revision；`STRATEGY_POLICY/UTILITY`逐项绑定regime/policy版本与content hash。禁止为非model事实伪造model字段。PR04只拥有通用typed envelope/release/duplicate/as-of校验；领域builder分别归PR10/PR12/PR11/PR13/PR14/PR15/PR16，但必须共用同一ingress/ledger/pointer。pointer精确绑定ledger file/content hash/count/as-of与inner snapshot；canonical ingress拒绝generation回退；只有pointer精确匹配才可写ACTIVATION_READY。发布后同步四大方案current pointer，旧schema-v1/2,503-row声明只能作为历史引用。后续pointer变更采用两阶段协议：preview→apply前重算同一hash vector并重读pointer；CAS期望值是current pointer的`content_sha256`（当前`35777f0…e9c1`），不是release id；release/current/output仅允许固定audit路径。`BLOCKED` release可以发布缺口账本但绝不可消费；pointer替换后fsync失败只能只读复核，禁止重试写入/回切；baseline预期仍可为`BLOCKED`，必须断言精确blocker/hash而不用`--require-consistent`。

**明确不做**：不取最大 generation 充当 current；不把 d2 PREVIOUS_DAY2 与旧 00Z/18Z 改名匹配；未有一一字段映射的 d2 继续 BLOCKED。

**验收/出口**：baseline 的三类 ledger/pointer mismatch 被真实重建消除；所有直接输入必须在同一明确as-of下拥有content hash和evidence clock，P0、TAF、native linkage的station集合必须与canonical 45站集合完全相等，不能以TAF/P0同钟代替集合与lineage闭合。任何 builder `BLOCKED`、record/builder status 矛盾、duplicate scope、无 exact-45 P0/TAF/native binding 或未验证 inner snapshot 均不得写 `ACTIVATION_READY`；g4→g3 被拒绝；d2 mixed generation 每 scope 有独立、内容寻址的 supersession authority才可标记旧代为`SUPERSEDED`，否则保持`BLOCKED(ACTIVATION_CURRENT_AMBIGUOUS)`；active activation=0时release/consumer可消费activation也必须为0，`CURRENT` head不等于READY；blocker catalog唯一；MMMX d1只重跑exact cohort/binding，不重抓模型。四项定向负测必须通过：preview→apply输入漂移、将`BLOCKED` preview误作可消费、pointer替换后fsync不确定、release/current/output路径越界；不以全仓测试替代这些边界。

**当前状态**：`IN_PROGRESS`。active canonical SSOT为V11共103,938 facts：103,893条既有canonical fact加45条`market_bucket_bridge`；`native_arrival/receipt_manifest/binding/forward_applicability/p0_snapshot`均为0。Gen2只覆盖CYYZ、NZWN，均为`run_cycle=null`与`REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE`，不能解除native blocker或直接生成P0。PR04 current pointer仍为`35777f0d…e9c1`。十一类required scope、`P0_ACTIVATION/P0_SNAPSHOT` producer及blocked-receipt loader已存在；typed P0 natural key已逐项收口，generation移入version。真实V11 contract snapshot已由同一只读事务构建，含103,881条V4 canonical事实，contract SHA=`59837843470dacde2f90f2af7a64e4fc7c1e7471f076e2e3d279796ded583131`；八类DB逐字反查通过。最新preview已恢复`CANONICAL_CONTRACT_TRUTH_GAP`但仍`BLOCKED`。当前唯一首要出口是把coverage从“十一类type presence”提升为冻结45站registry与每类natural-key的expected/observed集合完全相等；真实V11 snapshot已闭合，expected-scope矩阵与release/current CAS仍未闭合。

#### PR04.A — activation supersession 合同与 retired d2 审计（PR04内部工作包，不新增PR）

**已闭合合同**：`CanonicalActivationSupersessionFact`只能经canonical contract/ingress进入active SSOT；release在单一DB snapshot中校验authority as-of、候选集合、scope与多代链。显式head不可由max generation、mtime、ingested_at或文件名推断。

**否定结论**：旧33个d2 g1/g4双代际activation只存在于retired历史；active V6 SSOT activation=0，也没有完整active d2 residual/OOS。因此禁止从retired表复制activation、补写新supersession或改名复活。

**后续适用门**：只有active SSOT未来真实出现多generation时才应用supersession合同。无显式authority继续`BLOCKED(ACTIVATION_CURRENT_AMBIGUOUS)`；`CURRENT`不等于`ACTIVATION_READY/CONSUMABLE`。

#### PR04.B — 十一类typed scope与current-head（PR04内部工作包，不新增PR）

**交付结果**：旧model-event gap ledger降为历史诊断；十一类scope由同一typed ledger、同一release和同一pointer表达。每类scope都有稳定natural key、owner、expected-scope规则和唯一terminal状态。相同semantic scope的多generation无显式supersession head时固定`BLOCKED(ACTIVATION_CURRENT_AMBIGUOUS)`，不得把generation/as-of变成新scope逃避唯一性。

#### PR04.C — V11 contract snapshot 与 typed P0 exact key（PR04内部工作包，不新增PR）

**交付结果**：`P0_ACTIVATION/P0_SNAPSHOT` natural key与canonical业务键逐字段一致；真实V11 DB由独立producer在同一只读事务内生成canonical contract snapshot，禁止补写旧contract或借adapter默认字段。

**已验证出口**：generation已移入version；activation key包含station/lead/run-cycle/vector/authority/system-set/family-set，snapshot再绑定event/decision/probability/activation/forward。真实contract含103,881条V4事实，八类DB逐字反查覆盖103,881行；旧truth-gap evidence仅在596个truth scope完全相等后重绑定。preview source mode已恢复`CANONICAL_CONTRACT_TRUTH_GAP`。

**明确不做**：不把V11扩展事实改名塞入V4数组；不把两条activation直接标成P0 consumable；不因snapshot约102MB全局取消资源门；不apply仍为BLOCKED的release。

#### PR04.D — typed expected-scope manifest 与 11-scope exact coverage gate（PR04内部工作包，不新增PR）

**Goal**：把“十一类type存在”这一弱覆盖判断替换为同一只读快照内的 expected-scope 集合证明。每个 expected natural key 必须有且只有一个同键 observed terminal row；不能构成 key 的输入必须留下结构化 unresolved receipt。终点不是产生一个 envelope，而是十一类scope的`expected = observed`，或正确保持`BLOCKED(EXPECTED_SCOPE_MANIFEST_MISSING)`。

**唯一输入与输出**：PR04.D不创建第二contract、第二ingress、第二ledger或按站点复制的builder。它只消费PR04.C同一SQLite read snapshot、冻结45站registry、各领域已有canonical事实和同一as-of；输出唯一`ExpectedTypedScopeManifest`，并把它绑定到release、ledger和current-pointer CAS。

每个scope至少输出：

```text
expected_key_set_sha256
observed_key_set_sha256
missing
excess
unresolved_input_receipts
state
```

实现后的唯一不可变审计输出命名为
`output/audits/pr04d_typed_expected_scope_manifest_<as_of_utc>.json`；它不是第二份
current pointer，也不因状态为`BLOCKED`而触发任何consumer切换。

任何一个集合不相等、as-of不同、key不能一一映射或输入事实不足，均为fail closed。不得由observed record倒推expected集合，不得用`scope_count > 0`、type presence、最新一行、默认字段或跨表补值替代集合证明。

**十一类 expected-scope 最小生成规则**：

| scope | expected natural-key 的唯一输入与最小规则 |
|---|---|
| `MODEL_OOS` | PR05–PR09每个lead/模型家族builder独立冻结的coverage basis manifest；release只消费其声明的station × family × lead × run-cycle builder key，禁止从当前model fact或builder status反推expected。 |
| `P0_ACTIVATION` | 已冻结canonical native/republisher cross-selection与其exact activation candidate；available-system set、independence-family set和run-cycle vector只能由selection component refs派生。无selection只写unresolved receipt。 |
| `P0_SNAPSHOT` | exact P0 activation × exact forward-applicability event/decision × exact market bridge × exact `ProbabilityEnvelope(P0)`；四者必须同snapshot、同as-of并逐项等值。 |
| `TRUTH_OBSERVATION` | canonical AWC METAR/SPECI observation index中每个被接受的官方message/revision；key至少含station、station-day、observation/revision valid-time和AWC authority。 |
| `TRUTH_DAY` | canonical station-day bounds中每个closed station-day × truth revision；必须与closed AWC daily truth exact join。 |
| `TAF_P0M` | artifact来自满足24个exact历史样本的TAF calibration scope；event来自exact P0 root × official TAF revision × decision。两种kind必须分别枚举，禁止latest-wins丢revision。 |
| `TRAJECTORY_P1` | artifact来自AWC trajectory calibration scope；event来自官方AWC observation（含correction review）× input P0/P0-M root × decision。不得只保留每站日最新一行。 |
| `STRATEGY_POLICY` | frozen global/station policy activation artifact：station × policy stage/version × season/maturity/liquidity × activation hash。 |
| `STRATEGY_UTILITY` | closed actual decision/settlement label：policy activation × candidate/baseline inventory × official WS snapshot × station/event/decision；预测概率或模拟成交不能替代。 |
| `MARKET_WS` | `REGISTRY`来自canonical market-bucket bridge的每个station/event/market/YES token/bucket contract；`SNAPSHOT`只来自该exact registry token的官方WS evidence。两种kind均需枚举。 |
| `RUNTIME` | 仅三个用户域服务`com.weather.weather_basket_yes`、`com.weather.market_ws`、`com.weather.clob_order_service` × exact `gui/<uid>` × verified release ID/hash vector。 |

**必须做**：manifest builder、typed envelope builder、release projection、ledger verifier和current consumer必须使用同一expected/actual集合结果；consumer须做second-pass集合相等验证，不能只相信`typed_scope_coverage_complete`布尔值。所有缺失或超集均保留精确natural key；key必需字段不存在时不伪造key，而以`DATA_MISSING`或`LINEAGE_MISMATCH`写入unresolved receipt。`P0_SNAPSHOT`在集合相等之外另验release as-of时TTL仍有效；apply前重读current pointer并执行CAS。

**当前状态与证据**：`BLOCKED(EXPECTED_SCOPE_MANIFEST_MISSING)`。弱`scope_count>0`判定已从release与ledger verifier删除；没有独立manifest时`typed_scope_coverage_complete=false`。最新真实preview逐类输出expected/observed hash、missing/excess：observed为`MODEL_OOS=90`、`P0_ACTIVATION=2`、`P0_SNAPSHOT=0`、`TRUTH_OBSERVATION=0`、`TRUTH_DAY=596`、`TAF_P0M=3`、`TRAJECTORY_P1=42`、`STRATEGY_POLICY=0`、`STRATEGY_UTILITY=0`、`MARKET_WS=1,023`、`RUNTIME=0`；因manifest缺失，十一类均明确`BLOCKED(EXPECTED_SCOPE_MANIFEST_MISSING)`。一条ZUUU不再可能判真，真实preview未apply，current pointer文件SHA前后均为`58a072b006c646e103bfd66fca5cf63398cca00c3a33f0bf78f74a0c50255c88`。

**验收/出口**：缺一条expected key、增加一条unexpected `ZUUU` key、跨as-of、latest-wins覆盖旧decision、错误bridge token、四个type-level伪receipt、适用domain少1站、错站、错lead、错lane、超集、stale P0 TTL和CAS drift必须分别`BLOCKED`，且current pointer字节不变。当前已完成少站、错lead/超集、manifest缺失、TTL等于截止时刻及coverage未完成禁止apply的负测。只有十一类集合完全相等、每个unresolved输入有唯一receipt、全部既有证据门通过且CAS无漂移才允许发布；发布仍不自动授予formal/live authority，不补模型raw、不重抓ECMWF、不创建P0/P0-M/P1、不放宽OOS或TAF门。

## 6. M2 — 模型泳道与 P0

M2主体仍为PR05–PR10，但PR10物化P0硬依赖PR15所属的静态前置切片PR15.A；该依赖不把PR15主体移出M4，也不新增第19个PR。

PR05–PR09所有builder共用同一覆盖门：每个产品必须精确输出`45站 × lead(d0/d1/d2)` shared coverage rows；非`OOS_READY` row必须恰有一个canonical blocker。`OOS_READY`均须绑定strict-as-of closed AWC truth、OOS evidence、raw receipt与`single_system_artifact_sha256`，再按authority lane分门：native lane还须native arrival + exact receipt manifest +完整ordinal binding union；republisher lane须Open-Meteo内容寻址receipt、明确provider/product映射、fetch/as-of clock、raw SHA，以及`native_arrival_indexed=false / p1_eligible=false / formal_live_authority=false`。禁止要求republisher生成native arrival，也禁止用republisher解除native blocker。所有cross selection、cross activation与P0固定为0；只完成d1、只列已有数据或缺失scope无terminal receipt，均不得宣称builder闭合。

每个PR06–PR09同时拥有其模型家族的证据落地责任：先复用已有raw与receipt，不重复下载对象；对可补的官方publication proof和本机completed-receipt proof建立内容寻址事实；对历史上无法补证的对象输出45站×lead terminal native blocker，并立即推进同家族Open-Meteo历史strict-OOS no-live lane。PR03.C只拥有公共合同/ingress/migration，不代替各模型builder采集或冻结证据。

### PR05 — 全球确定性五模型与区域模型 benchmark/ablation builders

**交付结果**：ECMWF deterministic、GFS、ICON Global、GEM、CMA及ICON-EU/ICON-D2/HRRR/NBM/NAM/AROME/HARMONIE/UKMO/JMA-MSM按每lead独立登记，只输出benchmark/ablation/candidate状态，不授权P0。

**当前实施事实**：shared coverage已闭合2,025 scope：450 `DATA_READY`、213 `CANDIDATE_ONLY`、1,362 `BLOCKED`、0 `OOS_READY`。15个确定性/区域产品的d0/d1/d2均有唯一terminal row；`single_system_artifact_sha256=0/2,025`，cross selection/activation/P0均为0。

**必须做**：d0/d1/d2分开历史评估；复用已有facts；每个产品有provider/product/family/run-cycle/source policy；native-first，republisher显式标记；输出只读诊断供跨集合ablation比较。

**明确不做**：`CalibrationActivation`不得授权P0；不把多个确定性模型叫原生集合；不跨lead/家族借OOS，不重抓ECMWF，不把区域模型替代全球集合。

**验收/出口**：row只允许`DATA_READY/CANDIDATE_ONLY/BLOCKED`；不得使用`BENCHMARK_READY`别名，也不得产生`OOS_READY`、单系统artifact、cross selection、activation或P0。

### PR06 — ECMWF ENS/PF50 builder 与 AIFS 独立产品泳道

**交付结果**：ECMWF ENS/PF50拥有逐成员、arrival、raw、20+8和`NativeEnsembleSystemArtifact`；AIFS Single及AIFS EPS被正式登记但不混淆产品角色。

**当前实施事实**：shared coverage已闭合405 scope：60 `DATA_READY`、255 `CANDIDATE_ONLY`、90 `BLOCKED`、0 `OOS_READY`，`single_system_artifact_sha256=0/405`。PF50官方历史对象仍仅覆盖d2/18Z且publication/arrival lineage不完整；AIFS Single forward不计OOS，AIFS republisher保持独立lineage。

**2026-08-04 canonical收口**：AIFS republisher builder只读V7 AWC truth，禁止读取legacy projection。三段retained capture固定as-of后仍仅CYYZ、NZWN达到20+8，43站样本短缺；新报告SHA=`dc40d8ef…497f`。两站artifact已与ICON两站artifact合并为同一canonical packet，经唯一ingress原子提交；AIFS产品固定为`ecmwf_aifs025_single`、independence family=`ECMWF`，不得改名为AIFS EPS。`run_cycle=null`原样保留，禁止猜补native cycle；activation/P0仍为0。

**必须做**：复用retained ECMWF raw；ENS/PF50逐member/run-init/lead/truth/receipt/hash exact；AIFS必须固定官方provider/product ID，AIFS Single保持deterministic AI benchmark/candidate；只有存在可验证官方AIFS EPS产品、成员和arrival receipt时才登记官方ensemble，Open-Meteo AIFS EPS始终保持republisher；全部ECMWF产品共享同一independence family语义。

**明确不做**：AIFS Single不冒充ensemble；不得从名称默认推断AIFS EPS为官方产品；IFS+AIFS不计两个独立家族；单系统不物化P0；不重复抓ECMWF。

**验收/出口**：ENS/PF50样本足scope才生成单系统artifact，其他唯一BLOCKED；retained objects可重放；AIFS角色不会增加cross-system独立计数。

### PR07 — DWD ICON Global/EPS 官方 40 成员 builder

**交付结果**：ICON Global/EPS为全球ensemble泳道，具备成员cohort、arrival、OOS与单系统artifact；ICON-EU/D2仍属区域benchmark泳道。

**当前实施事实**：shared coverage已闭合135 scope，全部为`BLOCKED(DATA_MISSING)`，0 `OOS_READY`、0 artifact、0 cross selection/activation/P0。Global EPS的40×45校验、单日candidate daily curve和partial native-arrival index仍只作输入证据；ICON-EU/D2及Open-Meteo未折算为Global覆盖。

**2026-08-04 canonical收口**：ICON deterministic republisher builder只读V7 canonical AWC truth；三段retained capture仍仅CYYZ、NZWN达到20+8，43站样本短缺；新报告SHA=`b8194438…c857`。两条artifact固定产品`dwd_icon_global`、independence family=`DWD`，并与AIFS两条artifact同事务提交。它们不等于ICON Global/EPS 40成员artifact，不解除PR07 native blocker；`run_cycle=null`不得猜补，activation/P0仍为0。

**必须做**：official Global/EPS 40-member cohort × 45站、run/lead/raw receipt、closed AWC truth、20+8；对象级有界放宽 64MB guard 并流式处理。

**明确不做**：不把 deterministic ICON Global、ICON-EU 或 ICON-D2 混入 EPS；不把 forward decode 当 OOS；不取消全局资源门。

**验收/出口**：每个合格 run 有完整 40 成员证据；缺失 cohort 唯一 BLOCKED；republisher 永无 formal/live authority。

### PR08 — NOAA GEFS 独立 builder

**交付结果**：已保留GEFS raw经正式arrival、成员、truth和单系统artifact合同转为独立可审计builder。

**当前实施事实**：44个完整45站retained包覆盖2026-06-18至2026-07-31，共61,380条member（45站×44日×31成员），已作为canonical `observational_candidate_not_oos`提交；每站44个完整31成员cohort，但strict member cohort=0、native arrival=0、binding=0。canonical-only builder因此输出45/45 `BLOCKED(LINEAGE_MISMATCH)`、artifact 0/45。这些旧eligibility不能称为严格historical OOS已就绪，也不能改写成`CANDIDATE_ONLY(ACTIVATION_ROOT_MISSING)`。

**必须做**：按 member/run-init/lead/truth revision/raw hash exact 重放；20+8；把 Open-Meteo GEFS 如有放入独立 republisher lane。candidate apply必须经过唯一canonical ingress并验证preview零写、幂等重放、整事务回滚、confirmation/quiescence门。`run_init_utc`只表示模式起报时刻，不得替代publication、received或native arrival；只有成员、arrival及其显式binding均可验证时才生成单系统artifact。

**明确不做**：不重复下载 raw；不让一个家族伪装成跨家族组合。

**验收/出口**：每scope READY或唯一BLOCKED，单系统artifact精确绑定成员事实与OOS；跨系统selection/activation只由PR10生成。

### PR09 — ECCC GEM/GEPS 加拿大模型 builder

**交付结果**：GEM deterministic由PR05作为benchmark；GEPS ensemble拥有独立原生集合builder和单系统artifact，两者product/family/source lineage明确分离。

**当前实施事实**：28个完整45站retained主包覆盖2026-07-03至2026-07-30，共26,460条member（45站×28日×21成员），已作为canonical `observational_candidate_not_oos`提交；每站28个完整21成员cohort，但strict member cohort=0、native arrival=0、binding=0。另有v3 hourly 7/27–7/30与主包natural key相同但receipt-set lineage不同，禁止双ingress或挑“最新”；7/31仍是未接入manifest的独立候选。canonical-only builder对登记的10个历史eligibility scope输出10/10 `BLOCKED(LINEAGE_MISMATCH)`、artifact=0；其余35站继续由全矩阵给出terminal blocker。

**必须做**：复用已有 ECCC raw；区分成员、run、lead、official arrival；使用 closed AWC truth 的 20+8；固定 republisher no-live 边界。candidate apply与GEFS共用唯一ingress和同一事务/幂等/回滚门；同natural key不同receipt-set必须显式阻断，不能按mtime、query time或文件名决胜。

**明确不做**：不统称为模糊加拿大模型；不把 republisher 提升 native/formal/live。

**验收/出口**：GEM benchmark与GEPS单系统scope状态唯一；GEPS成员事实、arrival、truth、OOS与`NativeEnsembleSystemArtifact`可反查。PR08/PR09都必须断言cross-system selection/activation/P0为0；selection/promotion/activation仅由PR10生成。

**PR08/PR09共用结构门**：72包inventory SHA=`b3f9beba…8384f7`；apply receipt SHA=`7b8f0c39…f9ba4ab`；幂等只读重放为0 insert/87,840 replay。V6-only真实重跑已固定55 scope全部`LINEAGE_MISMATCH`、0 artifact/activation/P0，报告SHA=`fb30d350…055f`；native arrival/manifest/binding仍为0。当前builder只有正确BLOCKED输出路径，尚缺证据完整时真正materialize single-system artifact的READY分支。Open-Meteo历史fallback只能进入独立republisher no-live lane，不得用于填充native manifest/binding。PR08/PR09禁止自行建第二表、跨表运行时兼容或猜测join。

### PR10 — 跨模型家族 exact-mask、authority 分门、activation 与 P0

**交付结果**：至少两个独立模型家族在同一严格holdout上形成exact-mask selection、artifact、activation与P0；native正式泳道和republisher no-live泳道拥有不同authority，不共享promotion receipt。

**必须做**：只消费PR06–PR09单系统artifact；exact available-system mask和正权重；native-first；republisher fallback也必须至少两个独立上游家族、独立历史OOS activation且`formal_live_authority=false`。运行时 P0 consumer 绑定 PR04 release 与对应 authority-lane activation exact hash；发布前的 PR11–PR16 builder 只能引用同一对象的 exact hash，并只输出未发布事实或 terminal receipt。另行物化当前forward P0 snapshot，固定`station × event_date × d0/d1/d2 × run-cycle × product`、capture/received、TTL、available-system mask与source lineage；native当前包不可用时，只能在同站同事件日且已有独立历史OOS activation时切入republisher no-live fallback。forward snapshot不计OOS，但缺包/过期必须阻断当前P0并输出唯一terminal state。

republisher泳道必须把三类事实分步写入同一canonical ingress后再编译P0：①产品逐字段匹配activation的fresh forward receipt；②同scope、同decision时点的独立native-unavailable/invalid/late receipt；③仅由历史train cohort生成的deterministic-temperature→bucket-probability distribution artifact。P0 snapshot只引用三者及V10 Gen2 activation的exact SHA，不复制或猜测native run/init/publication；`fresh_until_utc`由source capture一次性导出，重新编译不得续期。V10必须同时保留`forward_applicability / probability_distribution_artifact / p0_snapshot`，禁止删掉中间概率artifact。PR04只增加`P0_ACTIVATION/P0_SNAPSHOT` typed scope和natural key，不另造第二套activation fact或current pointer。

**明确不做**：单套集合不物化P0；成员数不代替系统独立性；确定性/区域activation不授权P0；IFS+AIFS不重复计家族；forward不计OOS；不得把AIFS Single与AIFS ENS、ICON deterministic与ICON EPS改名兼容；不得把输入缺席、republisher自身无arrival proof或机械布尔值当成native不可用证据；不得用加权温度中心、holdout RMSE或旧native/legacy fallback compiler伪造桶概率。

**验收/出口**：任一P0可反查至少两个独立家族的事实、单系统artifact、同holdout cross selection和activation；native P0另须formal promotion与完整manifest/binding，republisher P0固定`PRODUCTION_NO_LIVE_ONLY`。当前snapshot还必须反查精确产品forward receipt、独立native-unavailable receipt、概率分布artifact、market bucket bridge、TTL与lane选择receipt。错站、错日、错产品、过期、错activation、truth revision stale、概率向量不可复算或任一越权均整包零写`FAIL_CLOSED_NO_P0`；确定性benchmark activation=0；republisher永无native arrival/P1/formal/live authority。

**2026-08-04 W3b收口**：CYYZ/NZWN两条republisher cross-selection已进入V8唯一ingress。每条固定引用同站AIFS Single/ECMWF与ICON deterministic/DWD artifact exact hash；权重只用20条train的bias-corrected RMSE，8条holdout只评估，派生值固定12位十进制字符串。合同SHA=`7f75d3a3…f459`，首次提交2 insert、幂等重放0 insert/2 replay；post-selection active V8为103,887行、row fingerprint=`5035c94d…b3b6`。旧republisher fallback pointer到native P0 compiler的生产调用链已断开。activation/P0仍为0；下一步只做独立no-live activation，不得从selection直接生成P0。

**2026-08-04 W3c收口**：V8→V9迁移保持103,887行与`5035c94d…b3b6`指纹不变；CYYZ/NZWN两条Gen1静态`republisher_no_live_activation`已进入唯一ingress，合同SHA=`608d0d58…e036`，首次2 insert、幂等重放0 insert/2 replay。post-activation active V9为103,889行、row fingerprint=`2f7f6bea…82b8`。activation逐字继承`run_cycle=null`，重新验证selection、parent artifacts与最新闭日AWC truth；不包含forward snapshot/TTL/native-unavailable，不写通用/native activation、P0、P1或formal/live。下一步单独完成forward applicability receipt和no-live P0 snapshot，缺当前证据固定`FAIL_CLOSED_NO_P0`。

**2026-08-04 W3d只读取证收口**：CYYZ最近可见包是2026-08-03且超过6小时，NZWN虽有2026-08-04事件日包但capture同样超过6小时；两站均不满足freshness。更根本地，现存forward包产品为AIFS ENS+ICON Global EPS，而V9 activation绑定AIFS Single+ICON deterministic，不可逐字段对应。现存`native_source_unavailable`类字段均缺独立官方request/response/content hash/as-of/expiry，不能作为native-first切换证据；selection也没有把确定性温度转换为bucket probability的独立历史artifact。故W3d审计完成、实现出口未完成，当前P0继续为0。

**2026-08-04 W3e V10概率链收口**：V9→V10迁移前后103,889行与row fingerprint=`2f7f6bea…82b8`保持不变；V10一次加入`forward_applicability / probability_distribution_artifact / p0_snapshot`。CYYZ/NZWN两条train-only residual-bootstrap artifact已进入唯一ingress，holdout CRPS分别为`0.300794581158/0.436729795937`，均优于climatology；两条Gen2 activation已精确绑定distribution SHA与Gen1 predecessor。active V10为103,893行，但`forward_applicability=0`、`p0_snapshot=0`仍是正确状态。下一出口只实现exact-cohort native probe、fresh exact-product capture和post-capture re-probe；官方对象完整或状态不确定时继续`FAIL_CLOSED_NO_P0`。

**2026-08-04 W3f exact-cohort采集合同切片**：已新增PR10专用native probe与Open-Meteo exact-product capture/decode原语。AIFS固定官方`AIFS Single / oper / fc / sfc / 2t`完整6小时valid-time cohort，ICON固定官方`T_2M`逐小时完整station-day cohort；23/24/25小时DST日均按settlement timezone精确派生。网络只允许`http(s)://127.0.0.1:1082`代理、`trust_env=false`、流式字节上限且禁止direct retry。native结果只允许`NATIVE_COMPLETE_NO_FALLBACK / NATIVE_UNAVAILABLE / NATIVE_INVALID / NATIVE_LATE / INDETERMINATE`；404/410可形成terminal unavailable，403/429/5xx、3xx、DNS/TLS/timeout/transport/resource失败一律`INDETERMINATE`并保持零写。Open-Meteo请求固定同站坐标、同事件日、同settlement timezone、`temperature_2m`及`ecmwf_aifs025_single / dwd_icon_global`精确产品，daily max必须从完整hourly curve重算；forward TTL精确6小时、native evidence TTL精确5分钟，lead只能由decision local date严格导出d0/d1/d2。

> 本段与其后的W3f缺口描述是历史时点；当前实现状态以W3g/W3h段及第13节为准。

W3f仅闭合“可验证采集原语”切片，不是PR10出口：尚缺可执行`pre-probe → capture → post-probe → unique ingress → P0`编排器，完整native object receipt set、Open-Meteo raw body与normalized curve尚未以不可变内容地址引用进入canonical lineage；market bucket bridge仍不得接受裸64位SHA，canonical P0尚未原子投影为生产消费者读取的ProbabilityEnvelope、typed `P0_ACTIVATION/P0_SNAPSHOT`与PR04 release/current pointer。因此真实DB继续保持`forward_applicability=0 / p0_snapshot=0`；不执行网络、不启动服务、不解除pause、不下单。

**2026-08-04 W3g/W3h/W3i P0 消费接线工作包（代码合同已闭合，真实P0与release未闭合）**：本切片不新增第19个PR；断点仍归属`PR03.D1 → PR15.A → PR10.W3g → PR04.C/PR04.D`。PR03.D1已加入CAS evidence store、full manifest及唯一ingress物理对象rehash门；PR15.A已在V11提交45/45 canonical market bridge并完成0 insert/45 replay幂等回绕；PR10.W3g已加入专用原子seam，使canonical P0 fact与`ProbabilityEnvelope(P0)`同事务全有或全无，并封住direct writer、单边状态和release前消费；PR04.C已实现`P0_ACTIVATION` producer与`P0_SNAPSHOT` terminal receipt。W3i又完成typed P0业务键、真实V11 contract snapshot、八类DB反查及truth-gap evidence重绑定。当前真实`P0_ACTIVATION=2 ACTIVATION_READY`，`P0_SNAPSHOT=0 BLOCKED(DATA_MISSING)`，`forward_applicability/p0_snapshot=0`。最新PR04 preview为`CANONICAL_CONTRACT_TRUTH_GAP / BLOCKED`；current pointer不变，因此任何P0仍不得以`materializes_p0`或`MATERIALIZED_UNPUBLISHED`冒充可消费概率包。下一切片仅为PR04.D expected-scope矩阵，不新增第19个PR。

**真实network回执**：CYYZ官方AIFS 4/4、ICON 24/24完整，状态`NATIVE_COMPLETE_NO_FALLBACK`，在Open-Meteo capture前终止；NZWN AIFS 3/4、ICON 23/24可见，但分别出现一次`ReadError/ConnectError`，状态固定`INDETERMINATE`并同样在capture前终止。两次均`forward_applicability=0 / p0_snapshot=0 / order=0`。NZWN阻断证据已保存CAS manifest `b6801006…e14597`；CYYZ首次运行发生在blocked-probe CAS补口前，只保留完整receipt-set/hash而未重复请求补raw。任何一站均不能代表45站完成。

**PR10.W3g验收/出口**：`canonical P0 fact + P0 ProbabilityEnvelope`必须同事务全有或全无；后者精确绑定station/event/lead/timezone/decision/freshness、activation、forward applicability、distribution、官方bucket bridge和`PRODUCTION_NO_LIVE_ONLY`。post-probe为native complete/indeterminate，或任一receipt对象地址、产品、站点、日期、时区、lead、TTL、概率或bridge不一致时均零写。PR04.C的typed key/evidence/coverage/CAS任一失败时current pointer不变，且P0-M/P1/basket/service/gateway全部不可读取该P0。direct envelope writer、legacy native packet/materializer、裸SHA bridge和republisher越权进入P1/formal/live一律负测拒绝。详见父计划第17.7节的逐项断点、依赖和回绕矩阵。

## 7. M3 — P0-M 与 P1

### PR11 — 官方 AWC TAF exact-join 与原子 P0-M

**交付结果**：TAF exact-join只使用PR10 authoritative cross-native-ensemble P0 root，TAF TX校准与物理二次确认原子形成P0-M。

**必须做**：官方 AWC TAF raw、完整 revision/AMD/COR quarantine；station × lead × run-cycle × decision-time × P0 root exact join；每 scope 至少 24 个历史 exact sample。current TAF capture必须有请求/接收、validity window、revision freshness、资源门和terminal receipt，并与同一决策时刻current P0 root在一个P0-M物化事务中绑定；TAF缺失/陈旧/修订不明只阻断P0-M，不改写历史样本。

**明确不做**：不以当前 truth 伪造 TAF 输入；不以 Open-Meteo 替代官方 TAF revision；不生成 P0-F 分支。

**验收/出口**：15站 archive candidate inventory当前仅19–28日且15/15 `candidate_input_complete=false`，不得计为`OOS_READY`；当前exact join max=11、qualified=0、42/42 legacy d2/18Z scopes blocked。官方TAF raw/revision门、strict historical exact join≥24门、PR10 authoritative P0 root exact binding门、P0-M activation/materialization门必须分别通过；forward计数为0。全部45站每scope均为唯一canonical phase或`BLOCKED`。

**2026-08-04 canonical terminal**：V12中没有`historical_p0_root`、TAF revision或exact-join fact type；既有republisher `p0_snapshot`不具备历史strict-OOS root语义，旧TAF audit/legacy P0-M不得接入。因此`PR11TafP0mBasisBuilder`只物化一个builder级`BLOCKED(P0_ROOT_MISSING)`（scope count=0，receipt SHA=`7f52e613…b0514`），而不是从45站registry或truth-day伪造`TAF_P0M` key。P0-M artifact/event/envelope写入均为0，PR11保持`IN_PROGRESS`。


### PR12 — AWC station-day bounds、correction review 与 closed truth

**交付结果**：AWC raw → station-day bounds → correction/revision review → canonical closed truth 的唯一正式链。

**必须做**：45站 :00 METAR 与有效 SPECI；settlement timezone 和日界唯一；重复内存去重零写；COR/AMD/冲突 append-only review + station halt；closed-day revision/hash/commit time；只消费PR02.A既有raw重验与PR02.C真缺口补取/冻结的canonical结果和gap manifest，不在本PR另建第二套来源调查或ingress。PR12是observation/station-day bounds的唯一schema、parser和canonical ingress owner：它定义typed fact、验证官方报文并提供caller-owned outer transaction API；不自行拥有decision/intent/资本编排。每次poll保留官方request/received/raw/body SHA、revision/freshness、资源探针、去重结果与terminal receipt；其服务入口由PR16装载，但事实合同和唯一ingress归PR12。PR12的realtime→closed-day truth供给切片与PR06–PR09在M2并行推进：未闭日只服务realtime observation，闭日并通过revision review后才可进入后续历史strict-OOS；不得等PR10完成后才开始积累truth。

**明确不做**：不混 AMSC、跨源 fallback 或未经 review 的 correction；不自动恢复 halted scope；不把`:52`改名成`:00`，不降低时刻合同，不以Open-Meteo替代truth。

**验收/出口**：45站 day bounds 唯一；所有进入历史训练/OOS的truth均closed-day并绑定canonical revision/hash/commit time；realtime P1 observation可未闭日，但必须是官方AWC并绑定revision/freshness/as-of/correction review/halt状态。官方无合格`:00`对象的scope以唯一`OOS_SAMPLE_SHORT/DATA_MISSING`退出，不得假完成。

### PR13 — METAR/SPECI trajectory artifact、activation、proof 与 P1

**交付结果**：历史 trajectory → strict OOS → FORMAL_PROMOTED artifact/activation → update proof → P1，不再出现 rootless activation=NONE 的 P1。

**必须做**：scope natural key固定为`station_id × season × intraday_bucket × maturity × source/target contract`；当前冻结范围为45站×JJA×4 intraday（NIGHT/MORNING/AFTERNOON/EVENING）×D0=180 scopes，扩季节或maturity后必须重算总数；当前 P0/P0-M root 与完整 AWC trajectory exact binding；TAF eligible scope必须使用PR11 P0-M，只有TAF不可用且有唯一 blocker时才允许合法P0 fallback；P1 append-only；硬下界只剔除数学不可能的 YES 桶。逐小时动力曲线必须绑定同一P0根、至少两个已激活独立模型家族及content-addressed strict-as-of历史证据，并显式保存峰值、峰时、下一点、斜率、报文前缀和所用物理字段；late、wrong-root、static或alpha-zero证据全部fail closed。

**明确不做**：不以 AMSC/Open-Meteo/rootless 观测产生 P1；不以 stale P1 静默回退。

**验收/出口**：当前180/180 `BLOCKED_EVIDENCE`、0 formal eligible、promotion/activation缺失的事实必须保持，直到新证据真实改变；每scope均有唯一canonical phase或`BLOCKED`。合格scope须同时绑定`FORMAL_PROMOTED` artifact、exact activation hash、update proof、current P0/P0-M root、canonical ingress receipt、AWC observation revision/freshness/as-of/correction-halt证据，并产生可计数的materialized fresh P1；否则唯一blocker。

**2026-08-04 已物化terminal basis**：`PR13P1RootBasisBuilder`仅读取V12 `truth_observation_revision`与canonical `p0_snapshot`，逐行复核schema、stored/content hash、AWC origin、QC/halt与strict-as-of；它绝不读取legacy `basket_probability_envelope`。真实V12快照的1,128条revision形成46个station-day scope，而canonical P0 snapshot为0，因此全部为唯一`BLOCKED(P0_ROOT_MISSING)` terminal receipt（`pr13_p1_root_basis_20260804T120000Z_v1.json`，SHA=`91399fe…ce42a`）。本轮P1 artifact/envelope仍为0，PR13继续`IN_PROGRESS`；PR04 pointer、服务、篮子、gateway与live pause均不得改变。

## 8. M4 — 纯 YES 篮子、WS 与 no-live 服务

### PR14 — 策略 utility、目标库存与纯 YES 篮子

**交付结果**：P0/P0-M/P1 到 target/reserve/cadence，到真实 utility、ΔBasketScore 和唯一 YES target inventory 的闭环。

**必须做**：只消费已物化且带lineage的P0/P0-M/P1、regime、AWC硬排除和官方CLOB WS；P1尚未到达才按阶段使用P0-M，TAF尚未到达才使用P0，P1一旦出现但损坏/陈旧/错根/歧义必须停牌而非降级。PR14是regime唯一owner：`CanonicalRegimeFact`的自然键为`station_id × event_date × decision_at_utc × regime_version × source_evidence_hash`，必须通过同一canonical ingress并绑定进策略record，不另建ledger/pointer；缺失、陈旧或错lineage即停牌，regime只作方向一致性闸门，不得平移概率中心。完整物化`BasketPolicyTrainingFact`、`GlobalBasketPriorArtifact`、`StationTargetPolicyArtifact`、`StationReservePriceArtifact`、`StationCadenceArtifact`、`StationBasketPolicyActivation`、`ResolvedStationBasketPolicy`、`BasketCashPhasePolicyArtifact`、`BasketCashPhaseDecision`。真实utility natural key固定到`station × event_date × settlement_timezone × decision_at × policy_stage × strategy_version × season × maturity × liquidity × candidate inventory hash × baseline inventory hash × WS snapshot hash`，receipt必须绑定实际fill/partial fill、settlement、reserve/cadence机会成本；缺任一项输出terminal blocker，不生成空分数或promotion。cash-phase forward evidence必须保存cycle receipt content binding、target-local row identity与CAS append-only状态，只能在canonical SSOT label提交后投影；poison输入隔离并按明确retention处理。forward evidence永远不是OOS，也不得形成第二decision SSOT。underreach/predecessor与已报最高温形成新高后的upside/successor各最多两层；只有官方AWC新报文推进站点当日已报最高温才触发策略重算，同温或旧温必须零intent。同一正库存缺口的Maker腿必须作为一个完整、原子的post-only GTC bundle验证，任一腿证据/深度/预算门失败则整篮HOLD；确认撤销Maker后才可限价单桶FOK；立即执行腿`sum(all_in_yes_ask)<1`；成交后重算；只可卖S/B/L/C超额份额，M/A/F不得卖出。PR14只拥有调用PR12 caller-owned API的最外层原子编排，将observation/day bounds、decision、intent和资本预留同事务提交；不拥有第二套observation/day-bounds schema、parser或ingress。执行阻断唯一来自`auto_trade_decisions`。

**明确不做**：不以天气概率或模拟成交伪造策略 OOS；不产生 BUY NO、hard-NO 或 order 写。

**验收/出口**：不以“score不再全零”作为验收。每一个已发布target/reserve/cadence score row必须逐项绑定唯一 natural key、真实fill/settlement/WS/utility evidence与strict historical OOS；所有预期scope必须输出READY/CANDIDATE_ONLY/BLOCKED覆盖矩阵，且至少一个后续进入live allowlist的scope真实READY，否则PR14保持BLOCKED且PR17不得解pause。无真实 utility 只 candidate；五类事实同事务同成败，故障、重放、并发和回滚均无半写、重复intent或重复资本预留；will_order=0、order events=0。

### PR15 — 官方 CLOB WebSocket 新鲜盘口

**交付结果**：45站当前 active market scopes 的唯一 OFFICIAL_CLOB_WS、逐 token 原子盘口事实。

**必须做**：PR15是official market registry与bucket contract canonical fact/ingress的唯一owner。`MARKET_WS/REGISTRY`自然键为`station_id × event_date × market_id × yes_token_id × bucket_contract_sha256`；`MARKET_WS/SNAPSHOT`自然键为`station_id × event_date × market_id × yes_token_id × ws_sequence × snapshot_at_utc`，并绑定registry content hash。WS只负责在已验证映射上追加YES token的ask/bid depth、received/expiry；缺腿、乱序、重复、跨 token、stale 均 fail closed；不写 event_market_ticks。

**明确不做**：不使用 REST、前端缓存或 WS 之外盘口；WS不能自创market/token/bucket映射，也不授予下单权限。

**验收/出口**：所有 active scope 有 fresh exact token/depth/expiry；任一篮子腿缺失整篮 HOLD。

### PR16 — 唯一 gateway 与三个用户域服务 no-live 验收

**交付结果**：仅 com.weather.weather_basket_yes、com.weather.market_ws、com.weather.clob_order_service 在 gui/$(id -u) 运行的 no-live 闭环。

**必须做**：8765 health、唯一 gateway、写锁外读取盘口、Maker idempotency；取消未确认/迟到/未知回报进入 reconcile_required；acceptance 返回 structured blocker；退役UI/monitor/Bayes/model-scan/REST-refresh/task-queue/tick-writer的plist、控制命令与bootstrap入口；runtime只绑定一个已验证PR04 v2 current root及P0/P0-M/P1/WS/策略artifact exact hash vector。

**明确不做**：不使用 sudo 日常控制；不加载 system 同名服务、reported_trajectory_no 或第四服务；不解除 pause。

**验收/出口**：三服务 loaded、system/legacy及旧控制面为零、health=200、45站 worker为canonical phase或`BLOCKED`、gateway/orders/order events 为零；切换前后三服务均在同一`gui/uid`，system域为0，runtime hash vector不漂移或只经已验证CAS release切换。

## 9. M5 — 45站总验收与受控 live

### PR17 — 45站总验收、formal promotion 与受控 live cutover

**交付结果**：一份45站总 scorecard 与最新四大方案状态；符合门槛的 native scope 才能 FORMAL_PROMOTED，真实解除 pause 另列操作 receipt。

**必须做**：显式绑定最新已验证PR04 current release，并要求PR10–PR16每个scope均有canonical phase或`CANDIDATE_ONLY/BLOCKED` terminal state；计算非空`live_eligible_allowlist`，每项必须是同一station/event/policy-stage在native `FORMAL_PROMOTED` P0、当前可用P0-M/P1阶段根、regime、policy/utility、market registry、fresh WS、runtime的端到端`CONSUMABLE`交集，且exact hash vector一致；重跑 canonical/ingress/ledger/activation、所有模型泳道、P0/P0-M/P1、utility/basket/WS/order-state、三服务、资源与 recovery；全局风险/资本上限首轮受控；live授权receipt绑定唯一release/hash vector、非空allowlist、资本上限、rollback/reconcile顺序与熊先生明确授权。

**明确不做**：不让 republisher 获得 formal/live；不以单站代表45站；不在未授权时产生真实订单。

**验收/出口**：45站每 scope 唯一状态；`live_eligible_allowlist`非空且只包含端到端READY交集，其他站不调度。若allowlist为空或端到端READY=0，只能产生`VERIFIED_NO_LIVE`报告并固定`BLOCKED(NO_FORMAL_NATIVE_LIVE_SCOPE)`，绝不得解pause；forward OOS=0；hard-NO/BUY NO/第四服务/system dual-run=0；回滚按“暂停新intent→确认撤销Maker→确认无FOK在途→未知/迟到回报reconcile_required→SSOT重算→回退runtime release”演练通过，且不回切canonical pointer、不删除append-only事实、不依赖外置盘恢复。只有熊先生明确授权后，才可执行单独的 live pause 解除步骤。

## 10. 交付纪律

每个 PR 的提交说明必须附：目标、非范围、精确输入 hash/pointer、变更文件、数据 migration/backfill 说明、定向测试、一次45站只读 smoke、失败→恢复→再次失败回绕、零 gateway/order 断言、回滚方式、剩余 blocker。

代码合并、数据回填、artifact promotion、服务装载、live 解锁是五个不同状态转换。任何一个缺失都只能保持 no-live 或 BLOCKED，不得在 PR 标题或验收中称为“上线完成”。

## 11. 当前下一主线

按依赖顺序：PR02.C/PR04旧current、retained GEFS/GEPS 87,840 member、V6→V11迁移、AIFS/ICON 4条artifact、2条cross-selection、2条distribution、Gen2 activation、45站market bridge、P0原子seam、typed P0业务键、真实V11 contract snapshot、八类DB反查及truth-gap source mode恢复均已完成。PR04.D的manifest loader、expected/observed集合门、ledger/consumer二次验证、release-time P0 TTL和coverage未完成禁止apply已落地；真实preview已正确转为`typed_scope_coverage_complete=false`且pointer不变。当前只做各领域owner输出独立basis manifest并形成唯一expected manifest；非模型scope不得伪造为45×lead。真实`forward_applicability/p0_snapshot`仍为0；不得从Gen2 activation直接生成P0，其余43站保持唯一`OOS_SAMPLE_SHORT`。

## 12. 2026-08-04 W3a 强制复盘

| 项目 | 已确证事实 | 当前处理 | 禁止的错误动作 |
|---|---|---|---|
| PR01 | baseline合同已验证；当前21:08Z baseline identity=`a95ae2e…c7e4`，保留5个真实上游blocker。 | 只在真实输入变化时重建；不因`BLOCKED`重做合同。 | 因current工作树变化重做PR01、引用无artifact identity，或把历史receipt当当前指针。 |
| baseline/pointer | current root=`35777f0…e9c1`仍绑定post-PR02.C旧BLOCKED release；新preview含14,264 records与0 activation，尚未apply。 | 保持current不变；只有typed coverage与binding真实改善后才重跑preview/CAS，任何后续CAS前重读pointer content SHA。 | 把BLOCKED preview当消费root、回切旧pointer、取mtime/文件名或重抓raw。 |
| activation current | 当前真实active V10 SSOT有2条selection、2条distribution与Gen1/Gen2各4条republisher no-live activation；通用/native activation、forward applicability和P0均为0，旧d1/d2 generation只存在retired表。 | retired activation只审计；PR05–PR10只能从V10 canonical事实重建，Gen2不承担forward freshness。 | 从retired复制、补旧authority、取max generation、改名复活或把Gen2当当前P0许可。 |
| PR02 | 已封堵TEMP overlay/18Z/constructor、空binding/direct旧packet与explicit-18Z lineage三个指定入口；指定回归41 passed。 | 退役AMSC/custom旧CLI；P1 replay拒reference-only/自构authority并锚定可信archive receipt；核验45站authority projection而非只看schema status。 | 把当前入口的self-hash校验夸大为认证式全库provenance，或把旧AMSC/reference/custom station map改名为AWC。 |
| PR03 | AWC canonical/projection外层事务、V7→V9迁移与commit-fault零半写均已验证；4条artifact、2条selection及2条静态activation已由唯一ingress提交并幂等重放。 | PR03总体仍为`PARTIAL_VERIFIED_NO_LIVE`；真实arrival/manifest/binding、通用/native activation与P0均为0。 | 把静态activation当forward/P0、恢复legacy adapter/双读、为republisher另建native表，或用Last-Modified/receipt-set hash造arrival。 |
| legacy运行入口 | 仓库内monitor、AMSC poll/SPECI、task queue、weatherctl/control/install、旧cutover/recovery及剩余AMSC活化writer均已删除；三个正式gui服务与旧NO服务当前均absent，live pause保留。 | 两个root-owned plist保持disabled/unloaded并登记`BLOCKED(ROOT_DAEMON_RETIREMENT_REQUIRES_PASSWORDLESS_SUDO)`；仅在未来`sudo -n`可用时执行一次性删除并留receipt。 | 因当前未加载就宣称已退役、使用交互sudo、反复尝试不可用的`sudo -n`或恢复旧入口。 |
| canonical truth | active V10为103,893行：12,600 model + 596 truth + 2,845 residual + 87,840 ensemble member + 4 OOS artifact + 2 selection + 2 distribution + 4 activation；旧399个truth identity/hash被保留并append 197个新scope。 | builder只从active canonical model/truth/artifact/selection/distribution/activation重算；不得读取或复用retired residual/activation。 | 把V7–V9旧快照称为current；给旧行默认补`source_role`、改名重封、原地覆盖hash或恢复旧activation。 |
| historical AWC样本 | PR02.C final receipt已覆盖20,946/20,946 targets；replay v5为16,945 observations、859 observed station-days、596 truth-ready、45 blocked/0 complete；0/45达到20+8，d1最大19 train/8 holdout；596 station-days不等于每lead 596 OOS样本。 | 完整日进入canonical truth；不完整日和官方retention/exact-slot终局缺口进入唯一ledger，不再循环重抓。 | 把observed当truth-ready、将`:52/:53`改名成`:00`、以未来数据/Open-Meteo/AMSC补truth。 |
| post-PR02.C PR04 release | 21:08Z current已CAS；随后v3迁移、single-snapshot supersession与四类typed envelope完成，新preview仍为BLOCKED。 | current root不变；两类缺字段scope只保留terminal blocker receipt，后续由领域PR补真实identity。 | 把旧cutoff或retired activation当当前根因、以release id代替pointer CAS值、fsync失败后重试写或放宽as-of。 |
| OOS与模型raw | forward不是OOS；现有d1/d2确定性模型事实充足，当前根因不是ECMWF或全体模型raw缺失；但这些确定性facts不授权P0。 | PR05只做benchmark；PR06–PR09补原生集合单系统artifact，PR10负责跨集合P0。 | 等待未来样本、重复抓ECMWF、跨lead/家族借OOS、用确定性activation造P0。 |

以下为W3a当时的上下文冻结，已由第13节取代：真实active V10=103,893行，其中87,840 member、4条republisher OOS artifact、2条republisher cross-selection、2条probability distribution artifact、4条Gen1/Gen2 republisher activation、0 arrival、0 manifest、0 binding、0 forward applicability、0 P0；V10迁移前row SHA=`2f7f6bea…82b8`。两条Gen2只覆盖CYYZ/NZWN，其余43站保持`OOS_SAMPLE_SHORT`；55个native scope保持`LINEAGE_MISMATCH`。

## 13. 2026-08-04 W3j 文档化状态与下一PR出口

本节是W3j历史状态；第12节保留为W3a历史快照，二者均不得覆盖第14节W3k增量。

| 工作包 | 已落地文档合同与证据 | 当前状态 | 唯一下一出口 |
|---|---|---|---|
| PR03.D1 | CAS对象、full manifest、唯一ingress rehash与caller-owned事务边界 | `PARTIAL_VERIFIED_NO_LIVE` | 继续作为PR10原子P0 seam的证据入口，不另建writer |
| PR15.A | V11、45/45 canonical market bridge；45 insert，重放45 replay；AWC-only、registry/token/interval/strict-as-of负测 | `VERIFIED_NO_LIVE`子切片 | PR15主体仍须完成fresh official CLOB WS；bridge本身不代表盘口或live |
| PR10.W3g | canonical P0 fact + `ProbabilityEnvelope(P0)`同事务；persisted activation/forward/distribution/bridge/CAS反查；未发布消费围栏 | `IN_PROGRESS` | 真实forward/P0仍为0；只在native terminal fallback与fresh exact-product receipt成立时物化 |
| PR04.C | typed P0业务键已逐项对应canonical station/lead/run-cycle/vector/authority/system-set/family-set；generation仅为version；真实V11 contract snapshot、八类DB反查及596 truth-gap重绑定已完成 | `PARTIAL_VERIFIED_NO_LIVE`子切片 | 不再重做contract snapshot或typed key；两条activation仍不是可消费P0 |
| PR04.D | manifest contract/loader、十一类expected/observed集合门、ledger verifier、release-time P0 TTL、coverage未完成禁止apply及release-aware P0授权reader seam已落地 | `PARTIAL_VERIFIED_NO_LIVE / BLOCKED(EXPECTED_SCOPE_MANIFEST_MISSING)` | PR05–PR16各领域builder须输出独立basis；合并manifest后expected/observed完全相等，missing/excess=0且CAS无漂移才可apply；PR17只消费总结果 |

旧contract缺必需数组和typed P0 key不一致已关闭，不再作为当前根因。真实V11 snapshot包含103,881条V4事实，
contract SHA=`59837843470dacde2f90f2af7a64e4fc7c1e7471f076e2e3d279796ded583131`；projection覆盖103,881行，
SHA=`f27a886f6a6321a2339dfca30750f2e188719b2b4c9ac0fbbc0f813fea8b281e`。W3i的旧preview
`a3f6b9…f5fe6`仅保留为实现前历史证据，已由下述W3j preview取代。

最新PR04 blocker固定为：独立expected-scope manifest尚未由各领域builder产出。当前代码已禁止从observed facts反推，
没有manifest时十一类全部`BLOCKED(EXPECTED_SCOPE_MANIFEST_MISSING)`；最新preview content SHA=
`19970a9815fa48206043872350baf9e0db6730fd6492a249c11073938a756c5e`，`typed_scope_coverage_complete=false`，
0 activation scope、未apply。current pointer文件SHA前后均为
`58a072b006c646e103bfd66fca5cf63398cca00c3a33f0bf78f74a0c50255c88`。
该SHA是加入每站expected-key count与unresolved basis receipt后的第二次真实preview，已取代旧`90acf4bd…d20b2`回执；当前manifest schema下仍正确fail closed，且未切换current。

当前真实边界：45站bridge完成；两条no-live activation存在；真实P0为0；M1/M2/M5均未完成；三服务未启动，
live pause保留，gateway/order为0。下一主线是由PR05–PR10先提供MODEL/P0 basis，PR11–PR16并行补各自非模型basis和真实producer；
release只负责集合门，不替领域层造key；PR17只消费发布后的总结果并形成45站scorecard/live allowlist。不重复抓ECMWF、不重建V11 snapshot、不重复probe CYYZ/NZWN、不把legacy P0映射为canonical P0。

## 14. 2026-08-04 W3k 领域 basis 与 timezone 实施合同增量

本节只收紧既有PR04.D、PR05–PR13责任边界，不新增PR04.E/F、不新增第19个PR，也不创建第三份路线图。父计划第17节最高编号checkpoint保存当前证据与顺序；本节保存各PR可直接实现和验收的增量合同。

### PR04.D / PR17 — producer与consumer边界

- PR05–PR16是十一类expected basis与observed terminal rows的生产者；每类必须从自身canonical领域事实独立生成basis，绑定同一45站registry、release as-of、source hashes、expected keys和unresolved receipts。
- PR04.D只接受十一类basis恰好各一份，编译唯一manifest并验证`expected = observed`；不得读取release preview、typed observed envelope或当前ledger反推expected。
- PR17不生产PR04 basis，不参与解除`EXPECTED_SCOPE_MANIFEST_MISSING`。它只消费PR04已发布current release，计算45站端到端scorecard和非空live allowlist；由PR17反向补basis会形成PR04↔PR17循环，固定拒绝。

### PR05–PR09 — MODEL_OOS basis必须保留完整产品向量

- 真实coverage grain为`station × lead × product`，共2,835 rows；family-level expected key可以聚合，但每个key必须保存完整component vector：`product_id / provider_id / source_class / independence_family_id / run_cycle / coverage_row_content_sha256`。
- 当前有效basis为`output/audits/pr04d_model_oos_expected_scope_basis_20260804T100000Z_v2.json`，SHA=`7774b65ea3f756fb3bfd592ab4b2a510103506c2ac54e748395d2307ce68b95a`，810 keys/810 evidence；405 keys为多产品组合，最多10 components/key。任何丢失产品向量的旧basis无效且不得恢复。
- observed MODEL status必须绑定与expected evidence一致的`domain_basis_content_sha256`。natural key相等但basis SHA不等时输出`evidence_mismatch`并保持BLOCKED；当前90个truth-gap status不得因此假通过。

### PR10 — P0 activation/snapshot basis与原子物化

- `P0_ACTIVATION`只从canonical cross-selection和exact activation candidate派生；完整selection component refs决定system set、family set与run-cycle vector，无selection只写unresolved receipt。
- `P0_SNAPSHOT`只从activation、forward applicability、canonical market bridge和原子P0 envelope四者exact join派生；同站、同事件、同decision、同as-of、同authority/product vector任一不等即fail closed。
- 当前`forward_applicability=0 / p0_snapshot=0`，legacy 387个P0 envelope均缺canonical snapshot binding，禁止兼容映射。只在terminal native-unavailable与fresh exact-product republisher forward成立时调用既有outer transaction P0 seam。

### PR12/PR13 — observation timezone只能由canonical ingress形成

- `CanonicalObservationEvent` v2把`settlement_timezone`纳入identity；旧v1、缺失、非法IANA timezone、同sequence跨时区均拒绝。P1 observation timezone必须与P0/P0-M root逐字一致并写入lineage，atomic facts只能读取该lineage。
- 真实DB的21,102条legacy observation、1,555条legacy daily truth均尚无timezone列；2,099条既有P1中只有996个event id可对应observation，1,103个缺失，完整canonical timezone/hash binding为0。不得从registry/default/其他表回填。
- 实施固定为`隔离nullable migration演练 → 真实nullable migration → 保留AWC raw重新canonical ingress → PR13 exact fact id/hash/timezone binding`；无法逐项证明的旧行保持audit-only。`atomic_ledger`中的第二observation writer必须封口。

### PR11 — TafP0mBasisBuilder与TAF-24原子门

- 当前不是TAF raw为零：45站有44份exact-join artifact、141个strict samples，单scope最大11且0 scope达到24；capture=1,211、formal AWC rows=979/444 station-days、正式覆盖15站、quarantine=89（AMD 32/COR 57，MMMX 54）。
- 新builder只读PR10 canonical historical P0 root。ARTIFACT key=`station × timezone × season × lead × run_cycle`；EVENT key=`station × event_date × decision_at × p0_root_sha × taf_revision_id`。
- 每个scope必须绑定P0 selection/activation/run-vector、官方AWC TAF raw/revision/validity、闭日AWC truth、quarantine和24日集合。`<24`或root缺失只写terminal receipt；`>=24`才在同一outer transaction写artifact、P0-M envelope和EVENT。
- forward、AMSC、Open-Meteo TAF、legacy d2/00Z或d2_fallback/18Z P0-M均不能进入正式basis。旧45条P0-M只覆盖RKPK三个event且season为空，固定为不可映射历史证据。

### 增量验收与下一出口

- MODEL basis：同family多产品不丢component；错product/provider/source/run-cycle或basis SHA均确定性阻断。
- timezone：nullable migration幂等、零默认回填；重放同一AWC fact幂等，缺失/冲突timezone零写；P1与atomic facts精确绑定同一lineage。
- TAF：23样本只能terminal blocked，24个exact historical sample才原子三写；AMD/COR未完整隔离、错P0 root、错decision或forward输入均零写。
- 下一顺序固定为`PR10 P0 basis → PR12 migration/re-ingress → PR13 binding → PR11 builder → PR14–PR16 basis → PR04合并preview/CAS → PR17总验收`。不重抓ECMWF、不等待未来OOS、不启动服务、不解除pause、不下单。
