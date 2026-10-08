import gzip
import json
import pathlib
import tempfile
import unittest
import fit_storage as storage
from rebuild import HERE


class Receipts(unittest.TestCase):
    def test_bounded_roundtrip_and_tampered_object(self):
        original=storage.HERE
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            storage.HERE=pathlib.Path(tmp)
            try:
                result=dict(schema='example',taf_fit_receipts=[dict(day='2026-10-01',parameters=[1,2])],p1_fit_receipts=[dict(day='2026-10-02',parameters=[3,4])]);path=storage.HERE/'fit.json';storage.write_fit_report(path,result)
                restored=storage.load_fit_report(path)
                self.assertEqual(restored['taf_fit_receipts'],result['taf_fit_receipts']);self.assertEqual(restored['p1_fit_receipts'],result['p1_fit_receipts'])
                index=json.loads(path.read_text());ref=index['p1_fit_receipts'][0];target=storage.HERE/ref['path']
                with gzip.open(target,'wt') as out:json.dump(dict(day='2026-10-02',parameters=[5,6]),out)
                with self.assertRaises(ValueError):storage.load_fit_report(path)
            finally:storage.HERE=original

    def test_oversize_receipt_fails_closed(self):
        original=storage.HERE
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            storage.HERE=pathlib.Path(tmp)
            try:
                path=storage.HERE/'fit.json'
                with self.assertRaises(RuntimeError):storage.write_fit_report(path,dict(taf_fit_receipts=[dict(day='2026-10-01',untrusted='x'*(2*1024**2))],p1_fit_receipts=[]))
                self.assertFalse(path.exists())
            finally:storage.HERE=original


if __name__=='__main__':unittest.main()
