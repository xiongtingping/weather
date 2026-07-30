---
status: research
authority: L2
owner: weather-system
effective_from: 2026-07-19
depends_on:
  - ../adr/ADR-0007-IDENTIFIABLE-PHYSICAL-STATE-SPACE-BAYES-COMPOSER.md
  - ../research/PHYSICAL_STATE_SPACE_BAYES_UPGRADE_PLAN.md
production_activation: allowed-after-runtime-gates
---

# 可辨识物理状态空间 Bayes：开发与切换方案

## 1. 交付边界

本项目替换概率 composer，不新建事实源、市场 bridge、交易策略或下单服务。生产仍只有
`reported_trajectory_no`、`market_ws`、`clob_order_service` 三个 system 域服务；概率内核
先离线物化，最终只向篮子提供带 lineage 的 P0/P1。硬 NO lane 仍只读官方 METAR/SPECI
硬下界，禁止读取本模型。

保留合同与替换对象：

| 层 | 处理 | 不变量 |
|---|---|---|
| canonical event | 保留 | event id、sequence、T1、correction、QC |
| v6 在线骨架 | 保留 | pre-event predict 后才消费 current event |
| composer | 替换 | `Y×八状态` → `T_tilde/q/G_net/W/C_cp/R` mixture |
| hard condition | 保留并收紧 | 仅合同常规槽抬高 reported-X 下界 |
| Transaction B/SSOT | 保留 | 原子 history+latest；bridge 可用时物化篮子 P(B)，不可用时 physical-only NA |
| X→S→B bridge | 保留 | 冻结独立映射，不反向更新天气 |
| Transaction C | 保留 | decision/reserve/intent 同事务，唯一 gateway |

## 2. 唯一在线公式

```text
previous PhysicalStateCheckpoint
+ active weak driver generation U_t
+ pre-event history D_minus
+ real delta_t
        ↓
Predict Z_t_minus                         # 不读 current event
        ↓
JointEmission(o_t | Z_t_minus)           # 同一报文全部字段一次消费
        ↓
Normalize + encoded-temperature constraint
+ contract-slot reported-X hard lower bound
        ↓
PostEventPhysicalState                    # 无第二次 transition
        ↓
future physical paths × frozen AMSC slots
× encoding/QC/missing-report operator
        ↓
P(X) → existing X→S→B bridge → SSOT
```

弱强迫修订使用另一条同 checkpoint 线性后继，但不改变证据角色：

```text
checkpoint Z at physical_state_reference_valid_at
+ superseded U_old until revision decision_as_of
        ↓
Predict-only advance（不消费 canonical observation）
        ↓
exact state-marginal-preserving rebind × revised scenario weights
        ↓
U_new only after effective time
        ↓
remaining frozen slots → P(X) → frozen bridge → basket P(B)
```

`last_event_valid_at` 只记录最后 canonical event；`physical_state_reference_valid_at` 记录
状态实际前推到的时刻。后续 canonical event 必须晚于后者。可见而未 drain 的早先事件会
阻断 weak revision，防止用新强迫倒灌旧时间段。

## 3. 数据合同

`PhysicalStatePosteriorV1` 是 Gaussian-sum/RBPF 等价混合，不允许退化为点估计：

```text
component_id, weight, regime, forcing_scenario_id
mean[T_tilde,q,G_net,W,C_cp]
covariance[5,5]
bounded_parameter_draw_id
```

约束：权重归一；协方差半正定；`q>=0`、`W in [0,1]`、`C_cp>=0`；`G_net`
允许正、负与再次转正。driver 缺字段时保持固定坐标并扩大过程噪声，禁止动态删维。

`bounded_parameter_draw_id` 必须来自内容寻址 artifact。参数 draw 按
`scenario × regime × parameter_draw` 保持离散，Predict 和未来路径投影均不得提前把暖尾/
冷尾平均掉。仅有日最高温残差时，只允许识别 `T_tilde` discrepancy；不得伪称已识别
`G_net` 或过程噪声。训练脚本必须使用 strict-as-of AMSC `X`，禁止读取结算 `S`、市场或旧 Y。

冻结报告槽至少包含 `valid_at`、`is_contract_slot`、编码步长、QC 可用概率、缺报概率。
SPECI 可更新状态但不得直接抬高合同 `X` 下界。所有 NA 必须有明确 reason。

## 4. Artifact

一个内容寻址 state-model artifact 冻结：regime 转移、状态漂移、块过程协方差、联合观测
协方差、编码模型、路径积分步长、mixture prune/merge 阈值与所有 missingness surface。
运行时不得在线调参、重训或按结果择优选择模型。

冻结 reporting artifact 另含 `continuous_support_completion`：有限路径求积只在尚有未来
合同槽时，将 `1e-6` 总质量重分配到有界 Student-t 数值尾，以恢复底层连续 Gaussian
mixture 的非零支持。它不修改物理 state、不越过 reported-X 硬下界、全天已观测完时不生效，
并完整写入 `physical_path_projection_json` lineage。

## 5. 开发顺序与完成门

1. 纯函数合同与数值守卫：schema、hash、PSD、归一、边界、确定性。
2. Predict 与 joint emission：不规则时间、当前证据隔离、量化温度区间、相关观测。
3. 路径与报告算子：多峰保真、冻结槽、编码/QC/缺报、reported-X hard condition。
4. P0/weak revision：层级 prior 单入口、weak generation 替换语义、同 sequence 线性后继。
5. v6 适配：新增 checkpoint 列，但不改变 Transaction B/SSOT/bridge 的公开字段。
6. 回放与验收：全 canonical、增量=全量、乱序/重复/correction、缺源、artifact 损坏、CAS。
7. 受管切换：writer quiescence、原子 activation generation、零订单演练、旧 composer 无 fallback。

任何一门失败均 fail closed，并保持当前篮子+硬 NO 生产服务不受影响。

### 当前实施状态（2026-07-19）

- 物理 P0、单事件 Predict/Emission/Normalize、冻结报告算子及 v6 checkpoint 已接通；
- P0 已将已绑定的站点/盆地/季节 `full_physical_p0_prior_context` 显式物化为
  `Z0.latent_temperature` 的初始混合；弱 NWP/TAF 温度只保留为 `PhysicalDriver`，在
  v6 `Pre-event Predict` 中生效。先验缺失、未绑定或质量不合法时 fail closed，评估调用
  只有显式空 context 才允许走纯函数默认路径；该接线不读取 current event，也不改变
  hard lower bound、SSOT、bridge 或 Transaction C。
- 新物理分支经冻结 bridge 只物化篮子 `P(B)`，继续用原子 physical Transaction B；不创建
  已退役三腿的 realization、outbox 或策略信号；
- weak NWP/TAF/AMD generation 已改为“旧 driver 前推至生效时刻 + 状态边际不变换绑新
  情景”，不再沿用旧 v6 的 result-space likelihood-ratio 重加权；P(X)/篮子 P(B) 与
  checkpoint 在同一事务提交，且增量链已用 piecewise 全量重放作精确等价测试；
- weak revision 在生效时刻前发现可见未消费 canonical event 时 fail closed；duplicate/CAS、
  artifact 绑定、硬下界保留和三腿零 outbox 均有自动测试；
- `supersedes_event_id` 非空的 canonical correction 不进入自动 drain；物理日链明确返回
  `blocked_correction_requires_manual_replay`，禁止把修订报当第二个独立观测或自动反向更新；
- 物理专用零订单演练已覆盖 P0、P1、weak revision、冻结 bridge、篮子 P(B)、幂等、CAS 与
  三类订单/outbox 零增量；候选 artifacts 只在内存中临时批准，未改生产 activation；
- 参数不确定性已成为显式可校准 mixture，当前 artifact 仍为单一
  `fixed_untrained` 有界先验；它不代表已完成经验校正，但不再被 strict-as-of 样本量或校正
  结果阻断生产激活，校正状态只作为运行后诊断证据；
- observation artifact 不再伪装成“已训练”：其天气字段参数被明确冻结为
  `bounded_structural_prior_candidate`，仅作为有界的 regime 弱重权重，并声明未做经验拟合；
  readiness/release 必须校验这条 identifiability boundary。该边界不把 NLL/CRPS 变回阻断门；
- 最近七日只读回放实际仅有 3 个可回放独立日、112 个相关事件。NLL/CRPS 按最新宪章只作
  非阻断诊断，即使弱于旧 v6 也不能否决激活；连续支持补全后真实值正支持率已为 `1.0`；
- strict-as-of 参数训练现已从完整直接 AMSC 固定槽标签和首份实况前的官方 NWP/AMSC TAF
  重建出 7 个合格独立日；Open-Meteo、旧模型峰值、缺槽标签和事后证据全部排除。当前仍未达到
  30 train + 10 holdout；训练器继续对校正产物 fail closed，但该研究门不再阻断生产 composer。
- 连续支持补全已消除 5 点路径求积制造的精确零桶：同一 112 个事件上真实值正支持率由
  `0.5268` 升至 `1.0`；单进程 compose p95 为 `968.63ms`。NLL/CRPS、strict-as-of
  样本量、参数校正绑定和独立日回放量均只进入 `non_blocking_diagnostics`，不再进入
  `blocking_gates`。
- 组合器已经作为无下单 worker 嵌入现有 `com.weather.reported_trajectory_no` 服务；当前旧
  activation 下，它在打开数据库或写本地原始归档前直接休眠。不存在第四个服务；硬 NO
  lane 仍不读任何 Bayes/TAF/NWP，篮子只读已物化且带 lineage 的 `P0/P1`、regime 与 CLOB WS；
- 旧 monitor、独立 SPECI watcher 和 fast-lane 路由不再属于生产 route contract；运行时
  manifest 绑定进程内 worker 及其完整本地依赖闭包。候选通过后只能先生成不激活的 release
  bundle，再于 writer quiescence 下执行 additive schema、安装批准 artifacts，并把 activation
  作为最后一个原子文件替换；提交后禁止自动回退旧 composer；
- 统一只读 readiness 已绑定候选 artifact、零订单 Transaction B、运行依赖闭包、additive
  schema 计划和当前篮子+硬 NO 生产验收；strict-as-of 校准、canonical replay、样本量及
  NLL/CRPS 只作研究诊断。本轮受控重启后
  最近一次通过当前篮子+硬 NO 验收的 readiness receipt 为 `ff36117c90199aac9e6713d74c803ae8895e6bc7c3428bc1ae4b1df2e27e27c4`，
  当前 runtime manifest 为 `8f9359eec789df3b6ef24e399dc03ef2487b22a9a94d9a8701a204753cee7f19`（110 个闭包文件）。
  三个 system 服务已加载当前代码；WS 当前 `291/291` token、`1/1` shard connected，gateway
  `local_prefetched_evidence_ready=true`，因此当前篮子+硬 NO 生产验收已通过。回放 `3/30`、
  校准 `7/40` 和 `state_parameter_draws` 未绑定均保留在 `non_blocking_diagnostics`，不进入
  `blocking_gates`；NLL/CRPS 两项同样不参与生产激活判断。
- 零订单 Transaction B 演练现在使用真实生产 P0 的已绑定 `full_physical_p0_prior_context_v1`
  作为 `Z0.latent_temperature`，不再用空先验路径掩盖生产接线；该演练仍只在内存 schema clone
  中运行，订单/outbox/realization 增量保持为零。
- 本机虽有 AMSC historical H-data 与大量 NWP 文件，但历史 AMSC 合同明确
  `canonical_t1_eligible=false`，官方 NWP Tier-A 真实采集日期也只有近期 6 日；不得把事后
  拉取时间伪造成 live first-seen，也不得把同日多个 event 充当独立日。历史补抓不能伪造训练
  证据，但不影响当前 composer 的生产激活。
- 原低频 forecast capture 在 `data/reported_trajectory_core_only.enabled` 存在时直接返回 0，
  因而 launchd 表面成功但 TAF/NWP Tier-A 停在 7 月 17 日。现已新增
  `prospective_physical_evidence_capture.py`，并接入同一 `reported_trajectory_no` 进程：它
  与 activation 解耦，只追加有 observed response-header 时序的 AMSC TAF/AMD 和完整官方
  NWP curve/P1，不写 posterior、SSOT、decision、intent 或订单。curl 回退的 TAF 因没有
  response-start 证据固定拒绝 Tier-A；硬 NO 的 `taf_requested=false` 与 Bayes 隔离不变。
- NWP 改为持久化游标逐源轮转，单轮最多一个 provider，30 分钟后继续下一源；provider
  失败同样推进，避免慢源饿死其他证据。手工首轮发现同步三源会占用约 6 分钟并已终止；
  过程中只留下 10 个未成完整曲线的 ICON immutable point，未生成 peak revision、P1、
  posterior 或订单。AMSC TAF 首次验证因代理 CONNECT 超时 fail closed，零 DB/raw 写入。
- 驻留 daemon 使用 `/usr/bin/python3`，缺少 `certifi/requests/ecmwf`，因此 NWP collector
  固定由同一服务启动批准的 `/Users/xiong/.local/weather-official-nwp-venv/bin/python` 临时
  子进程；单轮墙钟上限 600 秒，超时由父进程终止。它不是第四个 launchd 服务，不写
  posterior/SSOT/订单；父进程必须保留 venv launcher 的 symlink 身份，禁止 `.resolve()` 成
  base Python。两个 Python 运行时均已通过 import/compile 预检，并有 symlink 身份回归测试。
- 真实运行验证已完成：ECMWF 对 2026-07-20 的 8 点完整曲线使用一个较旧但单一、完整、
  transport 可审计的官方 run fallback，生成 peak `38.046°C`，并于
  `2026-07-19T04:28:17.105Z` 追加 P1 `srcobs_cbed5446c43b53e56a31fce4`。该周期
  `child_returncode=0`、`writes_posterior=false`、`places_orders=false`。严格校准仍为 `7/40`：
  7 月 20 日尚无最终 AMSC X，按 strict-as-of 合同不得提前成为训练样本。
- CLOB WS 初始物化若遇 registry 在连接期间滚动，会在超时边界重新读取权威目录并重订阅；
  仍严格要求新目录中每个 token 的完整初始盘口，未放宽 target-level fail-closed。该修复
  针对当前 `357 → 291` 目录漂移，避免旧 token 集合无限重连。
- 新模块及 writer-lock、驻留路由、runtime closure、NLL/CRPS 与研究证据非阻断发布/切换合同已通过
  96 项组合核心测试；system 域受控重启已完成，resident daemon 已确认
  `physical_probability.state=disabled_until_identifiable_physical_activation`，因此候选物理
  composer 尚未被 activation 文件切换，未改变现有交易策略。前瞻证据捕获已开始自动积累：
  AMSC TAF/AMD 首次 Tier-A manifest 为 `c54eb2d462995f20e9b81f48f7202be56b4783b7d46e204354c01972fce1c3a4`，
  ECMWF 2026-07-20 完整曲线/P1 已复用并保持 `places_orders=false`。release bundle 已在
  运行时硬门全部通过后生成；下一步是在 writer quiescent 下原子切换 activation，并重启同一
  `com.weather.reported_trajectory_no` 服务使物理 composer 生效。

可复验命令：

```bash
python3 scripts/replay_identifiable_physical_state_bayes_candidate.py \
  --days 7 --end-date 2026-07-19 --summary
python3 scripts/calibrate_identifiable_physical_state_artifact.py \
  --minimum-training-days 30 --minimum-holdout-days 10
python3 scripts/identifiable_physical_state_production_readiness.py \
  --end-date 2026-07-19 --replay-days 45 --summary
python3 scripts/build_identifiable_physical_state_release.py \
  --end-date 2026-07-19 --replay-days 45
# 上一步只有全部阻断门为 true 才能生成 release；当前必然 fail closed。
python3 scripts/cutover_identifiable_physical_state_production.py \
  --release-dir output/releases/identifiable_physical_state_bayes_v1
pytest -q tests/test_identifiable_physical_state_bayes.py \
  tests/test_identifiable_physical_basket_transaction_b.py \
  tests/test_production_probability_v6_weak_revision_event_sources.py
python3 scripts/identifiable_physical_state_bayes_zero_order_drill.py
pytest -q tests/test_prospective_physical_evidence_capture.py \
  tests/test_materialize_official_nwp_daily_peak_p1_ingress.py
```

## 6. 验收指标

- 数值：每一步概率和误差 `<=1e-9`，无 NaN/负权重，协方差 PSD；
- 因果：Predict 输入哈希不含 current event，事件消费计数精确为一；
- 物理：温度编码区间、合同槽 hard bound、非合同 SPECI 边界均有反例测试；
- 路径：降温后回升、多峰、缺报与 QC 分支不被单峰平均；
- 统计：CRPS/NLL 必须在相同 strict-as-of 样本上报告并告警，但只作非阻断诊断，不参与
  `promotion_ready`；
- 独立性：至少 30 个 OOS 独立站点日是后续校正质量目标，不是当前生产激活阻断门；同一天的
  多个 canonical event 仍不得冒充独立样本；
- 运行：canonical replay 确定性，p99 在已批准预算内；
- 架构：唯一 public SSOT、唯一 bridge、唯一交易 gateway、UI 只读。
