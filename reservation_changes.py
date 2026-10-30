"""Staff changes to paid bookings; supplemental invoices retain original payments."""
import json
import customer_party
import threading
import uuid
from datetime import datetime, timedelta, timezone

LOCK = threading.Lock()


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS reservation_changes(
        id TEXT PRIMARY KEY, reservation_id INTEGER NOT NULL,
        old_party INTEGER NOT NULL, new_party INTEGER NOT NULL,
        old_amount INTEGER NOT NULL, new_amount INTEGER NOT NULL,
        amount INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'DRAFT',
        snapshot TEXT NOT NULL, customer_id TEXT, order_id TEXT,
        invoice_id TEXT UNIQUE, invoice_url TEXT, paid_at TEXT,
        created_at TEXT NOT NULL, error TEXT)''')


def records(c, rid):
    return [dict(r) for r in c.execute(
        'SELECT id,amount,state,invoice_id,invoice_url,paid_at,error FROM reservation_changes '
        'WHERE reservation_id=? ORDER BY created_at,id', (rid,))]


def summary(c, rid):
    rows = records(c, rid)
    return {'additional_invoices': rows,
            'additional_unpaid': sum(r['amount'] for r in rows if r['state'] != 'PAID')}


def verify_original(app, c, rid):
    r = c.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone()
    if not r or r['status'] != 'CONFIRMED' or not r['square_invoice_id']:
        raise ValueError('決済済み予約を確認してください')
    delta = c.execute('SELECT COALESCE(SUM(amount),0) FROM reservation_changes '
                      'WHERE reservation_id=?',(rid,)).fetchone()[0]
    invoice = app.square('/v2/invoices/'+r['square_invoice_id'])['invoice']
    paid = sum(p.get('total_completed_amount_money',{}).get('amount',0)
               for p in invoice.get('payment_requests',[])
               if p.get('total_completed_amount_money',{}).get('currency')=='JPY')
    if invoice.get('status') != 'PAID' or paid != r['amount']-delta:
        raise ValueError('元のSquare決済額・返金状況を確認してください')


def change(app, c, rid, data):
    """Caller owns a BEGIN IMMEDIATE transaction. No network or message sends here."""
    r = c.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone()
    if customer_party.latest(c, rid):
        raise ValueError("お客様の人数変更履歴があります。お客様用変更画面から変更してください")
    if (not r or r['status'] != 'CONFIRMED' or r['payment_source'] != 'SQUARE'
            or not r['square_invoice_id'] or r['vip_through']):
        raise ValueError('Square請求書の決済済み通常予約のみ変更できます')
    for key in ('party_size', 'amount', 'visit_at', 'seating_area'):
        if data.get('expected_' + key) != r[key]:
            raise ValueError('予約が更新されています。開き直してください')
    if data.get('expected_guest_note') != (r['guest_note'] or ''):
        raise ValueError('備考が更新されています。開き直してください')
    if summary(c, rid)['additional_unpaid']:
        raise ValueError('未決済の差額請求があります。先に決済状況をご確認ください')
    party = data.get('party_size')
    if type(party) is not int or not r['party_size'] < party <= 8:
        raise ValueError('決済済み予約は増員に対応しています。減員は店舗対応です')
    note = data.get('guest_note')
    if not isinstance(note, str) or len(note) > 5000 or '\x00' in note:
        raise ValueError('備考は5000文字以内で入力してください')
    if not r['email']:
        raise ValueError('差額請求を送るメールアドレスが必要です')
    if app.parse_dt(r['visit_at']) <= datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None):
        raise ValueError('過去の予約は変更できません')
    if not r['party_size'] or r['amount'] <= 0 or r['amount'] % r['party_size']:
        raise ValueError('1名様の料金を確認してください')
    ok, message = app.availability_check(c, r['seating_area'], r['visit_at'], party,
        r['counter_round'], int(r['duration_minutes'] or 150), rid,
        allow_custom_start=r['seating_area'] in app.ROOMS, restore_existing=True)
    if not ok:
        raise ValueError(message)
    amount = r['amount'] // r['party_size'] * party
    delta = amount - r['amount']
    ident, ts = str(uuid.uuid4()), app.now_iso()
    due = min(datetime.now(timezone(timedelta(hours=9))) + timedelta(hours=24),
              app.parse_dt(r['visit_at']).replace(tzinfo=timezone(timedelta(hours=9))))
    snapshot = {k: r[k] for k in ('guest_name','email','phone','booking_language',
                                'visit_at','course_name','seating_area','counter_round')}
    snapshot.update(guest_note=note.strip(), due_date=due.date().isoformat(),
                    location_id=app.SQUARE_EN_LOCATION_ID if r['booking_language']=='en'
                    and app.SQUARE_EN_LOCATION_ID else app.SQUARE_LOCATION_ID)
    c.execute('INSERT INTO reservation_changes(id,reservation_id,old_party,new_party,'
        'old_amount,new_amount,amount,snapshot,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (ident,rid,r['party_size'],party,r['amount'],amount,delta,
         json.dumps(snapshot,ensure_ascii=False),ts))
    c.execute('INSERT INTO reservation_note_history(reservation_id,old_note,new_note,changed_at) '
              'VALUES(?,?,?,?)', (rid,r['guest_note'] or '',note.strip(),ts))
    c.execute('UPDATE reservations SET party_size=?,amount=?,guest_note=?,updated_at=? WHERE id=?',
              (party,amount,note.strip(),ts,rid))
    return {'ok':True,'party_size':party,'amount':amount,'difference':delta,'change_id':ident}


def issue(app, rid, ident):
    """Explicit staff action. Stable payloads and keys make uncertain API retries safe."""
    with LOCK:
        c = app.con()
        try:
            row = c.execute('SELECT * FROM reservation_changes WHERE id=? AND reservation_id=?',
                            (ident,rid)).fetchone()
            r = c.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()
            if not row or not r or r['status'] != 'CONFIRMED':
                raise ValueError('差額請求の対象予約を確認してください')
            row = dict(row)
            if row['state'] in ('INVOICED','PAID'):
                return {'ok':True, **row}
            s = json.loads(row['snapshot'])
            if not s['location_id']:
                raise ValueError('Square店舗が未設定です')
            c.execute("UPDATE reservation_changes SET state='ISSUING',error=NULL WHERE id=?",(ident,))
            c.commit()
            key = ident
            if not row['customer_id']:
                customer = app.square('/v2/customers',body={
                    'idempotency_key':key+'-customer','given_name':s['guest_name'],
                    'email_address':s['email'],'reference_id':'chg:'+ident})['customer']
                row['customer_id'] = customer['id']
                c.execute('UPDATE reservation_changes SET customer_id=? WHERE id=?',(customer['id'],ident))
                c.commit()
            if not row['order_id']:
                order = app.square('/v2/orders',body={'idempotency_key':key+'-order','order':{
                    'location_id':s['location_id'],'customer_id':row['customer_id'],
                    'reference_id':'chg:'+ident,'line_items':[{
                        'name':f"人数変更の差額 #{rid}（{row['old_party']}名→{row['new_party']}名）",
                        'quantity':'1','base_price_money':{'amount':row['amount'],'currency':'JPY'}}]}})['order']
                row['order_id'] = order['id']
                c.execute('UPDATE reservation_changes SET order_id=? WHERE id=?',(order['id'],ident))
                c.commit()
            if not row['invoice_id']:
                description = (f"予約番号：#{rid}\nご来店日時：{s['visit_at']}\n"
                    f"コース：{s['course_name']}\nお席：{app.seating_label(s)}\n"
                    f"大人{row['new_party']}名\nお料理代総額：{row['new_amount']:,}円\n"
                    f"お支払い済み：{row['old_amount']:,}円\n今回の差額請求：{row['amount']:,}円\n"
                    f"備考：{s['guest_note']}\nお支払期限：{s['due_date']}（日本時間）")
                invoice = app.square('/v2/invoices',body={'idempotency_key':key+'-invoice','invoice':{
                    'location_id':s['location_id'],'order_id':row['order_id'],
                    'primary_recipient':{'customer_id':row['customer_id']},'delivery_method':'EMAIL',
                    'title':'西天満つきや｜予約人数変更の差額','description':description,
                    'payment_requests':[{'request_type':'BALANCE','due_date':s['due_date']}],
                    'accepted_payment_methods':{'card':True}}})['invoice']
                row['invoice_id'] = invoice['id']
                c.execute('UPDATE reservation_changes SET invoice_id=? WHERE id=?',(invoice['id'],ident))
                c.commit()
            # GET before retrying publication: a timed-out publish may have succeeded.
            invoice = app.square('/v2/invoices/'+row['invoice_id'])['invoice']
            if invoice['status'] == 'DRAFT':
                invoice = app.square('/v2/invoices/'+row['invoice_id']+'/publish',body={
                    'idempotency_key':key+'-publish','version':invoice['version']})['invoice']
            if invoice['status'] not in ('UNPAID','PARTIALLY_PAID','PAID','PAYMENT_PENDING'):
                raise ValueError('Squareの請求状態を確認してください')
            state = 'PAID' if invoice['status']=='PAID' else 'INVOICED'
            url = invoice.get('public_url') or invoice.get('invoice_url') or ''
            c.execute('UPDATE reservation_changes SET state=?,invoice_url=?,paid_at=?,error=NULL WHERE id=?',
                      (state,url,app.now_iso() if state=='PAID' else None,ident))
            c.commit()
            return {'ok':True,'state':state,'invoice_id':row['invoice_id'],'invoice_url':url,
                    'amount':row['amount'],'email':s['email']}
        except Exception as exc:
            if 'row' in locals() and row:
                c.execute("UPDATE reservation_changes SET state='ERROR',error=? WHERE id=? AND state!='PAID'",
                          ('請求処理の結果を確認してください。同じ請求を再確認できます。',ident))
                c.commit()
            raise
        finally:
            c.close()


def reconcile(app):
    c = app.con()
    try:
        rows = [dict(r) for r in c.execute("SELECT id,invoice_id,amount FROM reservation_changes "
            "WHERE invoice_id IS NOT NULL AND state IN ('INVOICED','ERROR','ISSUING')")]
    finally:
        c.close()
    for row in rows:
        try:
            invoice = app.square('/v2/invoices/'+row['invoice_id'])['invoice']
            paid = sum(p.get('total_completed_amount_money',{}).get('amount',0)
                       for p in invoice.get('payment_requests',[])
                       if p.get('total_completed_amount_money',{}).get('currency')=='JPY')
            if invoice.get('status') != 'PAID' or paid != row['amount']:
                continue
            c = app.con()
            try:
                c.execute("UPDATE reservation_changes SET state='PAID',paid_at=?,error=NULL WHERE id=?",
                          (app.now_iso(),row['id']))
                c.commit()
            finally:
                c.close()
        except Exception:
            continue  # Leave visibly outstanding; a failed lookup never marks it paid.
