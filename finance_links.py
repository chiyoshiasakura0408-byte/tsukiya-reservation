"""Authenticated accounting receipt snapshots linked to existing bookings/customers."""
import hashlib
import hmac
import json
import re
import time
import unicodedata
from datetime import date
import line_receipts

ORIGIN = 'https://tsukiya-daily-finance.chiyoshi-a-0408.chatgpt.site'

def schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS finance_receipt_snapshots(receipt_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,payload TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS finance_receipt_links(receipt_id TEXT NOT NULL,entry_id TEXT NOT NULL,reservation_id INTEGER,customer_id INTEGER,file_id TEXT NOT NULL,sales INTEGER NOT NULL,status TEXT NOT NULL,PRIMARY KEY(receipt_id,entry_id))')
    c.execute('CREATE INDEX IF NOT EXISTS finance_links_reservation ON finance_receipt_links(reservation_id,status)')

def normalized_name(value):
    text=unicodedata.normalize('NFKC',value).casefold()
    text=re.sub(r'\s+', '', text)
    text=re.sub(r'(様|さま|サマ)$','',text)
    return ''.join(chr(ord(ch)+0x60) if '\u3041'<=ch<='\u3096' else ch for ch in text)

def branch(area):
    return 'main' if area=='COUNTER' else 'annex' if area in ('PRIVATE1','PRIVATE2','PRIVATE3') else 'unassigned'

def valid_uuid(value):
    return isinstance(value,str) and bool(re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',value))

def validate(x):
    if not isinstance(x,dict) or abs(time.time()-int(x.get('timestamp',0)))>300:raise ValueError()
    r=x['receipt']
    if not valid_uuid(r['id']) or not valid_uuid(r['fileId']) or type(r['revision']) is not int or r['revision']<1:raise ValueError()
    if r['status'] not in ('draft','confirmed','void') or not isinstance(r['entries'],list) or not 1<=len(r['entries'])<=30:raise ValueError()
    ids=set()
    for e in r['entries']:
        if not valid_uuid(e['id']) or e['id'] in ids:raise ValueError()
        ids.add(e['id'])
        if date.fromisoformat(e['date']).isoformat()!=e['date'] or e['branch'] not in ('main','annex','unassigned'):raise ValueError()
        if type(e['sales']) is not int or not 0<=e['sales']<=1000000000 or type(e['guests']) is not int or not 0<=e['guests']<=100000:raise ValueError()
        if not isinstance(e['customer'],str) or len(e['customer'])>100:raise ValueError()
        if 'reservationId' in e and (type(e['reservationId']) is not int or e['reservationId']<0):raise ValueError()
    return r

def sync(db,raw,signature,connect,sync_customers):
    secret=line_receipts.bridge_secret(db)
    expected=hmac.new(secret.encode(),b'tsukiya-finance-link-v1\n'+raw,hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(expected,signature):return 403,{'error':'unauthorized'}
    try:r=validate(json.loads(raw))
    except (ValueError,TypeError,KeyError,OverflowError):return 400,{'error':'invalid receipt'}
    c=connect()
    try:
        sync_customers(c)
        schema(c)
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT revision,payload FROM finance_receipt_snapshots WHERE receipt_id=?',(r['id'],)).fetchone()
        if old and old['revision']>r['revision']:
            c.rollback()
            return 409,{'error':'newer receipt already saved'}
        if old and old['revision']==r['revision'] and json.loads(old['payload'])!=r:
            c.rollback()
            return 409,{'error':'receipt revision conflict'}
        c.execute('DELETE FROM finance_receipt_links WHERE receipt_id=?',(r['id'],))
        results=[]
        for e in r['entries']:
            rows=[dict(v) for v in c.execute("SELECT id,guest_name,visit_at,party_size,seating_area,customer_id,status FROM reservations WHERE visit_at>=? AND visit_at<? AND status='CONFIRMED' AND (course_name IS NULL OR course_name NOT LIKE '決済テスト%') ORDER BY visit_at,id",(e['date'],e['date']+'T99'))]
            rows=[v for v in rows if e['branch']=='unassigned' or branch(v['seating_area'])==e['branch']]
            selected=e.get('reservationId')
            matches=[v for v in rows if v['id']==selected] if selected else [v for v in rows if normalized_name(e['customer']) and normalized_name(v['guest_name'])==normalized_name(e['customer']) and v['party_size']==e['guests'] and e['branch']!='unassigned']
            chosen=matches[0] if len(matches)==1 and selected!=0 else None
            state='excluded' if selected==0 else 'linked' if chosen and chosen['customer_id'] else 'review'
            if chosen:
                # Another photo must not silently count the same booking again.
                other=c.execute("SELECT 1 FROM finance_receipt_links WHERE reservation_id=? AND status='confirmed' LIMIT 1",(chosen['id'],)).fetchone()
                if other:chosen=None;state='review'
            if r['status']!='confirmed':state='inactive';chosen=None
            c.execute('INSERT INTO finance_receipt_links VALUES(?,?,?,?,?,?,?)',(r['id'],e['id'],chosen['id'] if chosen else None,chosen['customer_id'] if chosen else None,r['fileId'],e['sales'],r['status']))
            results.append({'entryId':e['id'],'state':state,'reservationId':chosen['id'] if chosen else None,'customerId':chosen['customer_id'] if chosen else None,'name':chosen['guest_name'] if chosen else '', 'visitAt':chosen['visit_at'] if chosen else '', 'candidates':[{'id':v['id'],'name':v['guest_name'],'visitAt':v['visit_at'],'guests':v['party_size'],'branch':branch(v['seating_area'])} for v in rows[:100]]})
        c.execute('INSERT INTO finance_receipt_snapshots VALUES(?,?,?) ON CONFLICT(receipt_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload',(r['id'],r['revision'],json.dumps(r,ensure_ascii=False,sort_keys=True)))
        c.commit()
        return 200,{'revision':r['revision'],'entries':results}
    except Exception:
        c.rollback()
        raise
    finally:c.close()

def for_reservation(c,reservation_id):
    schema(c)
    return [{'sales':row['sales'],'imageUrl':ORIGIN+'/api/uploads/'+row['file_id']} for row in c.execute("SELECT sales,file_id FROM finance_receipt_links WHERE reservation_id=? AND status='confirmed'",(reservation_id,))]
