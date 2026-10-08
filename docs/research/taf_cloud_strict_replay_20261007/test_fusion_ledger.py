import copy
import unittest
from unittest.mock import patch
from fusion_ledger import serve_one, served_fact, calculate_veto
from rebuild import utc
from bayes_basket_target import fusion_blend as FB


def fact():
    return dict(station_id='ZUUU',event_date='2026-10-04',horizon_bin=12,
        decision_at_utc='2026-10-04T10:00:00Z',settlement_timezone='Asia/Shanghai',
        checkpoint='cp',system_probabilities={'a':{'cold':.8,'warm':.2},'b':{'cold':.2,'warm':.8}},
        seat_weights={'a':.5,'b':.5},content_sha256='fact',truth_revisions=[
            dict(truth_revision=0,truth_id='t0',truth_lane='AWC',
                truth_known_at_utc='2026-10-04T16:00:00Z',truth_probability={'a':.8,'b':.2}),
            dict(truth_revision=1,truth_id='t1',truth_lane='SETTLEMENT_SOURCE',
                truth_known_at_utc='2026-10-06T16:00:00Z',truth_probability={'a':.2,'b':.8})])


class Ledger(unittest.TestCase):
    def setUp(self):
        self.controls=dict(family_by_system={'a':'A','b':'B'},family_caps={'A':.75,'B':.75},lead_by_system={'a':'h12_24','b':'h12_24'})
        self.params=dict(params_id='fit',computed_at=utc('2026-10-04T00:00:00Z'),
            computed_at_utc='2026-10-04T00:00:00Z',theta={'a':3.,'b':-3.},days={'a':40,'b':40},tau=5.)

    def test_no_future_params_and_exact_cutoff(self):
        p=fact();future={**self.params,'computed_at':utc('2026-10-04T10:00:00.000001Z')}
        tables=dict(params={'ZUUU':[future]},vetoes=[])
        self.assertEqual(serve_one(p,self.controls,tables)['receipt']['state'],'SEAT_NO_STATION_PARAMS')
        tables['params']['ZUUU'][0]['computed_at']=utc(p['decision_at_utc'])
        r=serve_one(p,self.controls,tables)
        self.assertEqual(r['receipt']['state'],'APPLIED')
        self.assertLessEqual(r['weights']['a'],.75+1e-12)
        self.assertAlmostEqual(sum(r['weights'].values()),1.)

    def test_veto_visibility_and_loader_restored_on_failure(self):
        p=fact();tables=dict(params={'ZUUU':[self.params]},vetoes=[dict(veto_id='v',computed_at=utc('2026-10-04T11:00:00Z'),vetoed=True)])
        self.assertEqual(serve_one(p,self.controls,tables)['receipt']['state'],'APPLIED')
        tables['vetoes'][0]['computed_at']=utc(p['decision_at_utc'])
        r=serve_one(p,self.controls,tables)
        self.assertEqual(r['receipt']['state'],'SEAT_VETOED');self.assertEqual(r['weights'],p['seat_weights'])
        original=FB.load_serving_tables
        with patch.object(FB,'overlay_system_weights',side_effect=RuntimeError('forced')):
            with self.assertRaises(RuntimeError):serve_one(p,self.controls,tables)
        self.assertIs(FB.load_serving_tables,original)

    def test_truth_revision_causality_and_duplicate_rejection(self):
        p=fact();r=serve_one(p,self.controls,dict(params={'ZUUU':[self.params]},vetoes=[]))
        self.assertIsNone(served_fact(p,r,utc('2026-10-04T15:59:59Z')))
        row,proof=served_fact(p,r,utc('2026-10-04T16:00:00Z'))
        self.assertEqual(row.revision,0);self.assertEqual(proof['serving_sha'],r['content_sha256'])
        row,proof=served_fact(p,r,utc('2026-10-06T16:00:00Z'))
        self.assertEqual(row.revision,1);self.assertEqual(proof['truth_lane'],'SETTLEMENT_SOURCE')
        p['truth_revisions'].append(copy.deepcopy(p['truth_revisions'][-1]))
        with self.assertRaises(ValueError):served_fact(p,r,utc('2026-10-06T16:00:00Z'))

    def test_unclosed_and_later_revision_cannot_change_prefix_veto(self):
        p=fact();r=serve_one(p,self.controls,dict(params={'ZUUU':[self.params]},vetoes=[]))
        at=utc('2026-10-05T00:00:00Z');first=calculate_veto([(p,r)],at)
        p2=copy.deepcopy(p);p2['truth_revisions'][-1]['truth_probability']={'a':0.,'b':1.}
        self.assertEqual(first,calculate_veto([(p2,r)],at))
        with self.assertRaises(ValueError):served_fact(p,r,utc('2026-10-04T09:59:59Z'))

    def test_non_applied_receipts_excluded_from_veto(self):
        p=fact();r=serve_one(p,self.controls,dict(params={},vetoes=[]))
        evidence,refs=calculate_veto([(p,r)],utc('2026-10-05T00:00:00Z'))
        self.assertEqual(evidence['station_days'],0);self.assertEqual(len(refs),1)
        r['receipt']['state']='APPLIED';r['weights']={'a':.75,'b':.25}
        evidence,refs=calculate_veto([(p,r)],utc('2026-10-05T00:00:00Z'))
        self.assertEqual(evidence['station_days'],1);self.assertFalse(evidence['enough_evidence'])


if __name__=='__main__':unittest.main()
