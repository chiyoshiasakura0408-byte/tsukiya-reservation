"""Versioned 24-hour terms. Existing reservations keep their agreed deadlines."""
from datetime import datetime, timedelta, timezone

POLICY = 'UNIFIED_24H_V1'
HOURS = 24


def register(c, rid, started_at):
    due = (datetime.fromisoformat(started_at) + timedelta(hours=HOURS)).isoformat()
    c.execute('UPDATE reservations SET payment_term_policy=?,payment_term_due_at=? WHERE id=?',
              (POLICY, due, rid))
    return due


def expire(app):
    """Only the new policy; never shorten legacy terms or cancel uncertain payments."""
    c = app.con()
    count = 0
    try:
        c.execute('BEGIN IMMEDIATE')
        rows = c.execute("SELECT * FROM reservations WHERE payment_term_policy=? "
                         "AND payment_term_due_at<=? AND source IN ('WEB','PHONE') "
                         "AND status IN ('PENDING','INVOICED','ERROR') "
                         "AND payment_source IS NULL AND payment_confirmed_at IS NULL "
                         "AND NOT EXISTS (SELECT 1 FROM payment_audit a WHERE a.reservation_id=reservations.id) "
                         "ORDER BY id LIMIT 50", (POLICY, app.now_iso())).fetchall()
        for row in rows:
            r = dict(row)
            if r['square_invoice_id']:
                try:
                    invoice = app.square('/v2/invoices/' + r['square_invoice_id'])['invoice']
                    if invoice.get('id') != r['square_invoice_id']:
                        continue
                    if invoice.get('status') == 'UNPAID':
                        if app.square_payment_deadline(invoice, r) is None:
                            continue
                        app.square('/v2/invoices/' + r['square_invoice_id'] + '/cancel',
                                   body={'version': invoice['version']})
                    elif invoice.get('status') not in ('CANCELED', 'CANCELLED'):
                        continue
                except Exception:
                    # A failed or ambiguous provider read must not release the seat.
                    continue
            now = app.now_iso()
            c.execute("UPDATE reservations SET status='CANCELLED',staff_seen_at=NULL,"
                      "cancellation_reason='PAYMENT_EXPIRED',cancelled_at=?,updated_at=? WHERE id=?",
                      (now, now, r['id']))
            if r['payment_method'] == 'BANK':
                app.bank_transfer.enqueue(c, r, app, 'cancelled')
            count += 1
        c.commit()
        return count
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
