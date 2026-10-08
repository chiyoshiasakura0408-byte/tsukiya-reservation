import base64
import io
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch
from PIL import Image
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp())
import run
import business_cards as cards

class CardsTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.p=patch.object(run,'DB',self.tmp.name+'/db');self.p.start()
  b=io.BytesIO();Image.new('RGB',(100,60)).save(b,format='PNG')
  self.data={'image':'data:image/png;base64,'+base64.b64encode(b.getvalue()).decode(),'fields':{'name':'名刺テスト','phone':'09012345678','company_name':'会社'},'request_id':str(uuid.uuid4())}
 def tearDown(self):self.p.stop();self.tmp.cleanup()
 def test_save_idempotent_and_link_preserves_customer(self):
  cid=cards.save(run,self.data)['customer_id'];self.assertEqual(cards.save(run,self.data)['customer_id'],cid)
  c=run.con();self.assertEqual(c.execute('SELECT count(*) FROM business_cards').fetchone()[0],1)
  run.sync_customers(c);self.assertEqual(len(cards.list_for(c,cid)),1);c.close()
  self.data['request_id']=str(uuid.uuid4());self.data['customer_id']=cid;self.data['fields']['name']='別名'
  cards.save(run,self.data);c=run.con();self.assertEqual(c.execute('SELECT name FROM customers WHERE id=?',(cid,)).fetchone()[0],'名刺テスト');self.assertEqual(len(cards.list_for(c,cid)),2);c.close()
 def test_duplicate_requires_explicit_link(self):
  cards.save(run,self.data);self.data['request_id']=str(uuid.uuid4())
  with self.assertRaises(ValueError):cards.save(run,self.data)
 def test_invalid_image_no_write(self):
  self.data['image']='https://localhost/private'
  with self.assertRaises(ValueError):cards.save(run,self.data)
 def test_scan_without_key_does_not_save(self):
  with patch.dict(os.environ,{'OPENAI_API_KEY':''}):
   with self.assertRaises(ValueError):cards.scan(self.data)
  c=run.con();self.assertEqual(c.execute('SELECT count(*) FROM customers').fetchone()[0],0);c.close()
if __name__=='__main__': unittest.main()
