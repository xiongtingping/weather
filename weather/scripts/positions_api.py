#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT_DIR / "data" / "archive.db"


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    conn.row_factory = sqlite3.Row
    return conn


def _load_latest_run_map(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT slug, event_date, created_at, payload_json
        FROM event_runs
        WHERE payload_json IS NOT NULL
        ORDER BY created_at ASC
        """
    )
    latest: Dict[Tuple[str, str], Dict] = {}
    for row in cur.fetchall():
        slug = str(row["slug"] or "")
        event_date = str(row["event_date"] or "")
        if not slug or not event_date:
            continue
        key = (slug, event_date)
        prev = latest.get(key)
        if prev is None or str(row["created_at"] or "") > str(prev.get("created_at") or ""):
            latest[key] = {
                "created_at": str(row["created_at"] or ""),
                "payload_json": row["payload_json"],
            }
    return latest


def _build_token_map(latest_runs: Dict[Tuple[str, str], Dict]) -> Dict[Tuple[str, str, str], Dict[str, str]]:
    token_map: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    for (slug, event_date), entry in latest_runs.items():
        payload_raw = entry.get("payload_json")
        if not payload_raw:
            continue
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            continue
        rows = payload.get("rows") or []
        for row in rows:
            outcome = str(row.get("outcome") or "")
            if not outcome:
                continue
            yes_token = row.get("yes_token")
            no_token = row.get("no_token")
            token_map[(slug, event_date, outcome)] = {
                "YES": str(yes_token or ""),
                "NO": str(no_token or ""),
            }
    return token_map


def _load_positions(conn: sqlite3.Connection) -> List[Dict[str, Optional[float]]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT slug, event_date, outcome, side, position_size, avg_price
        FROM edge_trades
        WHERE position_size IS NOT NULL AND position_size > 0
        """
    )
    positions = []
    for row in cur.fetchall():
        positions.append(
            {
                "slug": str(row["slug"] or ""),
                "event_date": str(row["event_date"] or ""),
                "outcome": str(row["outcome"] or ""),
                "side": str(row["side"] or "").upper(),
                "size": float(row["position_size"] or 0.0),
                "avg_price": float(row["avg_price"]) if row["avg_price"] is not None else None,
            }
        )
    return positions


def build_positions_payload(db_path: Path) -> List[Dict[str, Optional[float]]]:
    conn = open_db(db_path)
    try:
        latest_runs = _load_latest_run_map(conn)
        token_map = _build_token_map(latest_runs)
        positions = _load_positions(conn)
    finally:
        conn.close()
    payload: List[Dict[str, Optional[float]]] = []
    for pos in positions:
        key = (pos["slug"], pos["event_date"], pos["outcome"])
        token_entry = token_map.get(key) or {}
        token_id = token_entry.get(pos["side"] or "") or ""
        if not token_id:
            continue
        payload.append(
            {
                "token_id": token_id,
                "size": pos["size"],
                "avg_price": pos.get("avg_price"),
            }
        )
    return payload


class PositionsHandler(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB

    def _write_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/", "/positions"):
            self._write_json({"error": "not_found"}, status=404)
            return
        try:
            payload = build_positions_payload(self.db_path)
        except Exception as exc:
            self._write_json({"error": str(exc)}, status=500)
            return
        self._write_json(payload)

    def log_message(self, _format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve positions from archive.db as JSON.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to archive.db")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    parser.add_argument("--port", type=int, default=8787, help="Port to bind")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    handler = PositionsHandler
    handler.db_path = db_path
    server = HTTPServer((args.host, args.port), handler)
    print(f"Positions API listening on http://{args.host}:{args.port}/positions")
    server.serve_forever()


if __name__ == "__main__":
    main()
