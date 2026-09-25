# 宪章 199 融合覆盖层：训练目标由「中心 MSE＋CRPS」改为桶 LogLoss

写于计算之前（2026-09-25）。熊先生：「做：宪章 199 融合覆盖层：训练目标还是中心 MSE 加 CRPS，要改成桶 LogLoss。」

## 样本

- **训练行来源**：生产训练库 `data/fusion_blend_training.db` 的快照，45 站，事件日 08-14..09-24，含美国 11 个 ASOS 站。
- **打分行**：每站日一行，取最新真值修订；只取 `probability_source` 为 ENVELOPE 或 PACKET_RECONSTRUCTION 的行，也就是带各家市场梯子概率的已服务日前根。
- **权重**：每个根都按它自己的决策时刻 as-of，用生产 `fusion_blend.overlay_system_weights` 取权重（`mode=blend`）。只用该时刻已结算的训练行。

## 两条臂

两条臂都走同一个生产 overlay：同样的回退规则、同样的 `feasible` 投影、同样的 1e-6 成员下限。

- **现役**（`center_crps_blend_v1`）：生产 `blend_weights`，即中心 MSE（本站 21 天，岭 1）与分层 CRPS（全队同提前量档→本站）各半。
- **LogLoss**（`logloss_hier21`，与 09-24 研究 `fusion_logloss_20260924` 逐项同定义）：`blend_weights` 换成 `logloss_weights`。
  - 目标：最小化 mean −log(Σ_s w_s p_s(y)) ＋ ridge·‖w − prior‖²，p 下限 1e-9。p_s(y) 是该系统在真值梯子格上的概率。
  - 层级：全队（同提前量档，无则全队）向席位加岭 1；本站再向全队解加岭 10/n。
  - 约束：单纯形与族上限。
  - 没有中心那一半。
  - 研究期只在研究进程内替换，生产文件不动。

## 打分

- 权重作用于各家市场梯子概率做线性池（199 这一层的输出；后续各层两臂相同），再过第 224 条的 1e-3 下限，算桶 Brier 与 LogScore。
- 站日等权；三日块自助 2,000 次。

## 判据（宪章 211，模型改动）

- 合并面板上桶 Brier 与 LogScore 都下降，且 95% 区间上界都 < 0，才上线；否则不上线。
- 只报不判：宪章 134 之后（≥09-14）、ASOS 与非 ASOS、两臂权重不同的站日、回执状态分布、点估计。
