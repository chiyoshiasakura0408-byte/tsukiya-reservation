"""Replace an unpaid invoice while keeping the reservation and its audit trail."""
import threading
import uuid
from datetime import datetime, timedelta, timezone

LOCK = threading.Lock()
JST = timezone(timedelta(hours=9))


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS unpaid_invoice_changes(
        id TEXT PRIMARY KEY, reservation_id INTEGER NOT NULL,
        old_invoice_id TEXT NOT NULL UNIQUE, old_invoice_url TEXT,
        old_party INTEGER NOT NULL, new_party INTEGER NOT NULL,
        old_amount INTEGER NOT NULL, new_amount INTEGER NOT NULL,
        created_at TEXT NOT NULL)''')


def invoice_suffix(c, r):
    change = c.execute('SELECT * FROM unpaid_invoice_changes WHERE reservation_id=? '
                       'ORDER BY rowid DESC LIMIT 1', (r['id'],)).fetchone()
    if change and change['new_party'] == r['party_size'] and change['new_amount'] == r['amount']:
        return '-revision-' + change['id']
    return ''


def replace(app, rid, data):
    """Cancel only a verified unpaid invoice; persist the revision before issuing anew."""
    with LOCK:
        c = app.con()
        try:
            c.execute('BEGIN IMMEDIATE')
            saved = c.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone()
            if not saved:
                raise ValueError('予約が見つかりません')
            r = dict(saved)
            party = data.get('party_size')
            old_id = data.get('expected_square_invoice_id')
            prior = c.execute('SELECT * FROM unpaid_invoice_changes WHERE old_invoice_id=? '
                              'AND reservation_id=?', (old_id, rid)).fetchone()
            if (prior and prior['new_party'] == party and r['party_size'] == party
                    and r['amount'] == prior['new_amount']):
                return r
            for key in ('party_size', 'amount', 'visit_at', 'seating_area', 'square_invoice_id', 'phone', 'email'):
                if data.get('expected_' + key) != r.get(key):
                    raise ValueError('予約内容が更新されています。画面を開き直してください')
            if (r['source'] != 'PHONE' or r['status'] != 'INVOICED' or not old_id
                    or r.get('payment_source') or r.get('payment_confirmed_at')
                    or r.get('payment_method') == 'BANK'
                    or c.execute('SELECT 1 FROM payment_audit WHERE reservation_id=?', (rid,)).fetchone()):
                raise ValueError('未決済の電話予約請求のみ変更できます')
            if type(party) is not int or not 2 <= party <= 8 or party == r['party_size']:
                raise ValueError('変更後の人数を2名以上8名以下で選択してください')
            if not app.public_party_allowed(r['seating_area'], party):
                raise ValueError('この個室ではその人数を受け付けられません')
            now = datetime.now(JST)
            visit = app.parse_dt(r['visit_at']).replace(tzinfo=JST)
            if visit <= now:
                raise ValueError('来店前の予約のみ変更できます')
            if party < r['party_size'] and now.date() >= visit.date() - timedelta(days=3):
                raise ValueError('キャンセル料の対象期間です。減員の精算を店舗で確認してください')
            if not r['party_size'] or r['amount'] <= 0 or r['amount'] % r['party_size']:
                raise ValueError('1名様の料金を確認してください')
            if not r.get('phone') and not r.get('email'):
                raise ValueError('請求先の連絡先がありません')
            if party > r['party_size']:
                ok, message = app.availability_check(c, r['seating_area'], r['visit_at'], party,
                    r['counter_round'], r['duration_minutes'] or 150, rid,
                    allow_custom_start=True, restore_existing=True)
                if not ok:
                    raise ValueError(message)
            app.booking_blocks.ensure_allowed(c, r.get('phone'), r.get('email'), r.get('customer_id'), staff=True)
            invoice = app.square('/v2/invoices/' + old_id)['invoice']
            if invoice.get('id') != old_id:
                raise ValueError('元の請求書を確認できません')
            paid = sum(p.get('total_completed_amount_money', {}).get('amount', 0)
                       for p in invoice.get('payment_requests', []))
            if paid or invoice.get('status') not in ('UNPAID', 'CANCELED'):
                raise ValueError('決済済み・決済処理中の可能性があります。請求を変更していません')
            if invoice['status'] == 'UNPAID':
                invoice = app.square('/v2/invoices/' + old_id + '/cancel',
                                     body={'version': invoice['version']})['invoice']
            if invoice.get('id') != old_id or invoice.get('status') != 'CANCELED':
                raise ValueError('旧請求の取消結果を確認できません。新しい請求は発行していません')
            amount = r['amount'] // r['party_size'] * party
            ident, ts = str(uuid.uuid4()), app.now_iso()
            c.execute('INSERT INTO unpaid_invoice_changes '
                '(id,reservation_id,old_invoice_id,old_invoice_url,old_party,new_party,old_amount,new_amount,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?)',
                (ident,rid,old_id,r.get('square_invoice_url'),r['party_size'],party,r['amount'],amount,ts))
            c.execute("UPDATE reservations SET party_size=?,amount=?,status='PENDING',"
                "square_customer_id=NULL,square_order_id=NULL,square_invoice_id=NULL,square_invoice_url=NULL,"
                "invoice_issued_at=NULL,invoice_payment_due_at=NULL,invoice_sms_status=NULL,"
                "invoice_sms_sid=NULL,invoice_sms_sent_at=NULL,invoice_sms_error=NULL,"
                "reminder_status=NULL,last_error=NULL,updated_at=? WHERE id=?", (party,amount,ts,rid))
            app.payment_terms.register(c, rid, ts)
            out = dict(c.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone())
            c.commit()
            return out
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
