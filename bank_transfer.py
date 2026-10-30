"""Bank-intent bookings: no card invoice; audited staff reconciliation only."""
import os
import re
from datetime import datetime, timedelta, timezone, date
from email.message import EmailMessage
import concierge
import line_receipts

JST = timezone(timedelta(hours=9))
ACCOUNT_JA = '三井住友銀行 堂島支店\n普通 0655295\n口座名義：アサクラ チヨシ'
ACCOUNT_EN = 'Sumitomo Mitsui Banking Corporation, Dojima Branch\nOrdinary account 0655295\nAccount name: ASAKURA CHIYOSHI'

def schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS bank_notices(reservation_id INTEGER,stage TEXT,destination TEXT,outbox_id TEXT,PRIMARY KEY(reservation_id,stage,destination))')

def parse(x):
    method = x.get('payment_method', 'CARD')
    if method not in ('CARD', 'BANK'): raise ValueError('invalid payment method')
    if method == 'CARD': return method, '', ''
    if x.get('booking_language') == 'en': raise ValueError('English reservations accept credit cards only')
    payer = x.get('bank_payer', '')
    planned = x.get('bank_planned_date', '')
    if not isinstance(payer, str) or not 1 <= len(payer.strip()) <= 80 or any(ord(ch)<32 for ch in payer):
        raise ValueError('invalid bank payer')
    if not isinstance(planned, str): raise ValueError('invalid bank date')
    if planned:
        day = date.fromisoformat(planned)
        today = datetime.now(JST).date()
        if not today <= day <= today + timedelta(days=1): raise ValueError('bank date outside payment window')
    return method, payer.strip(), planned

def ready(db):
    c = concierge.connect(db)
    try:
        return bool(re.fullmatch(r'U[0-9a-f]{32}', concierge.setting(c, 'owner')) and os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
    finally: c.close()

def enqueue(c, r, app, stage='requested'):
    owner = concierge.setting(c, 'owner')
    if not re.fullmatch(r'U[0-9a-f]{32}', owner): raise ValueError('朝倉LINE未連携です')
    heading = {'requested':'振込入金確認依頼','overdue':'振込期限経過・入金確認依頼','confirmed':'振込入金確認済み','cancelled':'振込予約取消・入金状況確認'}[stage]
    text = (f"【つきや・{heading}】\n予約 #{r['id']} {r['guest_name']} 様\n"
            f"来店：{r['visit_at']}／{r['party_size']}名\n金額：{r['amount']:,}円\n"
            f"振込名義：{r['bank_payer']}\n振込予定日：{r['bank_planned_date'] or '未指定'}\n"
            f"振込期限：{datetime.fromisoformat(r['bank_due_at']).astimezone(JST).strftime('%Y/%m/%d %H:%M')}（日本時間）\n"
            + ('全額入金を記録し、予約確定しました。' if stage=='confirmed' else
               '予約は取消済みです。入金がある場合は返金等を個別に確認してください。' if stage=='cancelled' else
               '銀行明細の名義・金額を照合し、決済管理で全額着金を記録してください。銀行口座の自動監視・自動確定は行いません。')
            + '\n' + app.APP_BASE_URL.rstrip('/') + '/')
    for destination in (owner,):
        if c.execute('INSERT OR IGNORE INTO bank_notices(reservation_id,stage,destination) VALUES(?,?,?)',(r['id'],stage,destination)).rowcount:
            key=concierge.enqueue(c,destination,[{'type':'text','text':text}],channel='owner',kind='bank-transfer',reservation_id=r['id'])
            c.execute('UPDATE bank_notices SET outbox_id=? WHERE reservation_id=? AND stage=? AND destination=?',(key,r['id'],stage,destination))

def response(r, app=None):
    return {'reservation_id':r['id'],'payment_method':'BANK','status':r['status'],
            'bank_account':ACCOUNT_EN if r['booking_language']=='en' else ACCOUNT_JA,
            'bank_due_at':r['bank_due_at'],'amount':r['amount'],
            'management_url':app.cancellation_url(r) if app else ''}

def receipt(app, r):
    c=app.con()
    try:
        claimed=c.execute("UPDATE reservations SET bank_email_status='SENDING' WHERE id=? AND bank_email_status IS NULL",(r['id'],)).rowcount
        c.commit()
        if not claimed: return
        english=r['booking_language']=='en'
        deadline=datetime.fromisoformat(r['bank_due_at']).astimezone(JST).strftime('%Y/%m/%d %H:%M')
        body=(f"{r['guest_name']}\nReservation request #{r['id']} received. Awaiting bank transfer; not yet confirmed.\nVisit: {r['visit_at']} / {r['party_size']} guests\nAmount: JPY {r['amount']:,}\nTransfer name: {r['bank_payer']}\nTransfer by: {deadline} JST\n\n{ACCOUNT_EN}\n\nWe will confirm your reservation after staff verify full payment.\n" if english else
              f"{r['guest_name']} 様\nお申込み（受付番号 {r['id']}）を受け付けました。現在は入金確認待ちで、ご予約は未確定です。\n来店：{r['visit_at']}／{r['party_size']}名様\n振込金額：{r['amount']:,}円\n振込名義：{r['bank_payer']}\n振込期限：{deadline}（日本時間）\n\n{ACCOUNT_JA}\n\n全額の着金を店舗で確認後、ご予約確定の通知をお送りします。\n")
        link=app.cancellation_url(r)
        if link:
            body+='\n人数変更・キャンセル（2名以上） / Change guest count (minimum 2):\n'+link+'\n'
        body+='\n'+app.booking_policy.details(r)['beverage_policy_en' if english else 'beverage_policy']+'\n'+app.annex_message(r,english)+'\n西天満つきや'
        try:
            if not all((app.SMTP_HOST,app.SMTP_USER,app.SMTP_PASS,app.MAIL_FROM)): raise RuntimeError('SMTP未設定')
            msg=EmailMessage();msg['To']=r['email'];msg['From']=app.MAIL_FROM
            msg['Subject']='Tsukiya | Bank transfer instructions' if english else '【西天満つきや】銀行振込のご案内・入金確認待ち'
            msg.set_content(body)
            with app.smtplib.SMTP(app.SMTP_HOST,app.SMTP_PORT,timeout=30) as smtp:
                smtp.starttls();smtp.login(app.SMTP_USER,app.SMTP_PASS);smtp.send_message(msg)
            state='SENT'
        except Exception: state='ERROR'
        c.execute('UPDATE reservations SET bank_email_status=? WHERE id=?',(state,r['id']));c.commit()
    finally: c.close()

def overdue(app):
    c=concierge.connect(app.DB);c.close()
    c=app.con()
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute("SELECT * FROM reservations WHERE payment_method='BANK' AND status='PENDING' AND bank_due_at<=?",(app.now_iso(),)).fetchall()
            for r in rows: enqueue(c,r,app,'overdue')
    finally:c.close()
    c=app.con()
    try: pending=[dict(r) for r in c.execute("SELECT * FROM reservations WHERE payment_method='BANK' AND status='PENDING' AND bank_email_status IS NULL LIMIT 20")]
    finally:c.close()
    for row in pending: receipt(app,row)


def accounting_digest(db, base_url):
    """One bounded text in the existing 09:00 briefing; current unpaid requests."""
    import sqlite3
    c=sqlite3.connect(db);c.row_factory=sqlite3.Row
    try:
        if 'payment_method' not in {r[1] for r in c.execute('PRAGMA table_info(reservations)')}:
            return []
        rows=c.execute("SELECT * FROM reservations WHERE payment_method='BANK' AND status='PENDING' ORDER BY bank_due_at,id").fetchall()
        if not rows:return []
        now=datetime.now(timezone.utc)
        text=f"【銀行振込・朝の入金確認依頼】\n未確認 {len(rows)}件／合計 {sum(r['amount'] for r in rows):,}円\n銀行明細の名義・全額着金を照合してください。\n"
        shown=0
        for r in rows:
            due=datetime.fromisoformat(r['bank_due_at'])
            item=(f"\n予約 #{r['id']} {r['guest_name']} 様\n"
                  f"来店：{r['visit_at']}／{r['party_size']}名\n金額：{r['amount']:,}円\n"
                  f"名義：{r['bank_payer']}／予定日：{r['bank_planned_date'] or '未指定'}\n"
                  f"期限：{due.astimezone(JST).strftime('%m/%d %H:%M')}（日本時間）"
                  + ('【期限経過・要確認】' if due<=now else '') + '\n')
            if len((text+item).encode('utf-16-le'))//2>4400:break
            text+=item;shown+=1
        if shown<len(rows):text+=f"\nほか {len(rows)-shown}件。全件は決済管理で確認してください。\n"
        text+='\n確認後は決済管理から予約確定してください。\n'+base_url.rstrip('/')+'/'
        return [{'type':'text','text':text}]
    finally:c.close()

