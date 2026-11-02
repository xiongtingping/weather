"""Independent numerical, boundary, missingness and time-travel checks."""
import copy
import json
import math
import unittest
import numpy as np
from scipy.optimize._numdiff import approx_derivative
from model import Batch, geometry, features, predict, scored_rows
from evaluate import partition, paired_intervals, validate_evidence
from study import OUT, Guard


def fixture(width=1.0):
    return {'station':'TEST','sample_sha':'fixture','event_date':'2026-09-12','decision_at':'2026-09-11T10:00:00Z',
            'intervals_c':{'low':[None,20-width/2],'mid':[20-width/2,20+width/2],'high':[20+width/2,None]},
            'probabilities':{'low':.2,'mid':.5,'high':.3},'truth_c':20.,'tx_c':21.,'native_mean_c':20.,
            'cloud':.5,'precip':.2,'wind_mps':4.,'taf_age_h':2.,'taf_day_coverage':.8}


class Checks(unittest.TestCase):
    def test_crps_independent_cdf_integral(self):
        rng=np.random.default_rng(17)
        for _ in range(40):
            s=fixture(float(rng.uniform(.4,2)))
            p=rng.dirichlet([1,1,1]);s['probabilities']=dict(zip(s['probabilities'],p));s['truth_c']=float(rng.uniform(14,26))
            _,c,p,_,_=geometry(s);breaks=sorted(set(c.tolist()+[s['truth_c']]))
            integral=0.
            for a,b in zip(breaks,breaks[1:]):
                midpoint=(a+b)/2;F=float(p[c<=midpoint].sum());H=float(s['truth_c']<=midpoint)
                integral+=(b-a)*(F-H)**2
            actual=scored_rows([s],{'arm':'identity'})[0]['crps_c']
            self.assertAlmostEqual(actual,integral,places=10)

    def test_gradient_finite_difference(self):
        rows=[fixture(),fixture(1.13),fixture(.7)];rows[1]['truth_c']=22;rows[2]['truth_c']=18
        b=Batch(rows);x=np.array([[1,.5],[1,-.4],[1,1.1]]);theta=np.array([.1,-.02,.04,.07])
        loss,gradient=b.loss(theta,x,.1)
        numeric=approx_derivative(lambda t:b.loss(t,x,.1)[0],theta,method='3-point').ravel()
        np.testing.assert_allclose(gradient,numeric,rtol=1e-6,atol=1e-8)

    def test_identity_zero_and_tails(self):
        s=fixture();s['probabilities']={'low':0.,'mid':.7,'high':.3};s['truth_c']=10
        b=Batch([s]);q=b.predict(np.zeros((1,2)),np.ones((1,1)))
        np.testing.assert_allclose(q,b.p,rtol=0,atol=1e-15)
        r=scored_rows([s],{'arm':'identity'})[0]
        self.assertTrue(r['zero_hit']);self.assertIsNone(r['nll']);self.assertTrue(math.isfinite(r['crps_c']))

    def test_extreme_parameters_nonnegative_mass(self):
        b=Batch([fixture()])
        for a in [-2.,0.,2.]:
            for scale in [-2.,0.,2.]:
                q=b.predict(np.array([[a,scale]]),np.ones((1,1)))
                self.assertTrue(np.isfinite(q).all());self.assertGreaterEqual(q.min(),0);self.assertAlmostEqual(q.sum(),1)

    def test_market_boundaries_and_nonunit_grid(self):
        for width in [1.,2*5/9,.75,1.8]:
            s=fixture(width);boundary=20+width/2
            for y,hit in [(boundary-1e-8,1),(boundary,2),(boundary+1e-8,2),(-100,0),(100,2)]:
                s['truth_c']=y;self.assertEqual(geometry(s)[3],hit)

    def test_temperature_translation_invariance(self):
        a=fixture();b=copy.deepcopy(a);shift=17.2
        b['truth_c']+=shift
        b['intervals_c']={k:[None if x is None else x+shift for x in v] for k,v in b['intervals_c'].items()}
        self.assertAlmostEqual(scored_rows([a],{'arm':'identity'})[0]['crps_c'],scored_rows([b],{'arm':'identity'})[0]['crps_c'])

    def test_truth_does_not_enter_prediction_features(self):
        s=fixture();altered=copy.deepcopy(s);altered['truth_c']=-20
        for arm in ['tx','weather','joint']:
            np.testing.assert_array_equal(features([s],arm),features([altered],arm))
        b=Batch([s,altered]);q=b.predict(np.array([[.3,-.1]]),np.ones((2,1)))
        np.testing.assert_array_equal(q[0],q[1])

    def test_missing_tx_is_declared(self):
        a=fixture();a['tx_c']=None
        raw=features([a],'tx')[0]
        self.assertEqual(raw[-2],0);self.assertEqual(raw[-1],1)

    def test_geometry_fail_closed(self):
        for error in ('gap','negative','missing','nan'):
            s=fixture()
            if error=='gap':s['intervals_c']['mid'][0]+=.1
            if error=='negative':s['probabilities']['mid']=-.1
            if error=='missing':s['probabilities'].pop('mid')
            if error=='nan':s['probabilities']['mid']=float('nan')
            with self.assertRaises(ValueError):geometry(s)

    def test_future_label_dropped_from_fit(self):
        data=json.loads((OUT/'samples.json').read_text());train,val,refit,outer,_=partition(data)
        mutated=copy.deepcopy(data);target=train[0]['sample_sha']
        for s in mutated:
            if s['sample_sha']==target:s['truth_known_at']='2030-01-01T00:00:00Z'
        t2,_,r2,o2,_=partition(mutated)
        self.assertNotIn(target,{s['sample_sha'] for s in t2});self.assertNotIn(target,{s['sample_sha'] for s in r2})
        self.assertEqual([s['sample_sha'] for s in outer],[s['sample_sha'] for s in o2])

    def test_outer_truth_mutation_cannot_change_training(self):
        data=json.loads((OUT/'samples.json').read_text());before=partition(data)
        altered=copy.deepcopy(data)
        for s in altered:
            if s['event_date']>='2026-09-12':s['truth_c']+=30
        after=partition(altered)
        for i in [0,1,2]:self.assertEqual(before[i],after[i])

    def test_artifact_round_trip_and_scores_reproduce(self):
        data=json.loads((OUT/'samples.json').read_text());outer=partition(data)[3]
        arts=json.loads((OUT/'candidate_artifacts.json').read_text())['artifacts']
        stored=json.loads((OUT/'outer_scores.json').read_text())
        for arm,art in arts.items():
            self.assertEqual(scored_rows(outer,json.loads(json.dumps(art))),stored[arm])

    def test_paired_metrics_reject_different_samples(self):
        a=scored_rows([fixture()],{'arm':'identity'});b=copy.deepcopy(a);b[0]['sample_sha']='other'
        with self.assertRaises(ValueError):paired_intervals(a,b)

    def test_actual_evidence_and_future_receipt_mutations(self):
        data=json.loads((OUT/'samples.json').read_text());validate_evidence(data)
        for mutation in ['taf','native','created']:
            s=copy.deepcopy(data[0])
            if mutation=='taf':s['taf_known_at']='2030-01-01T00:00:00Z'
            if mutation=='native':s['root_receipts'][next(iter(s['root_receipts']))]='2030-01-01T00:00:00Z'
            if mutation=='created':s['p0_created_at']='2030-01-01T00:00:00Z'
            with self.assertRaises(ValueError):validate_evidence([s])
        with self.assertRaises(ValueError):validate_evidence([data[0],data[0]])

    def test_scoring_bucket_matches_materialization_contract(self):
        from bayes_basket_target.materialization import _anchor_bucket_for_temperature
        for width in [1.,2*5/9,.8]:
            s=fixture(width);keys=geometry(s)[0]
            for y in [10,20-width/2,20,20+width/2,30]:
                s['truth_c']=y
                expected=_anchor_bucket_for_temperature(y,root_lineage={'bucket_intervals_c_json':json.dumps(s['intervals_c'])},bucket_ordinals=dict(zip(keys,range(len(keys)))))
                self.assertEqual(keys[geometry(s)[3]],expected)


if __name__=='__main__':
    guard=Guard();unittest.main(verbosity=2)
