import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import nested_storage as S
from bayes_basket_target.contracts import content_sha256


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patch=patch.object(S,'HERE',self.root);self.patch.start()
        self.obj=dict(state='PASS',system_evaluations={'a':dict(system_id='a',probabilities=[.2,.8]),'b':dict(system_id='b',probabilities=[.1,.9])})
        self.path=self.root/'x.json.gz'

    def tearDown(self):self.patch.stop();self.tmp.cleanup()

    def test_legacy_compact_round_trip_and_shared_reuse(self):
        sha=content_sha256(self.obj);S.write_atomic(self.path,S.body_of(self.obj))
        self.assertEqual(S.read_object(self.path,sha)[0],self.obj)
        S.store_object(self.path,self.obj);self.assertEqual(S.read_object(self.path,sha)[0],self.obj)
        S.store_object(self.root/'y.json.gz',{**self.obj,'state':'REJECTED'})
        self.assertEqual(len(list((self.root/'nested_shared_objects').glob('*.json.gz'))),2)
        self.assertEqual(len(S.referenced_shared_paths(self.path)),2)

    def test_corrupt_and_missing_shared_objects_fail_closed(self):
        sha=S.store_object(self.path,self.obj);targets=sorted(S.referenced_shared_paths(self.path))
        original=targets[0].read_bytes();S.write_atomic(targets[0],S.body_of(dict(system_id='a',probabilities=[1.,0.])))
        with self.assertRaises(ValueError):S.read_object(self.path,sha)
        targets[0].write_bytes(original);targets[1].unlink()
        with self.assertRaises(FileNotFoundError):S.read_object(self.path,sha)

    def test_unknown_schema_and_payload_collision(self):
        sha=S.store_object(self.path,self.obj);record=S.physical_read(self.path)
        record['storage_schema']='unknown';S.write_atomic(self.path,S.body_of(record))
        with self.assertRaises(ValueError):S.read_object(self.path,sha)
        record['storage_schema']=S.SCHEMA;record['payload']['system_evaluations']={}
        S.write_atomic(self.path,S.body_of(record))
        with self.assertRaises(ValueError):S.read_object(self.path,sha)

    def test_reference_path_escape_and_system_mismatch(self):
        sha=S.store_object(self.path,self.obj);record=S.physical_read(self.path)
        record['evaluation_refs']['a']='../escape'
        S.write_atomic(self.path,S.body_of(record))
        with self.assertRaises(ValueError):S.read_object(self.path,sha)
        wrong=dict(system_id='b',probabilities=[.2,.8]);identity=content_sha256(wrong)
        S.write_atomic(S.shared_path(identity),S.body_of(wrong))
        record['evaluation_refs']['a']=identity;S.write_atomic(self.path,S.body_of(record))
        with self.assertRaises(ValueError):S.read_object(self.path,sha)

    def test_physical_and_expanded_limits(self):
        with patch.object(S,'LIMIT',200):
            S.write_atomic(self.path,b'"'+b'a'*300+b'"')
            with self.assertRaises(RuntimeError):S.physical_read(self.path)
        obj=dict(system_evaluations={str(i):dict(system_id=str(i),data='x'*1000) for i in range(10)})
        sha=S.store_object(self.path,obj)
        # Each referenced object and the compact wrapper fit; expansion does not.
        self.assertLess(len(S.body_of(S.physical_read(self.path))),2000)
        with patch.object(S,'LIMIT',2000):
            with self.assertRaisesRegex(RuntimeError,'expanded_object_limit'):S.read_object(self.path,sha)
        with patch.object(S,'LIMIT',100):
            with self.assertRaises(RuntimeError):S.body_of(dict(extra='x'*500))


if __name__=='__main__':unittest.main()
