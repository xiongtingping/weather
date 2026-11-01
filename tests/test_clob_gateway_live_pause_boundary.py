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
    # One exemption only: the board's own hand orders (user 2026-11-01).  No
    # lane id may be added -- an admitted ASOS argmax intent used to walk
    # past this gate (closed 2026-08-30).
    assert "if (livePausePresent() && strategyId !== MANUAL_BOARD_STRATEGY_ID)" in method
    assert 'const MANUAL_BOARD_STRATEGY_ID = "operator_manual_board_v1";' in source
    assert "clob_order_service_live_paused" in method
    assert "exchange_post_started: false" in method
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


def test_live_pause_admits_the_boards_manual_orders_and_no_lane(tmp_path):
    """The pause flag stops the lanes, not the operator's own hand.

    User 2026-11-01「暂停旗不应该影响手动买入和卖出」: with the flag up, a
    board buy or sell (``operator_manual_board_v1``) passes the pause gate and
    reaches persistence; any other strategy id is still refused there.  The
    read-only bit keeps no exception for anyone.
    """
    pause = tmp_path / "live_paused.flag"
    pause.write_text("test\n", encoding="utf-8")
    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    script = f"""
      process.env.PM_LIVE_PAUSE_FILE = {json.dumps(str(pause))};
      process.env.PM_DATA_DIR = {json.dumps(str(tmp_path))};
      process.env.PM_CLOB_IGNORE_REGION_BLOCK = '1';
      const {{ ClobOrderService }} = await import({json.dumps(service_uri)});
      const service = new ClobOrderService();
      service.client = {{
        createOrder: async () => {{ throw new Error('must not sign'); }},
        postOrder: async () => {{ throw new Error('must not post'); }},
      }};
      const out = {{}};
      for (const id of ['operator_manual_board_v1', 'proper_maker_v4', '']) {{
        let reached = false;
        service.persistIntent = () => {{ reached = true; throw new Error('stop here'); }};
        let result;
        try {{
          result = await service.placeOrderWithIdempotency({{
            strategy_id: id, token_id: '123456', side: 'SELL', order_type: 'FOK',
            price: 0.06, size: 60.87, allow_market: true, post_only: false,
            idempotency_key: `k-${{id}}`, order_intent_id: `i-${{id}}`,
          }});
        }} catch (err) {{ result = {{ thrown: String(err.message) }}; }}
        out[id || 'none'] = {{ reached, error: result.error || null }};
      }}
      process.env.PM_CLOB_ORDER_SERVICE_READ_ONLY = '1';
      out.read_only = await service.placeOrderWithIdempotency({{
        strategy_id: 'operator_manual_board_v1', idempotency_key: 'ro', order_intent_id: 'ro',
      }});
      console.log(JSON.stringify(out));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["operator_manual_board_v1"]["error"] != "clob_order_service_live_paused"
    for lane in ("proper_maker_v4", "none"):
        assert out[lane] == {"reached": False, "error": "clob_order_service_live_paused"}, lane
    assert out["read_only"]["error"] == "clob_order_service_read_only"


def test_gateway_wrapper_is_user_domain_and_names_one_known_mode():
    """What the wrapper still owes after go-live, and what it no longer does.

    This test asserted three things that stopped being true on 2026-08-30 --
    that the mode is production_no_live, that the launcher refuses anything
    else, and that the read-only bit is 1 -- and it has been red ever since,
    which is the wrong way for a suite to record a migration.  Red is how a
    suite says "somebody broke this"; a rule that was deliberately retired
    should stop being asserted, not sit failing as a monument to itself.

    What survives is the part the ruling did not touch: the wrapper runs in
    the user domain, it refuses an unset or unknown execution mode rather
    than defaulting, and it loads none of the retired lanes.
    """

    shell = (ROOT / "scripts" / "clob_order_service.sh").read_text(encoding="utf-8")
    assert 'if [ "${WEATHER_SERVICE_DOMAIN:-}" != "user" ]; then' in shell
    assert 'echo "[clob_order_service] user_domain_required"' in shell
    assert "export WEATHER_SERVICE_DOMAIN=user" in shell

    # Still a gate, just no longer a one-mode gate: the check names both
    # known modes and refuses everything else, so an unset or misspelled
    # PM_TARGET_EXECUTION_MODE cannot start the gateway.
    assert 'echo "[clob_order_service] known_execution_mode_required"' in shell
    assert '"production_live"' in shell
    assert '"production_no_live"' in shell
    assert "export PM_TARGET_EXECUTION_MODE=production_live" in shell

    # The read-only bit is the operator's, and it is 0 now by their ruling.
    # Asserted as "present and explicit" rather than as a value, so this test
    # stops being the thing that goes red when they change their mind.
    assert re.search(r"^export PM_CLOB_ORDER_SERVICE_READ_ONLY=[01]$", shell, re.M)

    for retired in (
        "sequential_bayes_execution_contract",
        "observation_to_order_",
        "signal_to_order_",
        "dynamic_three_leg",
        "target_realization",
    ):
        assert retired not in shell, retired


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


def test_the_gateway_has_no_undefined_identifiers():
    """A name that exists nowhere is a ReferenceError waiting for its path.

    ``parallelCopyFollow`` sat in placeOrderWithIdempotency from the pipeline
    refactor until 2026-08-31, defined nowhere in the file or its history,
    on a line that only executes when an order is actually submitted.  It
    cost the first two live probe orders and could not have been found by
    any test that did not reach the submit path -- which, before go-live,
    none could.  `node --check` cannot see it either: an undefined
    identifier is valid syntax.

    So the module is loaded and its exports touched, which is what surfaces
    a free variable at parse-to-runtime rather than at call time.
    """
    result = subprocess.run(
        [
            "node", "--input-type=module", "-e",
            "const m = await import(%s);"
            "if (typeof m.livePausePresent !== 'function') throw new Error('load failed');"
            "console.log('ok');"
            % json.dumps((ROOT / "scripts" / "clob_order_service.mjs").as_uri()),
        ],
        cwd=ROOT, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout

    # And the specific shape that bit, checked the only way that is reliable:
    # ask the engine.  A regex over the submit path cannot tell an identifier
    # from a keyword, but `new Function` over the method body throws
    # ReferenceError on a free variable at construction -- before any call --
    # which is exactly the class of defect parallelCopyFollow belonged to.
    source = (ROOT / "scripts" / "clob_order_service.mjs").read_text(encoding="utf-8")
    assert "parallelCopyFollow" not in _code_only(source)
