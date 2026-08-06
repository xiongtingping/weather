from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from weather_basket_yes_schema import apply_weather_basket_yes_schema


ROOT = Path(__file__).resolve().parents[1]


def test_exact_canonical_intent_is_only_pause_read_only_exception(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """INSERT INTO reported_trajectory_market_registry(
             slug,event_date,outcome,city_slug,station_id,settlement_timezone,
             settlement_unit,source_lane,source_eligible,source_block_reason,
             resolution_url,rules_sha256,yes_token,no_token,market_active,
             market_accepting_orders,registry_json,refreshed_at
           ) VALUES('slug','2099-08-05','82-83°F','atlanta','KATL','America/New_York',
                    'F','awc',1,NULL,'url',?,'123456','654321',1,1,'{}','2099-08-05T00:00:00Z')""",
        ("a" * 64,),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_windows(
             window_id,station_id,event_date,settlement_timezone,provider_id,
             source_class,forecast_generated_at_utc,fetched_at_utc,
             argmax_window_start_utc,argmax_window_end_utc,argmax_temperature_c,
             argmax_periods_json,points_receipt_sha256,forecast_receipt_sha256,
             raw_object_ref,coordinate_registry_sha256,content_sha256,state,
             timing_only,provides_temperature_bucket,provides_probability,enters_p0,enters_p0m
           ) VALUES('window','KATL','2099-08-05','America/New_York',
                    'NOAA_NWS_GRIDPOINT_HOURLY','official_forecast_product',
                    '2099-08-05T00:00:00Z','2099-08-05T01:00:00Z',
                    '2099-08-05T11:00:00Z','2099-08-05T13:00:00Z',30,?,
                    ?,?,'/raw',?,?, 'FROZEN',1,0,0,0,0)""",
        (
            json.dumps([{
                "start_utc": "2099-08-05T11:00:00Z",
                "end_utc": "2099-08-05T13:00:00Z",
            }]),
            "1" * 64, "2" * 64, "3" * 64, "4" * 64,
        ),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_entry_facts(
             entry_fact_id,station_id,event_date,source_lane,observation_id,
             observed_at_utc,received_at_utc,prior_high_c,new_high_c,raw_sha256,
             source_receipt_sha256,window_id,argmax_window_start_utc,market_slug,
             outcome,yes_token,settlement_scalar,settlement_unit,reason,
             content_sha256,gateway_submitted,created_at_utc
           ) VALUES('entry','KATL','2099-08-05','madis_hfmetar','obs',
                    '2099-08-05T12:00:00Z','2099-08-05T12:00:01Z',20,21,?,?,
                    'window','2099-08-05T11:00:00Z','slug','82-83°F','123456',82,'F',
                    'ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD',?,0,'2099-08-05T12:00:01Z')""",
        ("b" * 64, "c" * 64, "d" * 64),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_order_intents(
             order_intent_id,entry_fact_id,strategy_id,station_id,event_date,yes_token,
             side,order_type,limit_price,size,requested_notional,idempotency_key,
             ws_snapshot_sha256,guard_snapshot_sha256,guard_snapshot_json,state,
             gateway_request_json,gateway_response_json,created_at_utc,updated_at_utc
           ) VALUES('intent','entry','asos_argmax_new_high_yes_v1','KATL','2099-08-05',
                    '123456','BUY','FOK',0.20,10,2,'idem',?,?,'{}','SUBMITTING','{}','{}',
                    '2099-08-05T12:00:01Z','2099-08-05T12:00:01Z')""",
        ("e" * 64, "f" * 64),
    )
    conn.commit()
    conn.close()
    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    payload = {
        "strategy_id": "asos_argmax_new_high_yes_v1",
        "entry_fact_id": "entry", "order_intent_id": "intent",
        "idempotency_key": "idem", "token_id": "123456", "side": "BUY",
        "order_type": "FOK", "price": 0.20, "size": 10, "notional": 2,
        "allow_market": True, "post_only": False,
        "ws_snapshot_sha256": "e" * 64, "guard_snapshot_sha256": "f" * 64,
    }
    script = f"""
      const {{ asosArgmaxLiveAdmission, latestContiguousArgmaxPeriods }} = await import({json.dumps(service_uri)});
      const payload = {json.dumps(payload)};
      const nowMs = Date.parse('2099-08-05T12:00:02Z');
      const ok = asosArgmaxLiveAdmission(payload, {json.dumps(str(db_path))}, nowMs);
      const bad = asosArgmaxLiveAdmission({{...payload, token_id:'999999'}}, {json.dumps(str(db_path))}, nowMs);
      const outside = asosArgmaxLiveAdmission(payload, {json.dumps(str(db_path))}, Date.parse('2099-08-05T13:00:00Z'));
      const selected = latestContiguousArgmaxPeriods([
        {{start_utc:'2099-08-05T09:00:00Z',end_utc:'2099-08-05T10:00:00Z'}},
        {{start_utc:'2099-08-05T11:00:00Z',end_utc:'2099-08-05T13:00:00Z'}},
      ]);
      console.log(JSON.stringify({{ok,bad,outside,selected}}));
    """
    env = {
        **os.environ,
        "PM_DATA_DIR": str(tmp_path),
        "PM_ARCHIVE_DB": str(db_path),
        "PM_ASOS_ARGMAX_LIVE_ENABLED": "1",
    }
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["ok"]["ok"] is True
    assert value["bad"] == {"ok": False, "reason": "asos_argmax_live_intent_mismatch"}
    assert value["outside"] == {"ok": False, "reason": "asos_argmax_live_period_not_active"}
    assert value["selected"] == [{
        "start_utc": "2099-08-05T11:00:00Z",
        "end_utc": "2099-08-05T13:00:00Z",
    }]


def test_exact_filled_shares_are_admitted_for_gtc_exit_at_099(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    apply_weather_basket_yes_schema(conn)
    conn.execute(
        """INSERT INTO reported_trajectory_market_registry(
             slug,event_date,outcome,city_slug,station_id,settlement_timezone,
             settlement_unit,source_lane,source_eligible,source_block_reason,
             resolution_url,rules_sha256,yes_token,no_token,market_active,
             market_accepting_orders,registry_json,refreshed_at
           ) VALUES('slug','2099-08-05','82-83°F','atlanta','KATL','America/New_York',
                    'F','awc',1,NULL,'url',?,'123456','654321',1,1,'{}',
                    '2099-08-05T00:00:00Z')""",
        ("a" * 64,),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_windows(
             window_id,station_id,event_date,settlement_timezone,provider_id,
             source_class,forecast_generated_at_utc,fetched_at_utc,
             argmax_window_start_utc,argmax_window_end_utc,argmax_temperature_c,
             argmax_periods_json,points_receipt_sha256,forecast_receipt_sha256,
             raw_object_ref,coordinate_registry_sha256,content_sha256,state,
             timing_only,provides_temperature_bucket,provides_probability,enters_p0,enters_p0m
           ) VALUES('window','KATL','2099-08-05','America/New_York',
                    'NOAA_NWS_GRIDPOINT_HOURLY','official_forecast_product',
                    '2099-08-05T00:00:00Z','2099-08-05T01:00:00Z',
                    '2099-08-05T11:00:00Z','2099-08-05T13:00:00Z',30,?,
                    ?,?,'/raw',?,?, 'FROZEN',1,0,0,0,0)""",
        (
            json.dumps([{
                "start_utc": "2099-08-05T11:00:00Z",
                "end_utc": "2099-08-05T13:00:00Z",
            }]),
            "1" * 64, "2" * 64, "3" * 64, "4" * 64,
        ),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_entry_facts(
             entry_fact_id,station_id,event_date,source_lane,observation_id,
             observed_at_utc,received_at_utc,prior_high_c,new_high_c,raw_sha256,
             source_receipt_sha256,window_id,argmax_window_start_utc,market_slug,
             outcome,yes_token,settlement_scalar,settlement_unit,reason,
             content_sha256,gateway_submitted,created_at_utc
           ) VALUES('entry','KATL','2099-08-05','madis_hfmetar','obs',
                    '2099-08-05T12:00:00Z','2099-08-05T12:00:01Z',20,21,?, ?,
                    'window','2099-08-05T11:00:00Z','slug','82-83°F','123456',82,'F',
                    'ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD',?,0,
                    '2099-08-05T12:00:01Z')""",
        ("b" * 64, "c" * 64, "d" * 64),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_order_intents(
             order_intent_id,entry_fact_id,strategy_id,station_id,event_date,yes_token,
             side,order_type,limit_price,size,requested_notional,idempotency_key,
             ws_snapshot_sha256,guard_snapshot_sha256,guard_snapshot_json,state,
             gateway_request_json,gateway_response_json,exchange_order_id,size_matched,
             created_at_utc,updated_at_utc
           ) VALUES('buy','entry','asos_argmax_new_high_yes_v1','KATL','2099-08-05',
                    '123456','BUY','FOK',0.20,10,2,'buy-idem',?,?,'{}',
                    'EXCHANGE_ACCEPTED','{}','{}','buy-order',10,
                    '2099-08-05T12:00:01Z','2099-08-05T12:00:01Z')""",
        ("e" * 64, "f" * 64),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_exit_order_intents(
             exit_order_intent_id,buy_order_intent_id,entry_fact_id,strategy_id,
             station_id,event_date,yes_token,side,order_type,limit_price,size,
             source_buy_size_matched,idempotency_key,guard_snapshot_sha256,state,
             created_at_utc,updated_at_utc
           ) VALUES('exit','buy','entry','asos_argmax_yes_exit_099_v1','KATL',
                    '2099-08-05','123456','SELL','GTC',0.99,10,10,'exit-idem',?,
                    'SUBMITTING','2099-08-05T12:00:02Z','2099-08-05T12:00:02Z')""",
        ("9" * 64,),
    )
    conn.commit()
    conn.close()
    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    payload = {
        "strategy_id": "asos_argmax_yes_exit_099_v1",
        "buy_order_intent_id": "buy", "entry_fact_id": "entry",
        "order_intent_id": "exit", "idempotency_key": "exit-idem",
        "token_id": "123456", "side": "SELL", "order_type": "GTC",
        "price": 0.99, "limit_price": 0.99, "size": 10, "notional": 0,
        "allow_market": False, "post_only": False,
        "guard_snapshot_sha256": "9" * 64,
    }
    script = f"""
      const {{ asosArgmaxExitAdmission }} = await import({json.dumps(service_uri)});
      const payload = {json.dumps(payload)};
      const ok = asosArgmaxExitAdmission(payload, {json.dumps(str(db_path))});
      const oversell = asosArgmaxExitAdmission({{...payload,size:11}}, {json.dumps(str(db_path))});
      const wrongPrice = asosArgmaxExitAdmission({{...payload,price:.98,limit_price:.98}}, {json.dumps(str(db_path))});
      console.log(JSON.stringify({{ok,oversell,wrongPrice}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        env={**os.environ, "PM_ASOS_ARGMAX_LIVE_ENABLED": "1"},
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["ok"]["ok"] is True
    assert value["oversell"] == {
        "ok": False, "reason": "asos_argmax_exit_intent_mismatch",
    }
    assert value["wrongPrice"] == {
        "ok": False, "reason": "asos_argmax_exit_intent_mismatch",
    }


def test_rotation_sell_admission_is_exact_and_cannot_oversell_or_change_target(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    apply_weather_basket_yes_schema(conn)
    for slug, outcome, token in (
        ("old-slug", "80-81°F", "111111"),
        ("new-slug", "82-83°F", "222222"),
    ):
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry(
                 slug,event_date,outcome,city_slug,station_id,settlement_timezone,
                 settlement_unit,source_lane,source_eligible,source_block_reason,
                 resolution_url,rules_sha256,yes_token,no_token,market_active,
                 market_accepting_orders,registry_json,refreshed_at
               ) VALUES(?,?,?,'atlanta','KATL','America/New_York','F','awc',1,NULL,
                        'url',?,?,?,1,1,'{}','2099-08-05T00:00:00Z')""",
            (slug, "2099-08-05", outcome, "a" * 64, token, str(int(token) + 1)),
        )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_entry_facts(
             entry_fact_id,station_id,event_date,source_lane,observation_id,
             observed_at_utc,received_at_utc,prior_high_c,new_high_c,raw_sha256,
             source_receipt_sha256,window_id,argmax_window_start_utc,market_slug,
             outcome,yes_token,settlement_scalar,settlement_unit,reason,
             content_sha256,gateway_submitted,created_at_utc
           ) VALUES('old-entry','KATL','2099-08-05','madis_hfmetar','old-obs',
                    '2099-08-05T11:00:00Z','2099-08-05T11:00:01Z',20,21,?,?,
                    'window','2099-08-05T11:00:00Z','old-slug','80-81°F','111111',80,'F',
                    'ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD',?,0,'2099-08-05T11:00:01Z')""",
        ("b" * 64, "c" * 64, "d" * 64),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_entry_facts(
             entry_fact_id,station_id,event_date,source_lane,observation_id,
             observed_at_utc,received_at_utc,prior_high_c,new_high_c,raw_sha256,
             source_receipt_sha256,window_id,argmax_window_start_utc,market_slug,
             outcome,yes_token,settlement_scalar,settlement_unit,reason,
             content_sha256,gateway_submitted,created_at_utc
           ) VALUES('new-entry','KATL','2099-08-05','madis_hfmetar','new-obs',
                    '2099-08-05T12:00:00Z','2099-08-05T12:00:01Z',21,22,?,?,
                    'window','2099-08-05T11:00:00Z','new-slug','82-83°F','222222',82,'F',
                    'ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD',?,0,'2099-08-05T12:00:01Z')""",
        ("e" * 64, "f" * 64, "1" * 64),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_order_intents(
             order_intent_id,entry_fact_id,strategy_id,station_id,event_date,yes_token,
             side,order_type,limit_price,size,requested_notional,idempotency_key,
             ws_snapshot_sha256,guard_snapshot_sha256,guard_snapshot_json,state,
             gateway_request_json,gateway_response_json,exchange_order_id,size_matched,
             created_at_utc,updated_at_utc
           ) VALUES('old-buy','old-entry','asos_argmax_new_high_yes_v1','KATL','2099-08-05',
                    '111111','BUY','FOK',0.20,10,2,'old-buy-idem',?,?,'{}',
                    'EXCHANGE_ACCEPTED','{}','{}','buy-order',10,
                    '2099-08-05T11:00:01Z','2099-08-05T11:00:01Z')""",
        ("2" * 64, "3" * 64),
    )
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_rotation_sell_intents(
             rotation_sell_intent_id,target_entry_fact_id,source_buy_order_intent_id,
             strategy_id,station_id,event_date,old_yes_token,new_yes_token,side,
             order_type,limit_price,size,ws_snapshot_sha256,guard_snapshot_sha256,
             state,idempotency_key,created_at_utc,updated_at_utc
           ) VALUES('rotation','new-entry','old-buy','asos_argmax_rotation_sell_fak_v1',
                    'KATL','2099-08-05','111111','222222','SELL','FAK',0.18,8,?,?,
                    'SUBMITTING','rotation-idem','2099-08-05T12:00:01Z',
                    '2099-08-05T12:00:01Z')""",
        ("4" * 64, "5" * 64),
    )
    conn.commit()
    conn.close()
    service_uri = (ROOT / "scripts" / "clob_order_service.mjs").as_uri()
    payload = {
        "strategy_id": "asos_argmax_rotation_sell_fak_v1",
        "target_entry_fact_id": "new-entry",
        "source_buy_order_intent_id": "old-buy",
        "order_intent_id": "rotation", "idempotency_key": "rotation-idem",
        "token_id": "111111", "new_yes_token": "222222",
        "side": "SELL", "order_type": "FAK", "price": 0.18,
        "limit_price": 0.18, "size": 8, "notional": 0,
        "allow_market": True, "post_only": False,
        "ws_snapshot_sha256": "4" * 64, "guard_snapshot_sha256": "5" * 64,
    }
    script = f"""
      const {{ asosArgmaxRotationSellAdmission }} = await import({json.dumps(service_uri)});
      const payload = {json.dumps(payload)};
      const ok = asosArgmaxRotationSellAdmission(payload, {json.dumps(str(db_path))});
      const oversell = asosArgmaxRotationSellAdmission({{...payload,size:11}}, {json.dumps(str(db_path))});
      const changedTarget = asosArgmaxRotationSellAdmission(
        {{...payload,new_yes_token:'333333'}}, {json.dumps(str(db_path))}
      );
      const changedPrice = asosArgmaxRotationSellAdmission(
        {{...payload,price:.17,limit_price:.17}}, {json.dumps(str(db_path))}
      );
      console.log(JSON.stringify({{ok,oversell,changedTarget,changedPrice}}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        env={**os.environ, "PM_ASOS_ARGMAX_LIVE_ENABLED": "1"},
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["ok"]["ok"] is True
    assert value["oversell"]["reason"] == "asos_argmax_rotation_sell_intent_mismatch"
    assert value["changedTarget"]["reason"] == "asos_argmax_rotation_sell_intent_mismatch"
    assert value["changedPrice"]["reason"] == "asos_argmax_rotation_sell_intent_mismatch"
