# P1 尾段线回放（2026-09-21）

用户问「q = 归一化(归一化(市场中价×活桶)×L) 是测试出来的吗」→「测试」。

- 样本/根/真值桶：`scripts/fit_p1_observation_likelihood.py` 的 `load_samples`/`RootCache`/`scorable_rows`（09-05→09-20，45 站）。
- L：生产 `p1_direct_update.compile_direct_update`（S＝在役，根特征取当时的根；M＝同一函数，根概率换成市场中价）、`p1_observation_likelihood.evidence_by_bucket`（g1＝g，gα＝g^α）；mask＝L≡1。
- 规则与执行：车道的 `evaluate_rule`/`taker_take`（5U、走一跳、深度余量、门槛对走后价），每桶一次、站日≤2 笔。`EXEC=ask` 为研究口径（1 股按卖一、门槛对卖一）。
- 档位：`BIN=clock`＝车道现读的回执 `clock_hours`（剩余小时＋站点峰时偏移，宪章 157）；`BIN=raw`＝剩余小时。
- 盘口：报文 as_of 后 ≥1 分钟与 ≥8 分钟的第一张 COMMITTED 快照，结果按实际延迟分档。
- 限制：拟合件训练到 09-20，全窗口对 f 与 g 都是样本内；16 天；按天自助。

运行：`BIN=clock EXEC=lane python3 tail_L_replay.py 2026-09-05 2026-09-20 out.jsonl && python3 tail_L_summary.py out.jsonl`
