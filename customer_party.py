"""Authenticated guest party changes; money is reconciled explicitly by staff."""
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


class ChangeError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS customer_party_changes(
        id TEXT PRIMARY KEY, reservation_id INTEGER NOT NULL,
        request_id TEXT NOT NULL, old_party INTEGER NOT NULL,
        new_party INTEGER NOT NULL CHECK(new_party>=2), unit_price INTEGER NOT NULL,
        old_amount INTEGER NOT NULL, new_amount INTEGER NOT NULL,
        fee INTEGER NOT NULL, fee_total INTEGER NOT NULL,
        paid_amount INTEGER NOT NULL, balance INTEGER NOT NULL,
        state TEXT NOT NULL, created_at TEXT NOT NULL, settled_at TEXT,
        UNIQUE(reservation_id,request_id))''')


def latest(c, rid):
    row = c.execute('SELECT * FROM customer_party_changes WHERE reservation_id=? '
                    'ORDER BY rowid DESC LIMIT 1', (rid,)).fetchone()
    return dict(row) if row else None


def summary(c, rid):
    row = latest(c, rid)
    return {'customer_party_change': row}


def open_balance(c, rid):
    row = latest(c, rid)
    return bool(row and row['state'] == 'OPEN')


def paid_history(c, rid):
    return bool(c.execute('SELECT 1 FROM customer_party_changes '
                         'WHERE reservation_id=? AND paid_amount>0 LIMIT 1', (rid,)).fetchone())


def authenticate(app, c, token):
    if not isinstance(token, str) or not re.fullmatch(r'[0-9]{1,18}\.[0-9a-f]{64}', token):
        raise ChangeError('リンクが無効です。店舗へお問い合わせください。', 403)
    r = c.execute('SELECT * FROM reservations WHERE id=?', (int(token.split('.')[0]),)).fetchone()
    if not r or not app.ADMIN_TOKEN or not hmac.compare_digest(app.cancellation_token(r), token):
        raise ChangeError('リンクが無効です。店舗へお問い合わせください。', 403)
    return r


def now(app):
    return app.datetime.now(JST)


def check_booking(app, c, r):
    visit = app.parse_dt(r['visit_at']).replace(tzinfo=JST)
    if now(app) >= visit:
        raise ChangeError('人数変更の受付期限を過ぎています。店舗へお問い合わせください。', 410)
    bank = (r['status'] == 'PENDING' and r['payment_method'] == 'BANK'
            and not r['payment_source'] and not r['square_invoice_id'] and not r['square_order_id'])
    if not (r['status'] == 'CONFIRMED' or bank):
        raise ChangeError('この予約は人数変更できません。カード決済中の場合は決済を完了してください。')
    if r['vip_through'] or r['seating_area'] not in ('COUNTER', *app.ROOMS):
        raise ChangeError('この予約の人数変更は店舗へお問い合わせください。')
    if app.reservation_changes.records(c, r['id']):
        raise ChangeError('店舗で差額請求を手配中です。人数変更は店舗へお問い合わせください。')
    if c.execute('SELECT 1 FROM cancellation_refunds WHERE reservation_id=?', (r['id'],)).fetchone():
        raise ChangeError('返金手続き中です。人数変更は店舗へお問い合わせください。')


def proposal(app, c, r, party):
    if type(party) is not int or not 2 <= party <= 8:
        raise ChangeError('ご予約人数は2名以上8名以下で入力してください。', 400)
    check_booking(app, c, r)
    if not app.public_party_allowed(r['seating_area'], party):
        raise ChangeError('現在のお席ではその人数に変更できません。店舗へお問い合わせください。')
    # A decrease never consumes capacity; a same-party request is a safe no-op.
    if party > r['party_size']:
        ok, message = app.availability_check(c, r['seating_area'], r['visit_at'], party,
            r['counter_round'], int(r['duration_minutes'] or 150), r['id'],
            allow_custom_start=r['seating_area'] in app.ROOMS, restore_existing=True)
        if not ok:
            raise ChangeError(message)
    previous = latest(c, r['id'])
    if previous and previous['new_amount'] != r['amount']:
        raise ChangeError('店舗で金額を調整しています。人数変更は店舗へお問い合わせください。')
    if previous:
        unit, fees = previous['unit_price'], previous['fee_total']
    else:
        if not r['party_size'] or r['amount'] <= 0 or r['amount'] % r['party_size']:
            raise ChangeError('料金の確認が必要です。店舗へお問い合わせください。')
        unit, fees = r['amount'] // r['party_size'], 0
    paid = (previous['paid_amount'] if previous and previous['state'] == 'OPEN'
            else r['amount']) if r['payment_source'] in ('SQUARE', 'BANK') else 0
    visit_day = app.parse_dt(r['visit_at']).date()
    fee = (max(0, r['party_size'] - party) * unit
           if r['status'] == 'CONFIRMED' and now(app).date() >= visit_day - timedelta(days=3) else 0)
    total = party * unit + fees + fee
    return {'old_party': r['party_size'], 'party_size': party,
            'old_amount': r['amount'], 'unit_price': unit, 'course_amount': party * unit,
            'fee': fee, 'fee_total': fees + fee, 'amount': total, 'paid_amount': paid,
            'balance': total - paid if paid else 0,
            'payment_pending': r['status'] == 'PENDING',
            'pay_at_restaurant': not paid and r['status'] == 'CONFIRMED'}


def proof(app, r, data, issued):
    payload = {'id': r['id'], 'updated_at': r['updated_at'], 'visit_at': r['visit_at'],
               'area': r['seating_area'], 'status': r['status'], 'issued': issued, **data}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hmac.new(app.ADMIN_TOKEN.encode(), b'customer-party-v1|' + raw, hashlib.sha256).hexdigest()


def public_row(app, c, r):
    row = latest(c, r['id'])
    return {'reservation_id': r['id'], 'name': r['guest_name'], 'visit_at': r['visit_at'],
            'party_size': r['party_size'], 'amount': r['amount'], 'course_name': r['course_name'],
            'seat': app.customer_seating_label(r), 'status': r['status'],
            'min_party': 4 if r['seating_area'] == 'PRIVATE3' else 2,
            'max_party': 4 if r['seating_area'] in ('PRIVATE1', 'PRIVATE2') else 8,
            'bank_due_at': r['bank_due_at'] if r['payment_method'] == 'BANK' else None,
            'balance': row['balance'] if row and row['state'] == 'OPEN' else 0,
            'fee_total': row['fee_total'] if row else 0,
            'settlement_pending': bool(row and row['state'] == 'OPEN')}


def handle(app, data, confirm=False):
    c = app.con()
    try:
        c.execute('BEGIN IMMEDIATE')
        r = authenticate(app, c, data.get('token'))
        request_id = data.get('request_id')
        if confirm and (not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', request_id)):
            raise ChangeError('画面を開き直してください。', 400)
        if confirm:
            existing = c.execute('SELECT new_party FROM customer_party_changes '
                'WHERE reservation_id=? AND request_id=?', (r['id'], request_id)).fetchone()
            if existing:
                if existing['new_party'] != data.get('party_size'):
                    raise ChangeError('処理済みの変更です。最新の予約内容をご確認ください。')
                return 200, {'ok': True, 'duplicate': True, **public_row(app, c, r)}
        check_booking(app, c, r)
        result = public_row(app, c, r)
        if data.get('party_size') is None:
            return 200, result
        change = proposal(app, c, r, data.get('party_size'))
        issued = data.get('issued') if confirm else int(now(app).timestamp())
        if confirm:
            if (type(issued) is not int or not 0 <= int(now(app).timestamp()) - issued <= 900
                    or data.get('accepted') is not True or not isinstance(data.get('quote'), str)
                    or not hmac.compare_digest(proof(app, r, change, issued), data['quote'])):
                raise ChangeError('予約内容または料金が更新されました。もう一度変更内容をご確認ください。')
            if change['party_size'] == r['party_size']:
                return 200, {'ok': True, **result}
            ident, ts = str(uuid.uuid4()), app.now_iso()
            state = 'OPEN' if change['balance'] else 'SETTLED'
            c.execute("UPDATE customer_party_changes SET state='SUPERSEDED' "
                      "WHERE reservation_id=? AND state='OPEN'", (r['id'],))
            c.execute('''INSERT INTO customer_party_changes
                (id,reservation_id,request_id,old_party,new_party,unit_price,old_amount,
                 new_amount,fee,fee_total,paid_amount,balance,state,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (ident,r['id'],request_id,r['party_size'],change['party_size'],change['unit_price'],
                 r['amount'],change['amount'],change['fee'],change['fee_total'],
                 change['paid_amount'],change['balance'],state,ts))
            c.execute('UPDATE reservations SET party_size=?,amount=?,staff_seen_at=NULL,updated_at=? '
                      'WHERE id=?', (change['party_size'],change['amount'],ts,r['id']))
            # Durable audit without polluting guest notes or claiming payment/refund completion.
            c.commit()
            return 200, {'ok': True, **public_row(app, c,
                c.execute('SELECT * FROM reservations WHERE id=?', (r['id'],)).fetchone())}
        return 200, {**result, 'proposal': change, 'issued': issued,
                     'quote': proof(app, r, change, issued)}
    except ChangeError as exc:
        c.rollback()
        return exc.status, {'error': str(exc)}
    finally:
        c.close()


def staff_list(c):
    return [dict(r) for r in c.execute('''SELECT ch.*,r.guest_name,r.visit_at,r.status AS booking_status
        FROM customer_party_changes ch JOIN reservations r ON r.id=ch.reservation_id
        WHERE ch.rowid=(SELECT MAX(x.rowid) FROM customer_party_changes x
                       WHERE x.reservation_id=ch.reservation_id)
        ORDER BY CASE WHEN ch.state='OPEN' THEN 0 ELSE 1 END,ch.created_at DESC LIMIT 100''')]


def settle(app, c, rid, data):
    row = latest(c, rid)
    r = c.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone()
    if (not row or row['state'] != 'OPEN' or row['id'] != data.get('change_id')
            or row['balance'] != data.get('expected_balance') or data.get('owner_confirmed') is not True
            or not r or r['status'] != 'CONFIRMED' or r['amount'] != row['new_amount']):
        raise ChangeError('精算対象が更新されています。再読み込みしてください。')
    c.execute("UPDATE customer_party_changes SET state='SETTLED',paid_amount=new_amount,"
              "settled_at=? WHERE id=?", (app.now_iso(),row['id']))
    c.execute('INSERT INTO payment_audit(reservation_id,source,amount,reference,confirmed_by,created_at) '
              'VALUES(?,?,?,?,?,?)', (rid,'PARTY_ADJUSTMENT',row['balance'],row['id'],'朝倉',app.now_iso()))
    return {'ok': True}
