# 原生物理字段：历史可回放性审计

本阶段终点已达到：完成指定留存目录的有界盘点、历史 P0 精确配对、温度字段语义抽检与拒绝性测试。**原生物理候选的效果验证仍未完成，不能宣称共同误差已有解。**

## 已执行结果

窗口为 2026-08-14..09-19。`audit_native_physics.py` 只读原数据库与 `data/runtime/bayes_basket_target_nwp/official_native_capture`，逐文件处理 437 个包、约 1,457.66 MiB；没有网络请求、生产写入或服务动作。结果在 `native_physics_inventory.json`，配对曲线在 `native_temperature_curve_matches.json`。

| 检查 | 结果 | 含义 |
|---|---:|---|
| 冻结 P0 站日 | 1,414 | 沿用已冻结研究样本，不重新选样 |
| P0 lineage 中存在混合温度曲线 | 1,323 | 存在不等于逐成员物理路径合格 |
| 参与系统 scope | 3,482 | 每个站日可有多个系统 |
| 同 run、同 receipt、hash 与时区通过的系统 scope | 2,370 | 只称精确配对，不称正式 OOS 激活 |
| 至少配上一家系统的站日 | 1,220 | 不能把部分覆盖说成全模型覆盖 |
| 所有参与系统都配齐的站日 | 739 | 包含 ASOS，尚未解决其真值语义问题 |
| 盘点包内站点 capture | 16,570 | 其中 15,607 携带系统平均温度曲线 |

候选配对拒绝：run 不同 5,999 次、receipt 不同 75 次、曲线缺失 238 次。这是“包×目标样本”比较次数，不能当作独立坏站日数量。未配齐可能来自此目录未留存所需旧 run 等原因，本审计没有遍历全磁盘，不能断言其他目录不存在原对象。

## 字段×系统证据矩阵

| 系统 | 现有采集器选择 | 路径保留粒度 | 云／短波／雨／风的历史证据 |
|---|---|---|---|
| NOAA GEFS | `TMP:2 m above ground` 字节范围 | 系统平均时点温度；部分原 GRIB 在内容寻址库 | 本轮所查采集路径未采集这些变量 |
| ECMWF IFS PF50 | `param=mx2t3` 字节范围 | 系统平均三小时最高温；部分原 GRIB 在内容寻址库 | 同上 |
| ECCC GEPS | `TMP_TGL_2m` 多成员对象 | 系统平均时点温度与日最高；该采集函数未持久化原始文件 | 同上 |
| DWD ICON EPS | `t_2m` 对象 | 系统平均时点温度与日最高；该采集函数解码后释放原对象 | 同上 |
| ECMWF AIFS ENS | 本目录未建立对应合格包审计路径 | 未验证；不能用 AIFS Single 代替 | 未验证 |

依据：`noaa_gefs_archive.py` 的 `_message_range`、`ecmwf_ifs_archive.py` 的 `_parse_index`、`eccc_geps_archive.py` 的对象命名与 `_download_field`、`dwd_icon_eps_archive.py` 的 `t_2m` 下载／解码循环。源码哈希另存 `native_semantics_evidence.json`，代码今后变化不应改变本次结论的适用版本。

GRIB 实物抽检了两个当前本地缓存对象（valid day 为 09-20，**仅用于语义核对，不进入历史评分**）：GEFS 是 `2t / K / heightAboveGround=2 / instant / start=end=12`；ECMWF 是 `mx2t3 / K / heightAboveGround=2 / max / start=15,end=18`。两者不能把 valid_time 当成同一种瞬时温度坐标。ECMWF 峰值只定位到一个三小时时段，不能声称精确峰值时刻。

当前 `family_temperature_curve` 的点只有 `temperature_c` 与 `valid_time_utc`；研究使用必须额外绑定原字段 `stepType/startStep/endStep`。系统平均曲线不能恢复成员峰值时序：`max_t mean_m T(m,t)` 一般不等于 `mean_m max_t T(m,t)`。因此本轮导出明确标为 `SCOPE_MATCHED_NOT_OOS_CERTIFIED`，不作为已完成的逐成员过程输入。

## 为什么不能立即回填物理候选

1. 温度 range 的 hash 或 `.idx` hash 只证明该温度对象／索引；不能证明未下载的云雨风字段在历史决策前已被本机收到。GEFS 留存的 `index_line` 也只描述温度行，ECMWF receipt 保留索引行 hash 而非完整其他变量内容。
2. `calibration_station_forecast_physics` 是 Open-Meteo reference 数据，历史 run 与版本化 arrival 不足；不能改名加入 native 输入。
3. 当前公共归档可下载性、HTTP Last-Modified、模型 run_init 与本机首次接收是不同证据。若以后做公共可用性反事实回放，必须单列，不与实际历史 receipt 混合。
4. 09-12..09-19 已用于候选结果查看。基于这些结果新增的过程特征只能在这段做开发／回归检查，不能再次把它称作未见外层留出。

## 验证与边界

新增 13 项拒绝性检查通过：错误 run、站点、receipt、未来接收、字段回执 hash、回执内容篡改、重复时间、本地日边界、未签名变化、republisher 借用 native 标识、伪造源地址、曲线倒填到达，以及“未来 valid time 是合法预报”的正向反例。原 15 项概率实验测试也已重新通过，合计 28 项。

审计匹配了 capture 与曲线内容哈希、receipt set 和冻结 P0 声明的 run/arrival；**没有重新认证历史参数训练链、真实采集完成时钟、每个原始 GRIB 解码或 exact activation**。旧包中 `archive_query_at_utc` 的含义仍须与首次完整接收证据核对后才能给新输入晋级。未用文件 mtime 冒充历史到达。

已完成：可运行盘点、配对导出、字段语义证据、反例测试。仍阻断：原生非温度字段的严格历史接收链、逐成员温度路径恢复、ASOS 标签语义、新的独立留出。

**下一主线**：按 `SYSTEM_DESIGN.md` 与 `NATIVE_PHYSICS_CONTRACT.md` 先完成逐字段、逐成员、逐时段的输入合同及历史证据链；再做条件分布比较。当前无新采集任务、无生产变更，也没有重新训练已看过外层的 TAF 候选。
