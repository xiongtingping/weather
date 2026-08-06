#!/usr/bin/env python3
"""Build PR10 P0 activation/snapshot expected bases from canonical SSOT."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from bayes_basket_target.canonical_expected_scope_manifest import build_p0_expected_scope_bases
from bayes_basket_target.database import DEFAULT_TARGET_DB,resolve_target_database
from bayes_basket_target.resource_bounds import write_bounded_json
def main()->int:
 p=argparse.ArgumentParser(); p.add_argument('--db',type=Path,default=DEFAULT_TARGET_DB); p.add_argument('--as-of-utc',required=True); p.add_argument('--activation-basis-output',type=Path,required=True); p.add_argument('--snapshot-basis-output',type=Path,required=True); p.add_argument('--bundle-output',type=Path,required=True); a=p.parse_args()
 bundle=build_p0_expected_scope_bases(resolve_target_database(a.db),release_as_of_utc=a.as_of_utc); activation=bundle['activation_basis']; snapshot=bundle['snapshot_basis']
 for path,value in ((a.activation_basis_output,activation),(a.snapshot_basis_output,snapshot),(a.bundle_output,bundle)): write_bounded_json(path,value)
 print(json.dumps({'activation_expected_key_count':len(activation['expected_keys']),'activation_unresolved_count':len(activation['unresolved_input_receipts']),'snapshot_expected_key_count':len(snapshot['expected_keys']),'snapshot_unresolved_count':len(snapshot['unresolved_input_receipts']),'content_sha256':bundle['content_sha256']},indent=2,sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
