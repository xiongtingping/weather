# Refresh Coordinator

本系统的刷新任务必须先经过 `scripts/lib/weather_refresh_coordinator.sh` 仲裁，避免 live SSOT、AWC、模型扫描、市场刷新、数据源刷新和 facts materialize 同时争写 `data/archive.db`。

## 优先级

1. `hard_source_direct_poller` / AWC live SSOT 写入：最高优先级，持有 `data/live_ssot_writer.lock`；若统一 lane / DB writer 被非关键刷新占用，可抢占登记 owner。
2. auto-trade 下单 worker：不被非关键刷新阻塞，`auto_worker`/`auto_trade_worker`/`auto_trade_job_worker` owner 不被 live critical 保护误拦。
3. `market_refresh_*`：只更新市场盘口/快照，默认进入统一 `weather_refresh_lane`，写库前拿 `data/db_writer.lock`。
4. `model_scan*`：只更新模型/概率快照，默认进入统一 `weather_refresh_lane`，写库前拿 `data/db_writer.lock`。
5. facts materialize：默认由 live SSOT/fact 主链路负责；扫描后的 facts materialize 受 coordinator 开关控制。
6. 数据源刷新：weather discovery、AWC fact、precip nowcast 如果写库，必须先进入统一 lane，再拿 `data/db_writer.lock`；live 窗口内让路。
7. `market_ws_listener`：流式盘口源不占统一 lane，但必须在 `live_ssot_writer.lock` 存在时延后 flush，并使用短 SQLite busy 等待，避免阻塞 AWC/live SSOT 写入。
8. `daily_scan`：只在 refresh/scan/archive 写库段短持统一 lane + DB writer；写完立即释放，后续 settlement 复盘/校验不占刷新通道。
9. model release watcher：只读探测或写独立 JSON，不写 facts，不直接踢扫描，live 窗口内让路。
10. UI 重型只读接口：不写库、不占刷新 lane，但必须走 UI heavy lane，忙时返回旧缓存/busy，不允许并发重建 SSOT summary 把 heartbeat、live 监控和 SQLite 读写拖住。

## 硬规则

- 非 live 刷新在 `live_ssot_writer.lock`、AWC release watch 窗口、近期 hard-source DB lock 迹象出现时直接跳过。
- 非 live 重刷新默认共用 `PM_REFRESH_COORDINATOR_LANE_NAME=weather_refresh_lane`；`PM_MODEL_SCAN_LANE_NAME`、`PM_MARKET_REFRESH_LANE_NAME`、`PM_DAILY_SCAN_LANE_NAME` 也必须指向同一 lane，禁止新增独立 lane 绕开 coordinator。
- 非 live 任务等待统一 lane 期间必须持续复检 live/AWC critical；一旦进入 live 关键窗口，等待方立即退出。
- 统一 lane 开启后，`model_scan` 与 `market_refresh` 的旧式互相 pgrep/yield 保护默认关闭，冲突只由 coordinator 的 lane/db_writer 协议裁决。
- 旧 peer guard 必须显式 neutralize：`PM_MODEL_SCAN_IGNORE_MARKET_REFRESH_ACTIVE=1`、`PM_MODEL_SCAN_YIELD_TO_MARKET_REFRESH_WHEN_TICK_STALE=0`、`PM_MARKET_REFRESH_IGNORE_MODEL_SCAN_ACTIVE=1`、`PM_MARKET_REFRESH_YIELD_TO_MODEL_SCAN_ACTIVE=0`。
- `market_refresh` 和 `model_scan` 不再互相手工 kick；新市场信号只记录，扫描节奏由 launchd/coordinator 管。
- `market_refresh` 内部的 weather discovery 在启动前复检 live critical，并先进入统一 lane，避免脚本启动后新出现的 AWC/SSOT 写入窗口被 discovery 抢占。
- `market_refresh` 内部的 weather discovery 会拿 DB writer；若 market_refresh 已经持有 DB writer，则只复用当前锁。
- `model_scan` 内置 AWC/precip nowcast/facts 兜底刷新默认关闭；即使打开，也必须先过统一 lane，再拿 DB writer，拿不到就跳过。
- AWC fact 刷新默认只允许 live/hard-source 主链路执行；`PM_REFRESH_COORDINATOR_ALLOW_NONLIVE_AWC_REFRESH=0` 时，`model_scan` 内置 AWC 兜底即使被误开也会被 coordinator 拦截。
- facts materialize 默认只允许 `PM_REFRESH_COORDINATOR_FACTS_WRITE_OWNERS=weatherctl_facts,daily_scan` 这类数据源入口；`model_scan`/`market_refresh` scan 后顺手 materialize 必须额外设置 `PM_REFRESH_COORDINATOR_ALLOW_SCAN_FACTS_MATERIALIZE=1` 才放行。
- `market_refresh_frequent`、`market_refresh_urgent`、`model_scan_hourly`、`daily_scan` 使用统一 lane lock；常规刷新不再使用旧全局 `scan_global.lock`。
- 手工 `weatherctl facts` 也必须走统一 lane + DB writer，禁止绕开 live/AWC 保护直接 materialize。
- WebSocket 盘口流只允许短事务写 `event_market_ticks`；默认 `PM_MARKET_WS_DEFER_DURING_LIVE_SSOT=1`、`PM_MARKET_WS_DB_BUSY_TIMEOUT_MS<=500`、`PM_MARKET_WS_WRITE_RETRY_ATTEMPTS<=2`。
- live SSOT/AWC 抢占只允许作用在明确列入 `PM_HARD_SOURCE_PREEMPT_NONCRITICAL_OWNERS` 的非关键刷新 owner；auto-trade worker 和 hard-source 自己不在抢占名单内。
- `daily_scan` 属于可抢占非关键刷新 owner；真实 AWC/live SSOT 或真实下单积压可以打断 daily 写库段。
- UI/auto-trade 读取仍以事实层 SSOT 为准，前端不得复算第二口径。
- UI summary 不得实时叠加 market hydrate；盘口由 `market_ws_listener`/`market_refresh` 统一写入 SSOT 后读取，避免 UI 读侧和刷新写侧互相放大 DB 压力。
- UI 重型端点 `/api/ui_snapshot*`、`/api/most-likely-history`、`/api/latest`、`/api/services*`、`/api/auto-trade-config`、`/api/auto-trade-health*`、`/api/auto-trade-balance`、`/api/auto-trade-trace`、`/api/chengdu-intraday-summary`、`/api/chengdu-anomaly-alerts` 必须受 `PM_UI_HEAVY_MAX_CONCURRENT_REQUESTS` 限流；`/api/heartbeat` 必须保持快速路径。
- `/api/most-likely-history` 只能读取最近有限条 `event_fact_summary_cache`/`event_fact_history` 快照，并设置 SQLite deadline；禁止为了前端曲线全量扫描整日 `fact_json`，否则会抢占 Python GIL 并拖慢 heartbeat/live 监控。

## 统一入口契约

所有刷新入口只声明 owner 和任务类型，不自建优先级；当前角色契约为 `PM_REFRESH_COORDINATOR_ROLE_CONTRACT_VERSION=refresh_coordinator_v3`：

1. 入口先调用 `weather_refresh_skip_if_live_ssot <owner>`；live/AWC 关键窗口直接退出。
2. 需要长任务互斥时调用 `weather_scan_lane_acquire <owner>`；等待期间由 lane 函数持续复检 live/AWC。
3. 只有实际写 `data/archive.db` 的阶段调用 `weather_refresh_db_writer_acquire <owner>`；只读探测或独立 JSON 输出不得占 DB writer。
4. AWC/live SSOT 是最高优先级，可以抢占 `PM_HARD_SOURCE_PREEMPT_NONCRITICAL_OWNERS` 中登记的非关键 owner。
5. facts materialize 不再由多个刷新脚本默认顺手执行；默认由数据源入口负责，扫描后的 materialize 需要同时打开 `PM_FACTS_MATERIALIZE_AFTER_SCAN=1` 和 `PM_REFRESH_COORDINATOR_ALLOW_SCAN_FACTS_MATERIALIZE=1`。
6. AWC fact 刷新默认归 live/hard-source 主链路；非 live AWC 兜底需要显式打开 `PM_REFRESH_COORDINATOR_ALLOW_NONLIVE_AWC_REFRESH=1`，且仍要通过 live critical、统一 lane 和 DB writer。

`scripts/lib/weather_refresh_coordinator.sh` 在被 source 时会注入安全默认值：统一 lane 开、live protect 开、旧 peer guard 关、scan 后 facts materialize 关、scan facts 写入二次放行关、非 live AWC refresh 关、weather discovery 不踢 `model_scan`、`model_scan` 内置 AWC/precip/facts 兜底刷新关。入口脚本不得把这些默认值改回“自动写库/自动 kick”；如需临时打开，必须在 `.env` 或 launchd 环境中显式设置，并先说明影响面。

## 当前开关

- `PM_REFRESH_COORDINATOR_ENABLED=1`
- `PM_REFRESH_COORDINATOR_LIVE_PROTECT_ENABLED=1`
- `PM_REFRESH_COORDINATOR_UNIFIED_LANE_ENABLED=1`
- `PM_REFRESH_COORDINATOR_LANE_NAME=weather_refresh_lane`
- `PM_REFRESH_COORDINATOR_ROLE_CONTRACT_VERSION=refresh_coordinator_v3`
- `PM_MODEL_SCAN_LANE_NAME=weather_refresh_lane`
- `PM_MARKET_REFRESH_LANE_NAME=weather_refresh_lane`
- `PM_DAILY_SCAN_LANE_NAME=weather_refresh_lane`
- `PM_REFRESH_COORDINATOR_SUPPRESS_LEGACY_PEER_GUARDS=1`
- `PM_MODEL_SCAN_IGNORE_MARKET_REFRESH_ACTIVE=1`
- `PM_MODEL_SCAN_YIELD_TO_MARKET_REFRESH_WHEN_TICK_STALE=0`
- `PM_MARKET_REFRESH_IGNORE_MODEL_SCAN_ACTIVE=1`
- `PM_MARKET_REFRESH_YIELD_TO_MODEL_SCAN_ACTIVE=0`
- `PM_MARKET_REFRESH_DB_LOCK_ENABLED=1`
- `PM_MODEL_SCAN_DB_LOCK_ENABLED=1`
- `PM_WEATHER_DISCOVERY_COORDINATOR_LANE_ENABLED=1`
- `PM_WEATHER_DISCOVERY_KICK_MODEL_SCAN=0`
- `PM_HARD_SOURCE_PREEMPT_NONCRITICAL_REFRESH_ENABLED=1`
- `PM_HARD_SOURCE_PREEMPT_NONCRITICAL_OWNERS=model_scan,model_scan_hourly,market_refresh,market_refresh_frequent,market_refresh_urgent,weather_discovery,model_release_watcher,daily_scan,weatherctl_facts`
- `PM_REFRESH_COORDINATOR_ALLOW_NONLIVE_AWC_REFRESH=0`
- `PM_REFRESH_COORDINATOR_FACTS_WRITE_OWNERS=weatherctl_facts,daily_scan`
- `PM_REFRESH_COORDINATOR_ALLOW_SCAN_FACTS_MATERIALIZE=0`
- `PM_WEATHER_SCAN_PREEMPT_DAILY=1`
- `PM_MODEL_SCAN_AWC_FACT_REFRESH_ENABLED=0`
- `PM_MODEL_SCAN_AWC_FACT_REFRESH_PRE_LANE_ENABLED=0`
- `PM_MODEL_SCAN_AWC_FACT_REFRESH_LANE_WAIT_SEC=0`
- `PM_MODEL_SCAN_AWC_FACT_REFRESH_DB_LOCK_WAIT_SEC=0`
- `PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_ENABLED=0`
- `PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_LANE_WAIT_SEC=0`
- `PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_DB_LOCK_WAIT_SEC=0`
- `PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_LANE_WAIT_SEC=0`
- `PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_DB_LOCK_WAIT_SEC=0`
- `PM_FACTS_MATERIALIZE_AFTER_SCAN=0`
- `PM_MARKET_WS_DEFER_DURING_LIVE_SSOT=1`
- `PM_MARKET_WS_DB_BUSY_TIMEOUT_MS=250`
- `PM_MARKET_WS_WRITE_RETRY_ATTEMPTS=2`
- `PM_UI_SNAPSHOT_SUMMARY_LIVE_MARKET_HYDRATE=0`
- `PM_UI_MAX_CONCURRENT_REQUESTS=6`
- `PM_UI_MAX_CONCURRENT_REQUESTS_HARD_CAP=6`
- `PM_UI_PRIORITY_MAX_CONCURRENT_REQUESTS=2`
- `PM_UI_HEAVY_MAX_CONCURRENT_REQUESTS=1`
- `PM_UI_REQUEST_QUEUE_SIZE=64`
- `PM_MOST_LIKELY_MAX_CONCURRENCY=1`
- `PM_MOST_LIKELY_HISTORY_ROW_LIMIT=12`
- `PM_MOST_LIKELY_HISTORY_QUERY_TIMEOUT_SEC=0.8`
- `PM_MOST_LIKELY_PREWARM_ALL_WEIGHTS=0`
