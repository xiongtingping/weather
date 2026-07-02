import os
import scan_weather_market as s

slug = 'highest-temperature-in-atlanta-on-february-5-2026'
event_date = '2026-02-05'
outcome = '46-47°F'
side = 'YES'

with s.open_db() as conn:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT position_size, avg_price
        FROM edge_trades
        WHERE slug=? AND event_date=? AND outcome=? AND side=?
        """,
        (slug, event_date, outcome, side),
    )
    row = cur.fetchone()
    pos_size = float(row[0]) if row and row[0] is not None else 0.0
    avg_price = float(row[1]) if row and row[1] is not None else None

with s.open_db() as conn:
    cur = conn.cursor()
    cur.execute("SELECT MAX(run_date) FROM event_runs")
    row = cur.fetchone()
    run_date = str(row[0]) if row and row[0] else ""
    trade_token_map, _, _ = s.build_trade_sync_maps(conn, run_date)

token_id = ""
for meta in trade_token_map.values():
    if (
        meta.get('slug') == slug
        and meta.get('event_date') == event_date
        and meta.get('outcome') == outcome
        and meta.get('side') == side
    ):
        token_id = str(meta.get('token_id') or "")
        break

if not token_id:
    with s.open_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT token_id
            FROM edge_trade_fills
            WHERE slug=? AND event_date=? AND outcome=? AND outcome_side=?
            ORDER BY trade_time DESC
            LIMIT 1
            """,
            (slug, event_date, outcome, side),
        )
        row = cur.fetchone()
        if row and row[0]:
            token_id = str(row[0])

print('position', pos_size, 'avg_price', avg_price)
print('token_id', token_id)

if not token_id or pos_size <= 0:
    print('skip: missing token or position')
    raise SystemExit(0)

book = s.fetch_orderbook(token_id)
best_bid, best_ask, spread, bid_depth, ask_depth, best_bid_size, best_ask_size = s.best_bid_ask_and_depth(book, levels=5)
print('best_bid', best_bid, 'best_ask', best_ask, 'spread', spread, 'bid_depth_top5', bid_depth, 'best_bid_size', best_bid_size)

s.AUTO_TRADE_DRY_RUN = True
price = best_bid if best_bid is not None else best_ask
ok, payload = s._run_node_order(
    'place-order',
    token_id,
    'SELL',
    price=price,
    size=pos_size,
    order_type='GTC',
)
print('dry_run_ok', ok)
print('payload', payload)
