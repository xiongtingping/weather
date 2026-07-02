#!/usr/bin/env python3
"""
分析signal_alpha/sigma优化效果
重点观察：
1. 调整后"被signal_lcb挡住的单子"后续真实edge实现率是否改善
2. NO占比是否可控
3. consensus_adjustment的实际效果
4. model_spread降低系数后的影响
"""

import sqlite3
import json
from datetime import datetime, timedelta
from typing import Dict, List, Any
import statistics

DB_PATH = "/Users/xiong/weather/data/archive.db"

def analyze_signal_blocked_cases(hours_back: int = 24):
    """分析被signal_lcb阻止的案例"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    # 查询被signal_lcb阻止的案例
    cursor.execute("""
        SELECT
            created_at,
            slug,
            outcome,
            reason_codes_json,
            details_json
        FROM auto_trade_decisions
        WHERE created_at >= ?
        AND reason_codes_json LIKE '%signal_lcb_below_min%'
        ORDER BY created_at DESC
    """, (cutoff_str,))

    blocked_cases = []
    for row in cursor.fetchall():
        try:
            details = json.loads(row['details_json']) if row['details_json'] else {}
            blocked_cases.append({
                'created_at': row['created_at'],
                'slug': row['slug'],
                'outcome': row['outcome'],
                'signal_alpha': details.get('edge_signal_alpha'),
                'signal_sigma': details.get('edge_signal_sigma'),
                'signal_lcb': details.get('edge_signal_lcb'),
                'q_edge': details.get('edge_signal_components_q_edge'),
                'model_spread': details.get('edge_signal_components_model_spread'),
                'consensus_adj': details.get('edge_signal_components_consensus_adjustment'),
                'exec_edge': details.get('edge_tradeability_profit_value'),
            })
        except Exception as e:
            continue

    conn.close()

    if not blocked_cases:
        print(f"⚠️  过去{hours_back}小时没有被signal_lcb阻止的案例")
        return

    print(f"\n{'='*80}")
    print(f"📊 被signal_lcb阻止的案例分析（过去{hours_back}小时）")
    print(f"{'='*80}\n")

    print(f"总数: {len(blocked_cases)}")

    # 统计signal_alpha分布
    alphas = [c['signal_alpha'] for c in blocked_cases if c['signal_alpha'] is not None]
    if alphas:
        print(f"\nSignal_alpha分布:")
        print(f"  平均值: {statistics.mean(alphas):.4f}")
        print(f"  中位数: {statistics.median(alphas):.4f}")
        print(f"  最小值: {min(alphas):.4f}")
        print(f"  最大值: {max(alphas):.4f}")
        print(f"  负值占比: {sum(1 for a in alphas if a < 0) / len(alphas) * 100:.1f}%")

    # 统计signal_sigma分布
    sigmas = [c['signal_sigma'] for c in blocked_cases if c['signal_sigma'] is not None]
    if sigmas:
        print(f"\nSignal_sigma分布:")
        print(f"  平均值: {statistics.mean(sigmas):.4f}")
        print(f"  中位数: {statistics.median(sigmas):.4f}")
        print(f"  最小值: {min(sigmas):.4f}")
        print(f"  最大值: {max(sigmas):.4f}")

    # 统计model_spread分布
    spreads = [c['model_spread'] for c in blocked_cases if c['model_spread'] is not None]
    if spreads:
        print(f"\nModel_spread分布:")
        print(f"  平均值: {statistics.mean(spreads):.4f}")
        print(f"  中位数: {statistics.median(spreads):.4f}")
        print(f"  >0.10占比: {sum(1 for s in spreads if s > 0.10) / len(spreads) * 100:.1f}%")

    # 统计consensus_adjustment
    consensus_adjs = [c['consensus_adj'] for c in blocked_cases if c['consensus_adj'] is not None]
    if consensus_adjs:
        print(f"\nConsensus_adjustment分布:")
        print(f"  平均值: {statistics.mean(consensus_adjs):.4f}")
        print(f"  >0占比: {sum(1 for a in consensus_adjs if a > 0) / len(consensus_adjs) * 100:.1f}%")

    # 统计exec_edge（潜在收益）
    exec_edges = [c['exec_edge'] for c in blocked_cases if c['exec_edge'] is not None]
    if exec_edges:
        print(f"\nExec_edge分布（被阻止但可能有收益的案例）:")
        print(f"  平均值: {statistics.mean(exec_edges):.4f}")
        print(f"  >0占比: {sum(1 for e in exec_edges if e > 0) / len(exec_edges) * 100:.1f}%")
        print(f"  >0.05占比: {sum(1 for e in exec_edges if e > 0.05) / len(exec_edges) * 100:.1f}%")

    # NO占比
    no_count = sum(1 for c in blocked_cases if c['outcome'] and 'NO' in c['outcome'].upper())
    print(f"\nNO占比: {no_count / len(blocked_cases) * 100:.1f}%")

    # 显示前10个高exec_edge但被阻止的案例
    high_edge_blocked = sorted(
        [c for c in blocked_cases if c['exec_edge'] and c['exec_edge'] > 0.03],
        key=lambda x: x['exec_edge'],
        reverse=True
    )[:10]

    if high_edge_blocked:
        print(f"\n🎯 高潜在收益但被signal_lcb阻止的案例（Top 10）:")
        print(f"{'Slug':<30} {'Outcome':<15} {'Exec_edge':>10} {'Signal_lcb':>10} {'Model_spread':>12}")
        print("-" * 90)
        for c in high_edge_blocked:
            model_spread_str = f"{c['model_spread']:.4f}" if c['model_spread'] is not None else "N/A"
            signal_lcb_str = f"{c['signal_lcb']:.4f}" if c['signal_lcb'] is not None else "N/A"
            print(f"{c['slug'][:30]:<30} {(c['outcome'] or 'N/A')[:15]:<15} "
                  f"{c['exec_edge']:>10.4f} {signal_lcb_str:>10} "
                  f"{model_spread_str:>12}")


def compare_before_after_optimization():
    """对比优化前后的效果"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 优化时间点（假设是现在）
    optimization_time = datetime.utcnow()
    before_start = optimization_time - timedelta(hours=24)

    print(f"\n{'='*80}")
    print(f"📈 优化前后对比分析")
    print(f"{'='*80}\n")

    # 查询优化前24小时的数据
    cursor.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN reason_codes_json LIKE '%signal_lcb_below_min%' THEN 1 ELSE 0 END) as signal_blocked,
            SUM(CASE WHEN will_order = 1 THEN 1 ELSE 0 END) as approved
        FROM auto_trade_decisions
        WHERE created_at >= ? AND created_at < ?
    """, (before_start.strftime("%Y-%m-%d %H:%M:%S"),
          optimization_time.strftime("%Y-%m-%d %H:%M:%S")))

    before = cursor.fetchone()

    print(f"优化前24小时:")
    print(f"  总决策数: {before['total']}")
    print(f"  被signal_lcb阻止: {before['signal_blocked']} ({before['signal_blocked']/before['total']*100:.1f}%)")
    print(f"  通过审批: {before['approved']} ({before['approved']/before['total']*100:.1f}%)")

    print(f"\n⏳ 等待优化后数据积累...")
    print(f"💡 建议24小时后再次运行此脚本查看对比效果")

    conn.close()


def main():
    print(f"\n🔍 Signal优化效果分析")
    print(f"分析时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n")

    # 分析被signal_lcb阻止的案例
    analyze_signal_blocked_cases(hours_back=24)

    # 对比优化前后
    compare_before_after_optimization()

    print(f"\n{'='*80}")
    print(f"✅ 分析完成")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    main()
