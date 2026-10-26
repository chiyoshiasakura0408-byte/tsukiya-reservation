"""Durable embedded card checkout. No card numbers/CVV are accepted or stored.

The legacy invoice path is independent. New reservations use source WEB_CARD;
all seat writers continue to share reservations and BEGIN IMMEDIATE.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
from cryptography.fernet import Fernet

SOURCE = 'WEB_CARD'
TERMINAL = ('PAID', 'FAILED', 'EXPIRED', 'CANCELLED')
LOCKED = ('PAYING', 'UNKNOWN', 'REVIEW')
DECLINES = {'CARD_DECLINED', 'GENERIC_DECLINE', 'CVV_FAILURE', 'VERIFY_CVV_FAILURE',
            'ADDRESS_VERIFICATION_FAILURE', 'CARD_EXPIRED', 'INVALID_EXPIRATION',
            'INVALID_CARD_DATA', 'INVALID_CARD', 'CARD_DECLINED_VERIFICATION_REQUIRED'}

class CheckoutError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS web_checkouts(
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, secret_hash TEXT NOT NULL,
        payload_hash TEXT NOT NULL, reservation_id INTEGER NOT NULL UNIQUE,
        environment TEXT NOT NULL, location_id TEXT NOT NULL, amount INTEGER NOT NULL,
        language TEXT NOT NULL, state TEXT NOT NULL, expires REAL NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0,
        idempotency_key TEXT NOT NULL UNIQUE, order_id TEXT, payment_id TEXT UNIQUE,
        encrypted_request TEXT, last_error TEXT,
        FOREIGN KEY(reservation_id) REFERENCES reservations(id))''')
    c.execute('CREATE INDEX IF NOT EXISTS web_checkout_due ON web_checkouts(state,lease_until)')


def cipher():
    try:
        return Fernet(os.environ.get('CHECKOUT_TOKEN_ENCRYPTION_KEY', '').encode())
    except (ValueError, TypeError):
        raise CheckoutError('カード決済の設定を確認中です。店舗へお問い合わせください。', 503) from None


def enabled(app):
    return (os.getenv('WEB_CARD_CHECKOUT_ENABLED') == '1'
            and app.SQUARE_ENV in ('sandbox', 'production')
            and (app.SQUARE_ENV == 'sandbox' or os.getenv('WEB_CARD_PRODUCTION_APPROVED') == '1'))


def config(app, language='ja'):
    ready = bool(app.SQUARE_ENV in ('sandbox','production') and app.SQUARE_TOKEN
                 and app.SQUARE_LOCATION_ID and app.SQUARE_WEBHOOK_SIGNATURE_KEY
                 and app.APP_BASE_URL.startswith('https://') and os.getenv('SQUARE_APPLICATION_ID'))
    if ready:
        try:
            cipher()
        except CheckoutError:
            ready = False
    location = app.SQUARE_EN_LOCATION_ID if language == 'en' and app.SQUARE_EN_LOCATION_ID else app.SQUARE_LOCATION_ID
    sandbox = app.SQUARE_ENV == 'sandbox'
    return {'enabled': ready and enabled(app), 'environment': app.SQUARE_ENV,
            'application_id': os.getenv('SQUARE_APPLICATION_ID', '') if ready else '',
            'location_id': location if ready else '',
            'sdk_url': ('https://sandbox.web.squarecdn.com/v1/square.js' if sandbox
                        else 'https://web.squarecdn.com/v1/square.js') if ready else ''}


def require_ready(app):
    if not config(app)['enabled']:
        raise CheckoutError('画面内カード決済は現在準備中です。', 503)


def hash_secret(secret):
    if not isinstance(secret, str) or not re.fullmatch('[0-9a-f]{64}', secret):
        raise CheckoutError('お支払い画面を開き直してください。', 403)
    return hashlib.sha256(secret.encode()).hexdigest()


def get_session(c, checkout_id, secret):
    row = c.execute('SELECT * FROM web_checkouts WHERE id=?', (checkout_id,)).fetchone()
    digest = hash_secret(secret)
    if not row or not hmac.compare_digest(row['secret_hash'], digest):
        raise CheckoutError('お支払い画面を開き直してください。', 403)
    return dict(row)


def response(s):
    return {k:s[k] for k in ('id', 'reservation_id', 'amount', 'state', 'expires', 'language')}


def expire(app):
    c = app.con()
    try:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute("SELECT id,reservation_id FROM web_checkouts WHERE state='HOLD' AND expires<=?", (time.time(),)).fetchall()
        for s in rows:
            c.execute("UPDATE web_checkouts SET state='EXPIRED',updated=? WHERE id=? AND state='HOLD'", (time.time(),s['id']))
            c.execute("UPDATE reservations SET status='CANCELLED',cancellation_reason='CHECKOUT_EXPIRED',cancelled_at=?,updated_at=? WHERE id=? AND status='PENDING'", (app.now_iso(),app.now_iso(),s['reservation_id']))
        c.commit()
        return len(rows)
    finally:
        c.close()


def create(app, x):
    require_ready(app)
    if not isinstance(x, dict):
        raise CheckoutError('入力内容をご確認ください。')
    digest = hash_secret(x.get('checkout_secret'))
    try:
        request = str(uuid.UUID(x.get('request_id', '')))
        if request != x['request_id']:
            raise ValueError()
        app.booking_policy.require_public(x)
        if x.get('payment_method', 'CARD') != 'CARD' or x.get('cancellation_policy_accepted') is not True:
            raise ValueError()
        day = date.fromisoformat(x.get('date', ''))
        clock, area, party = x.get('time'), x.get('seating_area'), x.get('party_size')
        language = x.get('booking_language', 'ja')
        name, email, phone = (x.get(k, '') for k in ('guest_name','email','phone'))
        if any(not isinstance(v,str) for v in (name,email,phone)):
            raise ValueError()
        name,email,phone = name.strip(),email.strip().lower(),phone.strip()
        note,items,plate = app.guest_requests(x)
        today = datetime.now(timezone(timedelta(hours=9))).date()
        if (type(party) is not int or not app.public_party_allowed(area,party)
                or language not in ('ja','en') or day > today + timedelta(days=365)
                or not app.public_slot_allowed(day,clock,x.get('course'))
                or not 1 <= len(name) <= 80 or len(email)>254
                or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email)
                or not 10<=len(phone)<=20 or not re.fullmatch(r'[+\d ()-]+',phone)
                or sum(ch.isdigit() for ch in phone)<10):
            raise ValueError()
    except (ValueError, TypeError, AttributeError, KeyError):
        raise CheckoutError('日付・人数・連絡先・キャンセル規定をご確認ください。') from None
    # Bind all accepted booking values. Arbitrary client amount/location is ignored.
    canonical = json.dumps([str(day),clock,area,party,language,name,email,phone,note,items,plate],ensure_ascii=False)
    payload_hash = hashlib.sha256(canonical.encode()).hexdigest()
    expire(app)
    c = app.con()
    try:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM web_checkouts WHERE request_id=?',(request,)).fetchone()
        if old:
            s=get_session(c,old['id'],x['checkout_secret'])
            if s['payload_hash']!=payload_hash:
                raise CheckoutError('送信済みの予約内容は変更できません。',409)
            c.rollback()
            return response(s)
        app.booking_blocks.ensure_allowed(c,phone,email)
        if c.execute("SELECT count(*) FROM reservations WHERE source=? AND email=? AND created_at>=?",(SOURCE,email,(datetime.now(timezone.utc)-timedelta(days=1)).isoformat())).fetchone()[0]>=5:
            raise CheckoutError('申込回数の上限です。店舗へご連絡ください。',429)
        if c.execute("SELECT count(*) FROM web_checkouts s JOIN reservations r ON r.id=s.reservation_id WHERE r.email=? AND s.state IN ('HOLD','PAYING','UNKNOWN','REVIEW')",(email,)).fetchone()[0]>=1:
            raise CheckoutError('進行中のお支払いがあります。元のお支払い画面をご確認ください。',409)
        visit=f'{day.isoformat()}T{clock}'
        round_number=(1 if clock=='18:00' else 2) if area=='COUNTER' else None
        ok,message=app.availability_check(c,area,visit,party,round_number,150)
        if not ok:
            raise CheckoutError(message,409)
        now=app.now_iso();course,unit=app.bookable_course(day);amount=unit*party
        rid=c.execute('''INSERT INTO reservations(source,guest_name,phone,email,visit_at,party_size,
            course_name,booking_language,amount,seating_area,counter_round,duration_minutes,status,
            public_request_id,cancellation_policy_accepted_at,created_at,updated_at,guest_note,celebration_items,plate_message)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,150,'PENDING',?,?,?,?,?,?,?)''',
            (SOURCE,name,phone,email,visit,party,course,language,amount,area,round_number,
             'web-card:'+request,now,now,now,note,items,plate)).lastrowid
        app.marketing.bind(c,rid,x.get('attribution'),now,False)
        sid=uuid.uuid4().hex;ts=time.time();location=config(app,language)['location_id']
        c.execute('''INSERT INTO web_checkouts(id,request_id,secret_hash,payload_hash,reservation_id,
            environment,location_id,amount,language,state,expires,created,updated,idempotency_key)
            VALUES(?,?,?,?,?,?,?,?,?,'HOLD',?,?,?,?)''',
            (sid,request,digest,payload_hash,rid,app.SQUARE_ENV,location,amount,language,
             ts+900,ts,ts,'wc-'+sid))
        s=dict(c.execute('SELECT * FROM web_checkouts WHERE id=?',(sid,)).fetchone())
        c.commit()
        return response(s)
    except Exception:
        c.rollback();raise
    finally:
        c.close()


def status(app, sid, secret):
    expire(app)
    c=app.con()
    try:
        return response(get_session(c,sid,secret))
    finally:c.close()


def pay(app, sid, secret, source_id):
    # Existing sessions can finish after the new-booking feature is switched off.
    if not isinstance(source_id,str) or not re.fullmatch(r'cnon:[A-Za-z0-9:_-]{1,507}',source_id):
        raise CheckoutError('カード情報をもう一度ご確認ください。')
    c=app.con()
    try:
        c.execute('BEGIN IMMEDIATE')
        s=get_session(c,sid,secret)
        if s['state']!='HOLD':
            c.rollback();return response(s)
        if s['environment']!=app.SQUARE_ENV:
            raise CheckoutError('決済環境が一致しません。店舗へお問い合わせください。',503)
        if s['expires']<=time.time():
            c.rollback();expire(app);return status(app,sid,secret)
        r=c.execute('SELECT * FROM reservations WHERE id=?',(s['reservation_id'],)).fetchone()
        if not r or r['status']!='PENDING' or r['square_invoice_id']:
            raise CheckoutError('ご予約の状態を店舗へご確認ください。',409)
        body={'source_id':source_id,'idempotency_key':s['idempotency_key'],
              'amount_money':{'amount':s['amount'],'currency':'JPY'},
              'location_id':s['location_id'],'reference_id':'wc:'+sid,
              'autocomplete':True,'buyer_email_address':r['email']}
        encrypted=cipher().encrypt(json.dumps(body,sort_keys=True).encode()).decode()
        c.execute("UPDATE web_checkouts SET state='PAYING',encrypted_request=?,updated=?,lease_until=? WHERE id=? AND state='HOLD'",(encrypted,time.time(),time.time()+90,sid))
        c.commit()
    except Exception:c.rollback();raise
    finally:c.close()
    return submit(app,sid,claimed=True)


def submit(app,sid,claimed=False):
    c=app.con()
    try:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM web_checkouts WHERE id=?',(sid,)).fetchone()
        if not row:return None
        s=dict(row)
        if s['state'] not in ('PAYING','UNKNOWN') or (not claimed and s['lease_until']>time.time()):
            c.rollback();return response(s)
        if s['environment']!=app.SQUARE_ENV:
            c.rollback();return response(s)
        c.execute('UPDATE web_checkouts SET lease_until=?,updated=? WHERE id=?',(time.time()+90,time.time(),sid))
        c.commit()
    finally:c.close()
    try:
        body=json.loads(cipher().decrypt(s['encrypted_request'].encode()))
        if not s['order_id']:
            order=app.square('/v2/orders',body={'idempotency_key':'wo-'+sid,'order':{
                'location_id':s['location_id'],'reference_id':'wc:'+sid,
                'line_items':[{'name':'Crab omakase / お料理代','quantity':'1',
                               'base_price_money':{'amount':s['amount'],'currency':'JPY'}}]}})['order']
            if order.get('location_id')!=s['location_id'] or not order.get('id'):
                raise ValueError('Order mismatch')
            body['order_id']=order['id']
            c=app.con()
            try:
                c.execute('BEGIN IMMEDIATE')
                c.execute('UPDATE web_checkouts SET order_id=?,encrypted_request=? WHERE id=?',
                          (order['id'],cipher().encrypt(json.dumps(body,sort_keys=True).encode()).decode(),sid))
                c.execute('UPDATE reservations SET square_order_id=? WHERE id=?',(order['id'],s['reservation_id']))
                c.commit()
            finally:c.close()
        payment=app.square('/v2/payments',body=body)['payment']
        finalize(app,payment)
    except Exception as exc:
        # Only explicit terminal declines are safe to release. Network/5xx and
        # even malformed success responses remain UNKNOWN, preserving the key.
        try:codes={e.get('code') for e in json.loads(str(exc)).get('errors',[])}
        except (ValueError,AttributeError,TypeError):codes=set()
        failed=bool(codes) and codes.issubset(DECLINES)
        c=app.con()
        try:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT state FROM web_checkouts WHERE id=?',(sid,)).fetchone()
            if current and current['state'] in ('PAYING','UNKNOWN'):
                c.execute('UPDATE web_checkouts SET state=?,last_error=?,updated=?,lease_until=?,encrypted_request=CASE WHEN ? THEN NULL ELSE encrypted_request END WHERE id=?',
                          ('FAILED' if failed else 'UNKNOWN','CARD_DECLINED' if failed else 'RECONCILIATION_REQUIRED',time.time(),time.time()+90,failed,sid))
                if failed:
                    c.execute("UPDATE reservations SET status='CANCELLED',cancellation_reason='CARD_DECLINED',cancelled_at=?,updated_at=? WHERE id=? AND status='PENDING'",(app.now_iso(),app.now_iso(),s['reservation_id']))
            c.commit()
        finally:c.close()
    c=app.con()
    try:return response(dict(c.execute('SELECT * FROM web_checkouts WHERE id=?',(sid,)).fetchone()))
    finally:c.close()


def finalize(app,payment):
    pid=payment.get('id')
    if not isinstance(pid,str) or not re.fullmatch(r'[\w-]{1,100}',pid):
        raise ValueError('Missing payment id')
    current=app.square('/v2/payments/'+pid)['payment']
    ref=current.get('reference_id','')
    if not re.fullmatch(r'wc:[0-9a-f]{32}',ref):return False
    sid=ref[3:]
    c=app.con();confirmed=None
    try:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM web_checkouts WHERE id=?',(sid,)).fetchone()
        if not row:return False
        s=dict(row)
        if (current.get('source_type')!='CARD' or current.get('id')!=pid or s['environment']!=app.SQUARE_ENV or current.get('location_id')!=s['location_id']
                or current.get('order_id')!=s['order_id'] or not s['order_id']
                or current.get('amount_money')!={'amount':s['amount'],'currency':'JPY'}
                or current.get('total_money',current.get('amount_money'))!={'amount':s['amount'],'currency':'JPY'}):
            raise ValueError('Payment identity mismatch')
        if s['state']=='PAID':
            if s['payment_id']!=pid:raise ValueError('Duplicate paid identity')
            return True
        if current.get('status')!='COMPLETED':return False
        r=c.execute('SELECT * FROM reservations WHERE id=?',(s['reservation_id'],)).fetchone()
        if s['state'] not in ('PAYING','UNKNOWN') or r['status']!='PENDING':
            c.execute("UPDATE web_checkouts SET state='REVIEW',payment_id=?,updated=?,last_error='PAID_RESERVATION_CONFLICT' WHERE id=?",(pid,time.time(),sid));c.commit();return True
        c.execute("UPDATE reservations SET status='CONFIRMED',payment_source='SQUARE',payment_confirmed_at=?,last_error=NULL,updated_at=? WHERE id=?",(app.now_iso(),app.now_iso(),r['id']))
        c.execute("UPDATE web_checkouts SET state='PAID',payment_id=?,encrypted_request=NULL,lease_until=0,last_error=NULL,updated=? WHERE id=?",(pid,time.time(),sid))
        c.execute('INSERT INTO payment_audit(reservation_id,source,amount,reference,confirmed_by,created_at) VALUES(?,?,?,?,?,?)',
                  (r['id'],'SQUARE',s['amount'],pid,'Square Payments API',app.now_iso()))
        c.execute('INSERT OR IGNORE INTO owner_booking_events(reservation_id,registered) VALUES(?,?)',(r['id'],time.time()))
        confirmed=dict(c.execute('SELECT * FROM reservations WHERE id=?',(r['id'],)).fetchone())
        c.commit()
    except Exception:c.rollback();raise
    finally:c.close()
    if confirmed:
        # Confirmation is durable before any external delivery. Reconciliation
        # retries unclaimed notifications through the legacy delivery machinery.
        try:app.deliver_confirmation(confirmed)
        except Exception:pass
        try:
            app.guest_service.payment_event(app,{'type':'payment.updated','data':{'object':{'payment':current}}})
        except Exception:pass
    return True


def reconcile(app):
    expire(app)
    c=app.con()
    try:
        ids=[r[0] for r in c.execute("SELECT id FROM web_checkouts WHERE state IN ('PAYING','UNKNOWN') AND lease_until<=? ORDER BY updated LIMIT 20",(time.time(),))]
    finally:c.close()
    for sid in ids:
        submit(app,sid)


def guard_change(c,r):
    if r['source']!=SOURCE:return
    s=c.execute('SELECT state FROM web_checkouts WHERE reservation_id=?',(r['id'],)).fetchone()
    if s and s['state'] in LOCKED:
        raise CheckoutError('決済結果を確認中です。課金の照合が終わるまで予約を変更・取消できません。',409)


def cancel_hold(c,r):
    guard_change(c,r)
    if r['source']==SOURCE:
        c.execute("UPDATE web_checkouts SET state='CANCELLED',updated=? WHERE reservation_id=? AND state='HOLD'",(time.time(),r['id']))


def csp(nonce, sandbox):
    cdn='https://sandbox.web.squarecdn.com' if sandbox else 'https://web.squarecdn.com'
    pci='https://pci-connect.squareupsandbox.com' if sandbox else 'https://pci-connect.squareup.com'
    # 3DS submits an HTTPS form into an issuer iframe. The Sandbox uses
    # Square's simulator; live issuers have independent bank origins.
    frames = (f"'self' {cdn} https://api.squareupsandbox.com" if sandbox else "'self' https:")
    forms = ("'self' https://api.squareupsandbox.com" if sandbox else "'self' https:")
    return (f"default-src 'self'; base-uri 'self'; object-src 'none'; form-action {forms}; "
            f"script-src 'self' 'nonce-{nonce}' {cdn}; "
            f"style-src 'self' 'unsafe-inline' {cdn}; "
            f"connect-src 'self' {cdn} {pci} https://o160250.ingest.sentry.io; "
            f"frame-src {frames}; font-src 'self' https://square-fonts-production-f.squarecdn.com https://d1g145x70srn7h.cloudfront.net; "
            "img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'self'")


def secure_page(text):
    nonce=secrets.token_urlsafe(24)
    # Existing inline scripts and the loading animation receive a per-response
    # nonce. Attribute event handlers are not allowed on public payment pages.
    text=re.sub(r'<script\b',lambda _: '<script nonce="'+nonce+'"',text,flags=re.I)
    return text,nonce


def lookup(app,request,secret):
    c=app.con()
    try:
        s=c.execute('SELECT id FROM web_checkouts WHERE request_id=?',(request,)).fetchone()
        if not s:
            hash_secret(secret)
            raise CheckoutError('お支払いの受付は見つかりませんでした。',404)
        sid=s['id']
    finally:c.close()
    return status(app,sid,secret)


def abandon(app,sid,secret):
    c=app.con()
    try:
        c.execute('BEGIN IMMEDIATE')
        s=get_session(c,sid,secret)
        r=c.execute('SELECT * FROM reservations WHERE id=?',(s['reservation_id'],)).fetchone()
        if s['state']=='PAID':raise CheckoutError('確定済みの予約は取消手続きをご利用ください。',409)
        cancel_hold(c,r)
        if s['state']=='HOLD':
            c.execute("UPDATE reservations SET status='CANCELLED',cancellation_reason='CHECKOUT_ABANDONED',cancelled_at=?,updated_at=? WHERE id=? AND status='PENDING'",(app.now_iso(),app.now_iso(),r['id']))
        c.commit()
        return {'ok':True}
    except Exception:c.rollback();raise
    finally:c.close()
