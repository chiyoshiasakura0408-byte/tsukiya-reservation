import json
import os
import tempfile
import unittest
from datetime import datetime,timedelta
from unittest.mock import patch
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp(prefix='guest-test-import-'))
import run
import concierge as bot
import guest_service as care

class GuestServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.object(run,'DB',self.tmp.name+'/db.sqlite');p.start();self.addCleanup(p.stop)
        c=run.con();c.close();c=care.connect(run)
        with c:bot.put(c,'care_enabled','1');bot.put(c,'enabled','1');bot.put(c,'care_started','2000-01-01')
        c.close();self.now=datetime.now(bot.JST).replace(hour=12,minute=0,second=0,microsecond=0)
        clock=patch.object(care,'datetime',wraps=datetime);self.clock=clock.start();self.clock.now.return_value=self.now;self.addCleanup(clock.stop)

    def reservation(self,name='お客様',days=0,drink='日本酒',allowed='可',source='DIRECT',paid=False):
        c=run.con();ts=run.now_iso();day=(self.now.date()+timedelta(days=days)).isoformat()
        with c:
            cur=c.execute('INSERT INTO customers(match_key,name,preferred_drinks,alcohol_service,created_at,updated_at) VALUES(?,?,?,?,?,?)',(name,name,drink,allowed,ts,ts));cid=cur.lastrowid
            cur=c.execute("INSERT INTO reservations(source,guest_name,email,visit_at,party_size,course_name,amount,seating_area,status,customer_id,created_at,updated_at,payment_source,payment_confirmed_at) VALUES(?,?,? ,?,2,'コース',120000,'COUNTER','CONFIRMED',?,?,?,?,?)",(source,name,'test@example.com',day+'T18:00',cid,ts,ts,'SQUARE' if paid else None,ts if paid else None));rid=cur.lastrowid
            c.execute("INSERT INTO concierge_customers(user_id,customer_id) VALUES(?,?)",('line-'+str(cid),cid))
        c.close();return rid,cid

    def sake(self):
        return care.configure(run,{'action':'sake-save','name':'試験銘柄','description':'公式情報から確認した説明','photo':'https://example.com/sake.jpg','sources':['https://example.com/product'],'stock':5,'day':str(self.now.date()),'approved':True})['id']

    def jobs(self):
        c=care.connect(run)
        try:return [dict(r) for r in c.execute('SELECT * FROM guest_jobs')]
        finally:c.close()

    def test_sake_excludes_soft_drinks_unknown_and_duplicates(self):
        self.reservation('飲酒可');self.reservation('ソフトドリンク',drink='ソフトドリンク');self.reservation('未確認',allowed='')
        self.sake();care.plan(run,self.now);care.plan(run,self.now)
        self.assertEqual(sum(j['kind']=='sake' for j in self.jobs()),1)
        self.assertEqual(sum(j['kind']=='staff-sake' for j in self.jobs()),1)

    def test_reminder_all_sources_only_three_days(self):
        for source in ['DIRECT','CONCIERGE_LINE','PUBLIC','PHONE']:
            self.reservation(source,days=3,source=source)
        self.reservation('明日',days=1)
        care.plan(run,self.now.replace(hour=11));self.assertEqual(len(self.jobs()),0)
        care.plan(run,self.now);care.plan(run,self.now)
        self.assertEqual(sum(j['kind']=='reminder' for j in self.jobs()),4)

    def test_queue_not_recreated_while_line_pending(self):
        self.reservation(days=3);care.plan(run,self.now)
        care.deliver(run);care.deliver(run)
        c=care.connect(run);self.assertEqual(c.execute('SELECT count(*) FROM concierge_outbox').fetchone()[0],1);c.close()
        self.assertEqual(self.jobs()[0]['state'],'queued')

    def test_cancel_after_queue_prevents_line_send(self):
        rid,_=self.reservation(days=3);care.plan(run,self.now);care.deliver(run)
        c=care.connect(run)
        with c:c.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?",(rid,))
        self.assertFalse(care.validate_delivery(c,self.jobs()[0]['delivery_id']));c.close()

    def test_stock_zero_stops_queued_sake(self):
        self.reservation();sid=self.sake();care.plan(run,self.now);care.deliver(run)
        job=next(j for j in self.jobs() if j['kind']=='sake')
        c=care.connect(run)
        with c:c.execute('UPDATE premium_sake SET stock=0 WHERE id=?',(sid,))
        self.assertFalse(care.validate_delivery(c,job['delivery_id']));c.close()

    def test_thanks_includes_next_direct_booking(self):
        rid,cid=self.reservation(paid=True)
        c=run.con();nextday=(self.now.date()+timedelta(days=10)).isoformat()
        with c:c.execute("INSERT INTO reservations(source,guest_name,visit_at,party_size,course_name,amount,status,customer_id,created_at,updated_at) VALUES('DIRECT','お客様',?,2,'次回コース',120000,'CONFIRMED',?,'now','now')",(nextday+'T18:00',cid))
        c.close();care.plan(run,self.now)
        job=next(j for j in self.jobs() if j['kind']=='thanks');self.assertIn('次回のご予約',job['body']);self.assertIn(nextday,job['body'])

    def test_email_used_when_line_not_connected_without_double_send(self):
        self.reservation(days=3);c=care.connect(run)
        with c:bot.put(c,'enabled','0')
        c.close();care.plan(run,self.now)
        with patch.object(care,'send_email') as mail:
            care.deliver(run);care.deliver(run);self.assertEqual(mail.call_count,1)
        self.assertEqual(self.jobs()[0]['state'],'accepted')

    def test_line_general_inquiry_goes_to_asakura(self):
        rid,cid=self.reservation();c=care.connect(run)
        with c:
            bot.put(c,'owner','owner-id')
            result=bot.respond(c,'line-'+str(cid),'忘れ物について確認をお願いします',None,run.PUBLIC_COURSES,'https://example.com')
        self.assertIn('受け付け',result[0]['text']);self.assertEqual(c.execute('SELECT count(*) FROM concierge_requests').fetchone()[0],1);c.close()

    def test_soft_drink_checkbox_overrides_permission(self):
        rid,cid=self.reservation();self.sake();care.plan(run,self.now)
        c=care.connect(run)
        with c:c.execute("UPDATE customers SET soft_drink_only='1' WHERE id=?",(cid,))
        c.close();care.deliver(run)
        self.assertEqual(next(j for j in self.jobs() if j['kind']=='sake')['state'],'cancelled')

    def test_thanks_waits_four_hours_and_refreshes_next_booking(self):
        rid,cid=self.reservation(paid=True);care.plan(run,self.now)
        job=next(j for j in self.jobs() if j['kind']=='thanks')
        with patch.object(care.time,'time',return_value=job['due']-1):care.deliver(run)
        self.assertEqual(self.jobs()[0]['state'],'pending')
        c=run.con()
        with c:c.execute("INSERT INTO reservations(source,guest_name,visit_at,party_size,course_name,amount,status,customer_id,created_at,updated_at) VALUES('DIRECT','お客様','2099-01-01T18:00',2,'次回',120000,'CONFIRMED',?,'now','now')",(cid,))
        c.close()
        with patch.object(care.time,'time',return_value=job['due']):care.deliver(run)
        self.assertEqual(self.jobs()[0]['state'],'queued');self.assertIn('2099-01-01',self.jobs()[0]['body'])

    def test_stock_drafts_restrict_group_and_deduplicate(self):
        import line_delivery
        event={'webhookEventId':'stock-event','type':'message','source':{'type':'group','groupId':line_delivery.TARGET},'message':{'type':'text','text':f'隠し酒登録\n銘柄：テスト銘柄\n在庫：6杯\n提供日：{self.now.date()}'}}
        care.receive_stock(run.DB,[event,event]);care.drain_stock(run);care.drain_stock(run)
        c=care.connect(run);rows=c.execute('SELECT * FROM premium_sake').fetchall()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['approved'],0);self.assertEqual(rows[0]['stock'],6)
        self.assertEqual(c.execute("SELECT count(*) FROM concierge_outbox WHERE kind='stock-draft'").fetchone()[0],1);c.close()
        event['webhookEventId']='outside';event['source']['groupId']='other'
        care.receive_stock(run.DB,[event]);care.drain_stock(run)
        c=care.connect(run);self.assertEqual(c.execute('SELECT count(*) FROM premium_sake').fetchone()[0],1);c.close()

    def test_reservation_fields_owner_handoff_once(self):
        rid,_=self.reservation(days=3);c=care.connect(run)
        with c:
            c.execute("UPDATE reservations SET celebration_items='花束',guest_note='記念日、卵不可' WHERE id=?",(rid,));bot.put(c,'owner','owner-id')
        c.close();care.sync_requests(run);care.sync_requests(run)
        c=care.connect(run);rows=c.execute('SELECT * FROM concierge_requests').fetchall()
        self.assertEqual(len(rows),1);self.assertIn('花束',rows[0]['body']);self.assertIn('卵不可',rows[0]['body'])
        self.assertEqual(c.execute("SELECT count(*) FROM concierge_outbox WHERE kind='request'").fetchone()[0],1);c.close()

if __name__=='__main__':unittest.main()
