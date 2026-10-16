"""Private, bounded English conversations with durable idempotency and owner escalation."""
import hashlib
import json
import re
import time
import concierge
import concierge_ai
import english_concierge


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS english_chat_messages (
        token_hash TEXT NOT NULL, message_id TEXT NOT NULL, peer_hash TEXT NOT NULL,
        name TEXT NOT NULL, body TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'processing',
        request_id TEXT, PRIMARY KEY(token_hash,message_id))''')


def messages(c, key):
    setup(c)
    english_concierge.setup(c)
    return [dict(row) for row in c.execute('''SELECT m.message_id,m.body,m.answer,m.state,m.request_id,e.answer AS owner_answer
        FROM english_chat_messages m LEFT JOIN english_enquiries e ON e.request_id=m.request_id
        WHERE m.token_hash=? AND m.expires>? ORDER BY m.created LIMIT 30''', (key,time.time()))]


def handle(db, data, peer, base):
    token = data.get('token', '')
    if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token):
        raise ValueError('Please start a new conversation.')
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    c = concierge.connect(db)
    try:
        with c:
            setup(c)
            c.execute('BEGIN IMMEDIATE')
            if data.get('action') == 'chat-status':
                return 200, {'messages': messages(c,key)}
            body, name, mid = data.get('body'), data.get('name'), data.get('message_id')
            if not isinstance(body,str) or not 1<=len(body.strip())<=1500 or not isinstance(name,str) or not 1<=len(name.strip())<=100 or not isinstance(mid,str) or not re.fullmatch(r'[a-f0-9-]{36}',mid) or data.get('consent') is not True:
                raise ValueError('Please check your name, message and consent.')
            if data.get('website'):
                raise ValueError('Invalid request.')
            old = c.execute('SELECT * FROM english_chat_messages WHERE token_hash=? AND message_id=?',(key,mid)).fetchone()
            if old:
                if old['expires'] <= now:
                    return 410, {'error':'This conversation has expired. Please start a new enquiry.'}
                if old['state']=='processing' and old['created']<now-90:
                    return 503, {'error':'We could not complete this reply. Please use the personal enquiry form below.'}
                return 200, {'messages': messages(c,key)}
            peer_hash = hashlib.sha256(peer.encode()).hexdigest()
            count = c.execute('SELECT count(*) FROM english_chat_messages WHERE peer_hash=? AND created>?',(peer_hash,now-3600)).fetchone()[0]
            total = c.execute('SELECT count(*) FROM english_chat_messages WHERE created>?',(now-3600,)).fetchone()[0]
            history = messages(c,key)
            if count>=60 or total>=200 or len(history)>=30:
                return 429, {'error':'Please try again later, or contact our restaurant team using the enquiry form.'}
            if any(m['state']=='processing' for m in history):
                return 409, {'error':'Please wait for your previous reply before sending another message.'}
            c.execute('INSERT INTO english_chat_messages(token_hash,message_id,peer_hash,name,body,created,expires) VALUES(?,?,?,?,?,?,?)', (key,mid,peer_hash,name.strip(),body.strip(),now,now+english_concierge.LIFETIME))
        try:
            result = concierge_ai.reply(body, [{'guest':m['body'],'concierge':m['answer'],'owner':m['owner_answer']} for m in history])
        except Exception:
            result = {'answer':concierge_ai.HANDOFF,'needs_owner':True,'summary_ja':'AIの応答を利用できないため、英語問い合わせを朝倉へ引き継ぎ。','reason_ja':'AI応答エラー・未設定'}
        request_id = None
        if result['needs_owner']:
            # This secret is derived from the guest's private session and message ID,
            # never returned, stored as plaintext, or placed in an analytics event.
            subtoken = hashlib.sha256((token+':'+mid).encode()).hexdigest()
            status, enquiry = english_concierge.handle(db, {'action':'submit','token':subtoken,'name':name,'body':body,'consent':True}, peer, base)
            if status==200:
                request_id = enquiry['id']
            elif status==409:
                result['answer'] = 'A matching enquiry was recently received. Please check your original private enquiry link for our reply. No new booking has been created.'
            else:
                result['answer'] = 'Our personal assistance is temporarily unavailable. Please use the enquiry form below to try again later. No reservation has been confirmed.'
        with c:
            c.execute("UPDATE english_chat_messages SET answer=?,state='completed',request_id=? WHERE token_hash=? AND message_id=?",(result['answer'][:4000],request_id,key,mid))
        return 200, {'messages':messages(c,key)}
    finally:
        c.close()
