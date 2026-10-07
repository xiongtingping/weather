"""Temporal, category and change-group adversarial checks for the audit."""
import gzip
import json
import unittest
from datetime import timedelta
from audit import (HERE, LIMITS, AMOUNT, utc, metar_layers, cloud_interval,
                   forecast_scenarios, scenario_interval, select_taf, direction, guard)


def taf(groups, issue='2026-10-06T00:00:00Z', seen='2026-10-06T00:05:00Z', raw='TAF ZUUU'):
    return dict(issue_time_utc=issue, known_at=utc(seen), raw_taf=raw,
                awc_row=dict(validTimeFrom=utc('2026-10-06T00:00:00Z').timestamp(),
                             validTimeTo=utc('2026-10-08T00:00:00Z').timestamp(),fcsts=groups))


def group(start=0, end=24, change=None, clouds=None, bec=None):
    t=utc('2026-10-07T00:00:00Z')
    return dict(timeFrom=(t+timedelta(hours=start)).timestamp(),
                timeTo=(t+timedelta(hours=end)).timestamp(),fcstChange=change,
                clouds=clouds or [],timeBec=None if bec is None else (t+timedelta(hours=bec)).timestamp())


SCT=[dict(cover='SCT',base=3000)]
BKN=[dict(cover='BKN',base=3000)]


class BoundaryChecks(unittest.TestCase):
    def test_categories_are_intervals(self):
        self.assertEqual(cloud_interval(SCT,6500),(3/8,4/8))
        self.assertEqual(direction((5/8,7/8),(3/8,4/8)),'more')
        self.assertEqual(direction((3/8,4/8),(3/8,4/8)),'overlap')
        self.assertEqual(direction((1/8,2/8),(5/8,7/8)),'less')

    def test_clear_scope(self):
        for k in ('CAVOK','NSC','NCD'):
            self.assertEqual(cloud_interval([dict(cover=k)],5000),(0,0))
            self.assertIsNone(cloud_interval([dict(cover=k)],6500))
        self.assertEqual(cloud_interval([dict(cover='CLR')],6500),(0,0))

    def test_unknown_and_obscured(self):
        for x in ([],[dict(cover='SCT',base=None)],[dict(cover='OVX',base=100)],
                  [dict(cover='VV',base=200)],[dict(cover='SCT',base=float('nan'))],
                  [dict(cover='unexpected',base=3000)]):
            self.assertIsNone(cloud_interval(x,6500))

    def test_height_boundary(self):
        self.assertEqual(cloud_interval(SCT,3000),(0,0))
        self.assertEqual(cloud_interval(SCT,3001),(3/8,4/8))
        self.assertEqual(direction(cloud_interval(SCT,6500),cloud_interval([dict(cover='SCT',base=5000)],6500)),'overlap')
        self.assertEqual(direction(cloud_interval(SCT,5000),cloud_interval([dict(cover='SCT',base=5000)],5000)),'more')

    def test_max_not_sum(self):
        self.assertEqual(cloud_interval(SCT+BKN,6500),(5/8,7/8))

    def test_metar_trends_are_not_observations(self):
        self.assertEqual(metar_layers('METAR ZUUU SCT030 TEMPO BKN010 RMK OVC005'),SCT_with_type())
        self.assertEqual(metar_layers('METAR ZUUU CAVOK NOSIG BKN001'),[dict(cover='CAVOK',base=None)])
        self.assertIsNone(cloud_interval(metar_layers('METAR ZUUU FEW///TCU'),6500))

    def test_becmg_window(self):
        t=taf([group(0,3,clouds=SCT),group(3,24,'BECMG',BKN,bec=5)])
        self.assertEqual(scenario_interval(forecast_scenarios(t,utc('2026-10-07T04:00:00Z')),6500),(3/8,7/8))
        self.assertEqual(scenario_interval(forecast_scenarios(t,utc('2026-10-07T05:00:00Z')),6500),(5/8,7/8))
        self.assertEqual(direction((5/8,7/8),(3/8,7/8)),'overlap')

    def test_tempo_prob_do_not_force_cloud(self):
        for k in ('TEMPO','PROB'):
            t=taf([group(clouds=SCT),group(3,5,k,BKN)])
            self.assertEqual(scenario_interval(forecast_scenarios(t,utc('2026-10-07T04:00:00Z')),6500),(3/8,7/8))
            self.assertEqual(scenario_interval(forecast_scenarios(t,utc('2026-10-07T05:00:00Z')),6500),(3/8,4/8))

    def test_omitted_cloud_inherits(self):
        t=taf([group(0,3,clouds=SCT),group(3,24,'BECMG',bec=5),group(3,5,'TEMPO')])
        self.assertEqual(scenario_interval(forecast_scenarios(t,utc('2026-10-07T04:00:00Z')),6500),(3/8,4/8))
        self.assertIsNone(forecast_scenarios(taf([group()]),utc('2026-10-07T04:00:00Z')))
        self.assertIsNone(forecast_scenarios(taf([group(0,3,clouds=SCT),group(3,24,'FM')]),utc('2026-10-07T04:00:00Z')))

    def test_ambiguous_decoder_fails_closed(self):
        for gs in ([group(clouds=SCT),group(3,5,'OTHER',BKN)],
                   [group(clouds=SCT),group(3,5,'FM',BKN)],
                   [group(0,3,clouds=SCT),group(3,24,'BECMG',BKN)]):
            self.assertIsNone(forecast_scenarios(taf(gs),utc('2026-10-07T04:00:00Z')))

    def test_day1_requires_both_clocks(self):
        valid=utc('2026-10-07T00:00:00Z'); decision=valid+timedelta(minutes=6)
        good=taf([group()],seen='2026-10-06T00:00:00Z')
        self.assertIs(select_taf([good],valid,decision,'day1'),good)
        late=taf([group()])
        self.assertIsNone(select_taf([late],valid,decision,'day1'))
        self.assertIs(select_taf([late],valid,decision,'latest'),late)
        future=taf([group()],seen='2026-10-07T00:01:00Z')
        self.assertIsNone(select_taf([future],valid,decision,'latest'))

    def test_amendment_and_tie(self):
        at=utc('2026-10-07T00:00:00Z');x=taf([group()]);y=taf([group()],raw='TAF AMD ZUUU')
        self.assertIs(select_taf([x,y],at,at,'latest'),y)
        self.assertIsNone(select_taf([x,taf([group()])],at,at,'latest'))


def SCT_with_type(): return [dict(cover='SCT',base=3000,type=None)]


def full_data():
    result=json.loads((HERE/'results.json').read_text());n=0;matched=0
    with gzip.open(HERE/'paired_rows.jsonl.gz','rt') as f:
        for line in f:
            r=json.loads(line);n+=1
            assert r['strict_oos'] is False
            valid=utc(r['observation_valid_at']);at=utc(r['decided_at'])
            for mode in ('latest','day1'):
                t=r[mode]
                if t is None:continue
                matched+=1
                cutoff=valid if mode=='latest' else valid-timedelta(hours=24)
                assert utc(t['known_at'])<=cutoff and utc(t['issue'])<=cutoff<=at
                assert utc(t['valid_from'])<=valid<utc(t['valid_to'])
                assert ' COR ' not in ' '+t['raw_taf']+' '
                for h in LIMITS:
                    o=r['obs_intervals'][str(h)];fc=t['intervals'][str(h)]
                    if o is not None and fc is not None:
                        expected='more' if o[0]>fc[1] else 'less' if o[1]<fc[0] else 'overlap'
                    else:expected='unknown'
                    assert expected==t['directions'][str(h)]
                assert len(t['directions'])==len(LIMITS)
            if n%500==0:guard()
    assert n==result['summaries']['latest']['rows']==result['summaries']['day1']['rows']
    assert matched==result['summaries']['latest']['matched']+result['summaries']['day1']['matched']
    return dict(rows=n,matched_pairs=matched,checks=['both receipt and issue cutoff','half-open validity','COR excluded','interval direction recomputed at 4 thresholds','candidate role preserved'])


if __name__=='__main__':
    guard();suite=unittest.defaultTestLoader.loadTestsFromTestCase(BoundaryChecks)
    run=unittest.TextTestRunner(verbosity=2).run(suite)
    if not run.wasSuccessful():raise SystemExit(1)
    result=dict(boundary_test_groups=run.testsRun,full_data=full_data(),passed=True)
    (HERE/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))
