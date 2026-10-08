"""Independent hand-calculated boundaries; run with unittest, no DB writes."""
import unittest
from datetime import timedelta
from raw_cloud import decode, interval, scenarios, utc, base_increment, cloud_increment, DecodeError
from rebuild import choose
from bayes_basket_target.native_ensemble_oos import _historical_transport_scope_allowed


def taf(body, issue='2026-10-06T00:00:00Z', header='0600/0724'):
    t=utc(issue)
    return decode(f'TAF ZUUU {t:%d%H%M}Z {header} 22003MPS 6000 {body}',station='ZUUU',issue_time=issue)


class RawCloudBoundaries(unittest.TestCase):
    def test_month_and_year_rollover(self):
        for issue,header,end in [('2026-12-31T18:00:00Z','3118/0124','2027-01-02T00:00:00Z'),('2026-02-28T18:00:00Z','2818/0124','2026-03-02T00:00:00Z')]:
            d=taf('SCT050',issue,header);self.assertEqual(d['valid_to'],end)
            self.assertIsNone(interval(d,end));self.assertEqual(interval(d,utc(end)-timedelta(seconds=1)),(.375,.5))

    def test_fm_exact_half_open(self):
        d=taf('SCT050 FM061200 09005KT 9999 OVC020')
        self.assertEqual(interval(d,'2026-10-06T11:59:59Z'),(.375,.5))
        self.assertEqual(interval(d,'2026-10-06T12:00:00Z'),(1,1))

    def test_becmg_transition_and_inheritance(self):
        d=taf('SCT050 BECMG 0610/0612 BKN020 BECMG 0615/0617 09006KT')
        self.assertEqual(interval(d,'2026-10-06T09:59:59Z'),(.375,.5))
        self.assertEqual(interval(d,'2026-10-06T10:00:00Z'),(.375,.875))
        self.assertEqual(interval(d,'2026-10-06T12:00:00Z'),(.625,.875))
        self.assertEqual(interval(d,'2026-10-06T16:00:00Z'),(.625,.875))

    def test_tempo_never_becomes_prevailing(self):
        d=taf('SCT050 TEMPO 0610/0612 OVC020 PROB30 TEMPO 0611/0613 BKN010')
        self.assertEqual(interval(d,'2026-10-06T11:00:00Z'),(.375,1))
        self.assertEqual(interval(d,'2026-10-06T12:00:00Z'),(.375,.875))
        self.assertEqual(interval(d,'2026-10-06T13:00:00Z'),(.375,.5))

    def test_wind_only_overlay_inherits(self):
        d=taf('SCT050 TEMPO 0610/0612 09006KT')
        self.assertEqual(len(scenarios(d,'2026-10-06T11:00:00Z')),1)

    def test_fourth_cloud_layer_is_retained(self):
        d=taf('SCT006 BKN011 FEW012CB OVC020')
        self.assertEqual(interval(d,'2026-10-06T11:00:00Z',3000),(1,1))

    def test_clear_height_unknown_and_obscuration(self):
        for body in ('NSC','CAVOK','NCD'):
            d=taf(body);self.assertEqual(interval(d,'2026-10-06T11:00:00Z',5000),(0,0));self.assertIsNone(interval(d,'2026-10-06T11:00:00Z',6500))
        for body in ('VV003','BKN///'):
            self.assertIsNone(interval(taf(body),'2026-10-06T11:00:00Z'))

    def test_height_threshold_is_exclusive(self):
        d=taf('SCT050');at='2026-10-06T11:00:00Z'
        self.assertEqual(interval(d,at,5000),(0,0));self.assertEqual(interval(d,at,5001),(.375,.5))

    def test_amount_and_base_are_different(self):
        d=taf('SCT050');obs=[dict(cover='SCT',base=3000,type=None)];p=scenarios(d,'2026-10-06T11:00:00Z')
        self.assertAlmostEqual(base_increment(obs,p),.6096)
        self.assertEqual(cloud_increment((.375,.5),(.375,.5)),0)
        self.assertEqual(cloud_increment((.625,.875),(.375,.5)),.125)
        self.assertEqual(cloud_increment((.125,.25),(.375,.5)),-.125)
        self.assertIsNone(base_increment(obs,[[dict(cover='BKN',base=5000,type=None)]]))

    def test_base_requires_unique_all_scenarios_match(self):
        obs=[dict(cover='SCT',base=3000,type=None)];a=dict(cover='SCT',base=5000,type=None);b=dict(cover='SCT',base=6000,type=None)
        self.assertIsNone(base_increment(obs,[[a,b]]));self.assertIsNone(base_increment(obs,[[a],[b]]))
        self.assertIsNone(base_increment(obs+obs,[[a]]))

    def test_invalid_grammar_refuses(self):
        for body in ('SCT050 FM061200 09005KT','SCT050 FM0612 BKN020','SCT050 BECMG 0610/0614 BKN020 BECMG 0612/0615 OVC020','BKN020XYZ','HZSCT020 BKN030','SCT050 PROB50 0610/0612 BKN020','NIL'):
            with self.assertRaises(DecodeError):taf(body)

    def test_receipt_boundary_and_day1_cut(self):
        d=taf('SCT050');t=dict(decoded=d,known_at=utc('2026-10-06T01:00:00Z'))
        valid=utc('2026-10-07T01:00:00Z')
        self.assertIs(choose([t],valid,valid,'day1'),t)
        t['known_at']+=timedelta(microseconds=1);self.assertIsNone(choose([t],valid,valid,'day1'))
        self.assertIs(choose([t],valid,valid,'latest'),t)
        self.assertIsNone(choose([t],utc(d['valid_to']),utc(d['valid_to']),'latest'))

    def test_issued_future_and_ambiguous_revision_refuse(self):
        d=taf('SCT050');t=dict(decoded=d,known_at=utc(d['issue']))
        self.assertIsNone(choose([t],utc('2026-10-06T12:00:00Z'),utc('2026-10-05T23:00:00Z'),'latest'))
        self.assertIsNone(choose([t,t.copy()],utc('2026-10-06T12:00:00Z'),utc('2026-10-06T12:00:00Z'),'latest'))

    def test_superseded_sampling_grid_never_migrates(self):
        args=dict(system_id='DWD_ICON_GLOBAL_EPS',transport_provider_id='DWD_OPENDATA_ICON_EPS',lead_bucket='h00_12',run_cycle='00Z')
        self.assertTrue(_historical_transport_scope_allowed(**args,extraction_version='dwd_icon_global_eps_00z_d0_station_tmax_3h_v3'))
        self.assertFalse(_historical_transport_scope_allowed(**args,extraction_version='dwd_icon_global_eps_00z_d0_station_tmax_6h_v1'))
        args['transport_provider_id']='OPEN_METEO'
        self.assertFalse(_historical_transport_scope_allowed(**args,extraction_version='dwd_icon_global_eps_00z_d0_station_tmax_3h_v3'))


if __name__=='__main__':unittest.main()
