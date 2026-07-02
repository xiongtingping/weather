#!/usr/bin/env python3
"""Generate a cutover plan for replacing archive.db with a compact candidate.

This script is deliberately plan-only: it validates the candidate and prints the
exact commands, but it never stops services or moves database files.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "archive.db"
DEFAULT_REBUILD_DIR = ROOT / "data" / "archive_rebuilds"
DEFAULT_BACKUP_DIR = ROOT / "data" / "archive_backups"
DEFAULT_REPORT_DIR = ROOT / "data" / "reports"
USER_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"


WEATHER_USER_LABELS = [
    "com.weather.task_queue_worker",
    "com.weather.market_ws",
    "com.weather.market_refresh_1m",
    "com.weather.model_scan",
    "com.weather.model_scan_hourly",
    "com.weather.auto_trade_worker",
    "com.weather.auto_trade_entry_1m",
    "com.weather.auto_trade_watcher",
    "com.weather.trade_engine",
    "com.weather.trade_sync_loop",
    "com.weather.daily_scan",
    "com.weather.daily_trade_sms_summary",
    "com.weather.feedback_loop_hourly",
    "com.weather.q_calibrated_writeback_hourly",
    "com.weather.archive_db_governance_offpeak",
    "com.xiong.weather.ui",
]


def _stamp() -> str:
    return datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def _now_utc() -> str:
    return datetime.now(ZoneInfo("UTC")).replace(microsecond=0).isoformat()


def _fmt_bytes(value: int | float) -> str:
    num = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0 or unit == "TiB":
            return f"{num:.2f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024.0
    return f"{int(value or 0)}B"


def _run(cmd: list[str], *, timeout: int = 30) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "ok": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "timeout": True, "stdout": exc.stdout or "", "stderr": exc.stderr or ""}
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}


def _latest_candidate() -> Path:
    candidates = sorted(DEFAULT_REBUILD_DIR.glob("archive.compact_*.db"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit(f"candidate_missing dir={DEFAULT_REBUILD_DIR}")
    return candidates[-1]


def _sqlite_scalar(db: Path, sql: str, *, immutable: bool = True, timeout: int = 60) -> str:
    uri = f"file:{db}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    result = _run(["sqlite3", uri, sql], timeout=timeout)
    if not result.get("ok"):
        return ""
    return str(result.get("stdout") or "").strip()


def _candidate_checks(candidate: Path) -> dict[str, Any]:
    quick = _sqlite_scalar(candidate, "PRAGMA quick_check;", timeout=180)
    integrity = _sqlite_scalar(candidate, "PRAGMA integrity_check;", timeout=300)
    latest_missing = _sqlite_scalar(
        candidate,
        """
        SELECT COUNT(*)
        FROM event_fact_latest l
        LEFT JOIN event_fact_history h ON h.id = l.fact_id
        WHERE h.id IS NULL;
        """,
    )
    latest_compacted = _sqlite_scalar(
        candidate,
        """
        SELECT COUNT(*)
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE h.fact_json LIKE '%"history_compacted": true%';
        """,
    )
    ssot = _run(
        [
            "python3",
            "scripts/check_ssot_alignment.py",
            "--db",
            str(candidate),
            "--immutable",
        ],
        timeout=60,
    )
    ssot_payload: dict[str, Any] | None = None
    try:
        ssot_payload = json.loads(str(ssot.get("stdout") or "{}"))
    except json.JSONDecodeError:
        ssot_payload = None
    return {
        "quick_check": quick,
        "integrity_check": integrity,
        "latest_missing_fact_rows": int(latest_missing or 0),
        "latest_compacted_rows": int(latest_compacted or 0),
        "ssot_alignment": ssot_payload,
        "ssot_alignment_raw": {
            "returncode": ssot.get("returncode"),
            "stderr": str(ssot.get("stderr") or "")[:500],
        },
        "ok": quick == "ok"
        and integrity == "ok"
        and int(latest_missing or 0) == 0
        and int(latest_compacted or 0) == 0
        and bool(isinstance(ssot_payload, Mapping) and ssot_payload.get("ok")),
    }


def _parse_lsof(source: Path) -> list[dict[str, Any]]:
    result = _run(["lsof", "-F", "pcfn", str(source)], timeout=15)
    if not result.get("ok") and not result.get("stdout"):
        return []
    processes: dict[str, dict[str, Any]] = {}
    current_pid = ""
    for line in str(result.get("stdout") or "").splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            current_pid = value
            processes.setdefault(current_pid, {"pid": int(current_pid), "command": "", "files": []})
        elif tag == "c" and current_pid:
            processes[current_pid]["command"] = value
        elif tag == "n" and current_pid:
            processes[current_pid].setdefault("files", []).append(value)
    if not processes:
        return []
    ps = _run(["ps", "-p", ",".join(processes.keys()), "-o", "pid=,ppid=,command="], timeout=15)
    commands: dict[int, dict[str, Any]] = {}
    if ps.get("ok"):
        for raw in str(ps.get("stdout") or "").splitlines():
            parts = raw.strip().split(None, 2)
            if len(parts) >= 3:
                commands[int(parts[0])] = {"ppid": int(parts[1]), "command_line": parts[2]}
    out = []
    for pid_text, proc in sorted(processes.items(), key=lambda item: int(item[0])):
        pid = int(pid_text)
        proc.update(commands.get(pid, {}))
        out.append(proc)
    return out


def _launch_agent_info(label: str) -> dict[str, Any]:
    plist_path = USER_LAUNCH_AGENTS / f"{label}.plist"
    exists = plist_path.exists()
    program_text = ""
    if exists:
        try:
            payload = plistlib.loads(plist_path.read_bytes())
            args = payload.get("ProgramArguments")
            if isinstance(args, list):
                program_text = " ".join(str(item) for item in args)
        except Exception:
            program_text = ""
    state = _run(["launchctl", "print", f"gui/{_uid()}/{label}"], timeout=10)
    stdout = str(state.get("stdout") or "")
    loaded = bool(state.get("ok"))
    running = "state = running" in stdout
    pid = None
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pid = None
    return {
        "label": label,
        "plist": str(plist_path),
        "plist_exists": exists,
        "loaded": loaded,
        "running": running,
        "pid": pid,
        "program": program_text,
    }


def _uid() -> int:
    result = _run(["id", "-u"], timeout=5)
    try:
        return int(str(result.get("stdout") or "").strip())
    except Exception:
        return 501


def _commands(
    source: Path,
    candidate: Path,
    backup_dir: Path,
    stamp: str,
    launch_agents: list[dict[str, Any]],
) -> dict[str, list[str]]:
    uid = _uid()
    source_backup = backup_dir / f"archive.db.pre_cutover_{stamp}"
    wal_backup = backup_dir / f"archive.db-wal.pre_cutover_{stamp}"
    shm_backup = backup_dir / f"archive.db-shm.pre_cutover_{stamp}"
    loaded_labels = [
        str(agent.get("label"))
        for agent in launch_agents
        if isinstance(agent, Mapping) and bool(agent.get("loaded")) and str(agent.get("label") or "")
    ]
    stop_labels = loaded_labels
    start_labels = list(reversed(loaded_labels))
    return {
        "preflight": [
            f"python3 scripts/archive_db_cutover_plan.py --candidate {candidate}",
            f"python3 scripts/check_ssot_alignment.py --db {candidate} --immutable",
            f"sqlite3 'file:{candidate}?mode=ro&immutable=1' 'PRAGMA quick_check;'",
            f"lsof {source}",
        ],
        "stop_user_launchagents": [
            f"launchctl bootout gui/{uid}/{label} 2>/dev/null || true"
            for label in stop_labels
        ],
        "verify_quiescent": [
            f"lsof {source}",
            "pgrep -fl '/Users/xiong/weather/(scripts/market_ws_listener.py|scripts/hard_source_direct_poller.py|scan_weather_market.py|ui/server.py|scripts/task_queue_worker.py)' || true",
        ],
        "backup_and_swap": [
            f"mkdir -p {backup_dir}",
            f"sqlite3 {source} 'PRAGMA wal_checkpoint(TRUNCATE);'",
            f"cp -p {source} {source_backup}",
            f"test ! -e {source}-wal || cp -p {source}-wal {wal_backup}",
            f"test ! -e {source}-shm || cp -p {source}-shm {shm_backup}",
            f"cp -p {candidate} {source}.next",
            f"mv {source} {source}.swapped_{stamp}",
            f"mv {source}.next {source}",
        ],
        "restart_user_launchagents": [
            f"test ! -f {USER_LAUNCH_AGENTS / (label + '.plist')} || launchctl bootstrap gui/{uid} {USER_LAUNCH_AGENTS / (label + '.plist')} 2>/dev/null || launchctl kickstart -k gui/{uid}/{label}"
            for label in start_labels
        ],
        "post_checks": [
            "python3 scripts/check_ssot_alignment.py",
            f"sqlite3 'file:{source}?mode=ro' 'PRAGMA quick_check;'",
            "curl -fsS http://127.0.0.1:3002/api/ui_snapshot >/tmp/weather_ui_snapshot_after_cutover.json",
            "python3 scripts/weather_decision_core_audit.py",
        ],
        "rollback": [
            "launchctl bootout gui/{uid}/com.xiong.weather.ui 2>/dev/null || true".format(uid=uid),
            f"mv {source} {source}.failed_{stamp}",
            f"mv {source}.swapped_{stamp} {source}",
            f"test ! -e {wal_backup} || cp -p {wal_backup} {source}-wal",
            f"test ! -e {shm_backup} || cp -p {shm_backup} {source}-shm",
            f"launchctl bootstrap gui/{uid} {USER_LAUNCH_AGENTS / 'com.xiong.weather.ui.plist'} 2>/dev/null || launchctl kickstart -k gui/{uid}/com.xiong.weather.ui",
            "python3 scripts/check_ssot_alignment.py",
        ],
    }


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source_db).expanduser().resolve()
    candidate = Path(args.candidate).expanduser().resolve() if args.candidate else _latest_candidate().resolve()
    backup_dir = Path(args.backup_dir).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"source_db_missing path={source}")
    if not candidate.exists():
        raise SystemExit(f"candidate_missing path={candidate}")
    if source == candidate:
        raise SystemExit("candidate_must_not_equal_source")
    stamp = _stamp()
    checks = _candidate_checks(candidate)
    lsof_processes = _parse_lsof(source)
    launch_agents = [_launch_agent_info(label) for label in WEATHER_USER_LABELS]
    commands = _commands(source, candidate, backup_dir, stamp, launch_agents)
    source_size = source.stat().st_size
    candidate_size = candidate.stat().st_size
    return {
        "ok": bool(checks.get("ok")),
        "dry_run": True,
        "generated_at_utc": _now_utc(),
        "source_db": str(source),
        "candidate_db": str(candidate),
        "backup_dir": str(backup_dir),
        "source_db_size_bytes": source_size,
        "candidate_db_size_bytes": candidate_size,
        "estimated_release_bytes": max(0, source_size - candidate_size),
        "candidate_checks": checks,
        "active_source_db_processes": lsof_processes,
        "launch_agents": launch_agents,
        "commands": commands,
        "hard_gates": [
            "candidate_checks.ok must be true",
            "all weather writers/readers must be stopped through launchd",
            "lsof data/archive.db must be empty before backup_and_swap",
            "source archive.db and sidecars must be backed up before swap",
            "post_checks must pass before deleting any backup",
        ],
        "notes": [
            "This script does not execute cutover commands.",
            "system/com.polymarket.monitor.daemon is intentionally not stopped automatically here because the installed system daemon currently points outside /Users/xiong/weather; use lsof as the source-of-truth gate for this archive.db.",
            "Do not delete the old 29G archive until the new DB has run through a full live cycle.",
        ],
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_markdown(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checks = payload.get("candidate_checks", {}) if isinstance(payload.get("candidate_checks"), Mapping) else {}
    processes = payload.get("active_source_db_processes", [])
    agents = payload.get("launch_agents", [])
    commands = payload.get("commands", {}) if isinstance(payload.get("commands"), Mapping) else {}
    lines = [
        "# archive.db 小库切换预案",
        "",
        f"生成时间：{payload.get('generated_at_utc')}",
        f"源库：`{payload.get('source_db')}`",
        f"候选库：`{payload.get('candidate_db')}`",
        "",
        "## 当前状态",
        "",
        f"- 源库大小：{_fmt_bytes(int(payload.get('source_db_size_bytes') or 0))}",
        f"- 候选库大小：{_fmt_bytes(int(payload.get('candidate_db_size_bytes') or 0))}",
        f"- 预计释放：{_fmt_bytes(int(payload.get('estimated_release_bytes') or 0))}",
        f"- 候选库检查：`{checks.get('ok')}`",
        f"- quick_check：`{checks.get('quick_check')}`",
        f"- integrity_check：`{checks.get('integrity_check')}`",
        f"- latest_missing_fact_rows：`{checks.get('latest_missing_fact_rows')}`",
        f"- latest_compacted_rows：`{checks.get('latest_compacted_rows')}`",
        "",
        "## 当前持有 archive.db 的进程",
        "",
        "| PID | PPID | Command |",
        "| ---: | ---: | --- |",
    ]
    for proc in processes if isinstance(processes, list) else []:
        if not isinstance(proc, Mapping):
            continue
        lines.append(
            f"| {proc.get('pid')} | {proc.get('ppid', '')} | `{proc.get('command_line') or proc.get('command')}` |"
        )
    lines.extend(["", "## Weather LaunchAgents", "", "| Label | Loaded | Running | PID | Plist |", "| --- | --- | --- | ---: | --- |"])
    for agent in agents if isinstance(agents, list) else []:
        if not isinstance(agent, Mapping):
            continue
        lines.append(
            f"| `{agent.get('label')}` | `{agent.get('loaded')}` | `{agent.get('running')}` | {agent.get('pid') or ''} | `{agent.get('plist')}` |"
        )
    for section in ("preflight", "stop_user_launchagents", "verify_quiescent", "backup_and_swap", "restart_user_launchagents", "post_checks", "rollback"):
        values = commands.get(section, [])
        lines.extend(["", f"## {section}", "", "```bash"])
        lines.extend(str(item) for item in values)
        lines.append("```")
    lines.extend(
        [
            "",
            "## 硬门槛",
            "",
            "- 候选库检查必须为 true。",
            "- `lsof data/archive.db` 在切换前必须为空；否则中止。",
            "- 必须先备份原库和 sidecar，再做 `mv` 切换。",
            "- 切换后 `check_ssot_alignment.py`、`quick_check`、`/api/ui_snapshot` 必须通过。",
            "",
            "本预案不执行切换；真正替换仍需单独确认。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE))
    parser.add_argument("--candidate", default="", help="candidate DB path; defaults to newest data/archive_rebuilds/archive.compact_*.db")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--report-json", default="")
    parser.add_argument("--report-md", default="")
    parser.add_argument("--print-json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_plan(args)
    stamp = _stamp()
    report_json = Path(args.report_json) if args.report_json else DEFAULT_REPORT_DIR / f"archive_db_cutover_plan_{stamp}.json"
    report_md = Path(args.report_md) if args.report_md else DEFAULT_REPORT_DIR / f"archive_db_cutover_plan_{stamp}.md"
    payload["report_json"] = str(report_json)
    payload["report_md"] = str(report_md)
    _write_json(report_json, payload)
    _write_markdown(report_md, payload)
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                {
                    "ok": payload["ok"],
                    "report_json": str(report_json),
                    "report_md": str(report_md),
                    "candidate_db": payload["candidate_db"],
                    "candidate_size": _fmt_bytes(int(payload["candidate_db_size_bytes"])),
                    "estimated_release": _fmt_bytes(int(payload["estimated_release_bytes"])),
                    "active_source_db_processes": len(payload["active_source_db_processes"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
