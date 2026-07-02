# Weather Trading System VPS 部署准备

**准备时间**: 2026-04-23
**目标**: 准备将 weather trading system 部署到 VPS

---

## 📋 部署前检查清单

### 1. 数据清理状态 ✅

- [x] 数据库已清理优化（7.7GB）
- [x] 缓存文件已删除（16MB）
- [x] 日志文件已清理（15MB）
- [x] 定期清理服务已设置（每周日凌晨3点）
- [x] 备份文件已保留（7.7GB）

**当前磁盘使用**: 15.4GB（包含备份）/ 7.7GB（不含备份）

### 2. 系统优化状态 ✅

**模型刷新频率优化**：
- [x] 5分钟刷新（临近结算事件）
- [x] 1小时刷新（今日+2天事件）
- [x] 6小时刷新（全量深度扫描）

**市场盘口刷新优化**：
- [x] 30秒刷新（<8小时临近结算）
- [x] 1分钟刷新（常规事件）

### 3. 交易卡点分析 ⚠️

**最近24小时交易决策统计**：
- 总决策数: 204,995
- blocked: 204,944 (99.98%)
- error_timeout: 51 (0.02%)

**主要阻断原因**：
1. **market_tick_stale** (73,269次, 35.7%) - 盘口数据过期
2. **plain_lcb_pinned_tail_no_filtered** (50,124次, 24.5%) - LCB策略过滤
3. **plain_lcb_zero_prob_yes_filtered** (43,303次, 21.1%) - 概率过低
4. **market_closed** (10,455次, 5.1%) - 市场已关闭
5. **market_snapshot_inconsistent** (8,530次, 4.2%) - 市场快照不一致
6. **market_tick_missing** (6,528次, 3.2%) - 缺少盘口数据

**关键问题**：
- ⚠️ **CLOB API 超时严重**: 大量 `Connection to clob.polymarket.com timed out (connect timeout=1.2s)`
- ⚠️ **Worker 超时**: `worker_stuck_timeout` 导致任务重试
- ⚠️ **盘口数据时效性**: 35.7% 的决策因盘口过期被阻断

---

## 🔧 VPS 部署准备工作

### 参考文档
已读取以下 SOP 文档：
1. `/Users/xiong/poly/docs/ubuntu_vps_deploy_sop.md` - Ubuntu VPS 通用部署流程
2. `/Users/xiong/poly/docs/vps_dualhigh_deploy_sop.md` - DualHigh 策略专用部署
3. `/Users/xiong/poly/output/vps/staging/poly/vps/DEPLOY_README.md` - Windows Server 部署

### Weather System 特殊需求

**与 Poly DualHigh 的区别**：
1. **数据源**: Weather 依赖 Open-Meteo API（10,000次/天限制）
2. **数据库**: SQLite (7.7GB) vs PostgreSQL
3. **服务架构**:
   - Weather: 11个 launchd 服务（macOS）
   - Poly: 2个 systemd 服务（Ubuntu）
4. **网络依赖**:
   - Weather: Open-Meteo + Polymarket CLOB
   - Poly: 仅 Polymarket CLOB

### 需要准备的文件

#### 核心代码
- [ ] `scan_weather_market.py` - 主程序
- [ ] `weather/` - 核心模块目录
- [ ] `scripts/` - 所有脚本
- [ ] `requirements.txt` - Python 依赖

#### 配置文件
- [ ] `.env` - 环境变量（需手动配置，不能打包）
- [ ] `data/q_calibration_bundle.json` - Q值校准
- [ ] `data/q_quote_calibration_bundle.json` - 报价校准
- [ ] `data/auto_trade_config.json` - 交易配置

#### 数据库
- [ ] `data/archive.db` - 主数据库（7.7GB）
- [ ] 或考虑只上传 schema，VPS 上重新初始化

#### 服务配置
- [ ] 需要将 macOS launchd 转换为 Linux systemd
- [ ] 或使用 cron + supervisor

---

## 🚀 部署策略建议

### 方案A：完整迁移（推荐用于测试）

**优点**：
- 保留所有历史数据
- 可以立即开始交易

**缺点**：
- 需要上传 7.7GB 数据库
- 首次同步时间长

**步骤**：
1. 压缩整个项目（排除 .venv, logs, cache）
2. 上传到 VPS
3. 安装依赖
4. 配置 systemd 服务
5. 启动服务

### 方案B：轻量部署（推荐用于生产）

**优点**：
- 上传快速（<100MB）
- VPS 上重新初始化数据库
- 干净的起点

**缺点**：
- 需要重新积累历史数据
- Q值校准需要重新训练

**步骤**：
1. 只上传代码和配置
2. VPS 上初始化空数据库
3. 导入 Q值校准文件
4. 启动服务，自动开始采集数据

### 方案C：混合部署（最推荐）

**优点**：
- 保留关键校准数据
- 数据库从零开始（节省空间）
- 快速上传

**缺点**：
- 需要等待数据积累（1-2天）

**步骤**：
1. 上传代码 + 配置 + 校准文件
2. 初始化空数据库
3. 导入最近7天的 event_fact_latest
4. 启动服务

---

## 📦 需要打包的文件清单

### 必须文件（方案C）
```
weather/
├── scan_weather_market.py
├── weather/                    # 核心模块
├── scripts/                    # 所有脚本
├── requirements.txt
├── .env.template              # 模板，不含真实密钥
├── data/
│   ├── q_calibration_bundle.json
│   ├── q_quote_calibration_bundle.json
│   ├── auto_trade_config.json
│   └── archive.db.schema      # 只要 schema，不要数据
└── deploy/
    └── systemd/               # systemd 服务配置（需创建）
```

### 可选文件
```
├── data/
│   ├── archive.db.backup      # 如果需要历史数据
│   └── event_fact_latest.csv  # 最近事实数据导出
```

---

## ⚙️ VPS 环境要求

### 最低配置
- **OS**: Ubuntu 22.04 LTS 或更高
- **CPU**: 2 核
- **内存**: 4GB（推荐 8GB）
- **磁盘**: 20GB（如果包含历史数据需要 30GB+）
- **网络**: 稳定的国际网络连接（访问 Open-Meteo 和 Polymarket）

### 软件依赖
- Python 3.10+
- SQLite 3.35+
- systemd
- cron

### 网络要求
- 能访问 `api.open-meteo.com`（天气数据）
- 能访问 `clob.polymarket.com`（市场数据）
- 建议配置 HTTP 代理（如果 CLOB 超时严重）

---

## 🔐 安全检查

### 敏感信息清理
- [ ] 确认 `.env` 不在打包文件中
- [ ] 确认没有私钥在代码中
- [ ] 确认 API 密钥不在日志中
- [ ] 确认备份文件不包含敏感数据

### VPS 安全配置
- [ ] 配置防火墙（只开放 SSH）
- [ ] 禁用 root 登录
- [ ] 配置 SSH 密钥认证
- [ ] 设置 fail2ban
- [ ] 定期备份数据库

---

## 📊 部署后验收标准

### 服务健康检查
- [ ] 所有 systemd 服务状态为 active
- [ ] 无持续重启或 OOM
- [ ] 日志无 Traceback 或 Exception

### 数据采集验收
- [ ] Open-Meteo API 调用成功
- [ ] 天气预测数据正常更新
- [ ] event_fact_latest 表持续增长

### 市场数据验收
- [ ] CLOB API 连接正常
- [ ] market_tick 数据新鲜度 <180秒
- [ ] market_snapshot 正常更新

### 交易功能验收
- [ ] auto_trade_decisions 正常生成
- [ ] blocked 比例 <95%（目前 99.98%）
- [ ] 有实际订单提交（如果满足条件）

---

## 🚨 已知问题和解决方案

### 问题1: CLOB API 超时严重
**现象**: `Connection to clob.polymarket.com timed out (connect timeout=1.2s)`

**可能原因**：
1. 本地网络到 CLOB 的延迟高
2. CLOB API 限流
3. 超时设置过短（1.2秒）

**解决方案**：
1. VPS 选择网络质量好的区域（美国/欧洲）
2. 增加超时时间到 3-5 秒
3. 配置 HTTP 代理
4. 减少并发请求数

### 问题2: market_tick_stale 阻断率高（35.7%）
**现象**: 大量决策因盘口数据过期被阻断

**原因**:
- 当前刷新频率不够（虽然已优化到30秒）
- CLOB API 超时导致刷新失败

**解决方案**：
1. 优先解决 CLOB 超时问题
2. 增加刷新重试次数
3. 降低 market_tick_stale_sec 阈值（当前180秒）

### 问题3: Worker 超时
**现象**: `worker_stuck_timeout` 导致任务重试

**原因**:
- 单次扫描时间过长
- 数据库查询慢
- API 调用阻塞

**解决方案**：
1. 优化数据库索引
2. 减少单次扫描的事件数量
3. 增加 worker 超时时间
4. 使用异步 API 调用

---

## 📝 下一步行动

### 立即执行（部署前）
1. [ ] 创建 systemd 服务配置文件
2. [ ] 准备 `.env.template` 模板
3. [ ] 导出数据库 schema
4. [ ] 打包必要文件
5. [ ] 编写部署脚本

### VPS 上执行（部署时）
1. [ ] 上传文件到 VPS
2. [ ] 安装系统依赖
3. [ ] 创建 Python 虚拟环境
4. [ ] 配置 `.env`
5. [ ] 初始化数据库
6. [ ] 安装 systemd 服务
7. [ ] 启动服务
8. [ ] 验收测试

### 部署后监控（运行时）
1. [ ] 监控 API 调用量（Open-Meteo <10,000/天）
2. [ ] 监控 CLOB 超时率
3. [ ] 监控数据库增长速度
4. [ ] 监控交易决策质量
5. [ ] 每周检查日志和错误

---

**状态**: 准备中，等待用户确认部署方案
