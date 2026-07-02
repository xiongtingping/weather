#!/usr/bin/env python3
"""
Legacy edge_realizable closed-trade calibration script.

Prefer scripts/edge_realizable_calibrate.py or scripts/auto_train_edge_realizable.sh
for current bucket-evidence calibration. This legacy script is kept for manual
closed-trade experiments and does not deploy by default.

功能：
1. 从历史订单数据统计成交率和收敛率
2. 按市场条件分桶
3. 生成动态校准文件
4. 支持增量更新和版本管理

使用：
  python3 train_edge_realizable_calibration.py [--days 90] [--min-samples 20] [--dry-run]
"""

import sys
import os
import sqlite3
import json
import argparse
from datetime import datetime
from collections import defaultdict
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from edge_realizable import edge_realizable_bucket_key, _default_bins
    USE_REAL_BUCKET_KEY = True
except ImportError:
    USE_REAL_BUCKET_KEY = False
    print("Warning: 无法导入 edge_realizable，使用简化的 bucket key")


def compute_bucket_key_simple(spread, depth_ratio, days_to_settle, edge_val):
    """简化的 bucket key 生成（fallback）"""
    spread_bins = [0.0, 0.01, 0.03, 0.06, 0.1, 0.2, 1.0]
    depth_bins = [0.0, 0.5, 1.0, 2.0, 4.0, 10.0, 100.0]
    days_bins = [0.0, 1.0, 2.0, 4.0, 8.0, 30.0, 9999.0]
    edge_bins = [0.0, 0.01, 0.03, 0.06, 0.1, 0.2, 1.0]

    def bin_index(val, bins):
        if val is None:
            return "u"
        for i, upper in enumerate(bins):
            if val <= upper:
                return str(i)
        return str(len(bins) - 1)

    s_idx = bin_index(spread, spread_bins)
    r_idx = bin_index(depth_ratio, depth_bins)
    d_idx = bin_index(days_to_settle, days_bins)
    e_idx = bin_index(edge_val, edge_bins)

    return f"d{d_idx}|s{s_idx}|r{r_idx}|e{e_idx}|x1|ot:ANY"


def get_bucket_key(spread, depth_ratio, days_to_settle, edge_val, order_type="ANY"):
    """获取 bucket key"""
    if USE_REAL_BUCKET_KEY:
        try:
            bins = _default_bins()
            return edge_realizable_bucket_key(
                days_to_settle=days_to_settle,
                spread=spread,
                depth_ratio=depth_ratio,
                edge_val=edge_val,
                delta_exit=None,
                order_type=order_type,
                bins=bins
            )
        except Exception as e:
            print(f"Warning: 使用真实 bucket key 失败: {e}")
            return compute_bucket_key_simple(spread, depth_ratio, days_to_settle, edge_val)
    else:
        return compute_bucket_key_simple(spread, depth_ratio, days_to_settle, edge_val)


def extract_trade_data(db_path, days=90):
    """从数据库提取交易数据"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 查询已结算的交易
    query = """
    SELECT
      et.slug,
      et.entry_at,
      et.state,
      et.entry_price,
      et.position_size,
      et.realized_pnl,
      et.last_fill_at,
      et.updated_at,
      et.side,
      et.outcome as trade_outcome,
      -- 从 auto_trade_decisions 获取市场条件
      json_extract(atd.details_json, '$.spread') as spread,
      json_extract(atd.details_json, '$.depth_ratio') as depth_ratio,
      json_extract(atd.details_json, '$.days_to_event') as days_to_settle,
      json_extract(atd.details_json, '$.exec_edge_entry') as exec_edge_entry,
      json_extract(atd.details_json, '$.order_type') as order_type
    FROM edge_trades et
    LEFT JOIN auto_trade_decisions atd
      ON et.slug = atd.slug
      AND et.event_date = json_extract(atd.details_json, '$.event_date')
      AND et.outcome = json_extract(atd.details_json, '$.outcome')
      AND et.side = json_extract(atd.details_json, '$.side')
    WHERE et.entry_at > datetime('now', '-{} days')
      AND et.state IN ('exit', 'settled', 'exited', 'stopped')
      AND et.realized_pnl IS NOT NULL
    ORDER BY et.entry_at DESC
    """.format(days)

    cursor = conn.execute(query)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return rows


def train_calibration(db_path, days=90, min_samples=20, verbose=True):
    """训练校准模型"""

    if verbose:
        print(f"开始训练校准模型...")
        print(f"  数据库: {db_path}")
        print(f"  时间窗口: {days} 天")
        print(f"  最小样本数: {min_samples}")

    # 1. 提取数据
    trades = extract_trade_data(db_path, days)

    if verbose:
        print(f"  提取交易数: {len(trades)}")

    if len(trades) < min_samples:
        print(f"Warning: 样本数不足 ({len(trades)} < {min_samples})，使用默认值")
        return {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "version": f"default_insufficient_data",
            "sample_n": len(trades),
            "p_fill_entry_default": 0.80,
            "p_converge_default": 0.85,
            "p_fill_exit_default": None,
            "p_fill_entry_buckets": {},
            "p_converge_buckets": {},
            "p_fill_exit_buckets": {},
            "legacy_na_reason": "insufficient_closed_trade_samples",
            "hold_cost_params": {
                "mode": "linear",
                "k_per_hour": 0
            }
        }

    # 2. 按 bucket 统计
    buckets = defaultdict(lambda: {
        "total": 0,
        "filled": 0,
        "profitable": 0
    })

    for trade in trades:
        spread = trade.get("spread")
        depth_ratio = trade.get("depth_ratio")
        days_to_settle = trade.get("days_to_settle")
        exec_edge = trade.get("exec_edge_entry")

        # 跳过缺少关键字段的记录
        if exec_edge is None:
            continue

        # 生成 bucket key
        key = get_bucket_key(
            spread=spread,
            depth_ratio=depth_ratio,
            days_to_settle=days_to_settle,
            edge_val=exec_edge,
            order_type=trade.get("order_type", "ANY")
        )

        # 统计
        buckets[key]["total"] += 1

        # 是否成交（有 realized_pnl 就说明成交了）
        if trade.get("realized_pnl") is not None:
            buckets[key]["filled"] += 1

            # 是否盈利
            if trade["realized_pnl"] > 0:
                buckets[key]["profitable"] += 1

    # 3. 计算概率
    p_fill_entry_buckets = {}
    p_converge_buckets = {}

    for key, stats in buckets.items():
        if stats["total"] >= min_samples:
            # p_fill_entry：成交率
            p_fill_entry_buckets[key] = {
                "p": round(stats["filled"] / stats["total"], 4),
                "hit": stats["filled"],
                "total": stats["total"]
            }

            # p_converge：盈利率（对于已成交订单）
            if stats["filled"] > 0:
                p_converge_buckets[key] = {
                    "p": round(stats["profitable"] / stats["filled"], 4),
                    "hit": stats["profitable"],
                    "total": stats["filled"]
                }

    # 4. 计算全局默认值
    total_count = sum(s["total"] for s in buckets.values())
    total_filled = sum(s["filled"] for s in buckets.values())
    total_profitable = sum(s["profitable"] for s in buckets.values())

    p_fill_entry_default = total_filled / total_count if total_count > 0 else 0.80
    p_converge_default = total_profitable / total_filled if total_filled > 0 else 0.85

    if verbose:
        print(f"\n训练结果:")
        print(f"  总样本数: {total_count}")
        print(f"  成交数: {total_filled} ({100*p_fill_entry_default:.1f}%)")
        print(f"  盈利数: {total_profitable} ({100*p_converge_default:.1f}%)")
        print(f"  buckets 数量: {len(p_fill_entry_buckets)}")

    # 5. 生成校准文件
    calibration = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "version": f"v1_{days}days_{datetime.utcnow().strftime('%Y%m%d')}",
        "sample_n": total_count,
        "training_window_days": days,
        "min_samples_per_bucket": min_samples,
        "p_fill_entry_default": round(p_fill_entry_default, 4),
        "p_converge_default": round(p_converge_default, 4),
        "p_fill_exit_default": None,
        "p_fill_entry_buckets": p_fill_entry_buckets,
        "p_converge_buckets": p_converge_buckets,
        "p_fill_exit_buckets": {},
        "legacy_na_reason": "closed_trade_training_has_no_exit_bucket_evidence",
        "hold_cost_params": {
            "mode": "linear",
            "k_per_hour": 0
        },
        "bins": _default_bins() if USE_REAL_BUCKET_KEY else None
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
    parser = argparse.ArgumentParser(description="训练 edge_realizable 校准模型")
    parser.add_argument("--days", type=int, default=90, help="训练数据时间窗口（天）")
    parser.add_argument("--min-samples", type=int, default=20, help="每个 bucket 的最小样本数")
    parser.add_argument("--db", default="/Users/xiong/weather/data/archive.db", help="数据库路径")
    parser.add_argument("--output", default="/Users/xiong/weather/data/edge_realizable_calibration.legacy_candidate.json", help="输出文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只训练不保存")
    parser.add_argument("--no-backup", action="store_true", help="不备份旧文件")
    parser.add_argument("--quiet", action="store_true", help="静默模式")

    args = parser.parse_args()

    # 训练
    calibration = train_calibration(
        db_path=args.db,
        days=args.days,
        min_samples=args.min_samples,
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
