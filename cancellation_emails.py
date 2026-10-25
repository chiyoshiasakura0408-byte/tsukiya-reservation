"""Durable payment-expiry emails. Capture future cancellations atomically.

SMTP has no idempotency API: ambiguous DATA failures are held for review,
never retried automatically. Successful means SMTP accepted, not inbox delivery.
"""
import smtplib
import time
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS cancellation_emails(
        reservation_id INTEGER PRIMARY KEY, state TEXT NOT NULL DEFAULT 'PENDING',
        message_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        next_try REAL NOT NULL DEFAULT 0, started REAL, accepted_at TEXT,
        error TEXT, created_at TEXT NOT NULL)''')
    # No historical backfill: old cancellations may already have been notified.
    c.execute('''CREATE TRIGGER IF NOT EXISTS capture_payment_expiry_email
        AFTER UPDATE OF status ON reservations
        WHEN NEW.status='CANCELLED' AND OLD.status IN ('PENDING','INVOICED','ERROR')
            AND NEW.cancellation_reason='PAYMENT_EXPIRED' AND NEW.amount>1
            AND COALESCE(NEW.course_name,'') NOT LIKE '%テスト%'
        BEGIN INSERT OR IGNORE INTO cancellation_emails(reservation_id,created_at)
            VALUES(NEW.id,strftime('%Y-%m-%dT%H:%M:%SZ','now')); END''')


def message(app, r, message_id):
    visit = app.parse_dt(r['visit_at'])
    if visit.tzinfo:
        visit = visit.astimezone(app.ZoneInfo('Asia/Tokyo'))
    english = r.get('booking_language') == 'en'
    mail = EmailMessage()
    mail['Subject'] = ('Your reservation has been cancelled – Tsukiya' if english
                       else '【西天満つきや】お支払期限超過によるご予約取消のお知らせ')
    mail['From'] = app.mail_sender()
    mail['To'] = r['email']
    mail['Message-ID'] = message_id
    mail['Reply-To'] = app.MAIL_FROM
    if english:
        body = (f"Dear {r['guest_name']},\n\nThank you for choosing Tsukiya at Nishi-Tenma.\n\n"
                f"Your reservation for {visit:%Y-%m-%d at %H:%M} (Japan time), "
                f"{r['party_size']} guests, has been cancelled because payment was not completed by the payment deadline.\n\n"
                "If you would like to book again, please make a new reservation through our official reservation page. Availability will be checked at the time of booking.\n\n"
                "If you have already paid or have any questions, please reply to this email.\n\nTsukiya at Nishi-Tenma")
    else:
        body = (f"{r['guest_name']}様\n\nこの度は西天満つきやをご予約いただき、誠にありがとうございます。\n\n"
                f"{visit:%Y年%m月%d日 %H:%M}・{r['party_size']}名様のご予約につきまして、"
                "お支払期限までに事前決済の完了を確認できなかったため、ご予約をお取り消しいたしました。\n\n"
                "改めてご予約をご希望の場合は、公式予約ページよりお申し込みください。お席はお申し込み時点の空き状況にてご案内いたします。\n\n"
                "すでにお支払い済みの場合やご不明な点がございましたら、このメールへご返信ください。\n\n西天満つきや")
    mail.set_content(body)
    return mail


def process(app):
    c = app.con()
    try:
        # A process may have died after SMTP accepted; never reclaim automatically.
        c.execute("UPDATE cancellation_emails SET state='REVIEW',error='Interrupted SMTP attempt; verify delivery' WHERE state='SENDING' AND started<?", (time.time()-600,))
        ids = [r[0] for r in c.execute("SELECT reservation_id FROM cancellation_emails WHERE state='PENDING' AND next_try<=? ORDER BY reservation_id LIMIT 20", (time.time(),))]
        c.commit()
    finally:
        c.close()
    for rid in ids:
        _send(app, rid)


def _send(app, rid):
    c = app.con()
    try:
        c.execute('BEGIN IMMEDIATE')
        r = c.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone()
        if not r or r['status'] != 'CANCELLED' or r['cancellation_reason'] != 'PAYMENT_EXPIRED':
            c.execute("UPDATE cancellation_emails SET state='SKIPPED',error='Reservation is no longer payment-expired' WHERE reservation_id=? AND state='PENDING'", (rid,))
            c.commit()
            return
        r = dict(r)
        email = r.get('email') or ''
        if not email or parseaddr(email)[1] != email or '@' not in email or '\n' in email or '\r' in email:
            c.execute("UPDATE cancellation_emails SET state='REVIEW',error='Missing or invalid email address' WHERE reservation_id=? AND state='PENDING'", (rid,))
            c.commit()
            return
        if not all((app.SMTP_HOST, app.SMTP_USER, app.SMTP_PASS, app.MAIL_FROM)):
            c.execute("UPDATE cancellation_emails SET error='SMTP is not configured',next_try=? WHERE reservation_id=? AND state='PENDING'", (time.time()+300,rid))
            c.commit()
            return
        mid = make_msgid(domain='tsukiya-reservation.onrender.com')
        claimed = c.execute("UPDATE cancellation_emails SET state='SENDING',attempts=attempts+1,started=?,message_id=?,error=NULL WHERE reservation_id=? AND state='PENDING' AND next_try<=?", (time.time(),mid,rid,time.time())).rowcount
        c.commit()
        if not claimed:
            return
    finally:
        c.close()
    server = None
    data_started = False
    state, error, accepted = 'REVIEW', None, None
    try:
        mail = message(app, r, mid)
        server = smtplib.SMTP(app.SMTP_HOST, app.SMTP_PORT, timeout=30)
        server.starttls()
        server.login(app.SMTP_USER, app.SMTP_PASS)
        # Recheck after connection setup, immediately before sending.
        c = app.con()
        try:
            current = c.execute('SELECT status,cancellation_reason,email,visit_at,party_size,guest_name FROM reservations WHERE id=?', (rid,)).fetchone()
            if not current or any(current[k] != r[k] for k in current.keys()):
                raise ValueError('Reservation changed; review before sending')
        finally:
            c.close()
        data_started = True
        refused = server.send_message(mail)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        state, accepted = 'SENT', app.now_iso()
    except Exception as exc:
        error = type(exc).__name__
        # Connection/login failures and explicit SMTP rejection are safe to retry.
        safe_retry = (not data_started and not isinstance(exc, (ValueError, smtplib.SMTPAuthenticationError))) or isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError))
        state = 'PENDING' if safe_retry else 'REVIEW'
    finally:
        if server:
            try:
                server.close()
            except Exception:
                pass
    c = app.con()
    try:
        c.execute("UPDATE cancellation_emails SET state=CASE WHEN ?='PENDING' AND attempts>=5 THEN 'REVIEW' ELSE ? END,error=?,accepted_at=?,next_try=? WHERE reservation_id=? AND state='SENDING' AND message_id=?", (state,state,error,accepted,time.time()+300,rid,mid))
        c.commit()
    finally:
        c.close()
