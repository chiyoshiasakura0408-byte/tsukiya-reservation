"""Persistent reservation-call history, independent of any browser session."""
import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, parse_qs
from xml.etree.ElementTree import Element, SubElement, tostring

SID = re.compile(r'CA[0-9a-fA-F]{32}')
TERMINAL = {'completed','busy','no-answer','failed','canceled'}
STATES = {'queued','initiated','ringing','in-progress',*TERMINAL}
SYNC_LOCK = threading.Lock()


def config(app):
    return {'account':app.TWILIO_ACCOUNT_SID,'token':app.TWILIO_AUTH_TOKEN,
            'number':os.getenv('TWILIO_VOICE_NUMBER','').strip(),
            'forward':os.getenv('TWILIO_VOICE_FORWARD_TO','').strip(),
            'base':app.APP_BASE_URL.rstrip('/')}


def ready(cfg):
    return bool(re.fullmatch(r'AC[0-9a-fA-F]{32}',cfg['account']) and cfg['token']
        and re.fullmatch(r'\+81\d{9,10}',cfg['number'])
        and re.fullmatch(r'\+81[789]0\d{8}',cfg['forward'])
        and cfg['number']!=cfg['forward'] and cfg['base'].startswith('https://'))


def normalize(value):
    value=unicodedata.normalize('NFKC',str(value or '')).strip()
    value=re.sub(r'[\s()‐‑−ー－-]','',value)
    if value.startswith('+81'):value='0'+value[3:]
    return value if re.fullmatch(r'0\d{9,10}',value) else (value if re.fullmatch(r'\+[1-9]\d{7,14}',value) else '')


def connect(app):
    c=app.con()
    c.executescript('''
      CREATE TABLE IF NOT EXISTS phone_calls(
        id INTEGER PRIMARY KEY, sid TEXT UNIQUE NOT NULL, from_number TEXT NOT NULL,
        to_number TEXT NOT NULL, received_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        inbound_status TEXT NOT NULL DEFAULT 'ringing', forward_status TEXT NOT NULL DEFAULT '',
        child_sid TEXT, child_seq INTEGER NOT NULL DEFAULT -1, duration INTEGER,
        customer_id INTEGER, note TEXT NOT NULL DEFAULT '', handled INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS phone_call_reservations(
        call_id INTEGER NOT NULL, reservation_id INTEGER NOT NULL UNIQUE,
        PRIMARY KEY(call_id,reservation_id));
      CREATE TABLE IF NOT EXISTS phone_call_sync(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS phone_calls_received ON phone_calls(received_at,id);
    ''')
    return c


def utcnow():return datetime.now(timezone.utc).isoformat()


def signature_valid(cfg,path,params,signature):
    if not cfg['token'] or not cfg['base'].startswith('https://'):return False
    payload=cfg['base']+path+''.join(k+params[k] for k in sorted(params))
    expected=base64.b64encode(hmac.new(cfg['token'].encode(),payload.encode(),hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected,signature or '')


def parse_event(app,path,raw,signature):
    cfg=config(app)
    if not ready(cfg):raise RuntimeError('電話連携の設定が未完了です')
    parsed=parse_qs(raw.decode('utf-8'),keep_blank_values=True,max_num_fields=100)
    if any(len(v)!=1 for v in parsed.values()):raise ValueError('duplicate fields')
    params={k:v[0] for k,v in parsed.items()}
    if not signature_valid(cfg,path,params,signature) or params.get('AccountSid')!=cfg['account']:
        raise PermissionError('invalid signature')
    if not SID.fullmatch(params.get('CallSid','')):raise ValueError('invalid call')
    return cfg,params


def xml_response(cfg,forward=False):
    root=Element('Response')
    if forward:
        dial=SubElement(root,'Dial',{'callerId':cfg['number'],'answerOnBridge':'true','timeout':'30',
            'action':cfg['base']+'/webhooks/voice/dial','method':'POST'})
        SubElement(dial,'Number',{'statusCallback':cfg['base']+'/webhooks/voice/leg',
            'statusCallbackMethod':'POST','statusCallbackEvent':'initiated ringing answered completed'}).text=cfg['forward']
    else:SubElement(root,'Hangup')
    return tostring(root,encoding='utf-8',xml_declaration=True)


def inbound(c,sid,number,target,when,status):
    if status not in STATES:status='ringing'
    c.execute('INSERT OR IGNORE INTO phone_calls(sid,from_number,to_number,received_at,updated_at,inbound_status) VALUES(?,?,?,?,?,?)',
              (sid,number[:64],target[:32],when,utcnow(),status))
    row=c.execute('SELECT * FROM phone_calls WHERE sid=?',(sid,)).fetchone()
    if row['inbound_status'] not in TERMINAL:
        c.execute('UPDATE phone_calls SET inbound_status=?,updated_at=? WHERE sid=?',(status,utcnow(),sid))


def update_leg(c,parent,child,status,duration=None,seq=None):
    if status not in STATES or not SID.fullmatch(child or ''):raise ValueError('invalid status')
    row=c.execute('SELECT * FROM phone_calls WHERE sid=?',(parent,)).fetchone()
    if not row:raise LookupError('parent call not received yet')
    if row['child_sid'] and row['child_sid']!=child:raise ValueError('unexpected child call')
    if row['forward_status'] in TERMINAL and status not in TERMINAL:return
    if seq is not None and seq<row['child_seq']:return
    c.execute('UPDATE phone_calls SET child_sid=?,forward_status=?,duration=COALESCE(?,duration),child_seq=?,updated_at=? WHERE id=?',
        (child,status,duration,max(row['child_seq'],seq if seq is not None else -1),utcnow(),row['id']))


def integer(value):
    if value in (None,''):return None
    n=int(value)
    if not 0<=n<=604800:raise ValueError('invalid duration')
    return n


def receive(app,path,raw,signature):
    cfg,p=parse_event(app,path,raw,signature)
    if path=='/webhooks/voice/incoming' and (p.get('To')!=cfg['number'] or p.get('Direction')!='inbound'):
        raise PermissionError('unexpected destination')
    c=connect(app)
    try:
        with c:
            if path=='/webhooks/voice/incoming':
                inbound(c,p['CallSid'],p.get('From','anonymous'),p['To'],utcnow(),p.get('CallStatus','ringing'))
                return xml_response(cfg,True)
            if path=='/webhooks/voice/status':
                if p.get('To')!=cfg['number'] or p.get('Direction')!='inbound':raise PermissionError('unexpected destination')
                # Initial webhook can be unavailable: reconciliation recovers the actual timestamp.
                if not c.execute('SELECT 1 FROM phone_calls WHERE sid=?',(p['CallSid'],)).fetchone():
                    raise LookupError('initial event missing')
                inbound(c,p['CallSid'],p.get('From',''),p['To'],utcnow(),p.get('CallStatus',''))
            elif path=='/webhooks/voice/leg':
                if p.get('To')!=cfg['forward']:raise PermissionError('unexpected forward destination')
                update_leg(c,p.get('ParentCallSid',''),p['CallSid'],p.get('CallStatus',''),integer(p.get('CallDuration')),integer(p.get('SequenceNumber')))
            elif path=='/webhooks/voice/dial':
                if not p.get('DialCallSid') and p.get('DialCallStatus') in ('busy','no-answer','failed','canceled'):
                    if not c.execute("UPDATE phone_calls SET forward_status=?,updated_at=? WHERE sid=? AND forward_status NOT IN ('completed','busy','no-answer','failed','canceled')",(p['DialCallStatus'],utcnow(),p['CallSid'])).rowcount:
                        if not c.execute('SELECT 1 FROM phone_calls WHERE sid=?',(p['CallSid'],)).fetchone():raise LookupError('parent missing')
                else:
                    update_leg(c,p['CallSid'],p.get('DialCallSid',''),p.get('DialCallStatus',''),integer(p.get('DialCallDuration')))
            else:raise ValueError('invalid path')
        return xml_response(cfg)
    finally:c.close()


def customer_candidates(c,number):
    key=normalize(number)
    if not key:return []
    return [dict(r) for r in c.execute('SELECT id,name,phone,allergies,disliked_foods,preferred_seat,note FROM customers ORDER BY id') if normalize(r['phone'])==key]


def list_calls(app,before=None,call_id=None):
    c=connect(app)
    try:
        # Reuse existing CRM import without changing phone-match identity automatically.
        app.sync_customers(c);c.commit()
        if call_id is not None:rows=c.execute('SELECT * FROM phone_calls WHERE id=?',(call_id,)).fetchall()
        else:rows=c.execute('SELECT * FROM phone_calls WHERE id<? ORDER BY id DESC LIMIT 51',(before or 9223372036854775807,)).fetchall()
        more=len(rows)>50;rows=rows[:50];items=[]
        for row in rows:
            item=dict(row);item['candidates']=customer_candidates(c,row['from_number'])
            item['reservations']=[dict(r) for r in c.execute('SELECT r.id,r.guest_name,r.visit_at,r.status FROM phone_call_reservations l JOIN reservations r ON r.id=l.reservation_id WHERE l.call_id=?',(row['id'],))]
            items.append(item)
        cfg=config(app)
        sync=dict(c.execute('SELECT key,value FROM phone_call_sync'))
        return {'items':items,'next_before':rows[-1]['id'] if more else None,'configured':ready(cfg),
            'number':cfg['number'],'last_sync':sync.get('last_sync'),'sync_error':sync.get('error','')}
    finally:c.close()


def update_call(app,call_id,data):
    if not isinstance(data,dict) or set(data)-{'note','handled','customer_id'}:raise ValueError('入力項目を確認してください')
    c=connect(app)
    try:
        with c:
            if not c.execute('SELECT 1 FROM phone_calls WHERE id=?',(call_id,)).fetchone():raise ValueError('着信が見つかりません')
            if 'note' in data:
                if not isinstance(data['note'],str) or len(data['note'])>2000:raise ValueError('メモは2000文字以内です')
                c.execute('UPDATE phone_calls SET note=? WHERE id=?',(data['note'],call_id))
            if 'handled' in data:
                if type(data['handled']) is not bool:raise ValueError('対応状況が不正です')
                c.execute('UPDATE phone_calls SET handled=? WHERE id=?',(int(data['handled']),call_id))
            if 'customer_id' in data:
                cid=data['customer_id']
                if cid is not None and (type(cid) is not int or not c.execute('SELECT 1 FROM customers WHERE id=?',(cid,)).fetchone()):raise ValueError('顧客を確認してください')
                c.execute('UPDATE phone_calls SET customer_id=? WHERE id=?',(cid,call_id))
        return {'ok':True}
    finally:c.close()


def validate_link(c,call_id):
    if call_id is None:return
    if type(call_id) is not int or not c.execute('SELECT 1 FROM phone_calls WHERE id=?',(call_id,)).fetchone():
        raise ValueError('着信履歴を選び直してください')


def attach(c,call_id,reservation_id,customer_id):
    if call_id is None:return
    c.execute('INSERT INTO phone_call_reservations VALUES(?,?)',(call_id,reservation_id))
    c.execute('UPDATE phone_calls SET customer_id=COALESCE(?,customer_id),handled=1 WHERE id=?',(customer_id,call_id))


def api_get(cfg,path):
    prefix='/2010-04-01/Accounts/'+cfg['account']+'/'
    if not path.startswith(prefix):raise ValueError('unexpected API path')
    auth=base64.b64encode((cfg['account']+':'+cfg['token']).encode()).decode()
    req=urllib.request.Request('https://api.twilio.com'+path,headers={'Authorization':'Basic '+auth})
    with urllib.request.urlopen(req,timeout=15) as r:return json.load(r)


def reconcile(app):
    cfg=config(app)
    if not ready(cfg) or not SYNC_LOCK.acquire(blocking=False):return
    c=None
    try:
        c=connect(app)
        previous=c.execute("SELECT value FROM phone_call_sync WHERE key='last_sync'").fetchone()
        since=datetime.fromisoformat(previous[0])-timedelta(days=1) if previous else datetime.now(timezone.utc)-timedelta(days=7)
        base='/2010-04-01/Accounts/'+cfg['account']+'/Calls.json'
        path=base+'?'+urlencode({'To':cfg['number'],'StartTime>':since.date().isoformat(),'PageSize':100})
        for page in range(20):
            result=api_get(cfg,path)
            with c:
                for row in result.get('calls',[]):
                    if row.get('to')!=cfg['number'] or row.get('direction')!='inbound' or not SID.fullmatch(row.get('sid','')):continue
                    when=parsedate_to_datetime(row['date_created']).astimezone(timezone.utc).isoformat()
                    inbound(c,row['sid'],row.get('from','anonymous'),row['to'],when,row.get('status','ringing'))
                    c.execute('UPDATE phone_calls SET received_at=? WHERE sid=?',(when,row['sid']))
            path=result.get('next_page_uri')
            if not path:break
        else:raise RuntimeError('履歴の取得上限に達しました。設定確認が必要です。')
        # Recover missing forwarding callbacks (parent completed does not mean owner answered).
        pending=c.execute("SELECT sid FROM phone_calls WHERE forward_status NOT IN ('completed','busy','no-answer','failed','canceled') AND received_at>? ORDER BY id DESC LIMIT 30",((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),)).fetchall()
        for item in pending:
            result=api_get(cfg,base+'?'+urlencode({'ParentCallSid':item['sid'],'PageSize':20}))
            with c:
                for child in result.get('calls',[]):
                    if child.get('parent_call_sid')==item['sid'] and child.get('to')==cfg['forward']:
                        update_leg(c,item['sid'],child['sid'],child['status'],integer(child.get('duration')))
        with c:
            c.execute("INSERT OR REPLACE INTO phone_call_sync VALUES('last_sync',?)",(utcnow(),))
            c.execute("INSERT OR REPLACE INTO phone_call_sync VALUES('error','')")
    except Exception:
        if c:
            with c:c.execute("INSERT OR REPLACE INTO phone_call_sync VALUES('error','Twilio履歴の照合に失敗しました。接続設定を確認してください。')")
    finally:
        if c:c.close()
        SYNC_LOCK.release()


def loop(app):
    while True:
        reconcile(app)
        time.sleep(60)
