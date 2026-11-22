"""LIFF-authenticated VIP requests. No seat allocation, invoice or payment writes."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
import concierge
import concierge_menu
import vip_access

TIMES = [f'{hour:02d}:{minute:02d}' for hour in range(17, 22) for minute in (0, 30) if hour < 21 or minute == 0]


def config(c):
    return {'liff_id': concierge.setting(c, 'vip_liff_id', os.getenv('VIP_LIFF_ID', '')),
            'channel_id': concierge.setting(c, 'vip_liff_channel_id', os.getenv('VIP_LINE_LOGIN_CHANNEL_ID', ''))}


def configure(c, data):
    lid, cid = data.get('liff_id', ''), data.get('channel_id', '')
    if not isinstance(lid, str) or not re.fullmatch(r'\d{6,20}-[A-Za-z0-9]{5,30}', lid):
        raise ValueError('LIFF IDをご確認ください。')
    if not isinstance(cid, str) or not re.fullmatch(r'\d{6,20}', cid):
        raise ValueError('LINEログインチャネルIDをご確認ください。')
    concierge.put(c, 'vip_liff_id', lid)
    concierge.put(c, 'vip_liff_channel_id', cid)
    return {'configured': True}


def verify_identity(c, token):
    cfg = config(c)
    if not cfg['liff_id'] or not cfg['channel_id']:
        raise ValueError('予約画面は現在準備中です。VIP担当へお問い合わせください。')
    if not isinstance(token, str) or not 20 <= len(token) <= 8192:
        raise PermissionError('LINEで本人確認をしてください。')
    req = urllib.request.Request('https://api.line.me/oauth2/v2.1/verify',
        data=urllib.parse.urlencode({'id_token': token, 'client_id': cfg['channel_id']}).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            identity = json.loads(response.read(32768))
    except urllib.error.HTTPError as exc:
        if exc.code in (400,401,403):raise PermissionError('LINEの本人確認が切れました。画面を開き直してください。') from None
        raise ValueError('LINEの本人確認に接続できません。少し後に再度お試しください。') from None
    except (OSError, ValueError):
        raise ValueError('LINEの本人確認に接続できません。少し後に再度お試しください。') from None
    if not isinstance(identity,dict):raise PermissionError('LINEの本人確認に失敗しました。')
    user = identity.get('sub', '')
    if identity.get('aud') != cfg['channel_id'] or identity.get('iss') != 'https://access.line.me' or not isinstance(identity.get('exp'),(int,float)) or identity['exp'] <= time.time() or not isinstance(user,str) or not re.fullmatch(r'U[0-9a-f]{32}',user):
        raise PermissionError('LINEの本人確認に失敗しました。')
    if not vip_access.approved(c, user):
        raise PermissionError('VIP利用の承認が必要です。VIP担当へお問い合わせください。')
    return user


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS vip_liff_requests (
        user_id TEXT NOT NULL, nonce TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
        payload TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(user_id,nonce))''')


def handle(db, action, data, base_url):
    if not isinstance(data,dict):raise ValueError('入力内容をご確認ください。')
    c = concierge.connect(db)
    try:
        user = verify_identity(c, data.get('id_token'))
        if concierge.setting(c,'enabled') != '1':raise ValueError('現在、予約リクエストを休止しております。')
        if action == 'profile':
            return {'name':concierge.profile(c,user).get('name',''), 'today':datetime.now(concierge.JST).date().isoformat(), 'times':TIMES}
        if action == 'course':
            try:day=date.fromisoformat(data.get('date',''))
            except (TypeError,ValueError):raise ValueError('日付をご確認ください。') from None
            return {'name':concierge_menu.course_name_for_day(day)}
        if action != 'request':raise ValueError('操作をご確認ください。')
        day, clock, party = data.get('date'), data.get('time'), data.get('party')
        try:
            visit = date.fromisoformat(day)
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day) or clock not in TIMES or type(party) is not int or not 2 <= party <= 8:
                raise ValueError()
            if datetime.fromisoformat(day+'T'+clock) <= datetime.now(concierge.JST).replace(tzinfo=None):raise ValueError()
        except (TypeError,ValueError):raise ValueError('未来の日付、17:00〜21:00（30分刻み）、2〜8名でお選びください。') from None
        name, note, nonce = data.get('name',''), data.get('note',''), data.get('nonce','')
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80 or not isinstance(note,str) or len(note) > 1000:
            raise ValueError('お名前・ご要望をご確認ください。')
        if data.get('acknowledged') is not True:raise ValueError('リクエスト内容をご確認ください。')
        if not isinstance(nonce,str) or not re.fullmatch(r'[a-zA-Z0-9-]{20,64}',nonce):raise ValueError('画面を開き直してください。')
        cake, bouquet, plate = data.get('cake',False), data.get('bouquet',False), data.get('plate_name','')
        cake_type = data.get('cake_type','no' if not cake else '')
        if type(cake) is not bool or type(bouquet) is not bool or cake_type not in (('whole','cut') if cake else ('no',)):
            raise ValueError('ケーキ・花束の選択をご確認ください。')
        if cake and (not isinstance(plate,str) or not 1 <= len(plate.strip()) <= 80):
            raise ValueError('プレート名を入力してください（80文字以内）。')
        plate = plate.strip() if cake else ''
        venue = data.get('venue')
        if venue not in ('main','annex'):raise ValueError('店舗を選択してください。')
        duration = data.get('duration_minutes')
        if venue == 'main':
            if clock not in ('18:00','20:30') or duration not in (None,120):raise ValueError('本店は18:00・20:30開始、2時間制です。')
            duration = 120
        if duration is not None and (type(duration) is not int or not 120 <= duration <= 240 or duration % 30):
            raise ValueError('ご利用時間は2〜4時間の間で、30分単位でお選びください。')
        payload = json.dumps({'date':day,'time':clock,'party':party,'name':name.strip(),'note':note.strip(),'cake':cake,'cake_type':cake_type,'bouquet':bouquet,'plate_name':plate,'duration_minutes':duration,'venue':venue},ensure_ascii=False,sort_keys=True)
        with c:
            c.execute('BEGIN IMMEDIATE')
            schema(c)
            # Recheck approval inside the write transaction; a concurrent revocation wins.
            if not vip_access.approved(c,user):raise PermissionError('VIP利用の承認が必要です。')
            existing = c.execute('SELECT * FROM vip_liff_requests WHERE user_id=? AND nonce=?',(user,nonce)).fetchone()
            if existing:
                if existing['payload'] != payload:raise ValueError('送信済みの内容は変更できません。新しいリクエストを作成してください。')
                return {'request_id':existing['request_id'],'status':'requested','confirmed':False}
            duplicate=c.execute("SELECT l.request_id FROM vip_liff_requests l JOIN concierge_requests r ON r.id=l.request_id WHERE l.user_id=? AND l.payload=? AND r.status='未回答'",(user,payload)).fetchone()
            if duplicate:return {'request_id':duplicate[0],'status':'requested','confirmed':False}
            if c.execute('SELECT count(*) FROM vip_liff_requests WHERE user_id=? AND created>?',(user,time.time()-60)).fetchone()[0] >= 5:
                raise ValueError('少し時間をおいてから送信してください。')
            owner=concierge.setting(c,'owner')
            if not owner:raise ValueError('担当者への接続を確認中です。少し後に再度お試しください。')
            request_id=uuid.uuid4().hex[:12]
            body=f'【VIP予約リクエスト】\n{day} {clock}\n{name.strip()}様\n{party}名様\n{concierge_menu.course_name_for_day(visit)}\n\n料理代の事前決済なし。受付開始前の日程を含むリクエストです。席・コース・料金の確認後に確定してください。\n冬季ドリンクオールインは18:00または20:30開始・滞在時間2時間の場合に適用。それ以外は飲料別途当日精算。'
            body+='\n\nケーキ：'+({'whole':'ホールケーキ','cut':'カットケーキ'}.get(cake_type,'希望しない'))+'\n花束：'+('希望する' if bouquet else '希望しない')
            body+='\n店舗：'+('本店｜カウンター（禁煙）' if venue=='main' else '別邸｜個室（喫煙）')
            if duration is not None:body+='\nご利用時間：'+str(duration)+'分'
            if cake:body+='\nプレート名：'+plate
            if note.strip():body+='\n\nご要望：'+note.strip()
            c.execute('INSERT INTO concierge_requests(id,user_id,body,created) VALUES(?,?,?,?)',(request_id,user,body,time.time()))
            c.execute('INSERT INTO vip_liff_requests VALUES(?,?,?,?,?)',(user,nonce,request_id,payload,time.time()))
            notice = concierge.enqueue(c,owner,[concierge.text_message(body+'\n\n受付：'+request_id+'\nこの通知に「確定」とリプライすると予約登録、それ以外の文章はお客様へ送信されます。\n管理画面：'+base_url+'/concierge',False)],channel='owner',kind='request')
            c.execute('INSERT INTO vip_notice_links VALUES (?,?)',(notice,request_id))
        return {'request_id':request_id,'status':'requested','confirmed':False}
    finally:c.close()



def remember_notice(c, row, response_body):
    if row['kind'] != 'request' or row['channel'] != 'owner':
        return
    link = c.execute('SELECT request_id FROM vip_notice_links WHERE outbox_id=?',(row['id'],)).fetchone()
    if link:
        for message in json.loads(response_body or b'{}').get('sentMessages', []):
            mid = str(message.get('id', ''))
            if re.fullmatch(r'\d{1,30}', mid):
                c.execute('INSERT OR IGNORE INTO vip_message_links VALUES(?,?,?)',(mid,link[0],row['user_id']))


def owner_reply(c,event):
    src = event.get('source', {})
    msg = event.get('message', {})
    owner = concierge.setting(c,'owner')
    if not owner or src.get('type') != 'user' or src.get('userId') != owner or msg.get('type') != 'text':
        return False
    quoted = msg.get('quotedMessageId')
    command = (msg.get('text') or '').strip()
    if quoted:
        link = c.execute('SELECT request_id FROM vip_message_links WHERE message_id=? AND destination=?',(quoted,owner)).fetchone()
        if not link:
            if command in ('予約確定','確定'):
                concierge.enqueue(c,owner,[concierge.text_message('引用元をVIP受付に照合できません。VIP予約は「確定 受付番号」と送信してください。',False)],channel='owner')
                return True
            return False
        request_id = link[0]
        customer_message = None if command in ('確定','予約確定') else command
    else:
        match = re.fullmatch(r'(?:予約)?確定\s+([a-f0-9]{12})',command)
        reply = re.fullmatch(r'返信\s+([a-f0-9]{12})\s+([\s\S]+)',command)
        if not match and not reply:
            return False
        request_id = (match or reply)[1]
        customer_message = reply[2] if reply else None
    schema(c)
    request = c.execute('SELECT l.*,r.status FROM vip_liff_requests l JOIN concierge_requests r ON r.id=l.request_id WHERE l.request_id=?',(request_id,)).fetchone()
    try:
        if customer_message is None:
            import run
            result = confirm_booking(run,c,request)
        else:
            if not request or not vip_access.approved(c,request['user_id']):
                raise ValueError('受付番号とVIP利用状況をご確認ください。')
            if not customer_message or len(customer_message.encode('utf-16-le'))//2 > 5000:
                raise ValueError('送信する本文は1〜5000文字で入力してください。')
            delivery = concierge.enqueue(c,request['user_id'],[{'type':'text','text':customer_message}],kind='vip-owner-message')
            c.execute('INSERT INTO vip_reply_deliveries VALUES(?,?,?)',(delivery,request_id,owner))
            result = 'お客様へのLINE送信を受け付けました。'
    except ValueError as exc:
        result = str(exc)
    concierge.enqueue(c,owner,[concierge.text_message('受付 '+request_id+'\n'+result,False)],channel='owner')
    return True


def confirm_booking(app,c,request):
    if not request:
        raise ValueError('VIP予約リクエストが見つかりません。')
    key = 'vip-request:'+request['request_id']
    existing = c.execute('SELECT id,status FROM reservations WHERE public_request_id=?',(key,)).fetchone()
    if existing:
        return f"登録済み予約 #{existing['id']}（{existing['status']}）"
    if request['status'] != '未回答' or not vip_access.approved(c,request['user_id']):
        raise ValueError('回答状況・VIP承認状況を管理画面で確認してください。')
    x = json.loads(request['payload'])
    cid = c.execute('SELECT customer_id FROM vip_access WHERE user_id=?',(request['user_id'],)).fetchone()[0]
    customer = c.execute('SELECT phone,email FROM customers WHERE id=?',(cid,)).fetchone()
    if not customer:
        raise ValueError('連携顧客を確認してください。')
    app.booking_blocks.ensure_allowed(c,customer['phone'],customer['email'],cid,staff=True)
    visit = x['date']+'T'+x['time']
    when = datetime.fromisoformat(visit)
    duration = x.get('duration_minutes') or 120
    end = when + timedelta(minutes=duration)
    course = app.bookable_course(when.date())
    if not course or end.date()!=when.date():
        raise ValueError('コース・利用時間を確認してください。')
    rows = c.execute('SELECT guest_name,customer_id FROM reservations WHERE substr(visit_at,1,10)=? AND status IN (?,?,?,?)',(x['date'],*app.ACTIVE_STATUSES))
    if any(r['customer_id']==cid or app.line_reservations.norm(r['guest_name'])==app.line_reservations.norm(x['name']) for r in rows):
        raise ValueError('同日の既存予約を確認してください。')
    through = x['venue']=='annex' and (x['time'] not in ('18:00','20:30') or duration!=120)
    requested_area = 'COUNTER' if x['venue']=='main' else 'PRIVATE'
    if through:
        requested_area = next((room for room in app.ROOMS if app.vip_slots.choose(app,c,x['date'],room,x['party']) is not None and app.availability_check(c,room,visit,x['party'],None,duration,special_slot=True)[0]),None)
        if not requested_area:
            raise ValueError('通しで利用できる個室に空きがありません。')
    area,rnd,_,_ = app.special_slot_availability(c,requested_area,visit,end.strftime('%H:%M'),x['party'])
    items = [v for v in ({'whole':'ホールケーキ','cut':'カットケーキ'}.get(x.get('cake_type')), '花束' if x.get('bouquet') else None) if v]
    ts = app.now_iso()
    rid = c.execute("""INSERT INTO reservations(source,status,public_request_id,customer_id,guest_name,phone,email,visit_at,
        party_size,course_name,amount,seating_area,counter_round,duration_minutes,vip_booking,vip_through,guest_note,
        celebration_items,plate_message,created_at,updated_at) VALUES('DIRECT','CONFIRMED',?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)""",
        (key,cid,x['name'],customer['phone'],customer['email'],visit,x['party'],course[0],course[1]*x['party'],area,rnd,duration,
         int(through),x.get('note',''),','.join(items),x.get('plate_name',''),ts,ts)).lastrowid
    detail = f"{x['name']}様　{x['date']} {x['time']}　{x['party']}名様"
    delivery = concierge.enqueue(c,request['user_id'],[concierge.text_message(detail+'\nご予約を承りました。',False)],kind='answer')
    c.execute("UPDATE concierge_requests SET status='回答送信待ち',delivery_id=? WHERE id=?",(delivery,request['request_id']))
    return f'予約 #{rid} を確定しました。\n'+detail
