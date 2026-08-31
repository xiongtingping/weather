from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from weather_basket_yes_schema import apply_weather_basket_yes_schema


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "scripts" / "clob_order_service.mjs"


def _minimum_lot_registry(db_path: Path) -> None:
    """A registry row stating the exchange's minimum lot for token 123456."""
    conn = sqlite3.connect(db_path)
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """INSERT INTO reported_trajectory_market_registry(
             slug,event_date,outcome,city_slug,station_id,settlement_timezone,
             settlement_unit,source_lane,source_eligible,source_block_reason,
             resolution_url,rules_sha256,yes_token,no_token,market_active,
             market_accepting_orders,registry_json,refreshed_at
           ) VALUES('slug','2099-08-05','82-83°F','atlanta','KATL','America/New_York',
                    'F','awc',1,NULL,'url',?,'123456','654321',1,1,?,
                    '2099-08-05T00:00:00Z')""",
        ("a" * 64, json.dumps({"order_min_size": 5, "order_price_min_tick_size": 0.01})),
    )
    conn.commit()
    conn.close()



def test_sqlite_idempotency_store_survives_restart_and_keeps_unresolved(tmp_path: Path) -> None:
    db_path = tmp_path / "clob-idempotency.sqlite"
    script = f"""
      import {{ DurableIdempotencyStore }} from {json.dumps(SERVICE.as_uri())};
      const dbPath = {json.dumps(str(db_path))};
      const first = new DurableIdempotencyStore(dbPath, 7200000, 512);
      first.put('idem-1', 'intent-1', 'pending_submit', {{token_id:'t'}}, {{pending:true}});
      const second = new DurableIdempotencyStore(dbPath, 7200000, 512);
      const pending = second.load().find((row) => row.idempotency_key === 'idem-1');
      if (!pending || pending.state !== 'pending_submit' || pending.order_intent_id !== 'intent-1') {{
        throw new Error('pending intent not durable');
      }}
      second.put('idem-1', 'intent-1', 'exchange_accepted', {{token_id:'t'}}, {{ok:true,orderID:'o1'}});
      const third = new DurableIdempotencyStore(dbPath, 7200000, 512);
      const accepted = third.load().find((row) => row.idempotency_key === 'idem-1');
      if (!accepted || accepted.state !== 'exchange_accepted' || accepted.response.orderID !== 'o1') {{
        throw new Error('terminal response not durable');
      }}
      console.log(JSON.stringify({{ok:true,state:accepted.state}}));
    """

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "ok": True,
        "state": "exchange_accepted",
    }


def test_signed_order_hash_allows_exact_exchange_reconcile() -> None:
    script = f"""
      import {{ ClobOrderService, expectedOrderIdForSignedOrder }} from {json.dumps(SERVICE.as_uri())};
      const zero = '0x0000000000000000000000000000000000000000';
      const bytes32 = `0x${{'00'.repeat(32)}}`;
      const signed = {{
        salt: '123', maker: zero, signer: zero, tokenId: '99',
        makerAmount: '1000000', takerAmount: '2000000', side: 'BUY',
        signatureType: 0, timestamp: '1720000000', metadata: bytes32,
        builder: bytes32, expiration: '0', signature: '0x00',
      }};
      const expected = expectedOrderIdForSignedOrder(signed, {{chainId: 137, negRisk: true}});
      if (!/^0x[0-9a-f]{{64}}$/.test(expected)) throw new Error(`bad hash: ${{expected}}`);

      const writes = [];
      const service = new ClobOrderService();
      service.idemStore = {{
        put: (...args) => writes.push(args),
        cleanup: () => {{}},
        delete: () => {{}},
      }};
      service.client = {{ getOrder: async (orderID) => ({{id: orderID, status: 'LIVE'}}) }};
      service.unresolved.set('idem-2', {{
        state: 'reconcile_required', order_intent_id: 'intent-2', created_ms: Date.now(),
        request: {{token_id: '99'}}, response: {{expected_order_id: expected}},
      }});
      const result = await service.reconcileOrder('idem-2');
      if (!result.ok || result.orderID !== expected || !result.reconciled_from_exchange) {{
        throw new Error(`reconcile failed: ${{JSON.stringify(result)}}`);
      }}
      if (service.unresolved.has('idem-2') || !service.idem.has('idem-2')) {{
        throw new Error('reconciled intent did not become terminal');
      }}
      if (!writes.some((row) => row[2] === 'exchange_accepted')) {{
        throw new Error('reconciled terminal state not persisted');
      }}
      console.log(JSON.stringify({{ok:true,orderID:result.orderID}}));
    """

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["orderID"].startswith("0x")


def test_submitted_unconfirmed_reconcile_stays_unresolved() -> None:
    script = f"""
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const writes = [];
      const service = new ClobOrderService();
      service.idemStore = {{
        put: (...args) => writes.push(args),
        cleanup: () => {{}},
        delete: () => {{}},
      }};
      service.client = {{
        getOrder: async (orderID) => ({{
          id: orderID,
          status: 'submitted_unconfirmed',
          original_size: null,
          size_matched: null,
        }}),
      }};
      service.unresolved.set('idem-unconfirmed', {{
        state: 'reconcile_required', order_intent_id: 'intent-unconfirmed', created_ms: Date.now(),
        request: {{token_id:'99'}},
        response: {{expected_order_id:'order-unconfirmed'}},
      }});
      const result = await service.reconcileOrder('idem-unconfirmed');
      if (!result.reconcile_required || result.reconcile_reason !== 'exchange_order_state_not_terminal') {{
        throw new Error(`unconfirmed order was accepted: ${{JSON.stringify(result)}}`);
      }}
      if (!service.unresolved.has('idem-unconfirmed') || service.idem.has('idem-unconfirmed')) {{
        throw new Error('unconfirmed intent left unresolved storage');
      }}
      if (writes.some((row) => row[2] === 'exchange_accepted')) {{
        throw new Error('unconfirmed intent was persisted as exchange_accepted');
      }}
      console.log(JSON.stringify({{ok:true,status:result.status}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "ok": True, "status": "submitted_unconfirmed",
    }


def test_restart_demotes_persisted_unconfirmed_exchange_acceptance(tmp_path: Path) -> None:
    db_path = tmp_path / "clob-unconfirmed.sqlite"
    script = f"""
      import {{ DurableIdempotencyStore, ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      process.env.PM_CLOB_ORDER_IDEMPOTENCY_DB = {json.dumps(str(db_path))};
      const store = new DurableIdempotencyStore({json.dumps(str(db_path))}, 7200000, 512);
      store.put('idem-restart', 'intent-restart', 'exchange_accepted',
        {{token_id:'99'}}, {{ok:true, orderID:'order-restart', status:'submitted_unconfirmed'}});
      const service = new ClobOrderService();
      service.loadIdempotency();
      const status = service.idempotencyStatus('idem-restart');
      if (!status.reconcile_required || !service.unresolved.has('idem-restart') || service.idem.has('idem-restart')) {{
        throw new Error(`restart accepted an unconfirmed intent: ${{JSON.stringify(status)}}`);
      }}
      console.log(JSON.stringify({{ok:true,state:service.unresolved.get('idem-restart').state}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "ok": True, "state": "reconcile_required",
    }


def test_restart_promotes_legacy_exact_exchange_identity_without_claiming_fill(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "clob-exact-identity.sqlite"
    script = f"""
      import {{ DurableIdempotencyStore, ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      process.env.PM_CLOB_ORDER_IDEMPOTENCY_DB = {json.dumps(str(db_path))};
      const orderID = '0x' + 'ab'.repeat(32);
      const response = {{
        ok:true,
        orderID,
        expected_order_id:orderID,
        expected_order_id_matches:true,
        status:'submitted_unconfirmed',
        reconcile_required:false,
        reconcile_attempted:true,
        reconciled_from_exchange:true,
      }};
      const store = new DurableIdempotencyStore({json.dumps(str(db_path))}, 7200000, 512);
      store.put('idem-exact', 'intent-exact', 'reconcile_required',
        {{token_id:'99'}}, response);
      const service = new ClobOrderService();
      service.loadIdempotency();
      const status = service.idempotencyStatus('idem-exact');
      const loaded = new DurableIdempotencyStore(
        {json.dumps(str(db_path))}, 7200000, 512
      ).load().find((row) => row.idempotency_key === 'idem-exact');
      if (
        !status.ok
        || status.reconcile_required
        || service.unresolved.has('idem-exact')
        || !service.idem.has('idem-exact')
        || loaded.state !== 'exchange_accepted'
        || loaded.response.fill_state !== 'unknown'
        || loaded.response.idempotency_terminal_reason !== 'exact_exchange_identity_recovered'
      ) {{
        throw new Error(`exact identity not restored safely: ${{JSON.stringify({{status,loaded}})}}`);
      }}
      if (loaded.response.status !== 'submitted_unconfirmed') {{
        throw new Error('legacy exchange status was falsified');
      }}
      console.log(JSON.stringify({{
        ok:true,state:loaded.state,fill_state:loaded.response.fill_state,
      }}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "ok": True,
        "state": "exchange_accepted",
        "fill_state": "unknown",
    }


def test_execution_guard_endpoint_returns_raw_evidence_not_authorization() -> None:
    script = f"""
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const service = new ClobOrderService();
      const calls = [];
      service.client = {{}};
      const now = new Date().toISOString();
      service.cachedCollateral = {{value:{{balance:'100000000',allowance:'100000000'}},observed_at:now,latency_ms:10}};
      service.readLatestWsQuoteSnapshot = () => ({{parsed:{{connection_observed_at:now,quotes_by_token:{{
        'token-1':{{bids:[{{price:.49,size:5}}],asks:[{{price:.50,size:5}}],created_at:now,book_observed_at:now}}
      }}}},snapshot_sha256:'a'.repeat(64),age_ms:1}});
      const result = await service.executionGuardSnapshot({{
        token_id:'token-1', side:'BUY', limit_price:0.50, size:4, notional:2,
      }});
      if (!result.ok || !result.evidence_only || result.authorizes_order !== false) {{
        throw new Error(`bad boundary: ${{JSON.stringify(result)}}`);
      }}
      if (calls.length !== 0 || result.evidence_source !== 'local_prefetched_clob_evidence') {{
        throw new Error(`event-time network call detected: ${{JSON.stringify(calls)}}`);
      }}
      console.log(JSON.stringify({{ok:true,calls:calls.length}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "calls": 0}


def test_target_token_snapshot_survives_unrelated_shard_reconnect(tmp_path: Path) -> None:
    manifest = tmp_path / "market_ws_latest_quotes.json"
    shard_dir = tmp_path / "market_ws_quote_shards"
    script = f"""
      import fs from 'node:fs';
      import path from 'node:path';
      import crypto from 'node:crypto';
      process.env.PM_MARKET_WS_LATEST_QUOTES_PATH = {json.dumps(str(manifest))};
      process.env.PM_MARKET_WS_QUOTE_SHARD_DIR = {json.dumps(str(shard_dir))};
      const {{ ClobOrderService }} = await import({json.dumps(SERVICE.as_uri())});
      const canonical = (value) => {{
        if (Array.isArray(value)) return `[${{value.map(canonical).join(',')}}]`;
        if (value && typeof value === 'object') return `{{${{Object.keys(value).sort().map(
          key => `${{JSON.stringify(key)}}:${{canonical(value[key])}}`
        ).join(',')}}}}`;
        return JSON.stringify(value);
      }};
      const hashed = (core) => ({{...core, snapshot_sha256:crypto.createHash('sha256').update(canonical(core)).digest('hex')}});
      const now = new Date().toISOString();
      fs.mkdirSync({json.dumps(str(shard_dir))}, {{recursive:true}});
      const manifestCore = {{
        schema:'clob_ws_latest_quote_snapshot_v1', state:'connected', connection_observed_at:now,
        active_shards:[1], expected_shards:2, quotes_by_token:{{}}, quote_storage:'sha256_token_shards_v1',
        quote_shard_dir:{json.dumps(str(shard_dir))}, subscribed_token_count:1, materialized_token_count:1,
        source:'official_polymarket_clob_websocket', evidence_only:true, authorizes_order:false,
      }};
      fs.writeFileSync({json.dumps(str(manifest))}, JSON.stringify(hashed(manifestCore)));
      const token = 'healthy-token';
      const quoteCore = {{
        schema:'clob_ws_token_quote_snapshot_v1', token_id:token,
        quote:{{bids:[],asks:[],created_at:now,book_observed_at:now}},
        source:'official_polymarket_clob_websocket', evidence_only:true, authorizes_order:false,
      }};
      const filename = `${{crypto.createHash('sha256').update(token).digest('hex')}}.json`;
      fs.writeFileSync(path.join({json.dumps(str(shard_dir))}, filename), JSON.stringify(hashed(quoteCore)));
      const service = new ClobOrderService();
      const target = service.readLatestWsQuoteSnapshot(1000, token);
      if (!target.parsed.quotes_by_token[token]) throw new Error('healthy target rejected');
      let globalRejected = false;
      try {{ service.readLatestWsQuoteSnapshot(1000); }} catch {{ globalRejected = true; }}
      if (!globalRejected) throw new Error('incomplete global topology accepted');
      fs.unlinkSync(path.join({json.dumps(str(shard_dir))}, filename));
      let missingRejected = false;
      try {{ service.readLatestWsQuoteSnapshot(1000, token); }} catch {{ missingRejected = true; }}
      if (!missingRejected) throw new Error('missing target shard accepted');
      console.log(JSON.stringify({{ok:true,targetAccepted:true,globalRejected,missingRejected}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "ok": True, "targetAccepted": True, "globalRejected": True, "missingRejected": True,
    }


def test_registered_held_yes_uses_scope_bound_auxiliary_official_book(tmp_path: Path) -> None:
    inventory = tmp_path / "held_yes.json"
    manifest = tmp_path / "market_ws_held_yes_quotes.json"
    shard_dir = tmp_path / "market_ws_held_yes_quote_shards"
    script = f"""
      import fs from 'node:fs';
      import path from 'node:path';
      import crypto from 'node:crypto';
      process.env.PM_HELD_YES_INVENTORY_FILE = {json.dumps(str(inventory))};
      process.env.PM_HELD_YES_WS_MANIFEST_PATH = {json.dumps(str(manifest))};
      process.env.PM_HELD_YES_WS_SHARD_DIR = {json.dumps(str(shard_dir))};
      const {{ ClobOrderService }} = await import({json.dumps(SERVICE.as_uri())});
      const canonical = (value) => {{
        if (Array.isArray(value)) return `[${{value.map(canonical).join(',')}}]`;
        if (value && typeof value === 'object') return `{{${{Object.keys(value).sort().map(
          key => `${{JSON.stringify(key)}}:${{canonical(value[key])}}`
        ).join(',')}}}}`;
        return JSON.stringify(value);
      }};
      const hashed = (core) => ({{...core, snapshot_sha256:crypto.createHash('sha256').update(canonical(core)).digest('hex')}});
      const token = '123456';
      const scope = [token];
      const scopeSha = crypto.createHash('sha256').update(scope.join('\\0')).digest('hex');
      const now = new Date().toISOString();
      fs.writeFileSync({json.dumps(str(inventory))}, JSON.stringify({{
        schema:'reported_trajectory_held_yes_inventory_v1', complete:true,
        token_ids:scope, active_token_ids:scope,
        active_token_ids_source:'official_local_clob_execution_guard_snapshot',
        active_token_ids_observed_at:now, active_token_ids_receipt_sha256:'a'.repeat(64),
      }}));
      fs.mkdirSync({json.dumps(str(shard_dir))}, {{recursive:true}});
      const manifestCore = {{
        schema:'clob_ws_held_yes_quote_manifest_v1', state:'ready', connection_observed_at:now,
        scope_token_ids:scope, scope_sha256:scopeSha,
        complete_book_token_count:1, complete_book_token_ids:scope,
        missing_complete_book_token_ids:[], quote_storage:'sha256_token_shards_v1',
        quote_shard_dir:{json.dumps(str(shard_dir))}, source:'official_polymarket_clob_websocket',
        evidence_only:true, authorizes_order:false, last_error:'',
      }};
      fs.writeFileSync({json.dumps(str(manifest))}, JSON.stringify(hashed(manifestCore)));
      const quote = {{
        token_id:token, source:'clob_ws', source_detail:'book',
        bids:[{{price:.4,size:4}}], asks:[{{price:.5,size:5}}],
        created_at:now, book_observed_at:now,
      }};
      const tokenCore = {{
        schema:'clob_ws_held_yes_token_quote_snapshot_v1', token_id:token,
        scope_sha256:scopeSha, scope_token_ids:scope, quote,
        source:'official_polymarket_clob_websocket', evidence_only:true, authorizes_order:false,
      }};
      const filename = `${{crypto.createHash('sha256').update(token).digest('hex')}}.json`;
      fs.writeFileSync(path.join({json.dumps(str(shard_dir))}, filename), JSON.stringify(hashed(tokenCore)));
      const service = new ClobOrderService();
      service.client = {{}};
      service.registeredPositionTokens.add(token);
      service.readLatestWsQuoteSnapshot = () => {{ throw new Error('primary must not be needed'); }};
      service.getBalanceAllowanceForPrefetch = async () => ({{balance:'5000000',allowances:{{exchange:'5000000'}}}});
      const result = await service.executionGuardSnapshot({{
        token_id:token, side:'SELL', limit_price:.4, size:1, notional:0,
      }});
      if (!result.ok || result.ws_evidence_source !== 'held_yes_auxiliary_official_clob_ws'
          || result.order_book.bids.length !== 1 || result.order_book.asks.length !== 1) {{
        throw new Error(JSON.stringify(result));
      }}
      quote.book_observed_at = new Date(Date.now() - 121000).toISOString();
      fs.writeFileSync(path.join({json.dumps(str(shard_dir))}, filename), JSON.stringify(hashed({{...tokenCore, quote}})));
      let staleRejected = false;
      try {{ service.readHeldYesWsQuoteSnapshot(1000, token); }} catch {{ staleRejected = true; }}
      if (!staleRejected) throw new Error('stale held book accepted');
      service.readLatestWsQuoteSnapshot = () => ({{parsed:{{connection_observed_at:now,quotes_by_token:{{
        [token]:{{...quote,book_observed_at:now}}
      }}}}}});
      const fallback = await service.executionGuardSnapshot({{
        token_id:token, side:'SELL', limit_price:.4, size:1, notional:0,
      }});
      if (!fallback.ok || fallback.ws_evidence_source !== 'primary_official_clob_ws') {{
        throw new Error(JSON.stringify(fallback));
      }}
      console.log(JSON.stringify({{ok:true,source:result.ws_evidence_source,staleRejected,
        fallback:fallback.ws_evidence_source}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "ok": True,
        "source": "held_yes_auxiliary_official_clob_ws",
        "staleRejected": True,
        "fallback": "primary_official_clob_ws",
    }


def test_bulk_snapshot_loads_requested_tokens_from_shards(tmp_path: Path) -> None:
    manifest = tmp_path / "market_ws_latest_quotes.json"
    shard_dir = tmp_path / "market_ws_quote_shards"
    script = f"""
      import fs from 'node:fs';
      import path from 'node:path';
      import crypto from 'node:crypto';
      process.env.PM_MARKET_WS_LATEST_QUOTES_PATH = {json.dumps(str(manifest))};
      process.env.PM_MARKET_WS_QUOTE_SHARD_DIR = {json.dumps(str(shard_dir))};
      const {{ ClobOrderService }} = await import({json.dumps(SERVICE.as_uri())});
      const canonical = (value) => {{
        if (Array.isArray(value)) return `[${{value.map(canonical).join(',')}}]`;
        if (value && typeof value === 'object') return `{{${{Object.keys(value).sort().map(key => `${{JSON.stringify(key)}}:${{canonical(value[key])}}`).join(',')}}}}`;
        return JSON.stringify(value);
      }};
      const hashed = (core) => ({{...core, snapshot_sha256:crypto.createHash('sha256').update(canonical(core)).digest('hex')}});
      const now = new Date().toISOString();
      fs.mkdirSync({json.dumps(str(shard_dir))}, {{recursive:true}});
      const manifestCore = {{
        schema:'clob_ws_latest_quote_snapshot_v1', state:'connected', connection_observed_at:now,
        active_shards:[0], expected_shards:1, quotes_by_token:{{}}, quote_storage:'sha256_token_shards_v1',
        quote_shard_dir:{json.dumps(str(shard_dir))}, subscribed_token_count:2, materialized_token_count:2,
        source:'official_polymarket_clob_websocket', evidence_only:true, authorizes_order:false,
      }};
      fs.writeFileSync({json.dumps(str(manifest))}, JSON.stringify(hashed(manifestCore)));
      for (const token of ['bulk-a', 'bulk-b']) {{
        const quoteCore = {{schema:'clob_ws_token_quote_snapshot_v1', token_id:token, quote:{{bids:[],asks:[],created_at:now,book_observed_at:now}}, source:'official_polymarket_clob_websocket', evidence_only:true, authorizes_order:false}};
        const filename = `${{crypto.createHash('sha256').update(token).digest('hex')}}.json`;
        fs.writeFileSync(path.join({json.dumps(str(shard_dir))}, filename), JSON.stringify(hashed(quoteCore)));
      }}
      const service = new ClobOrderService();
      const snapshot = service.readLatestWsQuoteSnapshot(1000, '', false, ['bulk-a', 'bulk-b']);
      if (Object.keys(snapshot.parsed.quotes_by_token).length !== 2) throw new Error('bulk shard quotes missing');
      console.log(JSON.stringify({{ok:true,count:Object.keys(snapshot.parsed.quotes_by_token).length}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "count": 2}


def test_execution_guard_marks_sdk_error_payload_unavailable() -> None:
    script = f"""
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const service = new ClobOrderService();
      service.client = {{}};
      service.readLatestWsQuoteSnapshot = () => {{ throw new Error('stale'); }};
      const result = await service.executionGuardSnapshot({{token_id:'t',side:'BUY',limit_price:.5,size:1,notional:.5}});
      if (result.ok || !result.evidence_errors.includes('clob_ws_order_book_depth_missing') || result.authorizes_order !== false) throw new Error(JSON.stringify(result));
      console.log(JSON.stringify({{ok:true,blocked:true}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "blocked": True}


def test_execution_guard_accepts_materialized_book_with_empty_asks() -> None:
    """An empty sell side is valid market evidence, not missing evidence."""

    script = f"""
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const service = new ClobOrderService();
      service.client = {{}};
      const now = new Date().toISOString();
      service.cachedCollateral = {{
        value:{{balance:'100000000',allowance:'100000000'}}, observed_at:now, latency_ms:1,
      }};
      service.readLatestWsQuoteSnapshot = () => ({{
        parsed:{{connection_observed_at:now,quotes_by_token:{{
          'token-empty-asks':{{
            bids:[{{price:.999,size:10}}], asks:[], created_at:now, book_observed_at:now,
          }}
        }}}}, snapshot_sha256:'a'.repeat(64), age_ms:1,
      }});
      const result = await service.executionGuardSnapshot({{
        token_id:'token-empty-asks', side:'BUY', limit_price:.985, size:5, notional:4.925,
      }});
      if (!result.ok || result.evidence_errors.length !== 0 || result.order_book.asks.length !== 0) {{
        throw new Error(JSON.stringify(result));
      }}
      console.log(JSON.stringify({{ok:true,asks:result.order_book.asks.length}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "asks": 0}


def test_execution_guard_rejects_quote_without_materialized_book_arrays() -> None:
    script = f"""
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const service = new ClobOrderService();
      service.client = {{}};
      const now = new Date().toISOString();
      service.cachedCollateral = {{
        value:{{balance:'100000000',allowance:'100000000'}}, observed_at:now, latency_ms:1,
      }};
      service.readLatestWsQuoteSnapshot = () => ({{
        parsed:{{connection_observed_at:now,quotes_by_token:{{
          'token-no-book':{{best_bid:.4,best_ask:.5,created_at:now}}
        }}}}, snapshot_sha256:'a'.repeat(64), age_ms:1,
      }});
      const result = await service.executionGuardSnapshot({{
        token_id:'token-no-book', side:'BUY', limit_price:.985, size:5, notional:4.925,
      }});
      if (result.ok || !result.evidence_errors.includes('clob_ws_order_book_depth_missing')) {{
        throw new Error(JSON.stringify(result));
      }}
      console.log(JSON.stringify({{ok:true,blocked:true}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "blocked": True}


def test_zero_yes_balance_does_not_require_an_unheld_yes_order_book() -> None:
    """An authoritative zero position must not force every YES token onto WS."""

    script = f"""
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const service = new ClobOrderService();
      service.client = {{}};
      service.readLatestWsQuoteSnapshot = () => {{ throw new Error('target quote absent'); }};
      service.getBalanceAllowanceForPrefetch = async () => ({{
        balance:'0', allowances:{{exchange:'0'}},
      }});
      const result = await service.executionGuardSnapshot({{
        token_id:'unheld-yes', side:'SELL', limit_price:.01, size:5, notional:0,
      }});
      if (!result.ok || result.evidence_errors.length !== 0
          || result.conditional_balance_allowance.balance !== '0') {{
        throw new Error(JSON.stringify(result));
      }}
      console.log(JSON.stringify({{ok:true,registered:service.registeredPositionTokens.size}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "registered": 0}


def test_positive_yes_balance_still_requires_materialized_sell_depth() -> None:
    script = f"""
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const service = new ClobOrderService();
      service.client = {{}};
      service.readLatestWsQuoteSnapshot = () => {{ throw new Error('target quote absent'); }};
      service.getBalanceAllowanceForPrefetch = async () => ({{
        balance:'5000000', allowances:{{exchange:'5000000'}},
      }});
      const result = await service.executionGuardSnapshot({{
        token_id:'held-yes', side:'SELL', limit_price:.01, size:5, notional:0,
      }});
      if (result.ok || !result.evidence_errors.includes('clob_ws_order_book_depth_missing')) {{
        throw new Error(JSON.stringify(result));
      }}
      console.log(JSON.stringify({{ok:true,registered:service.registeredPositionTokens.size}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "registered": 1}


def test_conditional_balance_prefetch_isolates_tokens_and_prunes_inactive_inventory(tmp_path: Path) -> None:
    inventory = tmp_path / "held_yes.json"
    inventory.write_text(json.dumps({
        "schema": "reported_trajectory_held_yes_inventory_v1",
        "complete": True,
        "token_ids": ["100", "200", "300"],
        "active_token_ids": ["100", "200"],
    }))
    script = f"""
      process.env.PM_HELD_YES_INVENTORY_FILE = {json.dumps(str(inventory))};
      const {{ ClobOrderService }} = await import({json.dumps(SERVICE.as_uri())});
      const service = new ClobOrderService();
      service.registeredPositionTokens.add('300');
      service.cachedConditional.set('300', {{value:{{balance:'1',allowance:'1'}},observed_at:new Date().toISOString()}});
      service.getBalanceAllowanceForPrefetch = async (params) => {{
        if (params.token_id === '100') return {{balance:'5000000',allowances:{{exchange:'5000000'}}}};
        return {{error:'one token failed'}};
      }};
      const result = await service.prefetchRegisteredPositionBalances();
      if (!service.cachedConditional.has('100') || service.cachedConditional.has('200')
          || service.cachedConditional.has('300') || service.registeredPositionTokens.has('300')
          || !service.conditionalPrefetchErrors.has('200')) {{
        throw new Error(JSON.stringify({{result, registered:[...service.registeredPositionTokens]}}));
      }}
      console.log(JSON.stringify({{result,registered:[...service.registeredPositionTokens].sort()}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["registered"] == ["100", "200"]
    assert payload["result"] == {"requested": 2, "ready": 1, "failed": 1}


def test_read_only_service_rejects_place_before_idempotency_or_exchange() -> None:
    script = f"""
      process.env.PM_CLOB_ORDER_SERVICE_READ_ONLY = '1';
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const service = new ClobOrderService();
      service.persistIntent = () => {{ throw new Error('must not persist'); }};
      service.client = {{ postOrder: async () => {{ throw new Error('must not post'); }} }};
      const result = await service.placeOrderWithIdempotency({{idempotency_key:'read-only',order_intent_id:'intent'}});
      if (result.error !== 'clob_order_service_read_only' || result.exchange_post_started !== false) {{
        throw new Error(JSON.stringify(result));
      }}
      console.log(JSON.stringify({{ok:true,error:result.error}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "error": "clob_order_service_read_only"}


def test_explicit_fak_buy_uses_local_limit_signing_before_post() -> None:
    """FAK is time-in-force; explicit price+size must not trigger market-info REST."""

    script = f"""
      process.env.PM_CLOB_FORCE_PROXY = '0';
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const zero = '0x0000000000000000000000000000000000000000';
      const bytes32 = `0x${{'00'.repeat(32)}}`;
      const calls = [];
      const service = new ClobOrderService();
      service.persistIntent = () => {{}};
      service.client = {{
        tickSizes: {{}}, negRisk: {{}},
        createMarketOrder: async () => {{ throw new Error('market builder must not run'); }},
        createOrder: async (order) => {{
          calls.push(['createOrder', order]);
          return {{
            salt:'123', maker:zero, signer:zero, tokenId:order.tokenID,
            makerAmount:'49995000', takerAmount:'50500000', side:'BUY', signatureType:0,
            timestamp:'1720000000', metadata:bytes32, builder:bytes32, signature:'0x00',
          }};
        }},
        postOrder: async (_signed, orderType) => {{
          calls.push(['postOrder', orderType]);
          return {{orderID:'exchange-order',status:'matched',size_matched:'50.5'}};
        }},
      }};
      const result = await service.placeOrder({{
        token_id:'99', price:.99, size:50.5, amount:49.995, side:'BUY',
        order_type:'FAK', allow_market:true, direct_target_order_context:true,
        strategy_id:'reported_trajectory_no_v1',
        target_execution_hard_deadline_at:'2099-01-01T00:00:00.000Z',
        order_intent_id:'intent',
      }}, 'idem', {{acceptedMs:Date.now(),lane:'direct_target_primary_entry_priority'}});
      if (!result.ok || calls.length !== 2 || calls[0][0] !== 'createOrder' || calls[1][0] !== 'postOrder') {{
        throw new Error(JSON.stringify({{result,calls}}));
      }}
      // The 0.99 limit requires a cent-aligned maker amount, so the
      // protected FAK is rounded down from 50.5 to 50 shares.
      if (calls[0][1].price !== .99 || calls[0][1].size !== 50 || result.order_build_ms > 50) {{
        throw new Error(JSON.stringify({{result,calls}}));
      }}
      console.log(JSON.stringify({{ok:true,calls:calls.map(row=>row[0]),order_build_ms:result.order_build_ms}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["calls"] == ["createOrder", "postOrder"]
    assert payload["order_build_ms"] <= 50


def test_controlled_rebalance_sell_fak_uses_gateway_limit_signing_without_market_flag() -> None:
    """The rebalance shape is a protected limit FAK, not a naked market order."""

    script = f"""
      process.env.PM_CLOB_FORCE_PROXY = '0';
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const zero = '0x0000000000000000000000000000000000000000';
      const bytes32 = `0x${{'00'.repeat(32)}}`;
      const calls = [];
      const service = new ClobOrderService();
      service.persistIntent = () => {{}};
      service.client = {{
        tickSizes: {{}}, negRisk: {{}},
        createMarketOrder: async () => {{ throw new Error('market builder must not run'); }},
        createOrder: async (order) => {{
          calls.push(['createOrder', order]);
          return {{
            salt:'456', maker:zero, signer:zero, tokenId:order.tokenID,
            makerAmount:'10000000', takerAmount:'25000000', side:'SELL', signatureType:0,
            timestamp:'1720000000', metadata:bytes32, builder:bytes32, signature:'0x00',
          }};
        }},
        postOrder: async (_signed, orderType) => {{
          calls.push(['postOrder', orderType]);
          return {{orderID:'sell-order',status:'filled',size_matched:'25'}};
        }},
      }};
      const result = await service.placeOrder({{
        token_id:'99', price:.40, size:25, side:'SELL',
        order_type:'FAK', allow_market:false, post_only:false,
        strategy_id:'weather_settlement_floor_biased_basket_v1',
        order_execution_domain:'settlement_floor_basket',
        order_intent_action:'controlled_rebalance',
        order_intent_id:'intent-sell',
      }}, 'idem-sell', {{acceptedMs:Date.now(),lane:'sell_parallel'}});
      if (!result.ok || calls.length !== 2 || calls[0][0] !== 'createOrder' || calls[1][0] !== 'postOrder') {{
        throw new Error(JSON.stringify({{result,calls}}));
      }}
      if (calls[0][1].side !== 'SELL' || calls[0][1].price !== .40 || calls[0][1].size !== 25) {{
        throw new Error(JSON.stringify({{result,calls}}));
      }}
      console.log(JSON.stringify({{ok:true,calls:calls.map(row=>row[0])}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "ok": True,
        "calls": ["createOrder", "postOrder"],
    }


def test_gateway_posts_signal_sell_and_no_buy_concurrently(tmp_path: Path) -> None:
    # Real token and size: since 2026-08-30 admission refuses an order that
    # does not meet the market's minimum lot, so a payload with neither a
    # token nor a size never reaches the serialisation this test is about.
    db_path = tmp_path / "archive.db"
    _minimum_lot_registry(db_path)
    script = f"""
      process.env.PM_LIVE_PAUSE_FILE = `/tmp/weather-test-no-pause-${{process.pid}}`;
      process.env.PM_ARCHIVE_DB = {json.dumps(str(db_path))};
      const {{ ClobOrderService }} = await import({json.dumps(SERVICE.as_uri())});
      const service = new ClobOrderService();
      service.persistIntent = () => {{}};
      service.saveIdempotency = () => {{}};
      // Funded: the collateral gate sits ahead of the serialisation this test
      // is about, and a BUY the account cannot pay for never reaches it.
      service.cachedCollateral = {{
        value: {{ balance: '1000000000', allowance: '1000000000' }},
        observed_at: new Date().toISOString(), latency_ms: 1,
      }};
      const calls = [];
      service.placeOrder = async (payload) => {{
        calls.push(`${{payload.side}}-start`);
        if (payload.side === 'SELL') await new Promise(resolve => setTimeout(resolve, 25));
        calls.push(`${{payload.side}}-end`);
        return {{ok:true, accepted:true, pending:false, status:'matched'}};
      }};
      const common = {{
        direct_target_order_context:true,
        strategy_id:'reported_trajectory_no_v1',
        target_execution_hard_deadline_at:'2099-01-01T00:00:00.000Z',
        token_id:'123456', price:0.5, size:10,
      }};
      const sell = service.placeOrderWithIdempotency({{
        ...common, side:'SELL', direct_target_exit_order:true,
        idempotency_key:'sell-idem', order_intent_id:'sell-intent',
      }});
      const buy = service.placeOrderWithIdempotency({{
        ...common, side:'BUY', outcome_side:'NO',
        idempotency_key:'buy-idem', order_intent_id:'buy-intent',
      }});
      await Promise.all([sell, buy]);
      if (JSON.stringify(calls) !== JSON.stringify(['SELL-start','BUY-start','BUY-end','SELL-end'])) {{
        throw new Error(JSON.stringify(calls));
      }}
      console.log(JSON.stringify({{ok:true,calls}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["calls"] == [
        "SELL-start", "BUY-start", "BUY-end", "SELL-end",
    ]


def test_gateway_rejects_retired_copy_follow_before_persistence_or_submit() -> None:
    script = f"""
      process.env.PM_LIVE_PAUSE_FILE = `/tmp/weather-test-no-pause-${{process.pid}}`;
      const {{ ClobOrderService }} = await import({json.dumps(SERVICE.as_uri())});
      const service = new ClobOrderService();
      let persisted = 0;
      let submitted = 0;
      service.persistIntent = () => {{ persisted += 1; }};
      service.saveIdempotency = () => {{}};
      service.placeOrder = async () => {{
        submitted += 1;
        return {{ok:true, accepted:true, pending:false, status:'live'}};
      }};
      const result = await service.placeOrderWithIdempotency({{
        strategy_id:'weather_copy_follow_v1',
        copy_follow_parallel_submission:true,
        token_id:'copy-token', side:'BUY', order_type:'GTC',
        idempotency_key:'copy-idem', order_intent_id:'copy-intent',
      }});
      console.log(JSON.stringify({{result,persisted,submitted}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["result"] == {
        "accepted": False,
        "error": "retired_strategy_forbidden",
        "exchange_post_started": False,
        "idempotency_key": "copy-idem",
        "ok": False,
        "order_intent_id": "copy-intent",
        "pending": False,
        "reconcile_required": False,
        "source": "clob_order_service",
        "strategy_id": "weather_copy_follow_v1",
    }
    assert payload["persisted"] == 0
    assert payload["submitted"] == 0


def test_exchange_lookup_does_not_treat_empty_response_as_open_order() -> None:
    script = f"""
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const service = new ClobOrderService();
      service.client = {{ getOrder: async () => null }};
      const empty = await service.lookupExchangeOrder({{ order_id: 'order-empty' }});
      if (empty.ok || empty.status !== 'unknown' || empty.error !== 'clob_order_lookup_empty_response') {{
        throw new Error(`empty lookup incorrectly accepted: ${{JSON.stringify(empty)}}`);
      }}
      service.proxy = {{ mode: 'proxy' }};
      service.requestJsonViaProxy = async () => ({{ response: null }});
      const proxyEmpty = await service.lookupExchangeOrder({{ order_id: 'order-proxy-empty' }});
      if (proxyEmpty.ok || proxyEmpty.status !== 'unknown' || proxyEmpty.error !== 'clob_order_lookup_empty_response') {{
        throw new Error(`proxy empty lookup incorrectly accepted: ${{JSON.stringify(proxyEmpty)}}`);
      }}
      service.client = {{ getOrder: async () => ({{ id: 'other-order', status: 'matched' }}) }};
      service.proxy = {{}};
      const mismatch = await service.lookupExchangeOrder({{ order_id: 'order-expected' }});
      if (mismatch.ok || mismatch.status !== 'unknown' || mismatch.error !== 'clob_order_lookup_identity_mismatch') {{
        throw new Error(`mismatched lookup incorrectly accepted: ${{JSON.stringify(mismatch)}}`);
      }}
      console.log(JSON.stringify({{empty: empty.status, proxyEmpty: proxyEmpty.status, mismatch: mismatch.status}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "empty": "unknown", "proxyEmpty": "unknown", "mismatch": "unknown",
    }


def test_a_throw_after_the_post_becomes_a_reconcile_receipt(tmp_path: Path) -> None:
    """An order may be resting; the caller has to be told, not thrown at.

    On 2026-08-31 a ReferenceError landed on the line after the submit
    promise was created.  The POST had already gone, the exchange had
    accepted the order, and what came back to the caller was
    "parallelCopyFollow is not defined": no order id, no idempotency record,
    unresolved at 0, and a live order resting on KSFO for eighteen minutes
    with nothing on this machine aware of it.  It was found by asking the
    account what it held.

    The identifier is gone, but the shape that made it dangerous is not
    unique to it -- anything throwing after the POST does the same.  So the
    post-submit path is wrapped, and this pins the wrap: the caller gets
    reconcile_required and the deterministic order id, which is knowable
    before the POST and is the only thing that makes the order findable.
    """
    db_path = tmp_path / "archive.db"
    _minimum_lot_registry(db_path)
    script = f"""
      process.env.PM_LIVE_PAUSE_FILE = `/tmp/weather-test-no-pause-${{process.pid}}`;
      const {{ ClobOrderService }} = await import({json.dumps(SERVICE.as_uri())});
      const service = new ClobOrderService();
      // Funded: the collateral gate sits ahead of everything this test
      // exercises, and an unpayable order never reaches the POST.
      service.cachedCollateral = {{value: {{balance: '1000000000', allowance: '1000000000'}},observed_at: new Date().toISOString(), latency_ms: 1}};
      service.persistIntent = () => {{}};
      service.saveIdempotency = () => {{}};
      // The POST succeeds -- the order is live on the exchange.
      service.placeOrder = async () => ({{
        ok: true, accepted: true, status: "live",
        orderID: "0xdeadbeef", exchange_post_started: true,
      }});
      // ...and then something after it throws, exactly as the ReferenceError
      // did.  The break is put on `inflight.set`, which is only reached after
      // the submit promise exists: the reads earlier in the method (`has`,
      // `get`) have to keep working or the throw lands before the POST and
      // proves nothing.
      const realInflight = service.inflight;
      service.inflight = {{
        has: (k) => realInflight.has(k),
        get: (k) => realInflight.get(k),
        delete: (k) => realInflight.delete(k),
        set: () => {{ throw new Error("synthetic post-submit failure"); }},
      }};
      const out = await service.placeOrderWithIdempotency({{
        strategy_id: 'bayes_basket_target_v1', token_id: '123456',
        side: 'BUY', order_type: 'GTC', post_only: true,
        price: 0.01, size: 5, notional: 0.05,
        idempotency_key: 'throw-after-post', order_intent_id: 'throw-after-post',
      }});
      console.log(JSON.stringify({{out, unresolved: service.unresolved.size}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        env={
            **os.environ,
            "PM_DATA_DIR": str(tmp_path),
            "PM_ARCHIVE_DB": str(db_path),
            "PM_CLOB_ORDER_SERVICE_READ_ONLY": "0",
        },
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout.strip().splitlines()[-1])
    out = value["out"]

    # Not a raw throw: a receipt that says an order may be out there.
    assert out["error"] == "submit_path_threw_after_post"
    assert out["reconcile_required"] is True
    assert out["exchange_post_started"] is True
    assert out["idempotency_key"] == "throw-after-post"
    assert out["order_intent_id"] == "throw-after-post"
    assert "synthetic post-submit failure" in out["error_detail"]

    # And it is recorded as unresolved, so the reconciler and the operator
    # can both find it.  Replaying this against the pre-fix code shows the
    # unresolved entry was already there at the instant of the throw -- what
    # was missing was any way for the caller to learn of it, and in the real
    # incident the submit promise then resolved and cleaned the entry away
    # while the caller was already gone with a bare ReferenceError.
    assert value["unresolved"] == 1
