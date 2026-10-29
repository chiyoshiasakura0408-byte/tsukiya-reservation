import json
import unittest
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from datetime import datetime, timedelta
from unittest.mock import patch

import test_workflow as workflow
import reservation_changes as changes

run = workflow.run


class PaidPartyChangesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow.WorkflowTest()
        self.fixture.setUp()
        self.rid = self.fixture.reservation()
        self.visit = (datetime.now()+timedelta(days=30)).strftime('%Y-%m-%dT18:00')
        c = run.con()
        c.execute("UPDATE reservations SET source='WEB',status='CONFIRMED',payment_source='SQUARE',"
                  "payment_confirmed_at='original-paid',confirmation_sent_at='original-mail',"
                  "square_order_id='original-order',seating_area='PRIVATE2',visit_at=?,amount=88000 WHERE id=?",
                  (self.visit,self.rid))
        c.commit(); c.close()
        self.data = dict(party_size=4,guest_note='子ども1名・食事なし（席のみ）',
            expected_party_size=2,expected_amount=88000,expected_visit_at=self.visit,
            expected_seating_area='PRIVATE2',expected_guest_note='')

    def tearDown(self):
        self.fixture.tearDown()

    def change(self, data=None):
        c = run.con()
        try:
            c.execute('BEGIN IMMEDIATE')
            result = changes.change(run,c,self.rid,data or self.data)
            c.commit()
            return result
        except Exception:
            c.rollback(); raise
        finally:
            c.close()

    def test_increase_and_note_preserve_payment_seat_and_identity(self):
        before = self.fixture.get(self.rid)
        with patch.object(run,'square') as square:
            result = self.change()
        square.assert_not_called()
        after = self.fixture.get(self.rid)
        self.assertEqual((after['party_size'],after['amount'],result['difference']),(4,176000,88000))
        self.assertEqual(after['guest_note'],self.data['guest_note'])
        for key in before:
            if key not in ('party_size','amount','guest_note','updated_at'):
                self.assertEqual(before[key],after[key],key)

    def test_stale_update_or_duplicate_request_does_not_double_change(self):
        self.change()
        with self.assertRaises(ValueError): self.change()
        self.assertEqual(self.fixture.get(self.rid)['amount'],176000)
        c=run.con();self.assertEqual(len(changes.records(c,self.rid)),1);c.close()

    def test_stale_note_rejected(self):
        self.data['expected_guest_note']='newer note'
        with self.assertRaises(ValueError): self.change()

    def test_original_payment_must_still_be_fully_paid(self):
        c=run.con()
        try:
            for state,amount in [('REFUNDED',88000),('PAID',44000)]:
                invoice={'status':state,'payment_requests':[{'total_completed_amount_money':{'amount':amount,'currency':'JPY'}}]}
                with patch.object(run,'square',return_value={'invoice':invoice}):
                    with self.assertRaises(ValueError):changes.verify_original(run,c,self.rid)
        finally:c.close()

    def test_room_capacity_not_bypassed(self):
        self.data['party_size']=5
        with self.assertRaises(ValueError): self.change()
        self.assertEqual(self.fixture.get(self.rid)['party_size'],2)

    def test_overlap_rejected(self):
        other=self.fixture.reservation()
        c=run.con();c.execute("UPDATE reservations SET seating_area='PRIVATE2',visit_at=? WHERE id=?",(self.visit,other));c.commit();c.close()
        with self.assertRaises(ValueError): self.change()

    def test_unpaid_and_decrease_rejected(self):
        self.data['party_size']=1
        with self.assertRaises(ValueError): self.change()
        self.data['party_size']=4
        c=run.con();c.execute("UPDATE reservations SET status='INVOICED' WHERE id=?",(self.rid,));c.commit();c.close()
        with self.assertRaises(ValueError): self.change()

    def square_stub(self, fail_publish=False):
        self.calls=[]; self.published=False
        def square(path,body=None):
            self.calls.append((path,body))
            if path=='/v2/customers':return {'customer':{'id':'customer'}}
            if path=='/v2/orders':return {'order':{'id':'extra-order'}}
            if path=='/v2/invoices':return {'invoice':{'id':'extra-invoice','version':0}}
            if path=='/v2/invoices/extra-invoice':
                return {'invoice':{'id':'extra-invoice','version':0,
                    'status':'UNPAID' if self.published else 'DRAFT',
                    'public_url':'https://example.com/invoice'}}
            if path.endswith('/publish'):
                self.published=True
                if fail_publish:raise TimeoutError()
                return {'invoice':{'id':'extra-invoice','status':'UNPAID','public_url':'https://example.com/invoice'}}
            raise AssertionError(path)
        return square

    def test_only_delta_is_invoiced_once_to_existing_email(self):
        ident=self.change()['change_id']
        with patch.object(run,'SQUARE_LOCATION_ID','location'),patch.object(run,'square',side_effect=self.square_stub()):
            # Location is frozen at save, so fixture needs a configured location there too.
            c=run.con();s=json.loads(c.execute('SELECT snapshot FROM reservation_changes').fetchone()[0]);s['location_id']='location';c.execute('UPDATE reservation_changes SET snapshot=?',(json.dumps(s),));c.commit();c.close()
            changes.issue(run,self.rid,ident)
            count=len(self.calls)
            changes.issue(run,self.rid,ident)
            self.assertEqual(len(self.calls),count)
        order=next(b for p,b in self.calls if p=='/v2/orders')
        self.assertEqual(order['order']['line_items'][0]['base_price_money']['amount'],88000)
        inv=next(b for p,b in self.calls if p=='/v2/invoices')['invoice']
        self.assertEqual(inv['delivery_method'],'EMAIL')
        self.assertIn('子ども1名・食事なし',inv['description'])
        self.assertIn('個室2',inv['description'])
        self.assertNotIn('card_id',str(inv))
        self.assertEqual(self.fixture.get(self.rid)['square_invoice_id'],'inv-test')

    def test_timeout_after_publish_recovers_without_second_publication(self):
        with patch.object(run,'SQUARE_LOCATION_ID','location'):
            ident=self.change()['change_id']
        with patch.object(run,'square',side_effect=self.square_stub(fail_publish=True)):
            with self.assertRaises(TimeoutError): changes.issue(run,self.rid,ident)
            result=changes.issue(run,self.rid,ident)
        self.assertEqual(result['state'],'INVOICED')
        self.assertEqual(sum(p.endswith('/publish') for p,b in self.calls),1)
        self.assertEqual(sum(p=='/v2/orders' for p,b in self.calls),1)

    def test_paid_reconciliation_checks_currency_amount_and_status(self):
        ident=self.change()['change_id']
        c=run.con();c.execute("UPDATE reservation_changes SET invoice_id='extra',state='INVOICED' WHERE id=?",(ident,));c.commit();c.close()
        for state,amount,currency,expected in [('PARTIALLY_PAID',88000,'JPY',88000),('PAID',1,'JPY',88000),('PAID',88000,'USD',88000),('PAID',88000,'JPY',0)]:
            invoice={'status':state,'payment_requests':[{'total_completed_amount_money':{'amount':amount,'currency':currency}}]}
            with patch.object(run,'square',return_value={'invoice':invoice}):changes.reconcile(run)
            c=run.con();self.assertEqual(changes.summary(c,self.rid)['additional_unpaid'],expected);c.close()

    def test_unpaid_difference_suppresses_full_payment_confirmation(self):
        self.change()
        with patch.object(run,'send_confirmation_sms') as sms:
            run.deliver_confirmation(self.fixture.get(self.rid))
        sms.assert_not_called()

    def test_customer_cancellation_requires_manual_split_payment_review(self):
        self.change()
        status,_=run.customer_cancellation(run.cancellation_token(self.fixture.get(self.rid)),True,True,0)
        self.assertEqual(status,409)
        self.assertEqual(self.fixture.get(self.rid)['status'],'CONFIRMED')

    def test_http_auth_origin_save_and_invoice(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin=f'http://127.0.0.1:{server.server_port}'
        def post(action,data,auth=True,valid_origin=True):
            headers={'Content-Type':'application/json','X-Tsukiya-Action':'paid-reservation-change'}
            if auth:headers['X-Admin-Token']='test-secret'
            if valid_origin:headers['Origin']=origin
            req=urllib.request.Request(origin+f'/api/reservations/{self.rid}/'+action,
                data=json.dumps(data).encode(),headers=headers)
            return json.load(urllib.request.urlopen(req))
        with patch.object(run,'APP_BASE_URL',origin),patch.object(run,'SQUARE_LOCATION_ID','location'):
            for auth,valid_origin,code in [(False,True,401),(True,False,403)]:
                with self.assertRaises(urllib.error.HTTPError) as err:
                    post('paid-party',self.data,auth,valid_origin)
                self.assertEqual(err.exception.code,code)
            with patch.object(run,'square',return_value={'invoice':{'status':'PAID','payment_requests':[{'total_completed_amount_money':{'amount':88000,'currency':'JPY'}}]}}):
                result=post('paid-party',self.data)
            with patch.object(run,'square',side_effect=self.square_stub()):
                invoice=post('difference-invoice',{'change_id':result['change_id']})
            self.assertEqual(invoice['amount'],88000)
            req=urllib.request.Request(origin+'/api/reservations',headers={'X-Admin-Token':'test-secret'})
            rows=json.load(urllib.request.urlopen(req))
            row=next(r for r in rows if r['id']==self.rid)
            self.assertEqual(row['additional_unpaid'],88000)
            self.assertEqual(row['additional_invoices'][0]['state'],'INVOICED')
