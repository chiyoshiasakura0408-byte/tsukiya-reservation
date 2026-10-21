import json
import os
import tempfile
import threading
import unittest
import urllib.request
import uuid
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='phone-invoice-test-'))
import run

class PhoneInvoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = patch.multiple(run, DB=Path(self.tmp.name)/'test.sqlite', ADMIN_TOKEN='test-secret',
                                     TWILIO_ACCOUNT_SID='test', TWILIO_AUTH_TOKEN='test', TWILIO_FROM_NUMBER='+8111111111')
        self.config.start()
        run.con().close()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), run.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.body = dict(guest_name='Test guest', phone='09012345678', email='',
                         visit_at='2026-12-05T18:00', party_size=2, seating_area='PRIVATE1',
                         request_id=str(uuid.uuid4()))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.config.stop(); self.tmp.cleanup()

    def post(self, path='/api/reservations/phone', body=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}'+path,
                data=json.dumps(self.body if body is None else body).encode(),
                headers={'x-admin-token':'test-secret','Content-Type':'application/json'})
        with urllib.request.urlopen(req) as response:
            return json.load(response)

    def test_phone_issues_and_sends_sms_once_for_repeated_request(self):
        with patch.object(run,'make_invoice',return_value=('c','o','i','https://example.com/invoice')) as invoice, \
             patch.object(run,'send_sms',return_value={'sid':'SM-test','status':'queued'}) as sms:
            first=self.post(); second=self.post()
            self.assertEqual(first['id'],second['id'])
            self.assertEqual(first['status'],'INVOICED')
            self.assertEqual(first['amount'],132000)
            self.assertEqual(first['invoice_sms_status'],'QUEUED')
            self.assertTrue(first['invoice_issued_at'])
            invoice.assert_called_once(); sms.assert_called_once()

    def test_email_uses_invoice_delivery_without_sms(self):
        self.body['email']='test@example.com'
        with patch.object(run,'make_invoice',return_value=('c','o','i','https://example.com/invoice')) as invoice, \
             patch.object(run,'send_sms') as sms:
            result=self.post()
            self.assertEqual(result['status'],'INVOICED')
            self.assertEqual(invoice.call_args.args[0]['email'],'test@example.com')
            sms.assert_not_called()

    def test_failure_preserves_booking_and_manual_retry_reuses_id(self):
        with patch.object(run,'make_invoice',side_effect=TimeoutError('timeout')) as invoice:
            first=self.post(); second=self.post()
            self.assertEqual(first['status'],'ERROR')
            self.assertEqual(first['id'],second['id'])
            invoice.assert_called_once()
        with patch.object(run,'make_invoice',return_value=('c','o','i','https://example.com/invoice')), \
             patch.object(run,'send_sms',return_value={'sid':'SM-test','status':'queued'}):
            result=self.post(f"/api/reservations/{first['id']}/send-invoice", {})
            self.assertEqual(result['id'],first['id'])
            self.assertEqual(result['status'],'INVOICED')

    def test_sms_failure_keeps_issued_invoice(self):
        with patch.object(run,'make_invoice',return_value=('c','o','i','https://example.com/invoice')) as invoice, \
             patch.object(run,'send_sms',side_effect=TimeoutError('timeout')) as sms:
            first=self.post(); second=self.post()
            self.assertEqual(first['status'],'INVOICED')
            self.assertEqual(first['invoice_sms_status'],'ERROR')
            self.assertEqual(first['id'],second['id'])
            invoice.assert_called_once(); sms.assert_called_once()

    def test_direct_booking_does_not_invoice(self):
        with patch.object(run,'make_invoice') as invoice, patch.object(run,'send_sms') as sms:
            result=self.post('/api/reservations/direct')
            self.assertEqual(result['status'],'CONFIRMED')
            invoice.assert_not_called(); sms.assert_not_called()

if __name__=='__main__': unittest.main()
