#!/usr/bin/env python3
"""
监控market_tick_stale改善情况
"""

import sqlite3
from datetime import datetime, timedelta, timezone
import time

DB_PATH = "/Users/xiong/weather/data/archive.db"

def check_market_tick_stale():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")

    # 查看最近5分钟的决策
    cursor.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN reason_codes_json LIKE '%market_tick_stale%' THEN 1 ELSE 0 END) as stale_count,
            SUM(CASE WHEN will_order = 1 THEN 1 ELSE 0 END) as approved
        FROM auto_trade_decisions
        WHERE created_at >= ?
    """, (cutoff,))

    row = cursor.fetchone()
    total, stale_count, approved = row

    if total > 0:
        stale_pct = stale_count / total * 100
        approval_pct = approved / total * 100
    else:
        stale_pct = 0
        approval_pct = 0

    # 查看最新的市场数据更新时间
    cursor.execute("""
        SELECT MAX(created_at) FROM event_market_ticks
    """)
    last_update = cursor.fetchone()[0]

    conn.close()

    now = datetime.utcnow()
    print(f"\n{'='*80}")
    print(f"Market_tick_stale监控 - {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*80}")
    print(f"最近5分钟决策:")
    print(f"  总数: {total}")
    print(f"  Market_tick_stale: {stale_count} ({stale_pct:.1f}%)")
    print(f"  通过审批: {approved} ({approval_pct:.1f}%)")
    print(f"\n最新市场数据更新: {last_update}")

    if stale_pct < 5:
        print(f"\n✅ Market_tick_stale已显著改善！")
    elif stale_pct < 10:
        print(f"\n🟡 Market_tick_stale有所改善，继续观察")
    else:
        print(f"\n🔴 Market_tick_stale仍然较高")

if __name__ == "__main__":
    print("开始监控market_tick_stale改善情况...")
    print("每30秒检查一次，按Ctrl+C停止\n")

    try:
        while True:
            check_market_tick_stale()
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n\n监控已停止")
