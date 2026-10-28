import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='gpt-api-'))
import run
import gpt_reservations as gpt

class GPTAPITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = patch.multiple(run, DB=Path(self.temp.name)/'test.sqlite',
                                     ADMIN_TOKEN='staff-test', APP_BASE_URL='https://tsukiya.test')
        self.config.start()
        self.env = patch.dict(os.environ, {'GPT_RESERVATION_API_KEY': 'gpt-test-' + 'x'*48})
        self.env.start()
        run.con().close()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), run.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.body = dict(mode='direct', guest_name='GPT test guest', phone='09012345678',
                         visit_at='2099-12-05T18:00', party_size=2, seating_area='PRIVATE1',
                         request_id=str(uuid.uuid4()))
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        self.config.stop()
        self.temp.cleanup()
    def call(self, path, body=None, headers=None):
        if headers is None:
            headers = {'Authorization': 'Bearer ' + os.environ['GPT_RESERVATION_API_KEY']}
        headers = {'Content-Type': 'application/json', **headers}
        req = urllib.request.Request(self.base + path,
                data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as r:
            return r.code, json.load(r)
    def create(self):
        status, row = self.call('/api/gpt/reservations', self.body)
        self.assertEqual(status, 200, row)
        return row
    def test_auth_fail_closed_and_no_staff_privileges(self):
        for headers in ({}, {'x-admin-token': 'staff-test'}, {'Authorization': 'Bearer wrong'}):
            self.assertEqual(self.call('/api/gpt/status', headers=headers)[0], 401)
            self.assertEqual(self.call('/api/gpt/reservations', self.body, headers=headers)[0], 401)
        with patch.dict(os.environ, {'GPT_RESERVATION_API_KEY': ''}):
            self.assertEqual(self.call('/api/gpt/status')[0], 401)
        for path in ('/api/reservations', '/api/customers', '/api/unpaid-invoices'):
            self.assertEqual(self.call(path)[0], 401)
        self.assertEqual(self.call('/api/gpt/reservations/1/cancel', {'request_id':str(uuid.uuid4())})[0],404)
    def test_create_replay_mismatch_and_seat_conflict(self):
        with patch.object(run, 'make_invoice') as invoice, patch.object(run, 'deliver_confirmation') as notify:
            first = self.create()
            self.assertEqual(self.create(), first)
            invoice.assert_not_called()
            notify.assert_not_called()
        self.assertNotIn('square_customer_id', first)
        self.assertEqual(first['amount'], 132000)
        other = dict(self.body, guest_name='Different')
        self.assertEqual(self.call('/api/gpt/reservations', other)[0], 409)
        other['request_id'] = str(uuid.uuid4())
        self.assertEqual(self.call('/api/gpt/reservations', other)[0], 409)
        c=run.con()
        self.assertEqual(c.execute('SELECT count(*) FROM reservations').fetchone()[0], 1)
        self.assertEqual(c.execute('SELECT count(*) FROM gpt_api_audit').fetchone()[0], 2)
        c.close()
    def test_phone_invoice_only_once(self):
        self.body['mode']='phone'
        with patch.object(run,'make_invoice',return_value=('c','o','i','https://example.test')) as invoice, \
             patch.object(run,'send_invoice_sms',side_effect=lambda row:row) as sms:
            first=self.create()
            self.assertEqual(self.create(),first)
            self.assertEqual(first['status'],'INVOICED')
            invoice.assert_called_once()
            sms.assert_called_once()
    def test_changes_reuse_business_rules_and_guard_stale_values(self):
        row=self.create()
        rid=row['id']
        root=f'/api/gpt/reservations/{rid}'
        note={'request_id':str(uuid.uuid4()),'guest_note':'allergy note','expected_guest_note':''}
        self.assertEqual(self.call(root+'/note',note)[0],200)
        self.assertEqual(self.call(root+'/note',note)[0],200)
        note.update(request_id=str(uuid.uuid4()), guest_note='stale')
        self.assertEqual(self.call(root+'/note',note)[0],409)
        time={'request_id':str(uuid.uuid4()),'time':'20:30','expected_visit_at':row['visit_at']}
        self.assertEqual(self.call(root+'/time',time)[0],200)
        time['request_id']=str(uuid.uuid4())
        self.assertEqual(self.call(root+'/time',time)[0],400)
        party={'request_id':str(uuid.uuid4()),'party_size':3,'expected_party_size':2,
               'expected_amount':132000,'expected_visit_at':'2099-12-05T20:30'}
        self.assertEqual(self.call(root+'/party',party)[1]['amount'],198000)
        room={'request_id':str(uuid.uuid4()),'seating_area':'PRIVATE2','expected_seating_area':'PRIVATE1',
              'expected_party_size':3,'expected_visit_at':'2099-12-05T20:30'}
        self.assertEqual(self.call(root+'/room',room)[0],200)
        self.assertEqual(self.call(root)[1]['seating_area'],'PRIVATE2')
        c=run.con();c.execute("UPDATE reservations SET payment_confirmed_at='paid' WHERE id=?",(rid,));c.commit();c.close()
        party.update(request_id=str(uuid.uuid4()),expected_party_size=3,expected_amount=198000,party_size=4)
        self.assertEqual(self.call(root+'/party',party)[0],400)
    def test_search_pagination_availability_and_validation(self):
        self.create()
        path='/api/gpt/reservations?from_date=2099-12-05'
        self.assertEqual(len(self.call(path)[1]['reservations']),1)
        self.assertEqual(self.call(path+'&after_id=1')[1]['reservations'],[])
        self.assertEqual(self.call('/api/gpt/reservations?from_date=2099-01-01&to_date=2099-03-01')[0],400)
        self.assertEqual(self.call('/api/gpt/availability?visit_at=2099-12-05T18:00&party_size=2&seating_area=PRIVATE1')[1]['available'],False)
        self.assertEqual(self.call('/api/gpt/availability?visit_at=2099-12-05T18:00&party_size=2&seating_area=PRIVATE2')[1]['available'],True)
        for changes in ({'amount':1},{'party_size':True},{'visit_at':'2099-12-05T18:00Z'},{'mode':'cancel'}):
            body=dict(self.body,request_id=str(uuid.uuid4()),**changes)
            self.assertEqual(self.call('/api/gpt/reservations',body)[0],400)
    def test_setup_key_requires_staff_and_origin(self):
        self.assertEqual(self.call('/api/gpt/setup-key',{})[0],401)
        self.assertEqual(self.call('/api/gpt/setup-key',{},headers={'x-admin-token':'staff-test'})[0],403)
        status,data=self.call('/api/gpt/setup-key',{},headers={'x-admin-token':'staff-test','Origin':run.APP_BASE_URL,'X-Tsukiya-Action':'gpt-setup'})
        self.assertEqual(status,200)
        self.assertEqual(data['api_key'],os.environ['GPT_RESERVATION_API_KEY'])
        code,data=self.call('/api/gpt/openapi.json',headers={})
        self.assertEqual(code,200)
        self.assertNotIn(os.environ['GPT_RESERVATION_API_KEY'],json.dumps(data))
        self.assertEqual(len(data['paths']),8)
    def test_interrupted_operation_does_not_replay(self):
        import hashlib
        c=run.con();gpt.schema(c)
        digest=hashlib.sha256(json.dumps(self.body,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        c.execute('INSERT INTO gpt_api_requests(request_id,path,body_hash,created_at) VALUES(?,?,?,?)',
            (self.body['request_id'],'/api/gpt/reservations',digest,run.now_iso()));c.commit();c.close()
        self.assertEqual(self.call('/api/gpt/reservations',self.body)[0],409)

if __name__=='__main__':unittest.main()
