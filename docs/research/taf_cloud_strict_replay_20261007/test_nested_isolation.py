"""Adversarial isolation checks use a disposable source, never production."""
import json
import pathlib
import sqlite3
import tempfile
import unittest
from recompile_nested import isolated_store,read_report
from rebuild import HERE


class Isolation(unittest.TestCase):
    def test_read_only_source_and_private_write_targets(self):
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            path=pathlib.Path(tmp)/'source.db';c=sqlite3.connect(path)
            c.execute('create table reference_fact(id integer primary key,value real)');c.execute('insert into reference_fact values(1,25)')
            c.execute('create table calibration_native_ensemble_activation(id integer primary key,value real)');c.execute('insert into calibration_native_ensemble_activation values(1,99)');c.commit();c.close();before=path.read_bytes()
            src,private=isolated_store(path)
            self.assertEqual(private.execute('select value from reference_fact').fetchone()[0],25)
            self.assertEqual(private.execute('select count(*) from calibration_native_ensemble_activation').fetchone()[0],0)
            private.execute('insert into calibration_native_ensemble_activation values(2,30)');private.commit()
            for sql in ('update source.reference_fact set value=40','drop table source.reference_fact','insert into source.reference_fact values(2,30)','update reference_fact set value=40','delete from source.calibration_native_ensemble_activation'):
                with self.assertRaises(sqlite3.DatabaseError):private.execute(sql)
            self.assertEqual(src.execute('select value from calibration_native_ensemble_activation').fetchone()[0],99)
            private.close();src.close();self.assertEqual(path.read_bytes(),before)

    def test_complete_builder_report_roundtrip(self):
        summary,report=read_report('ZUUU')
        self.assertEqual(report['station_id'],'ZUUU');self.assertEqual(report['as_of_utc'],summary['outer_cutoff'])
        self.assertGreaterEqual(len(report['ready_system_evaluations']),2)
        self.assertFalse(report['live_submit_authorized'])


if __name__=='__main__':unittest.main()
