---
status: archived
authority: L2
effective_from: 2026-07-19
owner: weather-system
updated_at: 2026-07-17
activation: reported_trajectory_no_v1
---

# Reported Trajectory NO 极简低延迟生产方案

## 唯一目标

系统只处理“官方报文已确认的最高温硬下界”，并买入最近一个已被数学排除的低温桶 NO。没有 YES、Bayes、先验、似然、NWP、TAF、regime、历史校准或前端计算。

例如报文把站点当日最高温推进到 36°C，则只允许买入最高上界严格低于 36°C 的最近桶 NO。下单语义固定为 `FAK BUY NO @ 0.985`：只吃 0.985 及以下卖盘，剩余立即取消，禁止裸 market order。

## 双来源泳道与统一资金池

| 来源 | 官方数据 | 市场范围 |
|---|---|---|
| AMSC | AMSC METAR/SPECI | 全国已注册中国最高温市场 |
| 国家官方源 + AWC relay | 对应国家原始 METAR/SPECI；不可用时显式记录 AWC relay | 已注册的非中国最高温市场 |

AMSC 与国际泳道负责互斥市场集合，不跨泳道 fallback、借资或交叉确认；国际泳道内部优先使用对应国家官方源，失败时才把同站 AWC relay 标记为 fallback，任一报文只消费一次。两条泳道共用一个无来源配额的资金池，出现合格硬事实就按账户可用余额与 `<=0.985` 的可执行深度全量成交。只取报文原文中的整数温度和 METAR/SPECI 身份；不请求 TAF，不解析云、雨、风或其他字段用于交易。

入场实际成交后，立即对买到的同一个 NO token 挂 `GTC SELL @ 0.99`。这是持仓退出，不是 YES 策略。若 FAK 零成交则释放全部预留；部分成交只为实际份额挂卖单；0.99 卖单成交后释放资本账本。

## 生产拓扑

生产系统域只允许三个常驻服务：

```text
AMSC/AWC temperature fetch
  -> reported_trajectory_no
     parse + dedupe + hard-bound + capital reserve
  -> clob_order_service
     current guard + idempotent FAK POST

Polymarket official CLOB WebSocket
  -> market_ws
     eligible NO tokens only
     -> per-token atomic quote shard
        -> clob_order_service
```

- `com.weather.weather_basket_yes`：当前 AWC 正式 truth、P1 与纯 YES 篮子唯一驻留服务；旧 `com.weather.reported_trajectory_no` label 仅为本历史方案中的退役名称，禁止加载。
- `com.weather.market_ws`：只订阅注册表内可交易 NO token；不写历史 tick。
- `com.weather.clob_order_service`：唯一网络下单权、余额预取、最终盘口验证和幂等。

UI、monitor、market refresh、model scan、task queue、archive guard、Bayes/Target Realization 执行服务全部停用。旧代码和历史表保留离线，不拥有生产执行权。

## 热路径与持久化

1. AMSC 全国站点使用单个批量请求；国际站点按国家官方源并发，AWC 只发一次全站 relay/shadow 请求；任一国家源完成即提交，不等待最慢国家。
2. 每条来源泳道持有一个长生命周期 SQLite 连接和 engine；站点列表内存缓存 60 秒。
3. 重复报文通过内存 observation ID 去重，零数据库写入；无新报文的轮询诊断最多每来源每 60 秒写一次。
4. 对潜在新报文先在 SQLite 写锁外冻结目标 NO 的本地盘口证据。
5. 单个 `BEGIN IMMEDIATE` 原子写入 observation、站点日最高温、`auto_trade_decisions`、intent 和资本预留。
6. 事务提交后直接调用本地 gateway；不经过数据库 outbox 轮询。
7. gateway 再次以最新 token 快照、余额、深度、pause 和幂等记录做最终检查，再发 CLOB POST。

数据库保持 WAL + FULL durability。性能优化来自减少事务、缩短写锁和消除冗余写，而不是牺牲订单事实的持久性。

## 盘口快照

旧方案每 50ms 全量改写约数千 token 的大 JSON，下单时再全量解析。现方案改为：

- 一个小型连接清单 `market_ws_latest_quotes.json`，只证明 WebSocket 连续在线；
- 每个 NO token 一个 SHA256 命名的原子快照文件；
- gateway 只读取目标 token 的小文件；
- 所有文件均带 canonical SHA256、来源、证据边界和 token identity；
- `event_market_ticks` 生产写入关闭。

## 调度参数

- AMSC 预计发布时间后 30 秒进入捕获窗口；窗口内和后台轮询均为 0.5 秒。
- AMSC 背景轮询间隔 0.5 秒；上一次请求未完成时不并发堆积。
- 国际国家源在每个整点/半点后的 90 秒使用 0.25 秒调度，来源级缓存把实际网络请求约束为最多每秒一次；跨半点旧缓存强制失效。
- NOAA/AWC relay 严格使用每分钟一次全站请求，并从 `:MM:20` 预留 worker、在 `:MM:30` 发出，避免整点前请求造成 60 秒旧缓存遮挡。其余时段保持 2 秒调度，但不突破 AWC 每分钟网络请求门禁。
- 国家源返回即独立写入 canonical/订单链；慢国家接口不再阻塞其他国家。运行时保留 180 秒有效时间 freshness 门禁拒绝迟到报文交易。
- 市场注册表每 300 秒刷新；NO token WebSocket 每 300 秒重建订阅。
- daemon 主循环 10ms；待协调订单每 100ms，空闲时每 1 秒检查。
- 状态文件每 30 秒写一次，不参与交易热路径。

## 硬门禁

- 市场规则、站点、日期、时区、单位或桶区间不明确：阻断。
- 未被对应官方来源泳道覆盖：阻断并保留为历史/blocked，不得跨泳道切换。
- 报文更正/修订：站点日停牌并人工复核，不推进硬下界。
- 同温、降温、重复原文：不产生新订单。
- 激活 config hash 漂移、CLOB WebSocket/余额/深度过期、价格高于 0.985：阻断。
- 旧执行 owner、非核心 weather 服务或未解决 gateway intent 存在：切换失败并保持 pause。

## 验收

受控切换：

```bash
python3 scripts/cutover_reported_trajectory_no_production.py \
  --confirm-live-reported-trajectory-no-v1
```

只读验收：

```bash
python3 scripts/reported_trajectory_no_production_acceptance.py
```

必须同时满足：三个核心服务且仅这三个已加载、generation/config hash 一致、AMSC 与 AWC 注册表均非空且来源集合互斥、统一全量资金池、入场 `0.985/FAK/NO`、退出 `0.99/GTC/SELL` 精确、TAF 禁用、WebSocket token 分片已启用、tick 归档零写、gateway 余额与行情预取有效、无未解决订单。

延迟验收分别记录来源传输、canonical 处理、gateway 本地证据读取和 exchange POST；不得用观测有效时间替代来源可见时间。没有自然新报文时，只允许使用隔离测试库和假 gateway 验证订单路径，禁止为测速制造真实订单。
