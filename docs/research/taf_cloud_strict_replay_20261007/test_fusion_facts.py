import copy
import unittest
from fusion_facts import horizon_bin,project
from fusion_refit import visible_fact
from rebuild import utc
from bayes_basket_target import fusion_blend as FB


class Fusion(unittest.TestCase):
    def test_horizon_boundaries_dst_and_closed_day(self):
        self.assertEqual(horizon_bin('2026-11-01T05:00:00Z','2026-11-01','America/New_York'),24)
        self.assertEqual(horizon_bin('2026-03-08T05:00:00Z','2026-03-08','America/New_York'),12)
        self.assertEqual(horizon_bin('2026-10-05T12:00:00Z','2026-10-05','UTC'),12)
        with self.assertRaises(ValueError):horizon_bin('2026-10-06T00:00:00Z','2026-10-05','UTC')
        with self.assertRaises(ValueError):horizon_bin('2026-10-06T01:00:00Z','2026-10-05','UTC')

    def test_asof_truth_revision_and_future_parent(self):
        p=dict(station_id='KATL',event_date='2026-10-04',horizon_bin=12,decision_at_utc='2026-10-04T12:00:00Z',system_probabilities={'a':{'x':1}},seat_weights={'a':1},content_sha256='x',truth_revisions=[dict(truth_revision=0,truth_id='t0',truth_lane='AWC',truth_known_at_utc='2026-10-05T04:00:00Z',truth_probability={'a':.8}),dict(truth_revision=1,truth_id='t1',truth_lane='SETTLEMENT_SOURCE',truth_known_at_utc='2026-10-06T04:00:00Z',truth_probability={'a':.2})])
        self.assertIsNone(visible_fact(p,utc('2026-10-05T03:59:59Z')))
        f,proof=visible_fact(p,utc('2026-10-05T04:00:00Z'));self.assertEqual(f.revision,0);self.assertEqual(proof['truth_id'],'t0')
        f,proof=visible_fact(p,utc('2026-10-06T04:00:00Z'));self.assertEqual(f.revision,1);self.assertEqual(proof['truth_lane'],'SETTLEMENT_SOURCE')
        with self.assertRaises(ValueError):visible_fact(p,utc('2026-10-04T11:59:59Z'))
        p['truth_revisions'].append(copy.deepcopy(p['truth_revisions'][-1]))
        with self.assertRaises(ValueError):visible_fact(p,utc('2026-10-06T04:00:00Z'))

    def test_fahrenheit_ladder_no_integer_temperature_proxy(self):
        intervals={'cold':[None,20.833333333333332],'70-71':[20.833333333333332,21.944444444444443],'warm':[21.944444444444443,None]}
        keys,points=FB.representative_points(intervals)
        self.assertEqual(keys[FB.truth_bucket_index(intervals,keys,21.94)],'70-71')
        self.assertEqual(keys[FB.truth_bucket_index(intervals,keys,21.944444444444443)],'warm')
        self.assertAlmostEqual(points[1],21.388888888888886)


if __name__=='__main__':unittest.main()
