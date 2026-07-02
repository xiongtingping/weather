#!/usr/bin/env python3
"""
edge_realizable 校准模型简化训练脚本

由于 edge_trades 和 auto_trade_decisions 匹配率低，
使用简化方案：只训练全局默认值，不分桶

从 edge_trades 统计：
- p_fill_entry: 假设所有记录都已成交（realized_pnl 存在）
- p_converge: 盈利率（realized_pnl > 0 的比例）
"""

import sys
import sqlite3
import json
import argparse
from datetime import datetime
from pathlib import Path

def train_simple_calibration(db_path, days=90, verbose=True):
    """简化训练：只计算全局默认值"""

    if verbose:
        print(f"开始训练校准模型（简化版）...")
        print(f"  数据库: {db_path}")
        print(f"  时间窗口: {days} 天")

    conn = sqlite3.connect(db_path)

    # 统计所有已退出的交易
    query = """
    SELECT
      COUNT(*) as total,
      SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as profitable,
      SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as loss,
      AVG(realized_pnl) as avg_pnl,
      SUM(realized_pnl) as total_pnl
    FROM edge_trades
    WHERE state = 'exit'
      AND realized_pnl IS NOT NULL
    """

    cursor = conn.execute(query)
    row = cursor.fetchone()
    conn.close()

    total, profitable, loss, avg_pnl, total_pnl = row

    if verbose:
        print(f"\n统计结果:")
        print(f"  总交易数: {total}")
        print(f"  盈利数: {profitable} ({100*profitable/total if total > 0 else 0:.1f}%)")
        print(f"  亏损数: {loss} ({100*loss/total if total > 0 else 0:.1f}%)")
        print(f"  平均 PnL: {avg_pnl:.4f}")
        print(f"  总 PnL: {total_pnl:.4f}")

    # 计算概率
    # p_fill_entry: 假设 100%（因为所有记录都有 realized_pnl）
    p_fill_entry = 1.0

    # p_converge: 盈利率
    p_converge = profitable / total if total > 0 else 0.50

    # 保守调整：如果盈利率太低，使用默认值
    if p_converge < 0.30:
        if verbose:
            print(f"\nWarning: 盈利率过低 ({p_converge:.2f})，使用默认值 0.50")
        p_converge = 0.50

    if verbose:
        print(f"\n校准参数:")
        print(f"  p_fill_entry: {p_fill_entry:.4f} (假设100%成交)")
        print(f"  p_converge: {p_converge:.4f} (实际盈利率)")
        print(f"  p_realize: {p_fill_entry * p_converge * p_fill_entry:.4f}")

    # 生成校准文件
    calibration = {
        "generated_at": datetime.now().isoformat() + "Z",
        "version": f"simple_v1_{days}days_{datetime.now().strftime('%Y%m%d')}",
        "sample_n": total,
        "training_window_days": days,
        "training_method": "simple_global_only",
        "p_fill_entry_default": round(p_fill_entry, 4),
        "p_converge_default": round(p_converge, 4),
        "p_fill_exit_default": round(p_fill_entry, 4),
        "p_fill_entry_buckets": {},
        "p_converge_buckets": {},
        "p_fill_exit_buckets": {},
        "hold_cost_params": {
            "mode": "linear",
            "k_per_hour": 0
        },
        "stats": {
            "total_trades": total,
            "profitable_trades": profitable,
            "loss_trades": loss,
            "avg_pnl": round(avg_pnl, 6) if avg_pnl else 0,
            "total_pnl": round(total_pnl, 6) if total_pnl else 0
        }
    }

    return calibration


def save_calibration(calibration, output_path, backup=True):
    """保存校准文件"""
    output_path = Path(output_path)

    # 备份旧文件
    if backup and output_path.exists():
        backup_path = output_path.with_suffix(f".{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak")
        output_path.rename(backup_path)
        print(f"  旧文件已备份: {backup_path}")

    # 保存新文件
    with open(output_path, "w") as f:
        json.dump(calibration, f, indent=2)

    print(f"✓ 校准文件已保存: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="训练 edge_realizable 校准模型（简化版）")
    parser.add_argument("--days", type=int, default=90, help="训练数据时间窗口（天）")
    parser.add_argument("--db", default="/Users/xiong/weather/data/archive.db", help="数据库路径")
    parser.add_argument("--output", default="/Users/xiong/weather/data/edge_realizable_calibration.json", help="输出文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只训练不保存")
    parser.add_argument("--no-backup", action="store_true", help="不备份旧文件")
    parser.add_argument("--quiet", action="store_true", help="静默模式")

    args = parser.parse_args()

    # 训练
    calibration = train_simple_calibration(
        db_path=args.db,
        days=args.days,
        verbose=not args.quiet
    )

    # 保存
    if not args.dry_run:
        save_calibration(
            calibration,
            output_path=args.output,
            backup=not args.no_backup
        )
    else:
        print("\n[DRY RUN] 不保存文件")
        print(json.dumps(calibration, indent=2))


if __name__ == "__main__":
    main()
