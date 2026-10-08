import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import nested_multiday as M


class Multiday(unittest.TestCase):
    def test_cutoff_is_first_actual_commit_missing_clock_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(M,'HERE',Path(tmp)):
            p=Path(tmp)/'p0_roots';p.mkdir()
            rows=[dict(day='2026-09-02',original_checkpoint='late',parent_committed_at='2026-09-02T11:00:00Z'),
                dict(day='2026-09-02',original_checkpoint='early',parent_committed_at='2026-09-02T10:00:00Z'),
                dict(day='2026-09-03',original_checkpoint='missing',reason='missing_original_packet')]
            with gzip.open(p/'ZUUU.jsonl.gz','wt') as out:
                for row in rows:out.write(json.dumps(row)+'\n')
            result,census,fingerprints=M.inventory(['ZUUU'],'2026-09-01','2026-09-03')
            self.assertEqual(result['ZUUU/2026-09-02']['anchor_checkpoint'],'early')
            self.assertEqual(census['station_dates_without_anchor'],2)
            self.assertEqual(census['root_without_committed_parent_clock'],1)
            self.assertEqual(result['ZUUU/2026-09-03']['state'],'NO_RESEARCH_PARENT_ANCHOR')
            self.assertEqual(len(fingerprints['ZUUU']),64)

    def test_resume_cannot_change_anchor_and_old_version_stays_pending(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(M,'HERE',Path(tmp)):
            record=dict(station='ZUUU',event='2026-10-05',outer_cutoff='2026-10-05T10:00:00Z',anchor_checkpoint='cp')
            target=Path(tmp)/'nested_oos/ZUUU/2026-10-05/summary.json';target.parent.mkdir(parents=True)
            summary={**record,'method_version':'actual_arrival_inner_truth_ladder_v3','report_header':{},'objects':[],'cross_states':{}}
            target.write_text(json.dumps(summary));self.assertEqual(M.current_report(record)['state'],'COMPONENT_REPORT_BUILT')
            summary['anchor_checkpoint']='different';target.write_text(json.dumps(summary))
            self.assertIsNone(M.current_report(record))
            summary['method_version']='older';target.write_text(json.dumps(summary));self.assertIsNone(M.current_report(record))


if __name__=='__main__':unittest.main()
