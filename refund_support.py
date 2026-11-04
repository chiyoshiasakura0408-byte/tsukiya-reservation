"""One durable support email per new card cancellation; never move money here."""
import time
import os
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import parseaddr, formataddr

RECIPIENT = 'square-jp@help-messaging.squareup.com'


def sender_address(app):
    return parseaddr(os.getenv('SQUARE_REFUND_MAIL_FROM', app.MAIL_FROM))[1]

LABELS = {
    'PENDING': '問い合わせ送信待ち', 'PREPARING': '決済情報確認中',
    'WAITING_CONFIG': 'メール設定待ち', 'SENDING': '問い合わせ送信中',
    'SENT': 'Squareへ問い合わせ送信済み', 'UNKNOWN': '送信結果要確認（自動再送停止）',
    'REVIEW': '問い合わせ要確認', 'SKIPPED_LEGACY': '導入前の返金・自動送信対象外',
    'SKIPPED': '問い合わせ対象外',
}


def schema(c):
    exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='refund_support_inquiries'").fetchone()
    c.execute('''CREATE TABLE IF NOT EXISTS refund_support_inquiries(
        reservation_id INTEGER PRIMARY KEY, amount INTEGER NOT NULL, state TEXT NOT NULL,
        created REAL NOT NULL, started REAL, sent REAL, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, error TEXT, message_id TEXT)''')
    if not exists:
        # Do not replay historical cancellations (including manually contacted cases).
        c.execute("INSERT OR IGNORE INTO refund_support_inquiries(reservation_id,amount,state,created) "
                  "SELECT reservation_id,amount,'SKIPPED_LEGACY',? FROM cancellation_refunds WHERE amount>0",
                  (time.time(),))


def enqueue(c, reservation, amount):
    if reservation['payment_source'] == 'SQUARE' and amount > 0:
        c.execute("INSERT OR IGNORE INTO refund_support_inquiries(reservation_id,amount,state,created) "
                  "VALUES(?,?,'PENDING',?)", (reservation['id'], amount, time.time()))


def payment(app, c, r):
    if r['source'] == 'WEB_CARD':
        checkout = c.execute("SELECT * FROM web_checkouts WHERE reservation_id=? AND state='PAID'", (r['id'],)).fetchone()
        if not checkout or checkout['environment'] != app.SQUARE_ENV:
            raise ValueError('Web payment record or environment mismatch')
        pid, order_id, location_id = checkout['payment_id'], checkout['order_id'], checkout['location_id']
    else:
        invoice = app.square('/v2/invoices/' + r['square_invoice_id'])['invoice']
        order_id = invoice.get('order_id')
        if invoice.get('status') != 'PAID' or not order_id:
            raise ValueError('Paid invoice required')
        order = app.square('/v2/orders/' + order_id)['order']
        ids = [t.get('payment_id') or t.get('id') for t in order.get('tenders', []) if t.get('type') == 'CARD']
        if len(ids) != 1 or not ids[0]:
            raise ValueError('Single card payment required')
        pid = ids[0]
        location_id = (app.SQUARE_EN_LOCATION_ID if r.get('booking_language') == 'en' and app.SQUARE_EN_LOCATION_ID else app.SQUARE_LOCATION_ID)
    p = app.square('/v2/payments/' + pid)['payment']
    if (p.get('id') != pid or p.get('status') != 'COMPLETED'
            or p.get('order_id') != order_id or p.get('location_id') != location_id
            or p.get('total_money') != {'amount': r['amount'], 'currency': 'JPY'}
            or p.get('refund_ids') or p.get('refunded_money', {}).get('amount', 0)):
        raise ValueError('Payment identity, amount or refund history mismatch')
    card = p.get('card_details', {}).get('card', {})
    last4 = card.get('last_4', '')
    if not (len(last4) == 4 and last4.isascii() and last4.isdigit() and card.get('card_brand') and p.get('created_at')):
        raise ValueError('Payment date, card brand and last four digits required')
    return p


def message(app, inquiry, p):
    card = p['card_details']['card']
    paid_at = datetime.fromisoformat(p['created_at'].replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=9)))
    sender = sender_address(app)
    msg = EmailMessage()
    msg['From'] = formataddr(('西天満つきや', sender))
    msg['Reply-To'] = sender
    msg['To'] = RECIPIENT
    msg['Subject'] = f"払い戻し資金の入金方法・振込先の問い合わせ（西天満つきや #{inquiry['reservation_id']}）"
    msg['Message-ID'] = inquiry['message_id']
    msg.set_content(
        'Square サポートご担当者様\n\n西天満つきやの朝倉です。\n'
        '予約キャンセルに伴い、下記カード決済への払い戻しを予定しています。\n'
        '現在の残高が不足する場合に備え、必要な入金額、振込先口座、振込名義、'
        '入金反映の目安と払い戻し手順をご案内ください。残高不足は未確認です。\n\n'
        '店舗名：つきや\n店舗正式名称：西天満つきや\nSquareアカウント代表者名：朝倉千勝\n'
        f"決済日時：{paid_at:%Y-%m-%d %H:%M:%S} 日本時間\n"
        f"取引額：{p['total_money']['amount']:,}円\n"
        f"払い戻し希望額：{inquiry['amount']:,}円\n"
        f"カード会社：{card['card_brand']}\nカード番号下4桁：{card['last_4']}\n"
        f"Square決済ID：{p['id']}\nSquare店舗ID：{p['location_id']}\n"
        f"Square登録メールアドレス：{sender}\n当店管理番号：{inquiry['reservation_id']}\n\n"
        '返金操作は店舗側で別途確認・承認して実行します。'
        '本メールは振込先・手順の問い合わせです。\n'
        '追加のアカウント確認が必要な場合は、'
        '安全な確認方法をご案内ください。\n'
        '以前の口座への先行振込は行わず、今回のご案内をお待ちします。\n'
        'ご回答は本メールへの返信でお願いいたします。\n\nつきや（西天満つきや）\n朝倉千勝\n')
    return msg


def process(app):
    c = app.con()
    try:
        now = time.time()
        with c:
            c.execute("UPDATE refund_support_inquiries SET state='UNKNOWN',error='Interrupted SMTP; check sent mail' WHERE state='SENDING' AND started<?", (now-600,))
            c.execute("UPDATE refund_support_inquiries SET state='PENDING' WHERE state='PREPARING' AND started<?", (now-600,))
        ids = [row[0] for row in c.execute("SELECT reservation_id FROM refund_support_inquiries WHERE state IN ('PENDING','WAITING_CONFIG') AND next_attempt<=? ORDER BY created LIMIT 10", (now,))]
    finally:
        c.close()
    for rid in ids:
        dispatch(app, rid)


def dispatch(app, rid):
    c = app.con()
    try:
        with c:
            claimed = c.execute("UPDATE refund_support_inquiries SET state='PREPARING',started=? WHERE reservation_id=? AND state IN ('PENDING','WAITING_CONFIG')", (time.time(), rid)).rowcount
        if not claimed:
            return
        job = dict(c.execute('SELECT * FROM refund_support_inquiries WHERE reservation_id=?', (rid,)).fetchone())
        r = c.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone()
        refund = c.execute('SELECT * FROM cancellation_refunds WHERE reservation_id=?', (rid,)).fetchone()
        if (app.SQUARE_ENV != 'production' or not r or not refund or r['status'] != 'CANCELLED'
                or r['payment_source'] != 'SQUARE' or refund['amount'] != job['amount']
                or refund['status'] in ('NONE', 'COMPLETED', 'PENDING', 'SUBMITTING') or job['amount'] <= 0):
            with c:
                c.execute("UPDATE refund_support_inquiries SET state='SKIPPED' WHERE reservation_id=?", (rid,))
            return
        if not all((app.SMTP_HOST, app.SMTP_USER, app.SMTP_PASS, sender_address(app))):
            with c:
                c.execute("UPDATE refund_support_inquiries SET state='WAITING_CONFIG' WHERE reservation_id=?", (rid,))
            return
        try:
            p = payment(app, c, dict(r))
            job['message_id'] = f'<tsukiya-refund-support-{rid}-{int(job["created"] * 1000000)}@tsukiya.local>'
            msg = message(app, job, p)
        except Exception as exc:
            # Preparation makes only read-only Square calls and may be retried.
            attempts = job['attempts'] + 1
            with c:
                c.execute('UPDATE refund_support_inquiries SET state=?,attempts=?,next_attempt=?,error=? WHERE reservation_id=?',
                          ('REVIEW' if attempts >= 3 else 'PENDING', attempts,
                           time.time() + 300 * attempts, type(exc).__name__, rid))
            return
        with c:
            c.execute("UPDATE refund_support_inquiries SET state='SENDING',started=?,message_id=?,error=NULL WHERE reservation_id=?", (time.time(), job['message_id'], rid))
        try:
            with app.smtplib.SMTP(app.SMTP_HOST, app.SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(app.SMTP_USER, app.SMTP_PASS)
                refused = server.send_message(msg, from_addr=sender_address(app), to_addrs=[RECIPIENT])
                if refused:
                    raise RuntimeError('Recipient refused')
        except Exception as exc:
            # SMTP may have accepted the mail. Never replay an uncertain send.
            with c:
                c.execute("UPDATE refund_support_inquiries SET state='UNKNOWN',error=? WHERE reservation_id=?", (type(exc).__name__, rid))
            return
        with c:
            c.execute("UPDATE refund_support_inquiries SET state='SENT',sent=?,error=NULL WHERE reservation_id=?", (time.time(), rid))
    finally:
        c.close()
