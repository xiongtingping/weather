# PostgreSQL 迁移实施报告

**执行时间**: 2026-04-23 01:25
**状态**: 阶段1-2完成，阶段3准备就绪

---

## ✅ 阶段1: SQLite 优化（已完成）

### 1.1 优化 SQLite 配置 ✅

**配置更新**:
```bash
PM_DB_BUSY_TIMEOUT_MS=30000  # 30秒
PM_DB_CONNECT_TIMEOUT_SEC=10  # 10秒
PM_MARKET_REFRESH_DB_BUSY_TIMEOUT_MS=30000
PM_MARKET_REFRESH_DB_CONNECT_TIMEOUT_SEC=10
```

**WAL 模式优化**:
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA wal_autocheckpoint=1000;
PRAGMA busy_timeout=30000;
```

**效果**: 数据库超时时间从 5秒 增加到 30秒

---

### 1.2 调整服务调度时间 ✅

**修改内容**:
- `daily_scan`: 02:10, 08:10, 14:10, 20:10 → **03:10, 09:10, 15:10, 21:10**
- `model_scan_hourly`: 每小时 :05 → **每2小时** (01:05, 03:05, 05:05, ...)

**效果**: 错开 `model_scan` 和 `daily_scan`，避免冲突

---

### 1.3 添加全局扫描锁 ✅

**创建文件**:
- `scripts/lib/acquire_scan_lock.sh` - 全局锁机制

**修改脚本**:
- `model_scan_hourly.sh` - 添加锁检查（不等待）
- `daily_scan_and_settlement.sh` - 添加锁检查（等待5分钟）

**效果**: 防止多个重量级扫描同时运行

---

## ✅ 阶段2: 任务队列系统（已完成）

### 2.1 数据库表结构 ✅

```sql
CREATE TABLE task_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    payload_json TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    worker_pid INTEGER,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3
);
```

**索引**:
- `idx_task_queue_status_priority` - 按状态和优先级查询
- `idx_task_queue_type_status` - 按类型和状态查询

---

### 2.2 任务队列组件 ✅

**创建文件**:
1. `scripts/task_queue_worker.py` - Worker 主程序
   - 从队列获取任务
   - 执行任务脚本
   - 处理失败和重试
   - 清理僵尸任务

2. `scripts/enqueue_task.py` - 入队工具
   - 将任务添加到队列
   - 支持优先级和 payload

3. `com.weather.task_queue_worker.plist` - launchd 服务
   - 自动启动 Worker
   - 保持运行（KeepAlive）

---

### 2.3 任务类型映射 ✅

```python
TASK_SCRIPTS = {
    "model_scan": "scripts/model_scan.sh",
    "model_scan_hourly": "scripts/model_scan_hourly.sh",
    "daily_scan": "scripts/daily_scan_and_settlement.sh",
    "market_refresh": "scripts/market_refresh_1m.sh",
    "market_refresh_urgent": "scripts/market_refresh_urgent.sh",
    "feedback_loop": "scripts/feedback_loop_hourly.sh",
}
```

---

### 2.4 使用方法

**入队任务**:
```bash
# 添加模型扫描任务（优先级 10）
python scripts/enqueue_task.py model_scan 10

# 添加市场刷新任务（优先级 5）
python scripts/enqueue_task.py market_refresh 5
```

**启动 Worker**:
```bash
# 手动启动
python scripts/task_queue_worker.py

# 或使用 launchd（自动启动）
launchctl load ~/Library/LaunchAgents/com.weather.task_queue_worker.plist
```

**查看队列状态**:
```bash
sqlite3 data/archive.db "
SELECT id, task_type, status, priority, created_at
FROM task_queue
ORDER BY created_at DESC
LIMIT 10;
"
```

---

## ✅ 阶段3: PostgreSQL 迁移准备（已完成）

### 3.1 PostgreSQL 环境 ✅

**已安装版本**:
- PostgreSQL 14 (未启动)
- PostgreSQL 15 (运行中) ✅
- PostgreSQL 17 (错误)

**数据库创建**:
```bash
createdb weather_trading
```

---

### 3.2 数据库抽象层 ✅

**创建文件**: `weather/db_abstraction.py`

**功能**:
- 统一的数据库接口
- 自动检测使用 SQLite 还是 PostgreSQL
- 自动转换占位符（? → %s）
- 上下文管理器支持

**使用方法**:
```python
from weather.db_abstraction import get_db_connection

# 自动使用配置的数据库类型
with get_db_connection() as db:
    result = db.fetchone("SELECT * FROM task_queue WHERE id = ?", (1,))
```

**切换数据库**:
```bash
# 使用 SQLite（默认）
export PM_DB_TYPE=sqlite

# 使用 PostgreSQL
export PM_DB_TYPE=postgresql
export PM_POSTGRESQL_DSN="postgresql://xiong@localhost/weather_trading"
```

---

### 3.3 Schema 转换工具 ✅

**功能**: 将 SQLite schema 转换为 PostgreSQL schema

```python
from weather.db_abstraction import convert_sqlite_to_postgresql_schema

sqlite_schema = """
CREATE TABLE test (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

pg_schema = convert_sqlite_to_postgresql_schema(sqlite_schema)
# 输出:
# CREATE TABLE test (
#     id SERIAL PRIMARY KEY,
#     created_at TEXT DEFAULT (NOW())
# );
```

---

## 📊 实施效果预测

### 阶段1效果（已生效）

| 指标 | 优化前 | 优化后预期 | 实际效果 |
|------|--------|------------|----------|
| 数据库锁错误 | 频繁 | <5% | 待观察 |
| 服务冲突 | 经常 | 罕见 | 待观察 |
| 超时错误 | 多 | 少 | 待观察 |

---

### 阶段2效果（待启用）

| 指标 | 当前 | 启用后预期 |
|------|------|------------|
| 并发扫描 | 2-3个 | 1个 |
| 数据库锁 | 频繁 | 极少 |
| 任务管理 | 分散 | 集中 |

---

### 阶段3效果（待迁移）

| 指标 | SQLite | PostgreSQL |
|------|--------|------------|
| 并发写入 | 差 | 优秀 |
| 锁机制 | 文件锁 | 行锁 |
| 性能 | 中等 | 高 |
| 运维复杂度 | 低 | 中 |

---

## 🚀 下一步行动

### 立即验证（今晚）

1. **观察数据库锁问题是否改善**
   ```bash
   tail -f data/market_refresh.log | grep "database is locked"
   ```

2. **检查服务调度是否错开**
   ```bash
   launchctl list | grep com.weather
   ps aux | grep scan_weather
   ```

---

### 本周执行（可选）

**如果阶段1效果好，可以启用任务队列**:

1. **启动 Worker**
   ```bash
   launchctl load ~/Library/LaunchAgents/com.weather.task_queue_worker.plist
   ```

2. **修改服务改为入队模式**
   ```bash
   # 例如：model_scan_hourly.sh
   # 从直接执行改为入队
   python scripts/enqueue_task.py model_scan_hourly 5
   ```

3. **观察队列运行情况**
   ```bash
   tail -f data/task_queue_worker.log
   ```

---

### 长期规划（3-6个月）

**仅在以下情况考虑 PostgreSQL 迁移**:
1. 阶段1+阶段2仍无法解决锁问题
2. 业务增长需要更高并发
3. 有充足的开发和测试时间

**迁移步骤**:
1. 导出 SQLite schema
2. 转换为 PostgreSQL schema
3. 创建所有表和索引
4. 导出数据（CSV 格式）
5. 导入到 PostgreSQL
6. 修改代码使用 `db_abstraction.py`
7. 全面测试
8. 切换到 PostgreSQL

---

## ✅ 完成清单

- [x] 阶段1.1: 优化 SQLite 配置
- [x] 阶段1.2: 调整服务调度时间
- [x] 阶段1.3: 添加全局扫描锁
- [x] 阶段2.1: 创建任务队列表
- [x] 阶段2.2: 实现 Worker 和入队工具
- [x] 阶段2.3: 创建 launchd 服务
- [x] 阶段3.1: 准备 PostgreSQL 环境
- [x] 阶段3.2: 创建数据库抽象层
- [x] 阶段3.3: 实现 Schema 转换工具
- [ ] 验证阶段1效果（24小时观察）
- [ ] 决定是否启用阶段2（任务队列）
- [ ] 评估是否需要阶段3（PostgreSQL）

---

## 📝 总结

**已完成**:
1. ✅ SQLite 优化（超时、调度、锁）
2. ✅ 任务队列系统（完整实现）
3. ✅ PostgreSQL 迁移准备（抽象层、工具）

**当前状态**:
- 阶段1已生效，正在观察效果
- 阶段2已就绪，可随时启用
- 阶段3已准备，等待需要时迁移

**推荐策略**:
1. 先观察阶段1效果（24小时）
2. 如果效果不够，启用阶段2
3. 只有在必要时才考虑阶段3

---

**状态**: 全部三个阶段已完成准备，等待验证和决策
