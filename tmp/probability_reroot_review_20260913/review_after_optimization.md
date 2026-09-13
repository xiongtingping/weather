# P0 / P0-M / P1 重新定根与续期：优化后复查

熊先生，结论是：**本轮优化对生产主路径有效，但三阶段的全部路径还不能通过闭环验收。** 已确认的剩余问题集中在真正的 TAF 校准分支、消费端根一致性，以及原生编译器中的 TAF 入口。

本次可验证终点线：核对最新改动；以 45 站生产记录验收已修部分；跨有/无校准 artifact、同轮/下一轮、根更新/TAF 更新、过期/缺证据等边界复现剩余问题；区分既有测试失败与新增回归。终点线已完成，以下是检查结果和修复顺序。本轮未修改生产代码或重启服务。

审查版本：`d6fa71a34c3f694b88c354fa0cc0cced7ad7f012`。本轮概率改动主要为 `f4ba9d30` 与 `53d95ddf`；按文件足迹确认，与同时间的估值改动分开审查。改前基线为 `b86dc71c7634344fe987de6d9ccd0f3751da5ef8`。结束前再次核对了相关代码未变化。

**已修复并获得生产证据的部分**

观测到驻留进程于 **2026-09-13 07:01:21 UTC** 启动，晚于两笔概率修复提交。按该时刻至 **08:55:31 UTC（北京时间 15:01–16:55）** 的 decision 窗口、事件日 09-13/09-14 读取记录，首轮保存的样本如下；45 站均有新 P0、P0-M、P1。这不表示每个站日都有三阶段，未来日不应有 P1，也不将 decision 窗口冒充真实提交窗口。

| 检查 | 新记录数 | 结果 |
|---|---:|---|
| P0 的期限不超过正权重系统最新 receipt + 6 小时 | 125 | 超期 0 |
| P0-M 有明确 P0 父节点 | 113 | 缺失 0 |
| P0-M 不超过 P0 根的期限 | 113 | 超期 0 |
| P0-M 不超过对应 TAF valid_until | 113 | 超期 0 |
| P1 不再复制旧概率续期 | 389 | reaffirmation 0；状态重算 166 |

代码也已确认：`_shift_window` 被 `_p0m_window_until` 替代；无校准分支用 exact root 判断是否需要重挂；重挂配额由每轮 4 个增加为有界全域 180 个；旧 TAF 重挂选择最新仍有效的已确认文本；P0-M→P1 的同轮阶段时钟有传递；refresh 拒绝有回执，复制续期分支已删除。P0 排除了零权重及未入组合系统对 receipt 的刷新。

**这 113 份 P0-M 全部是 `p0m_taf_observed_v4`。** 它们验证了无校准 artifact 的生产路径，不能用来证明真正的 TAF 校准分支也已修好。短窗口记录是运行验收，不能证明 CRPS、中心、宽度或市场反应能力已经改善，也不是晋级 OOS。

最终独立 SQL 复核同一 decision 窗口得到 P0=125、P0-M=113、P1=403：相比已保存样本新增 14 份 P1，全部也没有 reaffirmation。新出现的行把 `created_at` 同样写为旧的 decision 时间，例如 EDDM `08:52:42.508Z`。因此，**回放的逻辑时刻仍不能证明记录当时已经实际物化**；应保留独立的真实 `materialized_at/committed_at`，strict-as-of 消费同时检查它。本轮保留首次样本和复核差异，没有用后来的 403 行冒充首次快照。见 `final_verification.json`。

**优先级 1：真正的 TAF 校准分支没有采用新的完整重根流程。**

位置：[production_worker.py:14784](/Users/xiong/weather/bayes_basket_target/production_worker.py:14784)、[production_worker.py:14955](/Users/xiong/weather/bayes_basket_target/production_worker.py:14955)。

无 artifact 分支已经消费 `replay_target`，使用 `_p0m_replay_decision_at`。校准分支仍对经信息时序过滤后的 `captures` 调用 `validate_forecast_revision_chain`，仍用旧的 `previous/received + 1 秒` 决策时钟，并在 `decision_at > as_of_utc` 时跳过。两个后果都已复现：

| 同一类场景 | 无校准分支 | 匹配校准 artifact 的分支 |
|---|---|---|
| 新 P0 在 12:30:00 生成，同轮重挂 | 已绑定新根 | 仍绑定旧根；12:31 下一轮才追上 |
| 已确认 TAF 早于新模型 run | 正常挂到第三个根 | `forecast_revision_chain_empty`，仍停在第二个根 |

第一项是实际 worker、真实校准 proof/materialization、内存 SSOT 的完整调用，无需模拟校准器。第二项在相同已准入 fixture 上，仅注入“新 run 的初始化时间”，隔离信息时序门；不是生产事故计数。

**修法：先统一选择根、有效 revision 和决策时钟，再选择校准算子。** 对完整的已知 TAF revision 链验证身份与替代关系，然后解析该时点应生效的文本；两条分支共同消费同一个 `replay_target`。已有 TAF 随新根重新计算时，保留原始 issued/received 时间，计算时刻使用当前轮次，绝不能用过滤后为空的链否定已存在的确认。两条分支均输出新的 stage clock 给 P1；已知修订覆盖旧文本，不能累计当作多份独立证据。

**优先级 1：编译端要求 exact root，消费端仍允许旧根通过。**

位置：[production_worker.py:15831](/Users/xiong/weather/bayes_basket_target/production_worker.py:15831)、[production_worker.py:15874](/Users/xiong/weather/bayes_basket_target/production_worker.py:15874)。

`_same_p0_forecast` 仍只比较桶概率。`_replay_root_mismatch_reason` 仍只检查 P0 祖先；遇到 P1 后立即返回，不检查其直接 `prediction_root` 是否为最新兼容 P0-M。

隔离消费闸门的两个边界复现均返回 `None`（没有 replay blocker）：

- 新 P0-M 已改变分布，旧 P1 仍绑定旧 P0-M，但两者 P0 祖先相同。
- P0 桶概率不变、未来温度曲线改变；编译器已选择新 P0，消费闸门仍认为旧 P1 的根足够一致。

因此，下游重算失败或暂未完成时，**根一致性闸门本身仍不会阻止这类旧分支**。这不等于已经证明某筆旧概率订单实际成交；其他交易门仍可阻断。本轮历史基线中存在 444 次“P0 桶概率相同而曲线 hash 不同”的相邻转换，说明不能把桶概率相同当作所有下游输入相同。

**修法：让编译和消费共用同一个目标根解析器。** 同时检查 P0 祖先的 checkpoint/content 与 P1 直接 prediction root 的 stage/checkpoint/content。如果希望保留旧完整链的短暂可用性，应显式记录待切换 generation、允许的期限与阻断规则；不能以 `probabilities == probabilities` 暗中豁免。只检查内容真实性的 `assert_formal_probability_model_authority` 并不替代最新根一致性检查。

**优先级 1（严格回放）：原生编译器仍有绕过统一 TAF 时序的入口。**

位置：[native_ensemble_p0.py:933](/Users/xiong/weather/bayes_basket_target/native_ensemble_p0.py:933)、[native_ensemble_p0.py:891](/Users/xiong/weather/bayes_basket_target/native_ensemble_p0.py:891)。

`_taf_envelope_witness` 只筛 `issued_at_utc <= decision`，没有同等检查 `received_at_utc`、`provider_available_at_utc` 和文本的有效/替代状态。本地构造“06:00 签发、10:00 收到、07:00 到期”的记录，在 08:00 查询仍能得到 witness。它直接影响严格 as-of 回放；常驻 TAF 路径的 receipt 门不能保护这个独立入口。

另一处未统一的语义是 `_stage_for_native_compile`：相同 run 下新 TAF 到达返回 P0-M，下一次用相同证据重编又返回 P0。原生编译器可以直接生成 `p0m-native-*`，没有 resident 路径同样的 P0 父节点绑定；P1 选根及 formal authority 却需要这个绑定。这是仍存在的潜在结构冲突，**本轮生产样本未观察到这类 native P0-M**。

本轮改前数据中有 536 份 P0 带 TAF witness，按已捕获的 receipt/validity 复核，没有发现超前 receipt 或过期 witness。因此，这一项是确定可复现的入口缺门，不报告为已发生的线上泄漏。

**修法：TAF 使用唯一 canonical resolver。** 明确区分实际收到、签发、目标预测区间和替代状态；回放先按真实可知时间截断，再解读当时的有效文本。P0/P0-M 标签由固定的数据依赖合同决定，另用 `update_reason` 表示 NWP/TAF/时钟触发，不能把触发原因当阶段。若继续让 native 编译器生成 P0-M，也必须经过同一 P0 根及 revision/proof 绑定，不留第二种 P0-M 语义。

**接下来应明确的两个策略边界**

1. P0 目前取正权重系统 receipt 的 `max`。它已消除“完全未入组合的系统给概率续命”，但一个正权重系统的更新仍可遮住另一个正权重系统的老化。复现中 A 的 receipt 已有 32.5 小时、B 只有 0.5 小时，探针仍返回非 stale。应分别定义每个系统的证据资格期限、run 年龄、首次到达及最近成功复核时间；刷新复核时钟不等于获得新模型信息。是否允许部分旧系统继续参混，应由验证过的退化政策决定，不能仅凭 `max` 得出“所有输入都新鲜”。这是未明确的策略边界，不将其直接列为本次新增回归。
2. P1 新状态刷新只由最新 FORMAL 报文变化或原一小时 cadence 到期触发。在尚未到期的 08:20 加入 08:10 的新 SOFT_PATH 降温点，仍生成 0 份 packet。对于确实进入温度路径的 ASOS/MADIS、区域路径或 artifact 更新，应该比较完整证据 frontier，再用资源有界的事件合并控制频率。概率可以更新，交易仍遵循新高触发规则。P1 含新实况信息，不能机械要求它与 P0 同时过期；需要单独的观测年龄、缺测退化和预测覆盖资格。

**测试证据及其局限**

- 四个专项文件：61 通过。
- 扩大到 worker、bridge、materialization、ASOS 替补路径：185 通过，11 失败。
- 提取改前版本到独立临时目录，复跑相同扩大范围：177 通过，16 失败。当前的 11 项全部已在基线失败，新增失败 0；5 个旧失败已消失。基线目录随后清除，结果及版本信息保留。
- 边界复现使用当前实际函数及内存/临时数据库，不写生产 SSOT，不请求外部网络。覆盖同轮/下一轮、校准/无校准、迟到 TAF、变化的曲线、仅软路径新证据、拒绝刷新等。
- 检查发现测试 fixture 的覆盖缺口：`_bridged_taf_artifact` 从 active registry 取第一个 member，而当前第一个站为 FACT；fixture 改了时区、lead、cycle，却没有改 station_id。RKPK 测试因此可能实际走 `OBSERVED_NO_CALIBRATED_ARTIFACT`。本次独立复现显式设置匹配 station，并断言 `taf_revision_artifact_id` 存在后才检查校准分支。
- 改前的“45 站、90 站日”是运行记录审计，其中快照时仅 76 个站日已闭日。没有将这批记录或优化后短窗口认作 OOS。decision/created 常被赋予同一逻辑时钟，不能用二者之差宣称真实处理延迟为零。

下一步验收应要求：两个 TAF 分支全部通过同一重根矩阵；任一直接父根变化后旧 P1 不能被误判为已追上；同一输入反复重编不改变阶段语义；严格回放不能读取未到达的 TAF；缺证据/修订/跨当地午夜都遵守唯一合同。完成这些后，再进行 45 站按真实可知时间的闭日回放，检验中心、CRPS、覆盖率、分布宽度和观测到概率更新的真实延迟。

**下一个主线任务：统一两条 P0-M 的重根执行，并同步收紧 P1 消费端直接父根检查。** 这两处补齐前，不建议将本轮优化标记为“三阶段全路径已闭环”。

证据文件均在本目录：`postchange_summary.json`、`postchange_rows.json`、`postchange_queries.json`、`edge_reproductions.py/json`、`test_comparison.json`、三份测试日志。改前审计为 `manifest.json`、`analysis.json`、`envelopes.jsonl`。查询均只读、有索引范围及时间/内存/输出硬上限；第一次全域 distinct 触及查询时限、第一次快照触及输出上限后均停止该次结果使用，改为按站点索引和压缩 prefix 后完整重采，最终 manifest 无失败。
