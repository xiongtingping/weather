# 紧急修复：数据库损坏恢复

**时间**: 2026-04-23 01:05
**问题**: 数据库损坏（database disk image is malformed）
**状态**: ✅ 已恢复

---

## 🚨 问题描述

在应用交易卡点修复后，发现数据库损坏：

```
sqlite3.DatabaseError: database disk image is malformed
Tree 1997777 page 1997777: btreeInitPage() returns error code 11
```

**可能原因**：
1. VACUUM 操作时被中断
2. 多个进程同时写入数据库
3. 磁盘空间不足或 I/O 错误

---

## 🔧 恢复步骤

### 1. 停止所有服务 ✅
```bash
launchctl stop com.weather.*
pkill -f scan_weather_market.py
```

### 2. 从备份恢复数据库 ✅
```bash
cp data/archive.db.backup.20260423_004754 data/archive.db
rm -f data/archive.db-wal data/archive.db-shm
```

### 3. 重启所有服务 ✅
```bash
launchctl start com.weather.*
```

---

## ✅ 恢复验证

- [x] 数据库完整性检查通过
- [x] 所有服务已重启
- [x] 进程正常运行
- [x] 备份文件已保留

---

## 📊 数据损失评估

**备份时间**: 2026-04-23 00:48
**恢复时间**: 2026-04-23 01:05
**数据损失**: 约17分钟的数据

**影响**：
- 丢失17分钟的市场数据
- 丢失17分钟的交易决策记录
- 不影响 Q 值校准和配置文件

---

## 🛡️ 预防措施

### 立即执行

1. **禁用自动 VACUUM**
   - VACUUM 操作风险高
   - 改为手动执行

2. **增加数据库备份频率**
   - 当前：清理时备份
   - 改为：每小时自动备份

3. **添加数据库锁检查**
   - 执行 VACUUM 前检查是否有进程在使用
   - 等待所有写入完成

### 长期优化

1. **迁移到 PostgreSQL**
   - SQLite 不适合高并发写入
   - PostgreSQL 更稳定

2. **实现 WAL 模式优化**
   - 减少锁竞争
   - 提高并发性能

3. **监控数据库健康**
   - 定期完整性检查
   - 磁盘空间监控

---

## 📝 经验教训

1. **VACUUM 操作必须在服务停止时执行**
   - 不能在有进程运行时 VACUUM
   - 需要独占访问数据库

2. **备份策略很重要**
   - 幸好有最近的备份
   - 只损失了17分钟数据

3. **SQLite 的局限性**
   - 不适合高并发场景
   - 需要考虑迁移到 PostgreSQL

---

## 🔄 修改后的清理策略

### 新的清理流程

```bash
# 1. 停止所有服务
launchctl stop com.weather.*

# 2. 等待所有进程退出
sleep 5

# 3. 备份数据库
cp data/archive.db data/archive.db.backup.$(date +%Y%m%d_%H%M%S)

# 4. 清理数据
sqlite3 data/archive.db "DELETE FROM ..."

# 5. VACUUM（独占模式）
sqlite3 data/archive.db "VACUUM;"

# 6. 验证完整性
sqlite3 data/archive.db "PRAGMA integrity_check;"

# 7. 重启服务
launchctl start com.weather.*
```

---

## ✅ 当前状态

- 数据库已恢复
- 所有服务运行正常
- 交易卡点修复已生效
- 系统继续运行

**下一步**: 监控系统运行1小时，确认无异常
