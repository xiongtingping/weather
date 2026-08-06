"""PR16 unresolved runtime release basis for the three fixed user services."""
from __future__ import annotations
from datetime import datetime, timezone
import json, os, subprocess
from pathlib import Path
from typing import Any
from .contracts import ContractError, content_sha256
from .canonical_expected_scope_manifest import build_scope_basis

SCHEMA="bayes_basket_target_pr16_runtime_terminal_bundle_v1"
SERVICES=("com.weather.clob_order_service","com.weather.market_ws","com.weather.weather_basket_yes")
class PR16RuntimeBasisError(ContractError): pass
def _utc(v:object)->str:
 try: p=datetime.fromisoformat(str(v).replace('Z','+00:00'))
 except ValueError as e: raise PR16RuntimeBasisError('pr16_as_of_invalid') from e
 if p.tzinfo is None: raise PR16RuntimeBasisError('pr16_as_of_timezone_required')
 return p.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
def build_pr16_runtime_artifacts(*, current_pointer_path:str|Path, as_of_utc:str)->tuple[dict[str,Any],dict[str,Any],dict[str,Any]]:
 cutoff=_utc(as_of_utc); path=Path(current_pointer_path).expanduser().resolve(strict=False)
 if path.exists():
  pointer=json.loads(path.read_text(encoding='utf-8')); identity={k:v for k,v in pointer.items() if k!='content_sha256'}
  if pointer.get('content_sha256')!=content_sha256(identity) or pointer.get('state')!='BLOCKED': raise PR16RuntimeBasisError('pr16_current_pointer_not_blocked_valid')
  pointer_sha=str(pointer['content_sha256']); pointer_state='BLOCKED'; blocker='RUNTIME_RELEASE_ID_MISSING'
 else:
  pointer_sha=None; pointer_state='MISSING'; blocker='CURRENT_POINTER_MISSING'
 pointer_source_sha=pointer_sha or content_sha256({'current_pointer_path':str(path),'state':'MISSING'})
 uid=os.getuid(); launch_domain=f'gui/{uid}'; rows=[]
 for service in SERVICES:
  result=subprocess.run(['launchctl','print',f'{launch_domain}/{service}'],capture_output=True,text=True,check=False,timeout=5)
  if result.returncode==0: raise PR16RuntimeBasisError('pr16_service_must_remain_absent')
  row_identity={"schema":"bayes_basket_target_pr16_runtime_service_terminal_v1","service_label":service,"launch_domain":launch_domain,"as_of_utc":cutoff,"state":"BLOCKED","blockers":[blocker],"unproven_natural_key_fields":["runtime_release_id"],"service_present":False,"current_pointer_content_sha256":pointer_sha,"current_pointer_state":pointer_state,"starts_service":False}
  rows.append({**row_identity,'content_sha256':content_sha256(row_identity)})
 inventory_sha=content_sha256(rows)
 expected=build_scope_basis(scope_type='RUNTIME',as_of_utc=cutoff,expected_keys=(),source_content_sha256s=(pointer_source_sha,inventory_sha),unresolved_input_receipts=[x['content_sha256'] for x in rows])
 terminal_identity={"schema":"bayes_basket_target_pr16_runtime_terminal_basis_v1","as_of_utc":cutoff,"state":"BLOCKED","release_state":"BLOCKED","service_count":3,"terminal_scopes":rows,"current_pointer_content_sha256":pointer_sha,"current_pointer_state":pointer_state,"current_pointer_release_id_not_reused":True,"writes_database":False,"starts_services":False,"calls_order_gateway":False,"orders_submitted":0}
 terminals={**terminal_identity,'content_sha256':content_sha256(terminal_identity)}
 bundle_identity={"schema":SCHEMA,"as_of_utc":cutoff,"state":"BLOCKED","release_state":"BLOCKED","blockers":[blocker],"runtime_expected_key_count":0,"canonical_expected_basis_emitted":True,"unresolved_service_count":3,"expected_basis_content_sha256":expected['content_sha256'],"terminal_basis_content_sha256":terminals['content_sha256'],"current_pointer_content_sha256":pointer_sha,"current_pointer_state":pointer_state,"publishes_pr04_pointer":False,"starts_services":False,"releases_live_pause":False,"calls_order_gateway":False,"orders_submitted":0,"production_no_live":True}
 bundle={**bundle_identity,'content_sha256':content_sha256(bundle_identity)}
 return expected,terminals,bundle
__all__=['PR16RuntimeBasisError','SERVICES','build_pr16_runtime_artifacts']
