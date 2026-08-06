#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.pr15_market_ws_basis import build_pr15_market_ws_artifacts
from bayes_basket_target.resource_bounds import write_bounded_json
def main()->int:
 p=argparse.ArgumentParser(); p.add_argument('--db',type=Path,default=DEFAULT_TARGET_DB); p.add_argument('--as-of-utc',required=True); p.add_argument('--expected-basis-output',type=Path,required=True); p.add_argument('--registry-envelope-output',type=Path,required=True); p.add_argument('--terminal-basis-output',type=Path,required=True); p.add_argument('--terminal-receipt-output',type=Path,required=True); a=p.parse_args()
 expected,envelope,terminals,receipt=build_pr15_market_ws_artifacts(resolve_target_database(a.db),as_of_utc=a.as_of_utc)
 for path,value in ((a.expected_basis_output,expected),(a.registry_envelope_output,envelope),(a.terminal_basis_output,terminals),(a.terminal_receipt_output,receipt)): write_bounded_json(path,value)
 print(json.dumps({k:receipt[k] for k in ('state','registry_expected_key_count','registry_typed_record_count','snapshot_terminal_scope_count','snapshot_typed_record_count','content_sha256')},indent=2,sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
