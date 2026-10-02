# SSOT 预选计划冷归档与回收

`scripts/preselection_retention_lane.py` 只处理
`basket_p0_preselection_plan` 中超过七天、没有精确决策引用的旧计划。
每站最新计划、按 preselection_id 或站点日与 production_plan_sha256
引用的计划均保留；不可解析的决策或身份不一致立即拒绝删除。

## 运行边界

- 归档由 resident 的独立单线程 executor 执行，只读 SSOT，不占写者轮次。
- 外置目录固定为 `/Volumes/weather_data/weather_cold/ssot_preselection_rolling`。
  必须是另一设备且至少剩余 50 GiB；每批压缩后 fsync、回读校验哈希，
  再将回执持久化到本机 `data/runtime/preselection_retention_pending*.json`。
- 单批最多 1,500 行、64 MiB；一次扫描最多 5,000 行；最多排队 128 批。
  进程 RSS 预算为 512 MiB，组装批次前计入预计复制量；探针不可用即停止。
- `preselection_retention_apply` 是唯一写 SSOT 的部分，只通过 resident
  托管写者轮转运行。每轮最多四批，批间检查 60 秒总时限，子进程硬超时
  120 秒；每批有独立短事务及 applied 回执。
- 删除前重新检查外置归档、每行哈希、计划身份、归档后新决策引用和每站
  最新计划。事务内删除保护触发器仅暂时解除，提交前重新创建。
- 外置盘缺失或中途断开时推迟归档/清理，生产观测与交易照常运行。
- 待删队列续跑按十分钟合同参与维护公平排序；队列清空后恢复六小时检查。
  生产到期时仍由既有调度器保障维护不能连续占两个写者槽。

## 核验与容量口径

检查 `data/weather_basket_yes_status.json` 的 `preselection_archive` 和
`hosted_sweeps.results.preselection_retention_apply`，以及 pending/applied
回执行数、归档哈希和已删 rowid 是否仍存在。
日常重启仅用 `scripts/restart_resident.sh "理由" --only weather_basket_yes`。

SSOT 当前 `auto_vacuum=0`。删行增加 freelist，供后续写入复用，不直接缩小
数据库文件；`freelist_count × page_size` 不能算作 macOS 新增可用空间。
不得在低磁盘余量下对生产库执行全量 VACUUM，也不得使用外置盘作为生产
数据库备份、回滚、切换或 readiness 依赖。

外置对象只保存已判定非关键的旧计划事实。库与回执身份改变、归档损坏或
决策历史倒退时须先查明原因，不能手工跳过检查或绕开托管写者删行。
