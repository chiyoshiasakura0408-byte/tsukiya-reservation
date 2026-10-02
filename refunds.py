"""Durable cancellation refunds. Never submit a new key after an ambiguous result."""
import json
import threading
import uuid
from email.message import EmailMessage

LOCK = threading.Lock()
DELAY = 'カード明細への反映には通常さらに2〜7営業日ほどかかる場合があります。'
GUIDE = '''残高不足の場合の対応手順：
1. 登録メールアドレスから square-jp@help-messaging.squareup.com へ連絡。
2. 取引日・取引額・返金希望額・カード会社・カード番号下4桁・登録メールアドレス・代表者名を伝える。
3. Squareから振込先の案内を受けて入金。
4. 残高への反映連絡を受けて、返金手続きを再実行。
以前案内された口座があっても、先に振り込まず、毎回サポートの案内を待つ必要があります。このため、資金補充の部分は手動対応になります。
再実行前にSquareで元の返金状況を確認してください。処理中・結果未確認の返金に重ねて別の返金を行わないでください。
公式手順：https://squareup.com/help/jp/ja/article/8496-troubleshoot-customer-refund'''
LABELS = {'QUEUED':'返金受付', 'SUBMITTING':'返金結果確認中', 'PENDING':'返金処理中', 'COMPLETED':'Square返金処理完了', 'MANUAL':'返金対応待ち', 'FAILED':'返金対応待ち', 'REJECTED':'返金対応待ち', 'NONE':'返金対象なし'}

def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS cancellation_refunds(
        reservation_id INTEGER PRIMARY KEY, fee INTEGER NOT NULL, amount INTEGER NOT NULL,
        status TEXT NOT NULL, request_key TEXT NOT NULL, payload TEXT, refund_id TEXT,
        error TEXT, created_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS refund_notices(
        reservation_id INTEGER NOT NULL, stage TEXT NOT NULL, audience TEXT NOT NULL,
        status TEXT NOT NULL, PRIMARY KEY(reservation_id,stage,audience))''')

def enqueue(c, r, fee, now):
    paid = r['payment_source'] in ('SQUARE', 'BANK')
    amount = max(0, r['amount'] - fee) if paid else 0
    status = ('QUEUED' if r['payment_source']=='SQUARE' else 'MANUAL') if amount else 'NONE'
    c.execute('INSERT OR IGNORE INTO cancellation_refunds(reservation_id,fee,amount,status,request_key,created_at) VALUES(?,?,?,?,?,?)',
              (r['id'], fee, amount, status, str(uuid.uuid4()), now))

def public(c, rid):
    r = c.execute('SELECT fee,amount,status FROM cancellation_refunds WHERE reservation_id=?',(rid,)).fetchone()
    return dict(r) if r else {'fee':0,'amount':0,'status':'MANUAL'}

def update(app, rid, **values):
    c=app.con()
    c.execute('UPDATE cancellation_refunds SET '+','.join(k+'=?' for k in values)+' WHERE reservation_id=?',(*values.values(),rid))
    c.commit(); c.close()

def notify(app, job, reservation, stage):
    # SMTP errors can be ambiguous: retain a visible state rather than duplicate-send.
    for audience, recipient in [('store',app.MAIL_FROM),('guest',reservation['email'])]:
        if not recipient or not all((app.SMTP_HOST,app.SMTP_USER,app.SMTP_PASS,app.MAIL_FROM)):
            continue
        c=app.con()
        cur=c.execute('INSERT OR IGNORE INTO refund_notices VALUES(?,?,?,?)',(job['reservation_id'],stage,audience,'SENDING'))
        c.commit(); c.close()
        if not cur.rowcount: continue
        state=LABELS.get(stage,stage)
        body=f"予約 #{job['reservation_id']} {reservation['guest_name']} 様\nご来店予定：{reservation['visit_at']}\n予約キャンセル受付済み\nキャンセル料：{job['fee']:,}円\n返金対象額：{job['amount']:,}円\n返金状況：{state}\n{DELAY}"
        if stage in ('MANUAL','FAILED','REJECTED'):
            body+='\n返金手続きは完了していません。店舗で確認・対応いたします。'
        if audience=='store':
            body+='\n\n'+GUIDE+'\n\n確認情報：'+str(job.get('error') or '')
        msg=EmailMessage(); msg['From']=app.mail_sender(); msg['To']=recipient
        msg['Subject']=f'【西天満 つきや】キャンセル・{state}（予約 #{job["reservation_id"]}）'
        msg.set_content(body)
        result='SENT'
        try:
            with app.smtplib.SMTP(app.SMTP_HOST,app.SMTP_PORT,timeout=30) as s:
                s.starttls(); s.login(app.SMTP_USER,app.SMTP_PASS); s.send_message(msg)
        except Exception:
            result='UNKNOWN'
        c=app.con(); c.execute('UPDATE refund_notices SET status=? WHERE reservation_id=? AND stage=? AND audience=?',(result,job['reservation_id'],stage,audience)); c.commit(); c.close()

def process(app):
    if not LOCK.acquire(blocking=False): return
    try:
        c=app.con(); jobs=[dict(r) for r in c.execute('SELECT * FROM cancellation_refunds')]; c.close()
        for job in jobs:
            rid=job['reservation_id']; c=app.con()
            r=dict(c.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()); c.close()
            notify(app,job,r,job['status'])
            try:
                if job['status']=='QUEUED':
                    invoice=app.square('/v2/invoices/'+r['square_invoice_id'])['invoice']
                    if invoice.get('status')!='PAID' or not invoice.get('order_id'):
                        raise ValueError('請求書の全額決済を確認できません。Squareで確認してください。')
                    order=app.square('/v2/orders/'+invoice['order_id'])['order']
                    ids=[t.get('payment_id') or t.get('id') for t in order.get('tenders',[]) if t.get('type')=='CARD']
                    if len(ids)!=1 or not ids[0]:
                        raise ValueError('単一のカード決済を確認できません。分割・銀行入金は手動確認してください。')
                    p=app.square('/v2/payments/'+ids[0])['payment']
                    money=p.get('total_money',{})
                    if p.get('status')!='COMPLETED' or p.get('order_id')!=invoice['order_id'] or p.get('location_id')!=app.SQUARE_LOCATION_ID or money!={'amount':r['amount'],'currency':'JPY'} or p.get('refunded_money',{}).get('amount',0) or p.get('refund_ids'):
                        raise ValueError('決済額・店舗・既存返金を確認してください。自動返金を停止しました。')
                    payload={'idempotency_key':job['request_key'],'payment_id':p['id'],'amount_money':{'amount':job['amount'],'currency':'JPY'},'reason':'Reservation cancellation #'+str(rid)}
                    if p.get('version_token'): payload['payment_version_token']=p['version_token']
                    update(app,rid,payload=json.dumps(payload),status='SUBMITTING')
                    job['payload']=json.dumps(payload); job['status']='SUBMITTING'
                if job['status']=='SUBMITTING':
                    # Persisted exact request/key survives a timeout or process restart.
                    response=app.square('/v2/refunds',json.loads(job['payload']))['refund']
                    update(app,rid,refund_id=response['id'],status=response['status'],error=None)
                elif job['status']=='PENDING':
                    response=app.square('/v2/refunds/'+job['refund_id'])['refund']
                    update(app,rid,status=response['status'])
            except Exception as exc:
                # Do not blindly retry or issue a different idempotency key.
                update(app,rid,status='PENDING' if job['status']=='PENDING' else 'MANUAL',error=str(exc)[:1800])
            c=app.con(); current=dict(c.execute('SELECT * FROM cancellation_refunds WHERE reservation_id=?',(rid,)).fetchone()); c.close()
            notify(app,current,r,current['status'])
    finally: LOCK.release()
