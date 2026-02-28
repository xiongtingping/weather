# SSOT 全链路设计（入口 → 事实层 → API → UI 渲染）

> 目标：让 **UI / API / 缓存 / 结果来源** 在任意时刻都只从 **同一份事实数据（facts）** 读数；采集只写原始快照（raw snapshots）；所有衍生字段只由后端合成器（composer）生成并写回事实层。

## 1. 核心原则（必须遵守）

1) **唯一可信来源（SSOT）**  
   - UI 与对外 API 的读模型只有一个：`event_fact`（事实对象）。  
   - 任何“边际/优势/概率/一致性/告警”等展示与决策字段，都必须来自 `event_fact`。

2) **分层写入（禁止越权）**  
   - 采集器（模型/盘口）只写 `*_snapshots`（原始记录层），不得写 `edge_score` 等衍生字段。  
   - 合成器（composer）只读 snapshots，生成并写 `event_fact_*`（事实层），并附带溯源引用（provenance）。  
   - UI 永远只读 facts，不得从 `distribution` 临时推断顶栏字段，也不得混用不同接口/快照源做“前端合并”。

3) **空值语义一致**  
   - 未知就是 `NULL` / `null`，禁止把未知数值化为 `0` 参与计算或比较（彻底根治 `Number(null)===0`）。  

4) **可追溯、可复现**  
   - 每条 fact 必须能追溯到使用了哪些 raw snapshots（至少 `model_snapshot_id` / `market_snapshot_id`）。  
   - composer 必须版本化（`composer_version`），确保同输入可复现同输出。

5) **原子切换**  
   - “最新事实”只能通过指针表原子更新：插入 `event_fact_history` + 更新 `event_fact_latest` 在同一事务内完成。

---

## 2. 领域主键与事实对象边界

### 2.1 事件主键（全系统一致）
- `event_key = (slug, event_date)`  
- `event_date` 一律规范化为 `YYYY-MM-DD`（入库前强制裁剪，禁止带 `T...`）。

### 2.2 事实对象（UI/API 唯一读模型）
`event_fact` 是“某事件在某个 as_of 时刻”的事实快照，包含：

- **市场事实**：`best_bid/ask(+NO侧)`, `spread`, `depth_topN`, `market_updated_at`, `market_stale_sec`
- **模型事实**：`distribution`（每桶含 `q_raw/q_calibrated/q_min/q_max/per_model_q/...`）
- **策略事实**：`signal_edge_*`, `edge_points`, `time_factor_*`, `structure_factor`, `dominance`, `penalty`, `edge_score_*`
- **展示事实**：`top_bucket_stats`（顶部卡片只读此字段），`market_top_label`，各权重口径 top/mean 结果
- **溯源事实**：本条 fact 使用的 snapshot 引用与关键时间戳

> 关键：同一 fact 内的字段必须自洽，UI 不再“上面用 A、下面用 B”。

---

## 3. SQLite 事实库分层（表职责）

当前库里已存在 `model_snapshots` / `market_snapshots`（作为 raw snapshots 可直接复用）。在其上新增事实层两张表 + 合成运行表（可选）：

### 3.1 Raw Snapshots（原始记录层，append-only）
- `model_snapshots`（已存在）：模型请求响应（payload + meta）
- `market_snapshots`（已存在）：盘口/深度响应（payload + meta）

约束：
- 只追加，不做 in-place 更新（允许去重但不覆盖历史）。
- 必须记录 `created_at` 与必要 meta（请求参数、ttl、source、hash 等）。

### 3.2 Composer Runs（合成运行层，可选但强烈建议）
用于解释与排障：一次合成到底选了哪些输入、为什么失败（避免 UI/脚本各自猜测）。

- `fact_builds`
  - `id`
  - `run_date`（归档日/扫描日）
  - `slug`, `event_date`
  - `as_of`（本次合成的观察时刻）
  - `model_snapshot_id`, `market_snapshot_id`
  - `composer_version`
  - `status`（ok/skip/error）
  - `error`（短码 + message）
  - `created_at`

### 3.3 Facts（事实层，UI 唯一读取）
1) `event_fact_history`（append-only）
   - 每次合成产出的事实对象完整落表（`fact_json` + 常用索引列）

2) `event_fact_latest`（指针表）
   - 每个 `event_key` 只指向一个“当前最新可用”的 `fact_id`

> 注意：本设计不要求立刻删除/改写 `event_runs`，但 UI/API 的默认读路径必须切到 facts，避免再次出现“多数据源不一致”。

---

## 4. 建议 DDL（列 + JSON 混合，推荐）

推荐采用“少量索引列 + 全量 fact_json”的混合模式：  
- 便于 UI/API 快速筛选（例如按 `run_date`、`market_updated_at`、`edge_score` 排序）  
- 同时保留完整 JSON（避免列爆炸）

```sql
-- 事实历史（append-only）
CREATE TABLE IF NOT EXISTS event_fact_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT NOT NULL,
  slug TEXT NOT NULL,
  event_date TEXT NOT NULL,
  fact_as_of TEXT NOT NULL,

  model_snapshot_id INTEGER,
  market_snapshot_id INTEGER,
  composer_version TEXT NOT NULL,

  -- 常用索引列（按实际 UI 需求增减）
  market_updated_at TEXT,
  model_updated_at TEXT,
  market_stale_sec INTEGER,
  model_cache_age_sec INTEGER,

  best_side TEXT,                -- 'YES' | 'NO' | NULL
  best_outcome TEXT,             -- bucket label
  best_edge_score REAL,          -- NULL 表示无效/未知
  best_signal_edge_entry REAL,

  fact_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_fact_history_key
  ON event_fact_history(slug, event_date, created_at);
CREATE INDEX IF NOT EXISTS idx_event_fact_history_run_date
  ON event_fact_history(run_date, created_at);
CREATE INDEX IF NOT EXISTS idx_event_fact_history_best
  ON event_fact_history(best_edge_score, created_at);

-- 最新指针（每个 event_key 一行）
CREATE TABLE IF NOT EXISTS event_fact_latest (
  slug TEXT NOT NULL,
  event_date TEXT NOT NULL,
  fact_id INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (slug, event_date)
);

-- 合成运行（可选）
CREATE TABLE IF NOT EXISTS fact_builds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT NOT NULL,
  slug TEXT NOT NULL,
  event_date TEXT NOT NULL,
  as_of TEXT NOT NULL,
  model_snapshot_id INTEGER,
  market_snapshot_id INTEGER,
  composer_version TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fact_builds_key
  ON fact_builds(slug, event_date, created_at);
```

> `best_edge_score` 等索引列不是“事实的另一个来源”，只是 `fact_json` 的冗余索引，用于查询性能；真相仍以 `fact_json` 为准（composer 写入前可做一致性断言）。

---

## 5. Fact JSON Schema（对 UI/API 的契约）

### 5.1 顶层字段（建议）
- `slug`, `event_date`, `run_date`
- `title`, `city`, `event_url`, `market_var`, `market_unit`, `rounding_rule`, `event_tz`
- `as_of`（本 fact 观察时刻）

### 5.2 时间与新鲜度（告警统一口径）
- `market_updated_at`, `market_updated_source`（tick/run/payload 等）
- `model_updated_at`（模型快照时间）
- `market_stale_sec`（告警阈值，来自配置摘要）
- `model_cache_age_sec`, `model_cache_freshness`

### 5.3 顶部卡片唯一来源（禁止前端推断）
- `top_bucket_stats`
  - `label`（最高桶标签，必须与 distribution 内某行匹配）
  - `weighting`（当前默认 `equal`；未来支持 auto/scenario 也应在 fact 内显式给出）
  - `q_raw`, `q_min`, `q_max`, `q_equal`, `q_auto`, `q_scenario`, `q_calibrated`

### 5.4 distribution（表格/轨迹/策略的共同底座）
每桶一行（建议字段示意）：
- `outcome`（bucket label）
- `q_raw` / `q_calibrated` / `q_min` / `q_max`
- `best_bid/ask`、`spread`、`bid_depth_topN/ask_depth_topN`
- `best_bid_no/best_ask_no`、`spread_no`、`bid_depth_topN_no/ask_depth_topN_no`
- `per_model_q`（模型分解；包含成员数扩展信息）
- `signal_edge_*`、`edge_points`、`time_factor_yes/no`、`structure_factor`、`dominance`、`penalty`、`edge_score_yes/no`

### 5.5 provenance（可解释性）
- `inputs`
  - `model_snapshot_id`, `market_snapshot_id`
  - （可选）输入 meta 摘要与 hash
- `composer`
  - `composer_version`
  - `params`（关键 env/config 摘要，例如 `PM_TIME_STRUCTURE_MIN` 等）

---

## 6. 全链路数据流（入口 → 事实 → 渲染）

### 6.1 入口（触发）
- `start.command` / LaunchAgent / 定时脚本：只负责触发任务
  1) `model_fetch` → 写 `model_snapshots`
  2) `market_fetch` → 写 `market_snapshots`
  3) `compose_fact` → 读 snapshots → 写 `event_fact_history` + 更新 `event_fact_latest`

落地建议（迁移期）：
- 扫描链路在归档 `event_runs` 时同步写入 `model_snapshots/market_snapshots`（保证 facts 的输入存在）
- 扫描结束后主动执行一次物化（materialize），例如：`ui/server.py --materialize-facts --facts-date YYYY-MM-DD`

### 6.2 合成器（唯一事实生产者）
`compose_fact(event_key, as_of)` 的确定性步骤：
1) 选择输入：按 TTL/延迟策略选取“最近且可用”的 `model_snapshot` 与 `market_snapshot`
2) 标准化：清洗 members、扩展分布、桶映射、单位/舍入规则
3) 计算 distribution：`q_raw/q_min/q_max/q_calibrated/...`
4) 计算策略字段：`signal_edge_* / edge_points / time_factor / structure_factor / penalty / edge_score_*`
5) 生成 `top_bucket_stats`（一次性计算齐全）
6) 落库：插入 `event_fact_history`，原子更新 `event_fact_latest`

禁止：
- UI 自己补算 edge_score
- API 端点各自补算顶栏字段
- 不同端点对同一字段使用不同来源/不同回退口径

---

## 7. API 契约（只读 facts）

对 UI 的“默认路径”只保留 facts 接口：

1) `GET /api/facts?run_date=YYYY-MM-DD&page=1&page_size=200`  
   - 只从 `event_fact_latest` 取指针，再 join `event_fact_history` 返回事实对象列表

2) `GET /api/facts-range?slug=...&event_date=...&run_date=...`  
   - 只读 `event_fact_history`（按 created_at 升序），用于轨迹/回放

运维/排障接口（UI 默认不依赖）：
- `GET /api/provenance?fact_id=...`：返回输入 snapshots 摘要（解释“为什么是这样”）
- `GET /api/snapshots?...`：查看 raw（排障专用）

---

## 8. UI 渲染契约（只读一个字段集）

UI 状态模型简化为：
- `facts[]`（每项就是 `event_fact`）
- `filters`（纯前端过滤，不改事实）

硬性约束：
- 顶栏 q 卡片只读 `fact.top_bucket_stats.*`
- 原因统计只读 `fact` 内的策略字段（不再混用 runs/range 两套源）
- `null` 展示为 `—`，不得数值化为 0 参与逻辑判断

---

## 9. 并发与一致性（避免“双刷新互相覆盖”）

Raw snapshots 可并发追加；facts 采用“单写者/同 key 串行”：
- composer 对同一个 `event_key` 需要锁（进程内互斥或 DB 租约表）
- facts 不做局部 update：永远 `insert history + switch latest`（天然避免“空覆盖有效值”）

---

## 10. 迁移落地（分阶段，不推倒重来）

1) 新增 facts 表 + composer 写入（不影响现有 UI）  
2) 新增 `/api/facts` 与 `/api/facts-range`（并行提供）  
3) UI 切换只读 facts（移除前端推断与前端合并）  
4) 将旧 `/api/runs` 降级为调试接口或下线（避免未来再次绕过 SSOT）  

> 迁移期允许“facts 缺失时的回填”：优先用 `model_snapshots+market_snapshots` 合成 facts；仅当快照不足时再从旧 `event_runs` 回退补齐。目标是在扫描链路中让 composer 主动写入 facts，从而最终彻底不依赖 `event_runs` 的读路径。
