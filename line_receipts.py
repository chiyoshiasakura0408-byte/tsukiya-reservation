"""Durable signed receipt relay and review notifications to the linked group."""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.error

GROUP = 'Cc95ce7808ee54a2a3d19a4a7b7d78818'
RELAY = 'https://tsukiya-line-receiver.chiyoshi-a-0408.chatgpt.site/receipt-relay'
LOCK = threading.Lock()
MAX_IMAGE = 12 * 1024 * 1024

def schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS receipt_bridge_config(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS receipt_relay(id TEXT PRIMARY KEY, body BLOB NOT NULL, signature TEXT NOT NULL, created REAL NOT NULL, next_try REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT \'pending\', error TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS receipt_images(message_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, created REAL NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0)')
    c.execute('CREATE INDEX IF NOT EXISTS receipt_relay_pending ON receipt_relay(state,next_try)')
    c.execute("CREATE TABLE IF NOT EXISTS receipt_review_notices(message_id TEXT PRIMARY KEY, retry_key TEXT NOT NULL, created REAL NOT NULL, next_try REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending')")

    if 'notice_text' not in {row[1] for row in c.execute('PRAGMA table_info(receipt_review_notices)')}:
        c.execute("ALTER TABLE receipt_review_notices ADD COLUMN notice_text TEXT NOT NULL DEFAULT ''")

def enqueue(c, raw, signature, payload):
    schema(c)
    relevant = []
    now = time.time()
    for e in payload['events']:
        if e.get('source', {}).get('groupId') != GROUP or e.get('source', {}).get('type') != 'group':
            continue
        m = e.get('message', {})
        if e.get('type') == 'message' and m.get('type') == 'image' and m.get('contentProvider', {}).get('type', 'line') == 'line' and re.fullmatch(r'\d{1,30}', str(m.get('id', ''))):
            c.execute('INSERT OR IGNORE INTO receipt_images VALUES(?,?,?,0)', (m['id'], GROUP, now))
            relevant.append(e['webhookEventId'])
        if e.get('type') == 'unsend':
            mid = e.get('unsend', {}).get('messageId', '')
            if re.fullmatch(r'\d{1,30}', str(mid)):
                c.execute('INSERT INTO receipt_images VALUES(?,?,?,1) ON CONFLICT(message_id) DO UPDATE SET cancelled=1', (mid, GROUP, now))
                relevant.append(e['webhookEventId'])
    if relevant:
        c.execute('INSERT OR IGNORE INTO receipt_relay(id,body,signature,created,next_try) VALUES(?,?,?,?,?)', (hashlib.sha256(json.dumps(sorted(relevant)).encode()).hexdigest(),raw,signature,now,now))

def drain(db):
    with LOCK:
        c = sqlite3.connect(db, timeout=30)
        try:
            schema(c)
            now = time.time()
            # Bounded retention; the finance Site retains its authenticated receipt record.
            c.execute('DELETE FROM receipt_relay WHERE created<?', (now-7*86400,))
            c.execute('DELETE FROM receipt_images WHERE created<?', (now-7*86400,))
            row = c.execute("SELECT id,body,signature,attempts FROM receipt_relay WHERE state='pending' AND next_try<=? ORDER BY created LIMIT 1", (now,)).fetchone()
            c.commit()
            if not row: return
            try:
                secret = bridge_secret(db)
                if not secret or not bridge_secret(db,'receiver_token'): raise RuntimeError('bridge not configured')
                stamp = str(int(time.time()))
                signature = base64.b64encode(hmac.new(secret.encode(),b'tsukiya-receipt-relay-v1\n'+stamp.encode()+b'\n'+row[1],hashlib.sha256).digest()).decode()
                req = urllib.request.Request(RELAY, data=row[1], headers={'OAI-Sites-Authorization':'Bearer '+bridge_secret(db,'receiver_token'),'Content-Type':'application/json','X-Tsukiya-Relay-Signature':signature,'X-Tsukiya-Relay-Time':stamp})
                with urllib.request.urlopen(req, timeout=15) as r:
                    if r.status != 200: raise RuntimeError()
                c.execute("UPDATE receipt_relay SET state='forwarded',body=X'',signature='',error=NULL WHERE id=?", (row[0],))
            except Exception as e:
                error = 'finance HTTP '+str(e.code) if isinstance(e, urllib.error.HTTPError) else 'finance unavailable'
                c.execute('UPDATE receipt_relay SET attempts=attempts+1,next_try=?,error=? WHERE id=?', (now+min(300,10*2**min(row[3],5)),error,row[0]))
            c.commit()
        finally: c.close()

def loop(db):
    while True:
        try:
            drain(db)
            drain_reviews(db)
        except Exception: pass
        time.sleep(5)

def content(db, raw, signature):
    secret = bridge_secret(db)
    expected = hmac.new(secret.encode(), b'tsukiya-receipt-content-v1\n'+raw, hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(expected, signature): return 403, b'', ''
    try:
        x=json.loads(raw)
        mid=x['message_id']
        if not isinstance(mid,str) or not re.fullmatch(r'\d{1,30}',mid) or x['group_id']!=GROUP or abs(time.time()-int(x['timestamp']))>300:
            return 403,b'',''
    except (KeyError,ValueError,TypeError): return 400,b'',''
    c=sqlite3.connect(db,timeout=30)
    try:
        schema(c)
        allowed=c.execute('SELECT 1 FROM receipt_images WHERE message_id=? AND group_id=? AND cancelled=0 AND created>?',(mid,GROUP,time.time()-7*86400)).fetchone()
    finally:c.close()
    if not allowed:return 404,b'',''
    if x.get('action') == 'review_notice':
        notice_text=x.get('notice_text','')
        if not isinstance(notice_text,str) or len(notice_text)>4000:return 400,b'',''
        import uuid
        c=sqlite3.connect(db,timeout=30)
        try:
            with c:
                c.execute('INSERT OR IGNORE INTO receipt_review_notices(message_id,retry_key,created,next_try,notice_text) VALUES(?,?,?,?,?)',(mid,str(uuid.uuid4()),time.time(),time.time(),notice_text))
        finally:c.close()
        return 200,b'{"queued":true}','application/json'
    token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN','').strip()
    if not token:return 503,b'',''
    try:
        req=urllib.request.Request('https://api-data.line.me/v2/bot/message/'+mid+'/content',headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(req,timeout=12) as r:
            data=r.read(MAX_IMAGE+1)
        if len(data)>MAX_IMAGE:return 413,b'',''
        mime='image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/webp' if data[:4]==b'RIFF' and data[8:12]==b'WEBP' else ''
        return (200,data,mime) if mime else (415,b'','')
    except urllib.error.HTTPError as e:return (404 if e.code in (404,410) else 502),b'',''
    except Exception:return 502,b'',''

def status(db):
    c=sqlite3.connect(db,timeout=30)
    try:
        schema(c)
        counts=dict(c.execute('SELECT state,COUNT(*) FROM receipt_relay GROUP BY state'))
        error=c.execute('SELECT error FROM receipt_relay WHERE error IS NOT NULL ORDER BY created DESC LIMIT 1').fetchone()
        return {'receipt_import_enabled':bool(bridge_secret(db) and bridge_secret(db,'receiver_token')),'receipt_relay':counts,'receipt_relay_error':error[0] if error else None}
    finally:c.close()

def bridge_secret(db, key="secret"):
    c=sqlite3.connect(db,timeout=30)
    try:
        schema(c)
        row=c.execute("SELECT value FROM receipt_bridge_config WHERE key=?",(key,)).fetchone()
        return row[0] if row else ''
    finally:c.close()

def configure(db,secret,receiver_token):
    if not isinstance(secret,str) or not re.fullmatch(r'[0-9a-f]{64}',secret):raise ValueError('invalid key')
    if not isinstance(receiver_token,str) or not 16<=len(receiver_token)<=8192 or any(ord(ch)<33 or ord(ch)>126 for ch in receiver_token):raise ValueError('invalid receiver credential')
    c=sqlite3.connect(db,timeout=30)
    try:
        schema(c)
        with c:
            c.execute("INSERT INTO receipt_bridge_config VALUES('secret',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(secret,))
            c.execute("INSERT INTO receipt_bridge_config VALUES('receiver_token',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(receiver_token,))
    finally:c.close()

def review_recipient(db):
    # Only the authenticated owner-pairing flow may establish this destination.
    import concierge
    c=concierge.connect(db)
    try:
        owner=concierge.setting(c,'owner')
        return owner if re.fullmatch(r'U[0-9a-f]{32}',owner or '') else ''
    finally:c.close()

def drain_reviews(db):
    token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN','').strip()
    if not token:return
    recipient=review_recipient(db)
    if not recipient:return  # Keep pending; never fall back to the group.
    c=sqlite3.connect(db,timeout=30)
    try:
        schema(c)
        now=time.time()
        with c:
            # LINE retry keys expire after 24h; stop before re-delivery becomes possible.
            c.execute("UPDATE receipt_review_notices SET state='expired' WHERE state='pending' AND created<?",(now-23*3600,))
        row=c.execute("SELECT n.message_id,n.retry_key,n.notice_text FROM receipt_review_notices n JOIN receipt_images i ON i.message_id=n.message_id WHERE n.state='pending' AND n.next_try<=? AND i.cancelled=0 ORDER BY n.created LIMIT 1",(now,)).fetchone()
        if not row:return
        text=row[2] or '【つきや経理・伝票の確認依頼】\nLINEで受信した写真に確認が必要です。経理画面で内容をご確認ください。\nhttps://tsukiya-daily-finance.chiyoshi-a-0408.chatgpt.site'
        req=urllib.request.Request('https://api.line.me/v2/bot/message/push',data=json.dumps({'to':recipient,'messages':[{'type':'text','text':text}]}).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','X-Line-Retry-Key':row[1]})
        sent=False
        try:
            with urllib.request.urlopen(req,timeout=10) as response:sent=response.status==200
        except urllib.error.HTTPError as e:sent=e.code==409 and bool(e.headers.get('x-line-accepted-request-id'))
        except Exception:pass
        with c:c.execute("UPDATE receipt_review_notices SET state=?,next_try=? WHERE message_id=?",('sent' if sent else 'pending',now+60,row[0]))
    finally:c.close()

