import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


def _code_only(source: str) -> str:
    """Source with comments removed.

    The "must not appear" assertions below are about what the admission
    section *does*, not about what it says.  Explaining in a comment why an
    order must not reach the signer should not read as the signer being
    called there.
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))


def test_gateway_pause_gate_is_dynamic_and_precedes_idempotency_admission():
    source = (ROOT / "scripts" / "clob_order_service.mjs").read_text(encoding="utf-8")
    method = source.split("async placeOrderWithIdempotency(payload)", 1)[1].split(
        "const key = String(payload.idempotency_key", 1
    )[0]
    assert "if (livePausePresent())" in method
    assert "clob_order_service_live_paused" in method
    assert "exchange_post_started: false" in method
    # The pause gate carries no lane exemption: it is the operator's stop for
    # the whole gateway, and an admitted ASOS argmax intent used to walk past
    # it (closed 2026-08-30, see test_asos_argmax_gateway_admission).
    assert "if (!isAsosLive)" not in _code_only(method)
    code = _code_only(method)
    assert "persistIntent" not in code
    assert "createOrder" not in code
    assert "postOrder" not in code


def test_the_asos_order_lane_is_retired_at_the_chokepoint(tmp_path):
    """One mainline.  The bar is checked before every other admission test.

    Operator ruling 2026-08-31: there is no separate ASOS order lane -- the
    system has one, the three-stage P0 / P0-M / P1 basket ladder.  The lane
    had grown its own strategy ids, intents table, budget and gateway
    admission, and ran a submission step every resident cycle.

    Its three ids are in the retired set, which is tested first, so the
    ruling does not depend on any later gate still being in place.  The
    admission functions that used to sit behind it are gone from the source
    entirely, which is asserted here too: a retired lane that still carries
    its own admission path invites being switched back on.
    """
    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    script = f"""
      process.env.PM_DATA_DIR = {json.dumps(str(tmp_path))};
      const {{ ClobOrderService }} = await import({json.dumps(service_uri)});
      const service = new ClobOrderService();
      service.persistIntent = () => {{ throw new Error('must not persist'); }};
      service.client = {{
        createOrder: async () => {{ throw new Error('must not sign'); }},
        postOrder: async () => {{ throw new Error('must not post'); }},
      }};
      const out = {{}};
      for (const id of [
        'asos_argmax_new_high_yes_v1',
        'asos_argmax_yes_exit_099_v1',
        'asos_argmax_rotation_sell_fak_v1',
      ]) {{
        out[id] = await service.placeOrderWithIdempotency({{
          strategy_id: id, token_id: '123456', side: 'BUY', order_type: 'FOK',
          price: 0.2, size: 10, notional: 2, allow_market: true,
          post_only: false, idempotency_key: `k-${{id}}`, order_intent_id: id,
        }});
      }}
      console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    for strategy_id, payload in json.loads(result.stdout).items():
        assert payload["error"] == "retired_strategy_forbidden", strategy_id
        assert payload["ok"] is False
        assert payload["accepted"] is False
        assert payload["exchange_post_started"] is False

    source = (ROOT / "scripts" / "clob_order_service.mjs").read_text(encoding="utf-8")
    code = _code_only(source)
    for name in (
        "asosArgmaxLiveAdmission",
        "asosArgmaxExitAdmission",
        "asosArgmaxRotationSellAdmission",
        "isAsosLive",
        "ASOS_ARGMAX_LIVE_STATIONS",
        "PM_ASOS_ARGMAX_LIVE_ENABLED",
    ):
        assert name not in code, name


def test_gateway_starts_during_pause_instead_of_sleeping():
    shell = (ROOT / "scripts" / "clob_order_service.sh").read_text(encoding="utf-8")
    assert "export PM_LIVE_PAUSE_FILE=" in shell
    assert "exec sleep" not in shell
    assert 'exec "$NODE_BIN" "$ROOT/scripts/clob_order_service.mjs"' in shell
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "clob_order_service.sh")], check=True)


def test_live_pause_rejects_before_persistence_signing_or_exchange(tmp_path):
    pause = tmp_path / "live_paused.flag"
    pause.write_text("test\n", encoding="utf-8")
    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    script = f"""
      process.env.PM_LIVE_PAUSE_FILE = {json.dumps(str(pause))};
      process.env.PM_DATA_DIR = {json.dumps(str(tmp_path))};
      const {{ ClobOrderService }} = await import({json.dumps(service_uri)});
      const service = new ClobOrderService();
      service.persistIntent = () => {{ throw new Error('must not persist'); }};
      service.client = {{
        createOrder: async () => {{ throw new Error('must not sign'); }},
        postOrder: async () => {{ throw new Error('must not post'); }},
      }};
      const result = await service.placeOrderWithIdempotency({{
        idempotency_key: 'pause-test', order_intent_id: 'pause-intent',
      }});
      if (result.error !== 'clob_order_service_live_paused' || result.exchange_post_started !== false) {{
        throw new Error(JSON.stringify(result));
      }}
      console.log(JSON.stringify(result));
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
    assert json.loads(result.stdout)["error"] == "clob_order_service_live_paused"


def test_gateway_wrapper_is_user_production_no_live_only():
    shell = (ROOT / "scripts" / "clob_order_service.sh").read_text(encoding="utf-8")
    assert 'if [ "${WEATHER_SERVICE_DOMAIN:-}" != "user" ]; then' in shell
    assert 'echo "[clob_order_service] user_domain_required"' in shell
    assert 'if [ "${PM_TARGET_EXECUTION_MODE:-}" != "production_no_live" ]; then' in shell
    assert 'echo "[clob_order_service] production_no_live_required"' in shell
    assert "export WEATHER_SERVICE_DOMAIN=user" in shell
    assert "export PM_TARGET_EXECUTION_MODE=production_no_live" in shell
    assert "export PM_CLOB_ORDER_SERVICE_READ_ONLY=1" in shell
    assert "sequential_bayes_execution_contract" not in shell
    assert "observation_to_order_" not in shell
    assert "signal_to_order_" not in shell
    assert "dynamic_three_leg" not in shell
    assert "target_realization" not in shell


def test_gateway_launchd_entry_uses_the_current_wrapper_contract():
    plist = (ROOT / "launchd" / "com.weather.clob_order_service.plist").read_text(
        encoding="utf-8"
    )
    assert "/Users/xiong/weather/scripts/clob_order_service.sh" in plist
    assert "<key>WEATHER_SERVICE_DOMAIN</key>" in plist
    assert "<string>user</string>" in plist
    assert "<key>PM_TARGET_EXECUTION_MODE</key>" in plist
    assert "<string>production_no_live</string>" in plist
    assert "sequential_bayes_execution_contract" not in plist
    assert "dynamic_three_leg" not in plist


def test_balance_prefetch_never_uses_native_fetch_when_proxy_is_selected():
    source = (ROOT / "scripts" / "clob_order_service.mjs").read_text(encoding="utf-8")
    method = source.split("async getBalanceAllowanceForPrefetch(params)", 1)[1].split(
        "readLatestWsQuoteSnapshot", 1
    )[0]
    assert 'this.proxy?.mode === "proxy"' in method
    assert "response = await this.client.getBalanceAllowance(params)" in method


def test_proxy_order_posts_do_not_block_node_event_loop():
    source = (ROOT / "scripts" / "clob_order_service.mjs").read_text(encoding="utf-8")
    proxy_submit = source.split("async postOrderViaProxy", 1)[1].split("loadIdempotency", 1)[0]
    assert "await execFilePromise(\"/usr/bin/curl\"" in proxy_submit
    assert "execFileSync(\"/usr/bin/curl\"" not in proxy_submit


def test_balance_prefetch_is_strict_single_flight():
    source = (ROOT / "scripts" / "clob_order_service.mjs").read_text(encoding="utf-8")
    method = source.split("startBalancePrefetch()", 1)[1].split(
        "async getBalanceAllowanceForPrefetch", 1
    )[0]
    assert "this.balancePrefetchInflight > 0" in method
    assert "PM_CLOB_BALANCE_PREFETCH_MAX_INFLIGHT" not in method
