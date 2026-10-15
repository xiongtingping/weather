# 前推运行手册（冻结主臂，约 2026-10-29 复读）

熊先生 10-15「冻结主臂（增量+市场倾斜、κ=ln1.5、保众数）作兑现层 b 的前推候选臂，10-07 之后事件日只读，约 10-29 复读」。

- 冻结件：`FROZEN_ARM.json`（0ea35e4c，09-01..10-06 拟合；系数、标准化、κ、静默门、各站辐射源都钉死）。之后**不改**。
- 前推窗：事件日 2026-10-07 起，到复读当天已闭日为止（10-29 复读即 10-07..10-28，22 个事件日）。
- 干跑（10-16）：ZBAA/EHAM/KDAL × 10-07..10-09 取数与建行通过，115 行（dSW 可得 57%，与训练 60% 一致），**未评分**；`score` 在 10-29 前拒跑。

## 10-29 复读步骤

```bash
cd docs/research/sat_cloud_obs_20261008 && ../../../.venv-sat/bin/python goes_extract.py --start 2026-10-07 --end 2026-10-29 --products ABI-L2-DSRF --max-minutes 9
```
（GOES 每天约 3 分钟、按 --max-minutes 分批，重复跑直到无新日；跑前看 WS manifest 年龄与空闲内存，10-08 曾因整机换页卡生产 257 秒。）

```bash
cd docs/research/obs_minus_forecast_station_direction_20261015 && python3 forward.py fetch --from 2026-10-07 --to 2026-10-28
```
（Open-Meteo 记生产账本 lane=research：48 站 previous_day1 + 32 站卫星转发 ≈ 80 次，日额 500 以内。）

```bash
python3 forward.py build --from 2026-10-07 --to 2026-10-28 && python3 forward.py score
```

## 判决（与冻结时登记的一致）

宪章 240：ΔLogScore 主、ΔCRPS、ΔBrier，站日等权、按日 bootstrap；按站 BH q=0.10（≥10 站日）。对照臂 = 只用市场倾斜。
主臂三项同向显著且对照臂不更好 → 作为兑现层 b 的候选形态提交裁决；跨零 → 结案。P1 版本此段为 v38/v39（不再跨版本）。
