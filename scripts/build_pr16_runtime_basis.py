#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from bayes_basket_target.pr16_runtime_basis import build_pr16_runtime_artifacts
from bayes_basket_target.resource_bounds import write_bounded_json
def main()->int:
 p=argparse.ArgumentParser(); p.add_argument('--current-pointer',type=Path,required=True); p.add_argument('--as-of-utc',required=True); p.add_argument('--expected-basis-output',type=Path,required=True); p.add_argument('--terminal-basis-output',type=Path,required=True); p.add_argument('--terminal-receipt-output',type=Path,required=True); a=p.parse_args()
 expected,terminals,receipt=build_pr16_runtime_artifacts(current_pointer_path=a.current_pointer,as_of_utc=a.as_of_utc)
 for path,value in ((a.expected_basis_output,expected),(a.terminal_basis_output,terminals),(a.terminal_receipt_output,receipt)): write_bounded_json(path,value)
 print(json.dumps({k:receipt[k] for k in ('state','runtime_expected_key_count','unresolved_service_count','starts_services','content_sha256')},indent=2,sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
