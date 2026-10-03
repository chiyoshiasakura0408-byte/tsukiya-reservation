import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import patch
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp(prefix='concierge-test-import-'))
import run
import concierge as bot
import concierge_booking
import instagram_concierge as ig

class ConciergeV2(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=self.tmp.name+'/test.sqlite'
        p=patch.object(run,'DB',self.db);p.start();self.addCleanup(p.stop)
        env=patch.dict(os.environ,{'INSTAGRAM_APP_SECRET':'test-secret','INSTAGRAM_ACCOUNT_ID':'123','INSTAGRAM_ACCESS_TOKEN':'test-token','INSTAGRAM_VERIFY_TOKEN':'test-verify','INSTAGRAM_GRAPH_VERSION':'v25.0'})
        env.start();self.addCleanup(env.stop)
        c=run.con();c.close();c=bot.connect(self.db)
        with c:
            bot.put(c,'enabled','1');bot.put(c,'instagram_enabled','1');bot.put(c,'owner','owner-id')
            c.execute("INSERT INTO concierge_customers(user_id) VALUES('line-user')")
        c.close()

    def proposal(self):
        today=datetime.now(bot.JST).date();day=today.replace(month=11,day=10)
        if day<today:day=day.replace(year=day.year+1)
        slots=run.concierge_availability(day,2)
        c=bot.connect(self.db)
        with c:messages=bot.respond(c,'line-user',str(day)+' 2名',lambda d,p:slots,run.PUBLIC_COURSES,'https://example.com')
        c.close()
        return messages[1]['template']['actions'][0]['uri'].split('#')[1]

    def test_booking_no_square_and_idempotency(self):
        token=self.proposal();data={'guest_name':'テスト','phone':'09012345678','email':'test@example.com','cancellation_policy_accepted':True}
        with patch.object(run,'square',side_effect=AssertionError('must not charge')),patch.object(run,'deliver_confirmation',side_effect=lambda x:x):
            first=concierge_booking.booking(run,token,data)
            second=concierge_booking.booking(run,token,data)
        self.assertEqual(first,second)
        c=run.con();row=c.execute('SELECT * FROM reservations').fetchone();c.close()
        self.assertEqual(row['source'],'CONCIERGE_LINE');self.assertEqual(row['status'],'CONFIRMED');self.assertIsNone(row['square_invoice_id'])
        self.assertEqual(row['amount'],120000)

    def test_booking_rechecks_sold_out_and_consent(self):
        token=self.proposal()
        with self.assertRaises(ValueError):concierge_booking.booking(run,token,{'cancellation_policy_accepted':False})
        with patch.object(run,'availability_check',return_value=(False,'sold out')):
            with self.assertRaises(ValueError):concierge_booking.booking(run,token,{'guest_name':'テスト','phone':'09012345678','email':'test@example.com','cancellation_policy_accepted':True})
        c=run.con();self.assertEqual(c.execute('SELECT count(*) FROM reservations').fetchone()[0],0);c.close()

    def test_verified_profile_and_personalized_announcement(self):
        c=run.con()
        with c:c.execute("INSERT INTO customers(match_key,name,preferred_drinks,preferred_crab,preferred_seat,return_transport,created_at,updated_at) VALUES('one','朝倉','日本酒','ずわい蟹','個室','タクシー','now','now')")
        customer_id=c.execute('SELECT id FROM customers').fetchone()[0];c.close()
        command=bot.configure(self.db,{'action':'customer-link','customer_id':customer_id})['command']
        c=bot.connect(self.db)
        with c:
            self.assertEqual(bot.profile(c,'line-user'),{})
            self.assertIn('完了',bot.link_customer(c,'line-user',command))
            self.assertIn('確認できません',bot.link_customer(c,'line-user',command))
            text=bot.announcement(c,'line-user',{'crab':'ずわい蟹','origin':'北海道','arrival_date':'2026-11-10','sake':'新しい銘柄'})
        c.close()
        for phrase in ['朝倉様','お待たせ','北海道産','11月10日','新しい銘柄','タクシー','個室']:self.assertIn(phrase,text)
        self.assertNotIn('🦀',bot.text_message('ありがとうございます🦀')['text'])

    def instagram(self,text='Hello',mid='message1',age=0,signature=True):
        raw=json.dumps({'object':'instagram','entry':[{'id':'123','messaging':[{'sender':{'id':'456'},'recipient':{'id':'123'},'timestamp':(time.time()-age)*1000,'message':{'mid':mid,'text':text}}]}]}).encode()
        sig='sha256='+hmac.new(b'test-secret',raw,hashlib.sha256).hexdigest() if signature else 'bad'
        return ig.receive(self.db,raw,sig,run.concierge_availability,run.PUBLIC_COURSES,'https://example.com')

    def test_instagram_signature_dedup_and_english(self):
        self.assertEqual(self.instagram(signature=False)[0],403)
        self.instagram();self.instagram()
        c=bot.connect(self.db);rows=c.execute("SELECT * FROM concierge_outbox WHERE channel='instagram'").fetchall();c.close()
        self.assertEqual(len(rows),1);self.assertIn('Welcome',rows[0]['payload'])

    def test_instagram_request_handoff_and_expired_window(self):
        self.instagram('I have a shellfish allergy')
        c=bot.connect(self.db)
        self.assertEqual(c.execute('SELECT count(*) FROM concierge_requests').fetchone()[0],1)
        row=c.execute("SELECT * FROM concierge_outbox WHERE channel='instagram'").fetchone()
        with c:c.execute('UPDATE instagram_contacts SET last_inbound=?',(time.time()-90000,))
        with patch('urllib.request.urlopen',side_effect=AssertionError('must not send')):ig.deliver_row(c,row)
        self.assertEqual(c.execute('SELECT state FROM concierge_outbox WHERE id=?',(row['id'],)).fetchone()[0],'failed');c.close()

    def test_instagram_stale_event_never_reopens_window(self):
        self.instagram(age=90000)
        c=bot.connect(self.db);self.assertEqual(c.execute('SELECT count(*) FROM concierge_outbox').fetchone()[0],0);c.close()

    def test_instagram_no_line_prepayment_exemption(self):
        c=bot.connect(self.db)
        text=ig.respond(c,'ig:456','2026-11-10 2','en',run.PUBLIC_COURSES,'https://example.com',[{'course':'matsuba-seko','date':'2026-11-10','party_size':2,'area':'COUNTER','time':'18:00'}]);c.close()
        self.assertIn('Full payment',text);self.assertIn('/book?',text);self.assertNotIn('/concierge/book',text)

if __name__=='__main__':unittest.main()
