"""Coverage, receipt-time and canonical winner boundaries."""
import unittest
from datetime import timedelta
from historical_sources import coverage,HistoricalAsos,regional_at
from rebuild import utc


class SourceBoundaries(unittest.TestCase):
    def setUp(self):
        self.start=utc('2026-09-01T00:00:00Z');self.at=self.start+timedelta(hours=1)

    def rows(self,step=60):
        return [dict(id=str(k),_valid=self.start+timedelta(seconds=k),_arrival=self.start+timedelta(seconds=k),temperature_c=25.,receipt_sha='a'*64) for k in range(0,3601,step)]

    def test_late_receipts_cannot_repair_historical_coverage(self):
        rows=self.rows()
        for r in rows:r['_arrival']=self.at+timedelta(seconds=1)
        values,proof=coverage(rows,self.start,self.at,60,300)
        self.assertEqual(values,[]);self.assertFalse(proof['complete'])

    def test_ratio_and_gap_exact_boundaries(self):
        rows=self.rows();_,p=coverage(rows,self.start,self.at,60,300);self.assertTrue(p['complete'])
        rows=[r for r in rows if r['id'] not in {'180','240','300','360'}]
        _,p=coverage(rows,self.start,self.at,60,300);self.assertTrue(p['complete']);self.assertEqual(p['max_internal_gap_seconds'],300)
        rows=[r for r in rows if r['id']!='420']
        _,p=coverage(rows,self.start,self.at,60,300);self.assertFalse(p['complete']);self.assertEqual(p['max_internal_gap_seconds'],360)

    def test_madis_edge_and_gap(self):
        rows=self.rows(300);_,p=coverage(rows,self.start,self.at,300,600);self.assertTrue(p['complete'])
        rows=[r for r in rows if r['id']!='900'];_,p=coverage(rows,self.start,self.at,300,600);self.assertTrue(p['complete'])
        rows=[r for r in rows if r['id']!='1200'];_,p=coverage(rows,self.start,self.at,300,600);self.assertFalse(p['complete'])

    def test_unique_source_priority_and_no_mixed_maximum(self):
        h=object.__new__(HistoricalAsos);h.station='KAAA';h.tz='UTC';h.rows={'IEM':self.rows(),'MADIS':self.rows(300)}
        h.artifacts=[];p,values=h.at('2026-09-01',self.at);self.assertEqual(p['selected_lane'],'AWC');self.assertEqual(values,[])
        # A factually bound test artifact permits source selection; production
        # artifacts are never repaired into this fixture's eligibility.
        h.artifacts=[dict(committed='2026-08-31T00:00:00Z',gaps=[],state='OOS_READY',sha='b'*64,declared_lane='asos_1min',train=20,holdout=8,matched_measurements={'ASOS_5_MINUTE_TEMPERATURE_PRODUCT_PUBLISHED_EACH_MINUTE_TMPF':28})]
        p,values=h.at('2026-09-01',self.at);self.assertEqual(p['selected_lane'],'IEM');self.assertEqual(len(values),61)
        for r in h.rows['MADIS']:r['temperature_c']=50.
        self.assertEqual(max(r['temperature_c'] for r in values),25.)
        h.artifacts[0].update(declared_lane='MADIS_HFMETAR_ASOS',matched_measurements={'ASOS_RUNNING_5_MINUTE_AVERAGE_DISSEMINATED_EVERY_5_MINUTES':28})
        p,values=h.at('2026-09-01',self.at);self.assertEqual(p['selected_lane'],'MADIS')
        h.artifacts[0]['train']=19
        p,values=h.at('2026-09-01',self.at);self.assertEqual(p['selected_lane'],'AWC')

    def test_future_regional_fetch_is_unavailable(self):
        row=dict(ensemble_system_id='NCEP_HRRR_POINT',run_init_utc='2026-09-01T00:00:00Z',hourly_c={'2026-09-01T02:00:00Z':30.},knowable_at_utc='2026-09-01T01:00:01Z')
        p,receipt=regional_at([row],'KATL',self.at,25,25)
        self.assertIsNone(p['regional_headroom_c']);self.assertEqual(receipt,[])


if __name__=='__main__':unittest.main()
