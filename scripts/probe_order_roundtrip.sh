#!/bin/bash
# 下单链路往返探针 —— 挂一笔设计上不可成交的真实订单，验证全链路，然后撤销。
#
# 标的（2026-08-31 选定）：KSFO 8/31「60-61°F」YES，当时盘口 bid 0.01 / ask 0.04。
# 挂 BUY @0.001（低于最优买价 10 倍距离，post-only，要成交需整本买卖盘被砸穿）、
# 5 股（恰为交易所最小手）= 0.005 USDC 名义。已预先验证它能通过网关每道闸：
# 退役策略闸、只读位、暂停旗、抵押品闸（0.005 << 147.6）、最小手闸（5 = 最小手）、
# registry 定价规则（tick 0.001）。tick 在载荷里显式携带，覆盖网关的预热默认 0.01。
#
# 此脚本由操作者亲手运行 —— 向交易所提交订单是操作者的动作。
set -euo pipefail

GATEWAY="http://127.0.0.1:8765"
TOKEN="47806393369689836978311990762807637299707122389505717371880839859722782182268"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
IDEM="probe_roundtrip_${STAMP}"

echo "== 1/4 下单：BUY YES 5 股 @0.001（post-only GTC，名义 0.005 USDC）=="
PLACE=$(curl -s --max-time 30 -X POST "$GATEWAY/place-order" -H 'content-type: application/json' -d "{
  \"strategy_id\": \"bayes_basket_target_v1\",
  \"token_id\": \"$TOKEN\",
  \"side\": \"BUY\", \"order_type\": \"GTC\", \"post_only\": true,
  \"price\": 0.001, \"size\": 5, \"notional\": 0.005,
  \"tick_size\": 0.001, \"neg_risk\": true,
  \"idempotency_key\": \"$IDEM\", \"order_intent_id\": \"$IDEM\"
}")
echo "$PLACE" | python3 -m json.tool
ORDER_ID=$(echo "$PLACE" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('orderID') or d.get('exchange_order_id') or '')")
OK=$(echo "$PLACE" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('ok') or d.get('accepted') or d.get('pending'))")
if [ "$OK" != "True" ] && [ -z "$ORDER_ID" ]; then
  echo "!! 下单被拒或失败 —— 链路探针到此为止，上面的 error 字段就是结论。"
  exit 1
fi
echo; echo "== 2/4 交易所回执 order_id: ${ORDER_ID:-（异步，等回执）} =="
sleep 3

echo; echo "== 3/4 查订单状态 =="
curl -s --max-time 20 -X POST "$GATEWAY/order-status" -H 'content-type: application/json' \
  -d "{\"idempotency_key\": \"$IDEM\"}" | python3 -m json.tool
if [ -z "$ORDER_ID" ]; then
  ORDER_ID=$(curl -s --max-time 20 -X POST "$GATEWAY/order-status" -H 'content-type: application/json' \
    -d "{\"idempotency_key\": \"$IDEM\"}" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('orderID') or d.get('exchange_order_id') or '')")
fi

echo; echo "== 4/4 撤销 =="
if [ -n "$ORDER_ID" ]; then
  curl -s --max-time 30 -X POST "$GATEWAY/cancel-order" -H 'content-type: application/json' \
    -d "{\"order_id\": \"$ORDER_ID\", \"idempotency_key\": \"$IDEM\"}" | python3 -m json.tool
  echo; echo "== 验证已撤 =="
  sleep 2
  curl -s --max-time 20 -X POST "$GATEWAY/lookup-exchange-order" -H 'content-type: application/json' \
    -d "{\"order_id\": \"$ORDER_ID\"}" | python3 -m json.tool
else
  echo "!! 没拿到 order_id，无法定向撤销 —— 见下面的账户核验，它会把任何遗留挂单列出来"
fi

# 收尾核验：问账户，不问自己那一笔。
#
# 2026-08-31 的教训：探针 2 在 POST 之后抛了个 ReferenceError，订单被交易所收下
# 并挂了 18 分钟，而本机不知道它的 id、幂等库没有记录、unresolved 显示 0。按 id
# 查只能查到「我以为存在的东西」；只有问账户「你手上还有什么」才能发现孤儿单。
echo; echo "== 5/5 账户核验：交易所手上还有没有挂单 =="
OPEN=$(curl -s --max-time 30 -X POST "$GATEWAY/lookup-open-orders" -H 'content-type: application/json' -d '{}')
echo "$OPEN" | python3 -m json.tool
N=$(echo "$OPEN" | python3 -c "import json,sys;print(json.load(sys.stdin).get('total_open_orders','?'))")
if [ "$N" = "0" ]; then
  echo "✅ 账户零挂单 —— 本次探针未留下任何东西。"
else
  echo "⚠️  账户仍有 $N 笔挂单。若非你有意持有，逐笔撤销："
  echo "$OPEN" | python3 -c "
import json,sys
for o in json.load(sys.stdin).get('open_orders') or []:
    print(f\"   curl -s -X POST $GATEWAY/cancel-order -H 'content-type: application/json' -d '{{\\\"order_id\\\":\\\"{o['id']}\\\",\\\"idempotency_key\\\":\\\"manual_cancel\\\"}}'\")
"
fi
echo; echo "探针完成。idempotency_key = $IDEM（留档可查）"
