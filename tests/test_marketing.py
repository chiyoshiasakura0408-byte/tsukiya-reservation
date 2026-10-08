import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp())
import run
import marketing

SID='12345678-1234-1234-1234-123456789abc'
class MarketingTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.patch=patch.object(run,'DB',self.tmp.name+'/db');self.patch.start()
 def tearDown(self):self.patch.stop();self.tmp.cleanup()
 def booking(self,c,status='CONFIRMED',amount=120000,paid=True,source='WEB'):
  now='2026-10-08T10:00:00+00:00'
  cur=c.execute('''INSERT INTO reservations(source,guest_name,visit_at,party_size,amount,status,created_at,updated_at,payment_source,payment_confirmed_at)
   VALUES(?,?,?,?,?,?,?,?,?,?)''',(source,'private name','2026-10-08T18:00',2,amount,status,now,now,'SQUARE' if paid else None,now if paid else None))
  return cur.lastrowid
 def test_dedup_payment_refund_and_exclusions(self):
  c=run.con();now='2026-10-08T10:00:00+00:00'
  for _ in range(2):marketing.record(c,dict(session_id=SID,source='google_maps',event='visit',email='not stored'),now)
  rid=self.booking(c);marketing.bind(c,rid,dict(session_id=SID,source='instagram'),now)
  marketing.bind(c,rid,dict(session_id=SID,source='line'),now)
  c.execute('INSERT INTO cancellation_refunds VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(rid,0,120000,'COMPLETED','key',None,'refund',None,now,None,None,None))
  c.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?",(rid,))
  self.booking(c,amount=1);self.booking(c,paid=False,source='DIRECT')
  r=marketing.report(c,{'start':['2026-10-08'],'end':['2026-10-08']})
  maps=next(x for x in r['rows'] if x['source']=='google_maps')
  self.assertEqual((maps['visit'],maps['paid'],maps['paid_amount'],maps['cancelled'],maps['refunded_amount']),(1,1,120000,1,120000))
  self.assertEqual(sum(x['paid'] for x in r['rows']),1)
  self.assertEqual(sum(x['confirmed'] for x in r['rows']),1)
  self.assertNotIn('private name',json.dumps(r))
  self.assertNotIn('not stored',str(c.execute('SELECT * FROM marketing_events').fetchall()))
  marketing.update(c,{'id':rid,'action':'exclude','value':True},now)
  self.assertEqual(sum(x['paid'] for x in marketing.report(c,{'start':['2026-10-08'],'end':['2026-10-08']})['rows']),0)
  c.close()
 def test_japan_dates_and_come_not_inferred(self):
  c=run.con();rid=self.booking(c)
  c.execute("UPDATE reservations SET created_at='2026-10-07T16:00:00+00:00' WHERE id=?",(rid,))
  report=marketing.report(c,{'start':['2026-10-08'],'end':['2026-10-08']})
  self.assertEqual(sum(x['requests'] for x in report['rows']),1)
  self.assertEqual(sum(x['attended'] for x in report['rows']),0)
  marketing.update(c,{'id':rid,'action':'attend','value':True},'2026-10-08T10:00:00+00:00')
  self.assertEqual(sum(x['attended'] for x in marketing.report(c,{'start':['2026-10-08'],'end':['2026-10-08']})['rows']),1)
  with self.assertRaises(ValueError):marketing.record(c,{'session_id':SID,'source':'person@example.com','event':'visit'},'now')
  c.close()
 def test_http_auth_origin_size(self):
  server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
  base=f'http://127.0.0.1:{server.server_port}'
  try:
   with self.assertRaises(urllib.error.HTTPError) as ctx:urllib.request.urlopen(base+'/api/marketing')
   self.assertEqual(ctx.exception.code,401)
   body=json.dumps(dict(session_id=SID,source='google_maps',event='visit')).encode()
   for origin,code in [('https://evil.example',403),('https://nishitenma-tsukiya-home.chiyoshi-a-0408.chatgpt.site',200)]:
    req=urllib.request.Request(base+'/api/public/marketing-event',data=body,headers={'Origin':origin,'Content-Type':'text/plain'})
    if code==200:self.assertTrue(json.load(urllib.request.urlopen(req))['ok'])
    else:
     with self.assertRaises(urllib.error.HTTPError) as ctx:urllib.request.urlopen(req)
     self.assertEqual(ctx.exception.code,code)
   with patch.object(run,'ADMIN_TOKEN','test-marketing'):
    req=urllib.request.Request(base+'/api/marketing?start=2026-10-08&end=2026-10-08',headers={'x-admin-token':'test-marketing'})
    self.assertIn('rows',json.load(urllib.request.urlopen(req)))
  finally:server.shutdown();server.server_close()
 def test_weekly_archive_is_durable_idempotent_and_private(self):
  c=run.con();rid=self.booking(c)
  marketing.archive_due(c,datetime(2026,10,8,10,tzinfo=timezone.utc));c.commit();c.close()
  c=run.con()
  initial=marketing.archives(c,{'week':['initial']})['record']
  self.assertEqual(initial['owner'],'AI SNS担当')
  self.assertNotIn('reservations',initial)
  marketing.archive_due(c,datetime(2026,10,11,23,59,tzinfo=timezone.utc))
  self.assertEqual(len(marketing.archives(c,{'period':['weekly']})['records']),0)
  marketing.archive_due(c,datetime(2026,10,12,0,0,tzinfo=timezone.utc))
  saved=marketing.archives(c,{'week':['2026-10-05']})['record']
  self.assertEqual(saved['end'],'2026-10-11')
  c.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?",(rid,))
  marketing.archive_due(c,datetime(2026,10,12,1,tzinfo=timezone.utc))
  self.assertEqual(marketing.archives(c,{'week':['2026-10-05']})['record'],saved)
  marketing.archive_due(c,datetime(2026,10,26,0,tzinfo=timezone.utc))
  self.assertEqual(len(marketing.archives(c,{'period':['weekly']})['records']),3)
  self.assertNotIn('private name',json.dumps(marketing.archives(c,{'week':['initial']})))
  c.close()
 def test_marketing_page_hidden_and_archives_protected(self):
  from pathlib import Path
  for path in (Path(run.BASE)/'public').glob('*.html'):
   self.assertNotIn('href="/marketing"',path.read_text())
  self.assertFalse((Path(run.BASE)/'public/marketing.html').exists())
  handler=run.Handler.__new__(run.Handler)
  handler.path='/marketing';handler.redirect=lambda url:url
  self.assertEqual(handler.do_GET(),'/reservations')
  handler.path='/api/marketing/archives';handler.auth=lambda:False
  handler.send_json=lambda body,status=200,**kwargs:(body,status)
  self.assertEqual(handler.do_GET()[1],401)

 def test_daily_monthly_yearly_boundaries_and_persistence(self):
  c=run.con()
  marketing.archive_due(c,datetime(2026,12,31,3,tzinfo=timezone.utc))
  marketing.archive_due(c,datetime(2026,12,31,23,59,tzinfo=timezone.utc))
  self.assertEqual(marketing.archives(c,{'period':['daily']})['records'],[])
  marketing.archive_due(c,datetime(2027,1,1,0,tzinfo=timezone.utc));c.commit();c.close()
  c=run.con()
  for kind,key,end in [('daily','2026-12-31','2026-12-31'),('monthly','2026-12-01','2026-12-31'),('yearly','2026-01-01','2026-12-31')]:
   record=marketing.archives(c,{'key':[kind+':'+key]})['record']
   self.assertEqual(record['end'],end)
   self.assertEqual(record['kind'],kind)
   self.assertEqual(len(marketing.archives(c,{'period':[kind]})['records']),1)
  c.close()
