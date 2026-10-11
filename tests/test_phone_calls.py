import base64
import hashlib
import hmac
import http.client
import json
import os
import tempfile
import threading
import unittest
import uuid
from datetime import datetime,timezone
from email.utils import format_datetime,parsedate_to_datetime
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlencode
import run
import phone_calls as calls

A='AC'+'a'*32
P='CA'+'1'*32
CH='CA'+'2'*32
N='+815012345678'
F='+819012345678'
FROM='+818011112222'
BASE='https://example.com'

class PhoneCallsTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        for name,value in [('DB',temp.name+'/test.sqlite'),('TWILIO_ACCOUNT_SID',A),('TWILIO_AUTH_TOKEN','secret'),('APP_BASE_URL',BASE),('ADMIN_TOKEN','test'),('deliver_confirmation',lambda r:r)]:
            p=patch.object(run,name,value);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,{'TWILIO_VOICE_NUMBER':N,'TWILIO_VOICE_FORWARD_TO':F});p.start();self.addCleanup(p.stop)
        c=calls.connect(run);c.close()
    def event(self,path,fields):
        fields={'AccountSid':A,**fields}
        sig=base64.b64encode(hmac.new(b'secret',(BASE+path+''.join(k+fields[k] for k in sorted(fields))).encode(),hashlib.sha1).digest()).decode()
        return urlencode(fields).encode(),sig
    def send(self,kind,fields):
        path='/webhooks/voice/'+kind;raw,sig=self.event(path,fields);return calls.receive(run,path,raw,sig)
    def incoming(self,sid=P):
        return self.send('incoming',{'CallSid':sid,'From':FROM,'To':N,'Direction':'inbound','CallStatus':'ringing'})
    def row(self):return calls.list_calls(run)['items'][0]
    def test_headless_history_and_idempotent_incoming(self):
        xml=self.incoming();self.assertIn(F.encode(),xml);self.assertNotIn(b'<Pause',xml)
        self.assertIn(b'answerOnBridge="true"',xml)
        self.incoming();self.assertEqual(len(calls.list_calls(run)['items']),1)
        self.assertEqual(self.row()['from_number'],FROM)
        self.assertFalse(self.row()['handled']);self.assertIsNone(self.row()['customer_id'])
    def test_invalid_signature_account_destination_and_route(self):
        path='/webhooks/voice/incoming';raw,sig=self.event(path,{'CallSid':P,'From':FROM,'To':N,'Direction':'inbound'})
        for bad in ('',sig+'x'):
            with self.assertRaises(PermissionError):calls.receive(run,path,raw,bad)
        with self.assertRaises(PermissionError):calls.receive(run,'/webhooks/voice/dial',raw,sig)
        for fields in ({'To':F},{'AccountSid':'AC'+'b'*32},{'Direction':'outbound-api'}):
            with self.assertRaises(PermissionError):self.send('incoming',{'CallSid':P,'From':FROM,'To':N,'Direction':'inbound',**fields})
        self.assertFalse(calls.list_calls(run)['items'])
    def test_parent_completion_does_not_mean_owner_answered(self):
        self.incoming();self.send('status',{'CallSid':P,'From':FROM,'To':N,'Direction':'inbound','CallStatus':'completed'})
        self.assertEqual(self.row()['forward_status'],'')
        self.send('leg',{'CallSid':CH,'ParentCallSid':P,'To':F,'CallStatus':'no-answer','SequenceNumber':'3','CallDuration':'0'})
        self.send('leg',{'CallSid':CH,'ParentCallSid':P,'To':F,'CallStatus':'ringing','SequenceNumber':'1'})
        self.assertEqual(self.row()['forward_status'],'no-answer')
        self.incoming();self.assertEqual(self.row()['inbound_status'],'completed')
    def test_answer_duration_and_callbacks_repeated(self):
        self.incoming();self.send('leg',{'CallSid':CH,'ParentCallSid':P,'To':F,'CallStatus':'in-progress','SequenceNumber':'2'})
        for _ in range(2):self.send('dial',{'CallSid':P,'DialCallSid':CH,'DialCallStatus':'completed','DialCallDuration':'124'})
        self.assertEqual((self.row()['forward_status'],self.row()['duration']),('completed',124))
        with self.assertRaises(PermissionError):self.send('leg',{'CallSid':CH,'ParentCallSid':P,'To':'+819099999999','CallStatus':'completed'})
    def test_number_matching_candidates_not_automatic_identity(self):
        c=calls.connect(run)
        with c:
            for i,name,phone in [(1,'本人','080-1111-2222'),(2,'家族','＋８１ ８０１１１１２２２２'),(3,'他','090-1111-2222')]:
                c.execute('INSERT INTO customers(id,match_key,name,phone,created_at,updated_at) VALUES(?,?,?,?,?,?)',(i,str(i),name,phone,'now','now'))
        c.close();self.incoming();row=self.row()
        self.assertEqual(len(row['candidates']),2);self.assertIsNone(row['customer_id'])
        calls.update_call(run,row['id'],{'customer_id':2,'note':'休業日に電話で受付','handled':True})
        self.assertEqual((self.row()['customer_id'],self.row()['note'],self.row()['handled']),(2,'休業日に電話で受付',1))
        c=calls.connect(run);self.assertFalse(calls.customer_candidates(c,'anonymous'));c.close()
    def test_reconcile_restores_offline_events_and_child_status(self):
        now=format_datetime(datetime.now(timezone.utc).replace(microsecond=0))
        incoming={'sid':P,'from':FROM,'to':N,'direction':'inbound','date_created':now,'status':'completed'}
        child={'sid':CH,'parent_call_sid':P,'to':F,'status':'busy','duration':'0'}
        def fake(cfg,path):return {'calls':[child] if 'ParentCallSid' in path else [incoming],'next_page_uri':None}
        with patch.object(calls,'api_get',fake):calls.reconcile(run);calls.reconcile(run)
        rows=calls.list_calls(run);self.assertEqual(len(rows['items']),1)
        self.assertEqual(rows['items'][0]['forward_status'],'busy');self.assertTrue(rows['last_sync'])
        self.assertEqual(rows['items'][0]['received_at'],parsedate_to_datetime(now).isoformat())
    def test_config_missing_no_forward_and_no_api(self):
        with patch.dict(os.environ,{'TWILIO_VOICE_FORWARD_TO':''}):
            with self.assertRaises(RuntimeError):self.incoming()
            with patch.object(calls,'api_get') as api:calls.reconcile(run);api.assert_not_called()
    def test_history_pagination_and_private_http_booking_link(self):
        self.incoming()
        server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def req(path,data=None,auth=True,action=True):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port);headers={}
            if auth:headers['x-admin-token']='test'
            if action:headers['X-Tsukiya-Action']='phone-calls'
            if data is not None:headers['Content-Type']='application/json'
            conn.request('POST' if data is not None else 'GET',path,None if data is None else json.dumps(data),headers)
            r=conn.getresponse();result=r.status,json.loads(r.read());conn.close();return result
        self.assertEqual(req('/api/phone-calls',auth=False)[0],401)
        self.assertEqual(req('/api/phone-calls/1',{'note':'x'},action=False)[0],403)
        today=datetime.now(run.concierge.JST).date();day=today.replace(month=11,day=10)
        if day<today:day=day.replace(year=day.year+1)
        data={'call_id':1,'request_id':str(uuid.uuid4()),'guest_name':'電話のお客様','phone':'08011112222','email':'','visit_at':str(day)+'T19:00','time_specified':True,'party_size':2,'seating_area':'PRIVATE'}
        self.assertEqual(req('/api/reservations/phone',data,action=False)[0],403)
        status,booking=req('/api/reservations/phone',data);self.assertEqual(status,200,booking)
        self.assertTrue(booking['customer_id']);self.assertEqual(booking['status'],'PENDING')
        self.assertEqual(req('/api/reservations/phone',data)[1]['id'],booking['id'])
        history=req('/api/phone-calls')[1]['items'][0];self.assertEqual(len(history['reservations']),1);self.assertEqual(history['customer_id'],booking['customer_id'])
        self.assertTrue(history['handled'])
        data.update(call_id=999,request_id=str(uuid.uuid4()));self.assertEqual(req('/api/reservations/phone',data)[0],400)
        c=calls.connect(run)
        with c:
            for i in range(2,55):calls.inbound(c,'CA'+format(i+100,'032x'),FROM,N,calls.utcnow(),'ringing')
        c.close();first=calls.list_calls(run);second=calls.list_calls(run,before=first['next_before'])
        self.assertEqual(len(first['items']),50);self.assertEqual(len(second['items']),4)

    def test_dial_failure_without_child_and_http_signature(self):
        self.incoming();self.send('dial',{'CallSid':P,'DialCallStatus':'failed'})
        self.assertEqual(self.row()['forward_status'],'failed')
        server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        path='/webhooks/voice/incoming';raw,sig=self.event(path,{'CallSid':'CA'+'4'*32,'To':N,'From':FROM,'Direction':'inbound'})
        for sign,expected in [(sig,200),('wrong',403)]:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port);conn.request('POST',path,raw,{'Content-Type':'application/x-www-form-urlencoded','X-Twilio-Signature':sign})
            response=conn.getresponse();body=response.read();self.assertEqual(response.status,expected)
            if expected==200:self.assertIn(b'<Dial',body)
            conn.close()
