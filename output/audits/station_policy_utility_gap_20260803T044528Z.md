# Station policy utility gap — 2026-08-03T04:45:28Z

## 结论

`BLOCKED_NO_SAFE_UTILITY_ROWS`。没有生成 utility score，未写生产库、未调用 gateway、未提交订单。

## 已核验的本地证据

| 证据面 | 数量 | 可作为已实现 utility |
| --- | ---: | --- |
| station-policy facts | 1,500 | 否；全部为 forward/observational 或非 strict 历史事实 |
| 已冻结、已接受的 AWC 标签 | 865 | 否；缺与历史 policy fact/执行回执的原子绑定 |
| 官方 CLOB WS 快照 | 4,707 | 否；仅市场输入，不能代替成交 |
| order intents | 685 | 否；intent 不是 fill 证据 |
| order events | 0 | 否 |
| execution receipts | 0 | 否 |
| authoritative non-simulated fills | 0 | 否 |
| 直接 fact→receipt 绑定 | 0 | 否 |

历史 utility inventory 结果：P0 `0`、P0-M `0`、P1 `0` 条 `HISTORICAL_STRICT_OOS` fact；所有可见闭日 forward facts 均保留为 `observational_candidate_not_oos`，不计 OOS。三个阶段 × target/reserve/cadence 的 utility score rows 均为 `0`。

## 根因与最小补法

根因不是天气、模型或盘口缺失，而是从未保存一条可闭环的真实执行链：严格历史 policy candidate/baseline → 决策时官方 WS → 非模拟、权威 YES fill（或权威显式零成交取消）receipt → 闭日 AWC settlement label。缺失 receipt 绝不能推断成 zero-fill。

最小下一步：仅在已有真实历史 receipt 可被发现时，按上述同一站点 × 事件日 × 阶段 × family 原子绑定 1 条记录，然后运行 `validate_historical_station_policy_utility_binding`；在此之前保持 `UNBOUND`、0 score、不可晋级。

## 可复核工件

- `station_policy_exact_stage_scorecard_45station_utility_recheck_20260803T044528Z.json`，内嵌 canonical `content_sha256`：`d8f5610b0f285debd2159632fdf6665a0ae14a99ca166a6ebbc3efd266000cc3`
- `delta_v2_strategy_utility_closure_audit_20260803T044528Z.json`，内嵌 canonical `content_sha256`：`2c98ad9bc85c6ec4411528275cca21aa0c6a9d8120d30a08754135a50c0a42a7`
