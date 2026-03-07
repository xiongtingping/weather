#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Dict, Optional


def _ensure_eval_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS calibration_eval_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_version TEXT,
            bundle_path TEXT,
            sample_n INTEGER,
            label_n INTEGER,
            ece REAL,
            brier REAL,
            logloss REAL,
            psi REAL,
            roi_mean REAL,
            roi_lcb REAL,
            champion INTEGER,
            promoted INTEGER,
            status TEXT,
            notes_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _latest_eval(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, status, champion, promoted, notes_json
        FROM calibration_eval_runs
        ORDER BY id DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": int(row[0] or 0),
        "status": str(row[1] or ""),
        "champion": int(row[2] or 0),
        "promoted": int(row[3] or 0),
        "notes_json": str(row[4] or ""),
    }


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote candidate q calibration bundle to champion.")
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "data" / "archive.db"))
    parser.add_argument("--candidate", default=str(Path(__file__).resolve().parents[1] / "data" / "q_calibration_bundle.candidate.json"))
    parser.add_argument("--target", default=str(Path(__file__).resolve().parents[1] / "data" / "q_calibration_bundle.json"))
    parser.add_argument("--force", action="store_true", default=False)
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    cand_path = Path(args.candidate).expanduser().resolve()
    target_path = Path(args.target).expanduser().resolve()
    if not cand_path.exists():
        print(json.dumps({"ok": False, "error": f"candidate_missing:{cand_path}"}, ensure_ascii=False))
        raise SystemExit(1)

    allow = bool(args.force)
    eval_id = None
    status_text = "force"

    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        _ensure_eval_table(conn)
        latest = _latest_eval(conn)
        if latest:
            eval_id = int(latest.get("id") or 0)
            status_text = str(latest.get("status") or "")
            if not allow:
                allow = status_text == "promote_recommended" or int(latest.get("champion") or 0) == 1
            if allow:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE calibration_eval_runs
                    SET promoted = 1, status = ?
                    WHERE id = ?
                    """,
                    ("promoted", eval_id),
                )
                conn.commit()
        conn.close()

    if not allow:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "promotion_blocked",
                    "reason": status_text or "no_recommended_eval",
                    "eval_id": eval_id,
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = target_path.with_suffix(target_path.suffix + ".bak")
    if target_path.exists():
        shutil.copy2(target_path, backup_path)
    shutil.copy2(cand_path, target_path)

    payload = _load_json(target_path)
    out = {
        "ok": True,
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_path": str(target_path),
        "backup_path": str(backup_path) if backup_path.exists() else "",
        "version": str(payload.get("version") or ""),
        "eval_id": eval_id,
        "forced": bool(args.force),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
