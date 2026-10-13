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
        return 'retired-code'

    def approve(self,user='guest'):
        self.apply(user);self.decision(user)

    def proposal(self,user='guest',through=True,area='PRIVATE',party=2):
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

    def test_invite_single_use_expiry_and_no_owner_notice(self):
        c=bot.connect(self.db)
        with c:
            invite=vip_access.create_invite(c,{'name':'招待顧客'})
            expired=vip_access.create_invite(c,{'name':'期限切れ'})
            c.execute('UPDATE vip_invites SET expires=0 WHERE hash=?',(vip_access.digest(expired['command'].split()[1]),))
        c.close()
        self.send('guest',invite['command'])
        self.assertEqual(self.dbquery('SELECT status FROM vip_access')[0]['status'],'approved')
        self.assertEqual(self.dbquery('SELECT name FROM customers')[0]['name'],'招待顧客')
        self.send('other',invite['command'])
        self.send('expired',expired['command'])
        self.assertEqual(len(self.dbquery('SELECT * FROM vip_access')),1)
        self.assertFalse(self.dbquery("SELECT * FROM concierge_outbox WHERE channel='owner'"))
        self.send('guest',invite['command'])
        self.assertEqual(len(self.dbquery('SELECT * FROM vip_audit')),1)

    def test_self_withdraw_confirmation_and_retention(self):
        self.approve()
        self.send('guest','VIP退会を確定')
        self.assertEqual(self.dbquery('SELECT status FROM vip_access')[0]['status'],'approved')
        self.send('guest','VIP退会')
        self.send('guest','VIP退会をやめる')
        self.assertEqual(self.dbquery('SELECT status FROM vip_access')[0]['status'],'approved')
        self.send('guest','VIP退会')
        self.send('guest','VIP退会を確定')
        self.assertEqual(self.dbquery('SELECT status FROM vip_access')[0]['status'],'revoked')
        self.assertEqual(self.dbquery('SELECT stopped FROM concierge_customers')[0]['stopped'],1)
        self.assertEqual(len(self.dbquery('SELECT * FROM customers')),1)
        self.assertFalse(self.dbquery("SELECT * FROM concierge_outbox WHERE channel='owner'"))

    def test_direct_approval_without_owner_notifications(self):
        self.apply()
        self.assertEqual(self.dbquery('SELECT status FROM vip_access')[0]['status'],'pending')
        self.decision()
        self.assertEqual(self.dbquery('SELECT status FROM vip_access')[0]['status'],'approved')
        self.assertFalse(self.dbquery("SELECT * FROM concierge_outbox WHERE channel='owner'"))
        self.assertEqual(len(self.dbquery('SELECT * FROM vip_audit')),1)
        cid=self.dbquery('SELECT id FROM customers')[0]['id']
        self.decision(cid=cid)
        self.assertEqual(len(self.dbquery('SELECT * FROM vip_audit')),1)
        self.decision(decision='revoked')
        self.send('owner','VIP承認 retired-code',True)
        self.assertEqual(self.dbquery('SELECT status FROM vip_access')[0]['status'],'revoked')

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

    def test_new_counter_through_denied_existing_preserved(self):
        self.approve();token=self.proposal(area='COUNTER');browser=self.verify(token)
        with self.assertRaises(ValueError):self.book(token,browser)
        rid=self.insert()
        c=run.con()
        with c:c.execute("UPDATE reservations SET vip_through=1,vip_seats='[1, 2]' WHERE id=?",(rid,))
        for clock in ('18:00','20:30'):
            seats,_=vip_slots.counter_assignments(run,c,str(self.day),clock)
            self.assertEqual([n for n,r in seats.items() if r==rid],[1,2])
        c.close()

    def test_private_custom_price_duration_and_2030_normal(self):
        self.approve();token=self.proposal();browser=self.verify(token)
        result=self.book(token,browser,'18:30')
        self.assertEqual(self.book(token,browser),result)
        row=self.dbquery('SELECT * FROM reservations')[0]
        self.assertEqual((row['amount'],row['vip_through'],row['duration_minutes']),(120000,1,270))
        self.insert(area='PRIVATE2')
        token=self.proposal();browser=self.verify(token);result=self.book(token,browser,'20:30')
        row=self.dbquery('SELECT * FROM reservations WHERE id=?',(result['reservation_id'],))[0]
        self.assertEqual((row['seating_area'],row['vip_through'],row['duration_minutes']),('PRIVATE2',0,150))

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
        self.insert(area='PRIVATE1',clock='20:30');self.insert(area='PRIVATE2',clock='20:30')
        with self.assertRaises(ValueError):self.book(token,browser)

    def test_signed_line_through_selection_only_approved(self):
        self.approve();self.send('guest',str(self.day)+' 2名 通し')
        rows=self.dbquery('SELECT slot FROM concierge_proposals')
        self.assertEqual(len(rows),1);self.assertTrue(all(json.loads(r['slot'])['through'] and json.loads(r['slot'])['area']=='PRIVATE' for r in rows))
        self.assertFalse(run.public_slot_allowed(self.day,'19:17','matsuba-seko'))

    def test_concurrent_booking_serializes_last_seats(self):
        self.approve();self.insert(area='PRIVATE1')
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

    def test_staff_phone_and_direct_custom_time(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def call(path,data):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('POST',path,json.dumps(data),{'Content-Type':'application/json','x-admin-token':'test'})
            r=conn.getresponse();result=r.status,json.loads(r.read());conn.close();return result
        data={'guest_name':'電話予約','phone':'09012345678','email':'test@example.com','party_size':2,'visit_at':str(self.day)+'T19:00','seating_area':'PRIVATE','time_specified':True}
        with patch.object(run,'ADMIN_TOKEN','test'):
            status,row=call('/api/reservations/phone',data)
            self.assertEqual(status,200,row)
            self.assertEqual((row['vip_through'],row['duration_minutes'],row['status']),(1,240,'PENDING'))
            status,dup=call('/api/reservations/phone',{**data,'seating_area':row['seating_area']})
            self.assertEqual(status,400)
            status,bad=call('/api/reservations/phone',{**data,'seating_area':'COUNTER'})
            self.assertEqual(status,400)
            data.update(visit_at=str(self.day)+'T18:30',request_id='12345678-1234-1234-1234-123456789abc')
            status,direct=call('/api/reservations/direct',data)
            self.assertEqual(status,200,direct)
            self.assertEqual((direct['vip_through'],direct['duration_minutes'],direct['status']),(1,270,'CONFIRMED'))
            self.assertEqual(call('/api/reservations/direct',data)[1]['id'],direct['id'])

    def test_staff_2030_keeps_first_round_available(self):
        c=run.con()
        room,duration,through=vip_slots.staff_slot(run,c,'PRIVATE',str(self.day)+'T20:30',2)
        self.assertEqual((duration,through),(150,False))
        c.close();self.insert(area=room,clock='20:30')
        c=run.con();self.assertTrue(run.availability_check(c,room,str(self.day)+'T18:00',2)[0]);c.close()


    def test_owner_invite_delivery_is_private_and_idempotent(self):
        owner='U'+'a'*32
        c=bot.connect(self.db)
        with c:
            invite=vip_access.create_invite(c,{'name':'招待顧客'})
            token=invite['command'].split()[1]
            bot.put(c,'owner',owner)
        with patch.dict(os.environ,{'LINE_CHANNEL_ACCESS_TOKEN':'work-token'}):
            with c:
                first=vip_access.send_invite_to_owner(c,token,'https://example.com')
                second=vip_access.send_invite_to_owner(c,token,'https://example.com')
            self.assertEqual(first,second)
            rows=c.execute("SELECT * FROM concierge_outbox WHERE kind='vip-invite'").fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual((rows[0]['user_id'],rows[0]['channel']),(owner,'owner'))
            payload=json.loads(rows[0]['payload'])
            self.assertIn('招待顧客様',payload[0]['text'])
            self.assertIn(invite['url'],payload[0]['text'])
            self.assertEqual(payload[1]['type'],'image')
            self.assertIn('/vip-invite-qr?token=',payload[1]['originalContentUrl'])
            for invalid in ('','C'+'b'*32,'R'+'b'*32):
                with c:bot.put(c,'owner',invalid)
                with self.assertRaises(ValueError):vip_access.send_invite_to_owner(c,token,'https://example.com')
        c.close()

    def test_invite_qr_does_not_redeem_and_expires(self):
        c=bot.connect(self.db)
        with c:invite=vip_access.create_invite(c,{'name':'招待顧客'})
        token=invite['command'].split()[1]
        png=vip_access.invite_qr(c,token)
        self.assertTrue(png.startswith(bytes.fromhex('89504e470d0a1a0a')))
        self.assertIsNone(c.execute('SELECT used_by FROM vip_invites').fetchone()[0])
        with c:c.execute('UPDATE vip_invites SET expires=0')
        with self.assertRaises(ValueError):vip_access.invite_qr(c,token)
        with self.assertRaises(ValueError):vip_access.invite_qr(c,'invalid')
        c.close()

    def test_invite_http_auth_and_duplicate_send(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),run.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def call(path,data,headers):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('POST',path,json.dumps(data),{'Content-Type':'application/json',**headers})
            response=conn.getresponse();result=response.status,json.loads(response.read());conn.close();return result
        c=bot.connect(self.db)
        with c:bot.put(c,'owner','U'+'a'*32)
        c.close()
        with patch.object(run,'ADMIN_TOKEN','test'),patch.object(run,'APP_BASE_URL','https://example.com'),patch.dict(os.environ,{'LINE_CHANNEL_ACCESS_TOKEN':'work-token'}):
            hdr={'x-admin-token':'test','X-Tsukiya-Action':'vip-access'}
            self.assertEqual(call('/api/vip-access/invite-send',{}, {})[0],401)
            self.assertEqual(call('/api/vip-access/invite-send',{}, {'x-admin-token':'test'})[0],403)
            status,invite=call('/api/vip-access/invite',{'name':'招待顧客'},hdr)
            self.assertEqual(status,200,invite)
            data={'token':invite['command'].split()[1]}
            first=call('/api/vip-access/invite-send',data,hdr)
            second=call('/api/vip-access/invite-send',data,hdr)
            self.assertEqual(first[0],200,first)
            self.assertEqual(first,second)
