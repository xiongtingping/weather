"""Adversarial checks of native curve pairing; no production changes."""
import copy
import json
import unittest
from audit_native_physics import check_curve, require_hash
from bayes_basket_target.contracts import content_sha256
from study import ROOT, Guard


def seal(obj):
    obj['content_sha256']=content_sha256({k:v for k,v in obj.items() if k!='content_sha256'})


class PairingChecks(unittest.TestCase):
    def setUp(self):
        p=ROOT/'data/runtime/bayes_basket_target_nwp/official_native_capture/eccc_2026-09-18_d1.json'
        self.cap=next(c for c in json.loads(p.read_text())['captures'] if c['station_id']=='ZUUU')
        self.run=self.cap['run_init_utc'];sid=self.cap['ensemble_system_id']
        self.sample={'station':'ZUUU','event_date':'2026-09-18','timezone':'Asia/Shanghai',
                     'root_receipts':{sid:self.cap['archive_query_at_utc']},
                     'p0_nominal_decision_at':'2026-09-17T15:00:00Z','decision_at':'2026-09-17T15:01:00Z'}

    def reject(self,reason):
        seal(self.cap['family_temperature_curve']);seal(self.cap)
        with self.assertRaisesRegex(ValueError,reason):check_curve(self.cap,self.sample,self.run)

    def test_future_valid_times_are_valid_forecasts(self):
        self.assertTrue(check_curve(self.cap,self.sample,self.run)['points'])

    def test_same_day_later_run_cannot_replace_root(self):
        self.cap['run_init_utc']='2026-09-17T12:00:00Z';self.reject('run_mismatch')

    def test_matching_timestamp_does_not_allow_wrong_station(self):
        self.cap['family_temperature_curve']['station_id']='ZBAA';self.reject('scope_mismatch')

    def test_late_curve_receipt_fails(self):
        self.cap['family_temperature_curve']['available_at_utc']='2026-09-18T00:00:00Z';self.reject('future_receipt')

    def test_different_arrival_is_not_same_capture(self):
        self.cap['archive_query_at_utc']='2026-09-17T09:00:00Z';self.reject('receipt_mismatch')

    def test_mismatched_field_receipt_fails(self):
        self.cap['family_temperature_curve']['source_field_receipt_set_sha256']='a'*64;self.reject('receipt_hash_mismatch')

    def test_receipt_content_tampering_even_after_reseal_fails(self):
        self.cap['field_receipts'][0]['object_key']='wrong';self.reject('receipt_set_corrupt')

    def test_duplicate_time_fails(self):
        self.cap['family_temperature_curve']['points'].append(self.cap['family_temperature_curve']['points'][-1]);self.reject('curve_time_order')

    def test_timezone_day_boundary_fails(self):
        self.cap['family_temperature_curve']['points'][0]['valid_time_utc']='2026-09-17T15:00:00Z';self.reject('curve_wrong_day')

    def test_unsigned_change_fails(self):
        self.cap['latitude']=0
        with self.assertRaisesRegex(ValueError,'content_hash_mismatch'):check_curve(self.cap,self.sample,self.run)

    def test_republisher_cannot_borrow_native_model_id(self):
        self.cap['source_class']='aggregator_republisher';self.reject('non_native_source')

    def test_unapproved_url_cannot_borrow_source_class(self):
        self.cap['source_prefix']='https://dd.weather.gc.ca.attacker.invalid/';self.reject('non_native_source')

    def test_curve_cannot_backdate_arrival(self):
        self.cap['family_temperature_curve']['available_at_utc']='2026-09-17T07:00:00Z';self.reject('curve_arrival_mismatch')


if __name__=='__main__':
    guard=Guard();unittest.main(verbosity=2)
