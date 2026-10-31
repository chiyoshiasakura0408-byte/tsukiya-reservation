"""Notify guests only after SMTP accepts an email; never replay uncertain SMS."""
import hashlib
import json
import sqlite3
import time
from email.utils import parseaddr


def connect(app):
    c = sqlite3.connect(app.DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS guest_email_sms (
        event_key TEXT PRIMARY KEY, reservation_id INTEGER NOT NULL,
        email TEXT NOT NULL, phone TEXT NOT NULL, body TEXT NOT NULL,
        state TEXT NOT NULL, sid TEXT, error TEXT, created REAL NOT NULL,
        started REAL, accepted REAL)''')
    c.commit()
    return c


def body(row, kind):
    if row.get('booking_language') == 'en':
        text = ('Tsukiya at Nishi-Tenma: We have sent an email to your registered email address. '
                'Please check your inbox and spam/junk folder.')
        if kind in ('bank', 'payment-reminder'):
            text += ' Please complete payment by the deadline stated in the email.'
        return text
    text = ('西天満つきやでございます。ご登録のメールアドレス宛にメールをお送りしました。'
            '受信箱に見当たらない場合は、迷惑メールフォルダもご確認ください。')
    if kind == 'bank':
        text += 'お振込みには期限がございますので、メールに記載の期限内にお手続きをお願いいたします。'
    elif kind == 'payment-reminder':
        text += 'メールに記載のお支払期限内にお手続きをお願いいたします。'
    return text


def accepted(app, reservation, message, kind):
    """Call only after a successful SMTP send to this reservation's guest."""
    try:
        r = dict(reservation)
        recipient = parseaddr(str(message['To'] or ''))[1].strip().lower()
        if not recipient or recipient != str(r.get('email') or '').strip().lower():
            return
        phone = str(r.get('phone') or '').strip()
        # Exclude transport-generated headers so a retry of identical content
        # cannot create another SMS. A changed email creates a new event.
        content = json.dumps([r['id'], kind, recipient, str(message['Subject'] or ''),
                              message.get_content()], ensure_ascii=False)
        key = hashlib.sha256(content.encode()).hexdigest()
        c = connect(app)
        try:
            with c:
                c.execute('INSERT OR IGNORE INTO guest_email_sms '
                          '(event_key,reservation_id,email,phone,body,state,created) '
                          'VALUES(?,?,?,?,?,?,?)',
                          (key, r['id'], recipient, phone, body(r, kind),
                           'PENDING' if phone else 'SKIPPED', time.time()))
        finally:
            c.close()
        dispatch(app, key)
    except Exception as exc:
        # SMS failure must never turn an accepted email into an email retry.
        print('Guest email SMS notice failed: ' + type(exc).__name__)


def dispatch(app, key):
    c = connect(app)
    try:
        # Credentials stay inside the existing production SMS implementation.
        if not all(getattr(app, name, '') for name in
                   ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER')):
            return
        with c:
            claimed = c.execute("UPDATE guest_email_sms SET state='SENDING',started=?,error=NULL "
                                "WHERE event_key=? AND state='PENDING'", (time.time(), key)).rowcount
        if not claimed:
            return
        notice = dict(c.execute('SELECT * FROM guest_email_sms WHERE event_key=?', (key,)).fetchone())
        try:
            result = app.send_sms(notice['phone'], body=notice['body'])
            if not result.get('sid') or result.get('status') in ('failed', 'undelivered', 'canceled'):
                raise RuntimeError('SMS was not accepted')
            with c:
                c.execute('UPDATE guest_email_sms SET state=?,sid=?,accepted=? WHERE event_key=?',
                          ('ACCEPTED', result['sid'], time.time(), key))
        except Exception as exc:
            with c:
                c.execute("UPDATE guest_email_sms SET state='REVIEW',error=? WHERE event_key=?",
                          (type(exc).__name__, key))
            print('Guest email SMS needs review: ' + type(exc).__name__)
    finally:
        c.close()


def process(app):
    c = connect(app)
    try:
        with c:
            c.execute("UPDATE guest_email_sms SET state='REVIEW',error='Interrupted send; verify SMS history' "
                      "WHERE state='SENDING' AND started<?", (time.time()-600,))
        keys = [r[0] for r in c.execute("SELECT event_key FROM guest_email_sms WHERE state='PENDING' "
                                       'ORDER BY created LIMIT 20')]
    finally:
        c.close()
    for key in keys:
        dispatch(app, key)
