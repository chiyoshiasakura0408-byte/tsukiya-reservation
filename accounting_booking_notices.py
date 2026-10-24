"""One accounting notice when a reservation is confirmed on its visit date (JST)."""


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS accounting_booking_events(
        reservation_id INTEGER PRIMARY KEY, created REAL NOT NULL,
        outbox_id TEXT)''')
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reservations'").fetchone():
        return
    # No historical backfill: only confirmations occurring after installation.
    c.execute('''CREATE TRIGGER IF NOT EXISTS accounting_booking_insert
        AFTER INSERT ON reservations
        WHEN NEW.status='CONFIRMED' AND COALESCE(NEW.course_name,'') NOT LIKE '%テスト%'
        AND substr(NEW.visit_at,1,10)=date('now','+9 hours')
        BEGIN INSERT OR IGNORE INTO accounting_booking_events
        VALUES(NEW.id,strftime('%s','now'),NULL); END''')
    c.execute('''CREATE TRIGGER IF NOT EXISTS accounting_booking_confirm
        AFTER UPDATE OF status ON reservations
        WHEN NEW.status='CONFIRMED' AND OLD.status!='CONFIRMED'
        AND COALESCE(NEW.course_name,'') NOT LIKE '%テスト%'
        AND substr(NEW.visit_at,1,10)=date('now','+9 hours')
        BEGIN INSERT OR IGNORE INTO accounting_booking_events
        VALUES(NEW.id,strftime('%s','now'),NULL); END''')


def queue(c):
    import concierge
    import line_delivery
    schema(c)
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reservations'").fetchone():
        return
    rows = c.execute('''SELECT r.* FROM accounting_booking_events e
        JOIN reservations r ON r.id=e.reservation_id
        WHERE e.outbox_id IS NULL AND r.status='CONFIRMED'
        AND substr(r.visit_at,1,10)=date('now','+9 hours')
        ORDER BY e.created,r.id LIMIT 50''').fetchall()
    for record in rows:
        r = dict(record)
        seat = {'COUNTER': '本店・カウンター', 'PRIVATE1': '別邸・個室①',
                'PRIVATE2': '別邸・個室②', 'PRIVATE3': '別邸・個室③'}.get(
                    r.get('seating_area'), '別邸・個室')
        payment = ('決済済み' if r.get('payment_confirmed_at') or r.get('paid_at')
                   else '当日精算' if r['source'] == 'DIRECT'
                   else '予約管理画面で確認')
        body = (f"【予約確定・経理連絡】\n予約ID：{r['id']}\n{r['guest_name']}様\n"
                f"日時：{r['visit_at'].replace('T', ' ')}\n人数：{r['party_size']}名\n"
                f"店舗：{seat}\nコース：{r.get('course_name') or '未設定'}\n"
                f"合計：¥{r.get('amount', 0):,}\n支払い：{payment}")
        # Same DB transaction as the event's outbox link; retries reuse its UUID.
        key = concierge.enqueue(c, line_delivery.TARGET,
                                [{'type': 'text', 'text': body}],
                                channel='owner', kind='accounting-booking-confirmed',
                                reservation_id=r['id'])
        c.execute('UPDATE accounting_booking_events SET outbox_id=? WHERE reservation_id=?',
                  (key, r['id']))


def ready(c, outbox_id):
    return bool(c.execute('''SELECT 1 FROM accounting_booking_events e
        JOIN reservations r ON r.id=e.reservation_id
        WHERE e.outbox_id=? AND r.status='CONFIRMED'
        AND substr(r.visit_at,1,10)=date('now','+9 hours')''', (outbox_id,)).fetchone())
