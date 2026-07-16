from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "scripts" / "clob_order_service.mjs"


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


def test_execution_guard_endpoint_returns_raw_evidence_not_authorization() -> None:
    script = f"""
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const service = new ClobOrderService();
      const calls = [];
      service.client = {{
        getOrderBook: async (token) => ({{asset_id: token, timestamp:String(Date.now()), bids:[{{price:'0.49',size:'5'}}], asks:[{{price:'0.50',size:'5'}}]}}),
        getBalanceAllowance: async (params) => {{
          calls.push(params);
          return params.asset_type === 'COLLATERAL'
            ? {{balance:'100000000',allowance:'100000000'}}
            : {{balance:'0',allowance:'100000000'}};
        }},
      }};
      const result = await service.executionGuardSnapshot({{
        token_id:'token-1', side:'BUY', limit_price:0.50, size:4, notional:2,
      }});
      if (!result.ok || !result.evidence_only || result.authorizes_order !== false) {{
        throw new Error(`bad boundary: ${{JSON.stringify(result)}}`);
      }}
      if (calls.length !== 2 || !calls.some((row) => row.asset_type === 'COLLATERAL') || !calls.some((row) => row.asset_type === 'CONDITIONAL')) {{
        throw new Error(`missing balance evidence: ${{JSON.stringify(calls)}}`);
      }}
      console.log(JSON.stringify({{ok:true,calls:calls.length}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "calls": 2}


def test_execution_guard_marks_sdk_error_payload_unavailable() -> None:
    script = f"""
      import {{ ClobOrderService }} from {json.dumps(SERVICE.as_uri())};
      const service = new ClobOrderService();
      service.client = {{
        getOrderBook: async () => ({{error:'timeout'}}),
        getBalanceAllowance: async () => ({{error:'timeout'}}),
      }};
      const result = await service.executionGuardSnapshot({{token_id:'t',side:'BUY',limit_price:.5,size:1,notional:.5}});
      if (result.ok || result.evidence_errors.length !== 3 || result.authorizes_order !== false) throw new Error(JSON.stringify(result));
      console.log(JSON.stringify({{ok:true,errors:result.evidence_errors.length}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "errors": 3}


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
