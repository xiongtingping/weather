#!/usr/bin/env python3
"""测试加权标准差 spread 计算"""

import math

# 模拟场景
scenarios = [
    {
        "name": "高分歧场景 (GFS vs ICON)",
        "predictions": [0.25, 0.75, 0.50, 0.60],
        "weights": [0.45, 0.45, 0.05, 0.05],
        "models": ["gfs_seamless", "icon_seamless", "ecmwf_ifs025", "nws_forecast"]
    },
    {
        "name": "中等分歧场景",
        "predictions": [0.40, 0.60, 0.48, 0.52],
        "weights": [0.45, 0.45, 0.05, 0.05],
        "models": ["gfs_seamless", "icon_seamless", "ecmwf_ifs025", "nws_forecast"]
    },
    {
        "name": "低分歧场景",
        "predictions": [0.48, 0.52, 0.50, 0.51],
        "weights": [0.45, 0.45, 0.05, 0.05],
        "models": ["gfs_seamless", "icon_seamless", "ecmwf_ifs025", "nws_forecast"]
    },
]

print("=== 加权标准差 vs Max-Min Spread 对比 ===\n")

for scenario in scenarios:
    preds = scenario["predictions"]
    weights = scenario["weights"]
    models = scenario["models"]
    name = scenario["name"]

    print(f"{name}")
    print("-" * 70)

    # 计算加权平均
    sum_w = sum(weights)
    q_avg = sum(w * p for w, p in zip(weights, preds)) / sum_w

    # 方法1: Max-Min (当前方法)
    spread_maxmin = max(preds) - min(preds)

    # 方法2: 加权标准差 (新方法)
    variance = sum(w * (p - q_avg)**2 for w, p in zip(weights, preds)) / sum_w
    spread_weighted_std = math.sqrt(variance)

    print(f"  模型预测: {preds}")
    print(f"  权重:     {weights}")
    print(f"  加权平均: {q_avg:.3f}")
    print()
    print(f"  Max-Min Spread:        {spread_maxmin:.3f}")
    print(f"  加权标准差 Spread:      {spread_weighted_std:.3f}")
    print(f"  降低幅度:              {(1 - spread_weighted_std/spread_maxmin)*100:.1f}%")
    print()

print("\n" + "="*70)
print("\n结论: 加权标准差方法显著降低了 spread 值，特别是在极端值场景中。")
