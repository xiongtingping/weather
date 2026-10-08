import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from parent_clock import original_parent_clock,validate_original_packet_clock
import nested_multiday as M


class ParentClock(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.c.execute('create table basket_probability_envelope(station_id,event_date,decision_at_utc,created_at,content_sha256,checkpoint_id,stage,state)')
        self.row=('ZUUU','2026-09-02','2026-09-02T09:00:00Z','2026-09-02T10:00:00Z','source-sha','old-gate-refused','P0','COMMITTED')
        self.c.execute('insert into basket_probability_envelope values(?,?,?,?,?,?,?,?)',self.row)

    def tearDown(self):self.c.close()

    def test_clock_comes_from_actual_commit_and_rejects_scope(self):
        r=original_parent_clock(self.c,'ZUUU','2026-09-02','old-gate-refused')
        self.assertEqual(r['outer_cutoff'],'2026-09-02T10:00:00Z')
        self.assertEqual(r['original_envelope_content_sha'],'source-sha')
        with self.assertRaises(ValueError):original_parent_clock(self.c,'KATL','2026-09-02','old-gate-refused')
        with self.assertRaises(ValueError):original_parent_clock(self.c,'ZUUU','2026-09-03','old-gate-refused')

    def test_ambiguous_missing_and_backwards_receipts_reject(self):
        with self.assertRaises(ValueError):original_parent_clock(self.c,'ZUUU','2026-09-02','missing')
        self.c.execute('insert into basket_probability_envelope values(?,?,?,?,?,?,?,?)',self.row)
        with self.assertRaises(ValueError):original_parent_clock(self.c,'ZUUU','2026-09-02','old-gate-refused')
        self.c.execute('delete from basket_probability_envelope');r=list(self.row);r[3]='2026-09-02T08:59:59Z'
        self.c.execute('insert into basket_probability_envelope values(?,?,?,?,?,?,?,?)',r)
        with self.assertRaises(ValueError):original_parent_clock(self.c,'ZUUU','2026-09-02','old-gate-refused')

    def test_old_gate_failure_cannot_erase_existing_original_anchor(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(M,'HERE',Path(tmp)):
            folder=Path(tmp)/'p0_roots';folder.mkdir()
            r=dict(day='2026-09-02',original_checkpoint='old-gate-refused',state='REFUSED',reason='old_gate_did_not_pass')
            with gzip.open(folder/'ZUUU.jsonl.gz','wt') as out:out.write(json.dumps(r)+'\n')
            records,census,_=M.inventory(['ZUUU'],'2026-09-02','2026-09-02',self.c)
            self.assertEqual(records['ZUUU/2026-09-02']['state'],'SCHEDULED')
            self.assertEqual(records['ZUUU/2026-09-02']['outer_cutoff'],'2026-09-02T10:00:00Z')
            self.assertEqual(census['original_parent_clock_restored_independent_of_old_replay_gate'],1)

    def test_packet_arrival_exact_cutoff_and_future_receipt_rejected(self):
        clock=original_parent_clock(self.c,'ZUUU','2026-09-02','old-gate-refused')
        packet=dict(station_id='ZUUU',event_date='2026-09-02',received_at_utc=clock['outer_cutoff'])
        validate_original_packet_clock(packet,clock)
        packet['received_at_utc']='2026-09-02T10:00:00.000001Z'
        with self.assertRaises(ValueError):validate_original_packet_clock(packet,clock)
        del packet['received_at_utc']
        with self.assertRaises(KeyError):validate_original_packet_clock(packet,clock)

    def test_packet_station_day_binding_cannot_be_substituted(self):
        clock=original_parent_clock(self.c,'ZUUU','2026-09-02','old-gate-refused')
        for station,day in [('KATL','2026-09-02'),('ZUUU','2026-09-03')]:
            with self.assertRaises(ValueError):validate_original_packet_clock(dict(station_id=station,event_date=day,received_at_utc=clock['outer_cutoff']),clock)


if __name__=='__main__':unittest.main()
