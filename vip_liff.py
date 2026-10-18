"""LIFF-authenticated VIP requests. No seat allocation, invoice or payment writes."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime
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
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day) or clock not in TIMES or type(party) is not int or not 1 <= party <= 8:
                raise ValueError()
            if datetime.fromisoformat(day+'T'+clock) <= datetime.now(concierge.JST).replace(tzinfo=None):raise ValueError()
        except (TypeError,ValueError):raise ValueError('未来の日付、17:00〜21:00（30分刻み）、1〜8名でお選びください。') from None
        name, note, nonce = data.get('name',''), data.get('note',''), data.get('nonce','')
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80 or not isinstance(note,str) or len(note) > 1000:
            raise ValueError('お名前・ご要望をご確認ください。')
        if data.get('acknowledged') is not True:raise ValueError('リクエスト内容をご確認ください。')
        if not isinstance(nonce,str) or not re.fullmatch(r'[a-zA-Z0-9-]{20,64}',nonce):raise ValueError('画面を開き直してください。')
        payload = json.dumps({'date':day,'time':clock,'party':party,'name':name.strip(),'note':note.strip()},ensure_ascii=False,sort_keys=True)
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
            body=f'【VIP予約リクエスト】\n{day} {clock}\n{name.strip()}様\n{party}名様\n{concierge_menu.course_name_for_day(visit)}\n\n料理代の事前決済なし。受付開始前の日程を含むリクエストです。席・コース・料金の確認後に確定してください。\nVIP通し予約は冬季ドリンクオールイン対象外。'
            if note.strip():body+='\n\nご要望：'+note.strip()
            c.execute('INSERT INTO concierge_requests(id,user_id,body,created) VALUES(?,?,?,?)',(request_id,user,body,time.time()))
            c.execute('INSERT INTO vip_liff_requests VALUES(?,?,?,?,?)',(user,nonce,request_id,payload,time.time()))
            concierge.enqueue(c,owner,[concierge.text_message(body+'\n\n受付：'+request_id+'\n管理画面：'+base_url+'/concierge',False)],channel='owner',kind='request')
        return {'request_id':request_id,'status':'requested','confirmed':False}
    finally:c.close()

