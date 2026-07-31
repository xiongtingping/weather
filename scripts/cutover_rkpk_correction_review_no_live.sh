#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "已退役：RKPK单站system维护入口禁止使用；请运行全站用户域target-only切换：" >&2
echo "bash \"$ROOT/scripts/cutover_bayes_basket_target_probability_evidence_no_live.sh\" --confirm-target-probability-evidence-production-no-live" >&2
exit 78
