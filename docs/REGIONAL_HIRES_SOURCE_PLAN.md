---
status: research
authority: L3
owner: weather-system
effective_from: 2026-08-27
---

# 高分辨率区域模型接入方案（针对 RCSS 类高偏差站点）

2026-08-27 设计。动机：站级系统性失败诊断（看板第十一节）判定 RCSS/RPLL 五项全灭、
SBGR/WMKK 四项，且 RCSS/WMKK 恰是偏差层整月修不好的两个站——`bias_c` 已给出
+5.75/+2.87°C 的修正，修正后残差仍剩 +1.4°C。同日的系统性收口结论是：
**服务中心的事后修正四种形态全部实测否决，中心还想变准只能靠更好的证据，
不能靠重新加权已有的东西。** 本方案就是"更好的证据"。

## 病理与对症（为什么是区域模型）

松山机场（RCSS）在台北盆地底，场址海拔约 5 米，四周是 500–1000 米的山。
全球模型格点 9–25 公里，一个格点把半座山平均进来，2 米温度**结构性偏冷**——
这不是模型"错"，是分辨率看不见盆地。`bias_c` 能加常数，加不出
"今天东风闷热、明天锋面通透"的逐日起伏，这正是残差里那 81% 修不掉的部分的
站点局部版本。

**已核实（2026-08-27）**：JMA MSM（日本气象厅 5km 区域模型）
- 覆盖 RCSS：Open-Meteo 返回格点 25.05N/121.5625E，**格点海拔 4.0 米**——看得见盆地本身
- 实时可用（`models=jma_msm`），历史归档 ≥60 天（训练窗够）
- **按 lead 分开的历史预报可取**（previous-runs API 的 `temperature_2m_previous_day1/2`）——
  lead 桶词汇可以喂对
- 域内舰队站：**RCSS、RJTT、RKSI、RKPK、ZSPD、ZSQD** 六站（ZBAA 116.6E 在西界外）。
  其中 RKPK 也是 3/5 失败站（中心旗 +1.26°C）
- 欧洲区域模型同 API 可用（已验 AROME-HD @LFPB，格点海拔 54m）——
  LFPB/EDDM/EHAM/EPWA/EGLC 这批失败站是第二期的现成延伸

**诚实的边界**：WMKK、RPLL、SBGR **没有任何区域模型覆盖**（东南亚/南美无高分辨率
公开源）。本方案救不了它们；WMKK 按 08-25 核查是真热浪（ERA5 距平 +3.7），
病理不同，另案处理。

## 接入形态：单成员点预报系统（全部走既有闸门，零概率层改动）

系统里已有两条完整先例：
1. **`POINT_FORECAST_SYSTEMS` 注册表**（`official_native_forward_packet.py:61`）——
   TAF TX 与 METAR 路径都以 `expected_member_count: 1` 注册，独立 family，
   准入走 5 训练日+3 留出的点预报门槛
2. **AIFS 确定性系列**（`aifs_deterministic_*.py`）——一条确定性 NWP 从证据到
   P0 角色的完整集成

MSM 照此注册：
- `ensemble_system_id: JMA_MSM_POINT`，`independence_family_id: JMA_MSM`
  （独立 family——权重机制先族内聚合再跨族封顶，单列才能不稀释别族的封顶）
- 单成员无 spread，诚实宽度取训练残差（`native_ensemble_p0` 的
  single_member_bandwidth 已处理，TAF 先例）
- **准入不开新门**：OOS 三门（CRPS/Brier/NLL）+ Brier 容差通道、
  sqrt(n/28) 权重爬坡、席位赛出（宪章：2026-08-15 起 P0 席位一律赛出）、
  权重站本地（2026-08-14 裁定）——**MSM 只在它赢的站上拿权重，
  域外的站结构上碰不到，自限性是白送的**

## 分阶段（先证据后管道，每阶段带否决线）

### Phase 0：离线证据探针（无生产改动，tmp/ 脚本，约 1–2 小时）
用 Open-Meteo previous-runs 拉 60–90 天 MSM 的 day1/day2 预报，按站本地日窗口取
日最高，对 `calibration_station_daily_high_truth` 算残差；对照每个既有系统
**修正后**的残差（数据在 `calibration_native_ensemble_system_artifact` +
member_run）。六个域内站全测。
**否决线**：RCSS 上 MSM 的残差散度（去常数偏置后的 std）若不优于现役最好系统的
0.9 倍，整个方案就地停止，一行生产代码都不写。
**顺手产出**：欧洲失败站用 AROME/ICON-D2/KNMI/UKV 跑同款探针，定第二期的去留。

### Phase 1：历史 OOS 自举（1–2 天）
照 `build_rkpk_open_meteo_five_model_historical_oos.py` 先例，从 Open-Meteo 历史
归档构建带 `HISTORICAL_ASOF_REPUBLISHER_RECEIPT` 的训练事实与严格 OOS 留出
（宪章约束：只用已闭日、AWC 真值在 as-of 可见）。产出：MSM 六站的
`bias_c/spread_factor/reliability` artifact 候选 + OOS 成绩单。
**否决线**：OOS 三门在 RCSS 过不了就停在证据层，不建采集。

### Phase 2：前向采集道（重的部分，几天）
两条路，**默认走 (a)，(b) 需要你裁决**：
- **(a) 原生 JMA 采集**：JMA GRIB2（京都 RISH 镜像/JMA 开放数据）新增
  collector + `SUPPORTED_ARCHIVE_SCHEMAS` 条目。按 run 采集（原生采集以 run 为
  单位），只采 00Z/12Z 两个 cycle（对齐舰队 6 小时 ladder，少 scope、每 scope
  样本密）。体量：1 成员 × 2 cycle × 3 lead × 6 站 ≈ 每天几十行，SSOT 无感
  （仍需过 2GB 余量核对）。
- **(b) Open-Meteo 直接服务前向**：省掉采集器，但现行契约里 Open-Meteo 是
  republisher 血统，只准闭日回退不准实时服务 P0——放行等于改宪章，
  需要你明示裁决，我不默认。
风险登记：ICON 判例在案（区域采集道脆、不可回填、丢一天永久丢）——
MSM 归档在 Open-Meteo 有 60 天深度，**可回填**，比 ICON 处境好；
留档 sweep（`forward_packet_retention`）会连它的包一起留。

### Phase 3：准入与判决（自动 + 账本闭环）
- 前向积累 5/3 起评，席位赛出，落选就继续影子积累——**这一段没有人工开关**
- 终审走刚建成的账本闭环：包留档 → `artifact_parameter_override` 重编译 →
  小库 → 账本两臂。**残差必要非充分**（舰队层 −24% 残差 → 账本 −7.3 点的判例
  就在三天前），MSM 赢了席位也要在账本上验过才算数
- 成功判据：看板"哪些站是系统性地不行"里 RCSS/RKPK 的**中心旗熄灭**
  （残差 |·| ≤0.8°C）、声称比回升；账本闭环不劣化
- 失败判据：坐了席位 30 天，RCSS 中心残差没有实质收敛 → 按赛出机制自然退出，
  不需要拆除

## 层级定位

- **改哪一层**：证据层新增源（概率层最上游的输入端）。**概率层算术零改动、
  选桶零改动、执行零改动、准入闸零改动**——全部复用，这是方案最大的卖点
- **scope 粒度**：系统×站×季节×lead×cycle，站集 = MSM 域内六站
- **是否穿透下游**：穿透且全自动——中心变准 → 声称变准 → Kelly 定注变准 →
  账本改善，无需任何下游配合
- **各层影响面**：域外 39 站结构上不受影响（无 scope 即无权重）；
  域内六站也只在 MSM 赛赢时才动

## 已知陷阱预登记（来自记忆的判例）

1. cycle 决定 lead 桶——采错 cycle 永远配不到队友；只采 00Z/12Z 且从
   run_init_utc 推导 lead
2. 词汇：季节用站本地（宪章 62 自动）；六站全是 °C 站，°F 陷阱整个绕开
3. 测生产参数就调生产的 loader（本会话两次取样错误的教训）——Phase 1 复用
   RKPK 脚本的取数路径，不自建
4. SSOT 2GB 余量——任何新表写入前先按字节估
5. 模型 2m Tmax vs 官方结算 Tmax 的口径差由 bias_c 吸收（所有现役系统同款），
   Phase 0 评的是散度与逐日追踪力，不是原始偏置
