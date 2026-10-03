import unittest
import tempfile
import time
import os
from pathlib import Path
from unittest.mock import patch
import line_delivery as d

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Path(self.tmp.name)/'test.sqlite'
        c=d.connect(self.db)
        c.execute('INSERT INTO line_sources VALUES(?,?,?,1)',(d.TARGET,'group','now'))
        c.commit();c.close()
    def tearDown(self):self.tmp.cleanup()
    def test_same_day_no_duplicate(self):
        with patch.object(d,'api',return_value={}) as api, patch.object(d,'capture',return_value={'type':'image'}),patch.object(d,'message',return_value={'type':'text','text':'test'}):
            d.deliver(self.db,10000,'admin','https://example.com','2026-10-04')
            self.assertTrue(d.deliver(self.db,10000,'admin','https://example.com','2026-10-04')['already_sent'])
            self.assertEqual(api.call_count,2)
    def test_retry_preserves_payload_and_key(self):
        with patch.object(d,'api',side_effect=[{},RuntimeError('LINE API HTTP 500'),{}]) as api,patch.object(d,'capture',return_value={'type':'image'}) as capture,patch.object(d,'message',return_value={'type':'text','text':'test'}):
            with self.assertRaises(RuntimeError):d.deliver(self.db,1,'admin','https://example.com','2026-10-04')
            d.deliver(self.db,1,'admin','https://example.com','2026-10-04')
            self.assertEqual(api.call_args_list[1],api.call_args_list[2])
            self.assertEqual(capture.call_count,2)
    def test_wrong_group_fails_closed(self):
        c=d.connect(self.db);c.execute('DELETE FROM line_sources');c.commit();c.close()
        with patch.object(d,'api') as api:
            with self.assertRaises(RuntimeError):d.deliver(self.db,1,'admin','https://example.com')
            api.assert_not_called()
    def test_image_expiry_and_signature(self):
        folder=self.db.parent/'line-images';folder.mkdir();name='a'*32+'-original.jpg';(folder/name).write_bytes(b'test')
        with patch.dict(os.environ,{'LINE_CHANNEL_SECRET':'test'}):
            exp=int(time.time())+60;sig=d.image_signature(name,exp)
            self.assertIsNotNone(d.image_path(self.db,name,exp,sig))
            self.assertIsNone(d.image_path(self.db,name,exp,'bad'))
            self.assertIsNone(d.image_path(self.db,name,0,d.image_signature(name,0)))
            self.assertIsNone(d.image_path(self.db,'../secret',exp,sig))
    def test_default_disabled(self):self.assertFalse(d.status(self.db)['delivery_enabled'])

if __name__=='__main__':unittest.main()
