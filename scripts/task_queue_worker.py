#!/usr/bin/env python3
"""
任务队列 Worker
负责从队列中获取任务并执行
"""

import os
import sys
import time
import json
import sqlite3
import subprocess
import signal
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from archive_db_write_queue import connect_archive_db_rw  # noqa: E402

DB_PATH = ROOT_DIR / "data" / "archive.db"
LOG_PATH = ROOT_DIR / "data" / "task_queue_worker.log"


def _connect_db(owner):
    return connect_archive_db_rw(
        DB_PATH,
        owner=f"task_queue_worker.{owner}",
        timeout_sec=10.0,
        busy_timeout_ms=10000,
        row_factory=True,
        # A queue claim must not consume the AMSC source-to-posterior window
        # while an unrelated writer owns the global gate. The durable task
        # remains pending and is retried on the next fast poll.
        queue_wait_sec=TASK_QUEUE_DB_WRITE_WAIT_SEC,
    )

def _env_float(name, default):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    return max(0.1, value)

DEFAULT_IDLE_SLEEP_SEC = _env_float("TASK_QUEUE_IDLE_SLEEP_SEC", "1")
TASK_QUEUE_DB_WRITE_WAIT_SEC = _env_float("TASK_QUEUE_DB_WRITE_WAIT_SEC", "0.25")
MISSING_TABLE_RETRY_SEC = int(os.environ.get("TASK_QUEUE_MISSING_TABLE_RETRY_SEC", "300"))


def _compact_task_failure_detail(stdout: str, stderr: str) -> str:
    """Preserve a structured task blocker instead of recording only exit 1."""

    stream = str(stderr or "").strip()
    if stream:
        return stream[:500]
    output = str(stdout or "").strip()
    if not output:
        return ""
    try:
        result = json.loads(output)
    except (TypeError, ValueError):
        return output[:500]
    if not isinstance(result, dict):
        return output[:500]
    reason = str(result.get("block_reason") or result.get("error") or "").strip()
    if reason:
        return f"task_result:{reason}"[:500]
    return json.dumps(
        {key: result.get(key) for key in ("ok", "blocked", "skipped", "state") if key in result},
        ensure_ascii=False,
        separators=(",", ":"),
    )[:500]

# 任务类型到脚本的映射
TASK_SCRIPTS = {
    "model_scan": "scripts/model_scan.sh",
    "model_scan_hourly": "scripts/model_scan_hourly.sh",
    "daily_scan": "scripts/daily_scan_and_settlement.sh",
    "market_refresh": "scripts/market_refresh_1m.sh",
    "market_refresh_urgent": "scripts/market_refresh_urgent.sh",
    "feedback_loop": "scripts/feedback_loop_hourly.sh",
    "amsc_posterior_materialize": "scripts/materialize_amsc_posterior_task.py",
}

class TaskQueueWorker:
    def __init__(self):
        self.running = True
        self.current_task_id = None
        self.current_process = None
        self.queue_table_missing = False
        self.queue_probe_after = 0.0
        self.queue_missing_logged = False

        # 注册信号处理
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)

    def handle_signal(self, signum, frame):
        """处理终止信号"""
        self.log(f"Received signal {signum}, shutting down...")
        self.running = False

        if self.current_process:
            self.log(f"Terminating current task {self.current_task_id}...")
            self.current_process.terminate()
            try:
                self.current_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.current_process.kill()

    def log(self, message):
        """写入日志"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {message}\n"
        print(log_line, end="")

        try:
            with open(LOG_PATH, "a") as f:
                f.write(log_line)
        except Exception as e:
            print(f"Failed to write log: {e}")

    def _task_queue_table_exists(self, conn):
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_queue' LIMIT 1"
        )
        return cur.fetchone() is not None

    def _mark_queue_missing(self, reason):
        self.queue_table_missing = True
        self.queue_probe_after = time.time() + max(30, MISSING_TABLE_RETRY_SEC)
        if not self.queue_missing_logged:
            self.log(
                f"task_queue unavailable ({reason}); downgrade polling for {max(30, MISSING_TABLE_RETRY_SEC)}s"
            )
            self.queue_missing_logged = True

    def _recover_queue_missing(self):
        if self.queue_table_missing:
            self.log("task_queue recovered, resume normal polling")
        self.queue_table_missing = False
        self.queue_probe_after = 0.0
        self.queue_missing_logged = False

    def get_next_task(self):
        """从队列获取下一个待执行的任务"""
        if self.queue_table_missing and time.time() < self.queue_probe_after:
            return None
        conn = None
        try:
            with _connect_db("get_next_task") as conn:
                cur = conn.cursor()
                if not self._task_queue_table_exists(conn):
                    self._mark_queue_missing("no such table: task_queue")
                    return None
                self._recover_queue_missing()

                # 获取优先级最高的待执行任务
                cur.execute("""
                    SELECT * FROM task_queue
                    WHERE status = 'pending'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                """)

                task = cur.fetchone()

                if task:
                    task_id = task["id"]
                    # 标记为运行中
                    cur.execute("""
                        UPDATE task_queue
                        SET status = 'running',
                            started_at = datetime('now'),
                            worker_pid = ?
                        WHERE id = ?
                    """, (os.getpid(), task_id))
                    conn.commit()

                    self.log(f"Task #{task_id} ({task['task_type']}) acquired")
                    return dict(task)

                return None

        except sqlite3.OperationalError as e:
            err_text = str(e).strip()
            if "no such table" in err_text.lower() and "task_queue" in err_text.lower():
                self._mark_queue_missing(err_text)
            else:
                self.log(f"Error getting next task: {e}")
            return None
        except Exception as e:
            self.log(f"Error getting next task: {e}")
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def execute_task(self, task):
        """执行任务"""
        task_id = task["id"]
        task_type = task["task_type"]

        self.current_task_id = task_id

        # 获取任务脚本
        script_path = TASK_SCRIPTS.get(task_type)
        if not script_path:
            self.mark_task_failed(task_id, f"Unknown task type: {task_type}")
            return

        script_full_path = ROOT_DIR / script_path
        if not script_full_path.exists():
            self.mark_task_failed(task_id, f"Script not found: {script_path}")
            return

        self.log(f"Task #{task_id} executing: {script_path}")

        try:
            env = os.environ.copy()
            env["TASK_QUEUE_ID"] = str(task_id)
            if task.get("payload_json") not in (None, ""):
                env["TASK_PAYLOAD_JSON"] = str(task.get("payload_json") or "")
            command = ["/bin/bash", str(script_full_path)]
            if script_full_path.suffix == ".py":
                command = [sys.executable, str(script_full_path)]

            # 执行脚本
            self.current_process = subprocess.Popen(
                command,
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            stdout, stderr = self.current_process.communicate()
            exit_code = self.current_process.returncode

            self.current_process = None

            if exit_code == 0:
                self.mark_task_completed(task_id)
                self.log(f"Task #{task_id} completed successfully")
            else:
                error_msg = f"Exit code {exit_code}"
                detail = _compact_task_failure_detail(stdout, stderr)
                if detail:
                    error_msg += f": {detail}"
                self.mark_task_failed(task_id, error_msg)
                self.log(f"Task #{task_id} failed: {error_msg}")

        except Exception as e:
            self.mark_task_failed(task_id, str(e))
            self.log(f"Task #{task_id} error: {e}")

        finally:
            self.current_task_id = None
            self.current_process = None

    def mark_task_completed(self, task_id):
        """标记任务为已完成"""
        try:
            with _connect_db("mark_task_completed") as conn:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE task_queue
                    SET status = 'completed',
                        completed_at = datetime('now'),
                        worker_pid = NULL
                    WHERE id = ?
                """, (task_id,))
                conn.commit()
        except Exception as e:
            self.log(f"Error marking task {task_id} as completed: {e}")

    def mark_task_failed(self, task_id, error):
        """标记任务为失败"""
        try:
            with _connect_db("mark_task_failed") as conn:
                cur = conn.cursor()

                # 获取当前重试次数
                cur.execute("SELECT retry_count, max_retries FROM task_queue WHERE id = ?", (task_id,))
                row = cur.fetchone()

                if row:
                    retry_count = row["retry_count"]
                    max_retries = row["max_retries"]

                    if retry_count < max_retries:
                        # 重试
                        cur.execute("""
                            UPDATE task_queue
                            SET status = 'pending',
                                retry_count = retry_count + 1,
                                error = ?,
                                worker_pid = NULL
                            WHERE id = ?
                        """, (error, task_id))
                        self.log(f"Task #{task_id} will retry ({retry_count + 1}/{max_retries})")
                    else:
                        # 失败
                        cur.execute("""
                            UPDATE task_queue
                            SET status = 'failed',
                                completed_at = datetime('now'),
                                error = ?,
                                worker_pid = NULL
                            WHERE id = ?
                        """, (error, task_id))
                        self.log(f"Task #{task_id} failed permanently after {max_retries} retries")

                conn.commit()
        except Exception as e:
            self.log(f"Error marking task {task_id} as failed: {e}")

    def cleanup_stale_tasks(self):
        """清理僵尸任务（worker 已死但任务仍标记为 running）"""
        try:
            with _connect_db("cleanup_stale_tasks") as conn:
                cur = conn.cursor()
                if not self._task_queue_table_exists(conn):
                    self._mark_queue_missing("no such table: task_queue")
                    return

                cur.execute("""
                    SELECT id, worker_pid FROM task_queue
                    WHERE status = 'running'
                """)

                for row in cur.fetchall():
                    task_id = row["id"]
                    worker_pid = row["worker_pid"]

                    if worker_pid:
                        try:
                            os.kill(worker_pid, 0)  # 检查进程是否存在
                        except OSError:
                            # 进程不存在，重置任务
                            self.log(f"Task #{task_id} worker {worker_pid} is dead, resetting...")
                            cur.execute("""
                                UPDATE task_queue
                                SET status = 'pending',
                                    worker_pid = NULL,
                                    error = 'Worker died unexpectedly'
                                WHERE id = ?
                            """, (task_id,))

                conn.commit()
        except Exception as e:
            self.log(f"Error cleaning up stale tasks: {e}")

    def run(self):
        """主循环"""
        self.log("Task queue worker started")

        # 启动时清理僵尸任务
        self.cleanup_stale_tasks()

        while self.running:
            try:
                # 获取下一个任务
                task = self.get_next_task()

                if task:
                    # 执行任务
                    self.execute_task(task)
                else:
                    # 没有任务，等待
                    sleep_sec = DEFAULT_IDLE_SLEEP_SEC
                    if self.queue_table_missing:
                        sleep_sec = min(
                            60,
                            max(
                                DEFAULT_IDLE_SLEEP_SEC,
                                int(max(0, self.queue_probe_after - time.time())),
                            ),
                        )
                    time.sleep(sleep_sec)

            except KeyboardInterrupt:
                self.log("Interrupted by user")
                break
            except Exception as e:
                self.log(f"Unexpected error: {e}")
                time.sleep(DEFAULT_IDLE_SLEEP_SEC)

        self.log("Task queue worker stopped")

if __name__ == "__main__":
    worker = TaskQueueWorker()
    worker.run()
