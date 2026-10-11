import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import run
import concierge as bot
import concierge_booking as booking
import line_bot
import vip_access
import vip_slots


class VipTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=self.tmp.name+'/vip.sqlite'
        p=patch.object(run,'DB',self.db);p.start();self.addCleanup(p.stop)
        for name,value in [('deliver_confirmation',lambda row:row),('square',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('no billing')))]:
            stub=patch.object(run,name,value);stub.start();self.addCleanup(stub.stop)
        env=patch.dict(os.environ,{'CONCIERGE_LINE_CHANNEL_SECRET':'customer','LINE_CHANNEL_SECRET':'staff'});env.start();self.addCleanup(env.stop)
        c=run.con();c.close();c=bot.connect(self.db)
        with c:bot.put(c,'enabled','1');bot.put(c,'owner','owner')
        c.close();self.n=0
        today=datetime.now(bot.JST).date();self.day=today.replace(month=11,day=10)
        if self.day<today:self.day=self.day.replace(year=self.day.year+1)

    def send(self,user,text,work=False,source='user',signature=True):
        self.n+=1
        raw=json.dumps({'events':[{'webhookEventId':str(self.n),'type':'message','source':{'type':source,'userId':user},'message':{'type':'text','text':text}}]}).encode()
        sig=base64.b64encode(hmac.new(b'staff' if work else b'customer',raw,hashlib.sha256).digest()).decode() if signature else 'bad'
        return line_bot.receive(self.db,raw,sig) if work else bot.receive(self.db,raw,sig,run.concierge_availability,run.PUBLIC_COURSES,'https://example.com')

    def dbquery(self,query,args=()):
        c=bot.connect(self.db)
        try:return [dict(r) for r in c.execute(query,args)]
        finally:c.close()

    def apply(self,user='guest'):
        for text in ('利用申請','申請者','紹介者'):self.send(user,text)

    def decision(self,user='guest',decision='approved',cid=0):
        c=bot.connect(self.db)
        with c:vip_access.request_decision(c,{'user_id':user,'decision':decision,'customer_id':cid},'https://example.com')
        c.close()
        notice=self.dbquery("SELECT payload FROM concierge_outbox WHERE kind='vip-decision' ORDER BY created DESC LIMIT 1")[0]
        return json.loads(notice['payload'])[0]['text'].split('VIP承認 ')[1]

    def approve(self,user='guest'):
        self.apply(user);code=self.decision(user);self.send('owner','VIP承認 '+code,work=True)

    def proposal(self,user='guest',through=True,area='COUNTER',party=2):
        c=bot.connect(self.db)
        token=secrets.token_urlsafe(32)
        slot={'course':'matsuba-seko','date':str(self.day),'time':'18:00','area':area,'party_size':party,'through':through}
        with c:c.execute('INSERT INTO concierge_proposals(hash,user_id,slot,expires) VALUES(?,?,?,?)',(vip_access.digest(token),user,json.dumps(slot),time.time()+3600))
        c.close();return token

    def verify(self,token,user='guest'):
        info,browser=booking.verify(run,token,'');self.send(user,info['command']);return browser

    def book(self,token,browser,clock='19:17'):
        data={'guest_name':'申請者','phone':'09012345678','email':'test@example.com','cancellation_policy_accepted':True,'start_time':clock}
        return booking.booking(run,token,data,browser)

    def insert(self,area='COUNTER',clock='18:00',party=2):
        c=run.con()
        with c:
            cur=c.execute("INSERT INTO reservations(source,guest_name,visit_at,party_size,course_name,amount,seating_area,counter_round,status,created_at,updated_at) VALUES('DIRECT','通常予約',?,?,'コース',120000,?,?,'CONFIRMED','now','now')",(str(self.day)+'T'+clock,party,area,1 if clock=='18:00' else 2))
        c.close();return cur.lastrowid

    def test_pending_and_signed_owner_only_approval(self):
        self.apply();self.send('guest','VIP担当に相談');self.send('guest',str(self.day)+' 2名 通し')
        self.assertFalse(self.dbquery('SELECT * FROM concierge_proposals'))
        self.assertFalse(self.dbquery('SELECT * FROM concierge_requests'))
        self.assertEqual(len(self.dbquery("SELECT * FROM concierge_outbox WHERE kind='vip-access' AND channel='owner'")),1)
        code=self.decision()
        for user,source,signature in [('other','user',True),('owner','group',True),('owner','user',False)]:
            self.send(user,'VIP承認 '+code,True,source,signature)
            self.assertEqual(self.dbquery('SELECT status FROM vip_access')[0]['status'],'pending')
        self.send('owner','VIP承認 '+code,True);self.send('owner','VIP承認 '+code,True)
        self.assertEqual(self.dbquery('SELECT status FROM vip_access')[0]['status'],'approved')
        self.assertEqual(len(self.dbquery('SELECT * FROM vip_audit')),1)
        self.assertEqual(len(self.dbquery('SELECT * FROM customers')),1)

    def test_forwarded_link_wrong_identity_revoke_and_legacy_link(self):
        self.approve();token=self.proposal()
        with self.assertRaises(PermissionError):booking.booking(run,token)
        info,browser=booking.verify(run,token,'');self.send('other',info['command'])
        with self.assertRaises(PermissionError):booking.booking(run,token,browser=browser)
        self.send('guest',info['command']);self.assertTrue(booking.booking(run,token,browser=browser)['through'])
        with self.assertRaises(PermissionError):booking.booking(run,token,browser='copied-url-not-cookie')
        self.send('owner','VIP承認 '+self.decision(decision='revoked'),True)
        with self.assertRaises(PermissionError):self.book(token,browser)
        self.assertFalse(self.dbquery('SELECT * FROM reservations'))
        self.send('guest','お客様連携 copied-link')
        self.assertEqual(self.dbquery('SELECT status FROM vip_access')[0]['status'],'revoked')

    def test_browser_expiry_and_proposal_scope(self):
        self.approve();token=self.proposal();browser=self.verify(token);other=self.proposal()
        with self.assertRaises(PermissionError):booking.booking(run,other,browser=browser)
        c=bot.connect(self.db)
        with c:c.execute('UPDATE vip_browser SET expires=0')
        c.close()
        with self.assertRaises(PermissionError):self.book(token,browser)

    def test_counter_through_price_same_seats_rounds_idempotency_cancel(self):
        self.approve();self.insert(party=2);self.insert(clock='20:30',party=3)
        token=self.proposal();browser=self.verify(token);result=self.book(token,browser)
        self.assertEqual(self.book(token,browser),result)
        r=self.dbquery('SELECT * FROM reservations WHERE id=?',(result['reservation_id'],))[0]
        self.assertEqual(r['amount'],120000);self.assertEqual(r['vip_seats'],'[4, 5]')
        self.assertEqual(r['visit_at'],str(self.day)+'T19:17');self.assertEqual(r['duration_minutes'],223)
        c=run.con()
        for clock,rnd,remain in [('18:00',1,4),('20:30',2,3)]:
            seats,_=vip_slots.counter_assignments(run,c,str(self.day),clock)
            self.assertEqual([n for n,rid in seats.items() if rid==r['id']],[4,5])
            self.assertTrue(run.availability_check(c,'COUNTER',str(self.day)+'T'+clock,remain,rnd)[0])
            self.assertFalse(run.availability_check(c,'COUNTER',str(self.day)+'T'+clock,remain+1,rnd)[0])
        with c:c.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?",(r['id'],))
        self.assertEqual(vip_slots.choose(run,c,str(self.day),'COUNTER',2),[4,5]);c.close()

    def test_both_rounds_same_room_required(self):
        self.approve();self.insert(area='PRIVATE1',party=2);self.insert(area='PRIVATE2',clock='20:30',party=2)
        token=self.proposal(area='PRIVATE');browser=self.verify(token)
        with self.assertRaises(ValueError):self.book(token,browser)
        self.assertEqual(len(self.dbquery('SELECT * FROM reservations')),2)

    def test_through_room_blocks_entire_evening_even_with_late_start(self):
        self.approve();token=self.proposal(area='PRIVATE');browser=self.verify(token);self.book(token,browser,'21:10')
        c=run.con()
        for clock in ('18:00','20:30'):
            self.assertFalse(run.availability_check(c,'PRIVATE1',str(self.day)+'T'+clock,2)[0])
        c.close()

    def test_start_bounds_and_stale_capacity(self):
        self.approve();token=self.proposal();browser=self.verify(token)
        for clock in ('17:59','23:00','25:00','18:00:00','abc'):
            with self.assertRaises(ValueError):self.book(token,browser,clock)
        self.insert(clock='20:30',party=8)
        with self.assertRaises(ValueError):self.book(token,browser)

    def test_signed_line_through_selection_only_approved(self):
        self.approve();self.send('guest',str(self.day)+' 2名 通し')
        rows=self.dbquery('SELECT slot FROM concierge_proposals')
        self.assertEqual(len(rows),2);self.assertTrue(all(json.loads(r['slot'])['through'] for r in rows))
        self.assertFalse(run.public_slot_allowed(self.day,'19:17','matsuba-seko'))

    def test_concurrent_booking_serializes_last_seats(self):
        self.approve();self.insert(party=6)
        tokens=[self.proposal(),self.proposal()];browsers=[self.verify(t) for t in tokens]
        def attempt(pair):
            try:return self.book(*pair)['reservation_id']
            except ValueError:return None
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(attempt,zip(tokens,browsers)))
        self.assertEqual(sum(r is not None for r in results),1)

    def test_http_requires_cookie_custom_header_and_admin_for_decisions(self):
        self.approve();token=self.proposal()
        server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def call(path,data,headers={}):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('POST',path,json.dumps(data),{'Content-Type':'application/json',**headers});r=conn.getresponse();out=(r.status,json.loads(r.read()),r.getheader('Set-Cookie'));conn.close();return out
        self.assertEqual(call('/api/concierge/booking-info',{'token':token})[0],403)
        hdr={'X-Tsukiya-Action':'vip-booking'}
        status,info,cookie=call('/api/concierge/verify',{'token':token},hdr)
        self.assertEqual(status,200);self.assertIn('HttpOnly',cookie)
        self.send('guest',info['command'])
        self.assertEqual(call('/api/concierge/booking-info',{'token':token},hdr)[0],403)
        hdr['Cookie']=cookie.split(';')[0]
        self.assertEqual(call('/api/concierge/booking-info',{'token':token},hdr)[0],200)
        self.assertEqual(call('/api/vip-access/decision',{'user_id':'guest','decision':'approved','customer_id':0},{'X-Tsukiya-Action':'vip-access'})[0],401)
