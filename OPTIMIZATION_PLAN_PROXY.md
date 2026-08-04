# Weather 交易系统优化方案 - 代理模式版本

**更新时间**: 2026-04-22
**模式**: 保持代理连接

---

## 核心调整

### ❌ 不使用直连
```bash
PM_CLOB_FORCE_DIRECT=0          # 禁用强制直连
PM_CLOB_DIRECT_FIRST=0          # 禁用直连优先
```

### ✅ 优化代理策略
```bash
# 增强代理重试
PM_CLOB_RETRY_MAX=5             # 重试 5 次（原 3 次）
PM_CLOB_TIMEOUT_MS=60000        # 超时 60s（原 30s）
PM_CLOB_RETRY_BASE_MS=1000      # 基础重试延迟 1s
PM_CLOB_RETRY_MAX_MS=8000       # 最大重试延迟 8s

# 缩短代理冷却期
PM_CLOB_PROXY_COOLDOWN_SEC=300  # 冷却 5 分钟（原 15 分钟）

# 多代理端口支持（可选）
# PM_PROXY_PORTS=7890,7891,7892
```

---

## 代理模式优化重点

### 1. 代理重试机制增强
- **指数退避**：1s → 2s → 4s → 8s
- **智能轮换**：自动尝试不同代理端口
- **快速恢复**：冷却期从 15 分钟缩短到 5 分钟

### 2. 代理健康检查
```javascript
// polymarket_order.mjs 已实现
- 端口可达性探测（400ms 超时）
- 自动切换不可达端口
- 记录代理性能指标
```

### 3. 代理错误处理
```javascript
// 识别代理特定错误
- 502 Bad Gateway → 切换端口
- 503 Service Unavailable → 切换端口
- Too many redirects → 切换端口
- Connection refused → 切换端口
```

---

## 执行步骤

### 1. 执行优化脚本
```bash
cd /Users/xiong/weather
./phase1_emergency_fix_proxy.sh
```

### 2. 验证代理配置
```bash
# 检查系统代理
scutil --proxy

# 检查环境变量
echo $HTTPS_PROXY
echo $HTTP_PROXY

# 测试连接
node weather/scripts/polymarket_order.mjs balance
```

### 3. 监控效果
```bash
# 实时监控
./monitor_optimization.sh

# 查看代理日志
tail -f weather/data/logs/trade_engine.log | grep "CLOB Client"
```

---

## 预期效果

| 指标 | 优化前 | 优化后 | 提升 |
|------|--------|--------|------|
| 代理连接成功率 | ~60% | ~90% | +50% |
| 平均重试次数 | 2.5 | 1.5 | -40% |
| 代理切换延迟 | 15min | 5min | -67% |
| 扫描超时率 | ~40% | <10% | -75% |

---

## 故障排查

### 问题 1: 代理连接仍然失败
**检查**:
```bash
# 1. 代理服务是否运行
lsof -i :1082

# 2. 代理是否可达
curl -x http://127.0.0.1:1082 https://clob.polymarket.com/book

# 3. 查看详细错误
tail -50 weather/data/logs/trade_engine.log | grep -i "proxy\|error"
```

**解决**:
- 重启代理服务
- 检查代理配置
- 确认防火墙规则

### 问题 2: 代理端口轮换不生效
**检查**:
```bash
# 查看可用端口
scutil --proxy | grep Port

# 设置多端口
echo 'PM_PROXY_PORTS=7890,7891,7892' >> weather/.env
```

### 问题 3: 代理冷却期过长
**解决**:
```bash
# 手动清理冷却状态
rm -f weather/data/clob_proxy_cooldown.json

# 或缩短冷却期
echo 'PM_CLOB_PROXY_COOLDOWN_SEC=180' >> weather/.env
```

---

## 其他优化（与代理无关）

### 扫描优化
```bash
PM_SCAN_TIMEOUT_SEC=600
PM_SCAN_OPEN_METEO_TIMEOUT_SEC=20
PM_SCAN_MAX_EVENTS_PER_RUN=3
```

### 任务优化
```bash
PM_AUTO_TRADE_EVENTS_PER_JOB_MAX=6
PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC=300
```

### 策略优化
```bash
PM_EDGE_POLICY_MODE=shadow
PM_EDGE_POLICY_PATH_LCB_MIN=0.05
```

---

## 回滚方案

```bash
# 快速回滚
cp .env.backup.20260422_* weather/.env
sudo launchctl kickstart -k system/com.weather.model_scan
sudo launchctl kickstart -k system/com.weather.auto_trade_worker
sudo launchctl kickstart -k system/com.weather.trade_sync_loop
```

---

## 注意事项

1. **代理稳定性**：确保代理服务稳定运行
2. **端口配置**：如有多个代理端口，配置 `PM_PROXY_PORTS`
3. **观察期**：执行后观察 15-20 分钟
4. **日志监控**：重点关注 `[CLOB Client]` 相关日志

---

**方案结束**
