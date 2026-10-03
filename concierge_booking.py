"""One-time LINE booking capabilities; never creates an invoice or charges a card."""
import hashlib
import json
import re
import time
from datetime import date
import concierge


def booking(app, token, data=None):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,60}', token):
        raise ValueError('ご案内のリンクをご確認ください。')
    hashed = hashlib.sha256(token.encode()).hexdigest()
    setup = concierge.connect(app.DB); setup.close()
    c = app.con()
    try:
        c.execute('BEGIN IMMEDIATE')
        proposal = c.execute('SELECT * FROM concierge_proposals WHERE hash=?', (hashed,)).fetchone()
        if not proposal:
            raise ValueError('ご案内のリンクをご確認ください。')
        if proposal['reservation_id']:
            row = c.execute('SELECT * FROM reservations WHERE id=?', (proposal['reservation_id'],)).fetchone()
            return {'reservation_id':row['id'], 'status':row['status'], 'confirmed':row['status']=='CONFIRMED'}
        if proposal['expires'] < time.time():
            raise ValueError('ご案内の有効期限が過ぎました。LINEより再度空席をご確認ください。')
        if concierge.setting(c, 'enabled') != '1':
            raise ValueError('現在コンシェルジュの予約受付を休止しております。店舗へお問い合わせください。')
        slot = json.loads(proposal['slot'])
        course = app.PUBLIC_COURSES.get(slot['course'])
        if not course:
            raise ValueError('このコースは現在ご案内しておりません。')
        result = {**slot, 'course_name':course[0], 'amount':course[1]*slot['party_size'], 'prepayment_required':False}
        if data is None:
            return result
        if data.get('cancellation_policy_accepted') is not True:
            raise ValueError('キャンセルポリシーをご確認のうえ、同意してください。')
        fields = {}
        for key, maximum in (('guest_name',80),('phone',50),('email',254),('guest_note',1000)):
            value = data.get(key,'')
            if not isinstance(value,str) or len(value)>maximum:
                raise ValueError('入力内容をご確認ください。')
            fields[key]=value.strip()
        if not fields['guest_name'] or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',fields['email']) or not re.fullmatch(r'[+\d ()-]{7,50}',fields['phone']):
            raise ValueError('お名前・電話番号・メールアドレスをご確認ください。')
        note,celebrations,plate=app.guest_requests(data)
        fields['guest_note']=note
        day = date.fromisoformat(slot['date'])
        if not app.public_slot_allowed(day, slot['time'], slot['course']):
            raise ValueError('この日時は現在ご予約いただけません。')
        area = slot['area']
        party = slot['party_size']
        visit_at = slot['date']+'T'+slot['time']
        round_number = 1 if slot['time']=='18:00' else 2
        choices = app.ROOMS if area=='PRIVATE' else ('COUNTER',)
        room = next((r for r in choices if app.public_party_allowed(r,party) and app.availability_check(c,r,visit_at,party,round_number if r=='COUNTER' else None,150)[0]),None)
        if not room:
            raise ValueError('申し訳ございません。お席が埋まってしまいました。LINEより別のお席をご確認ください。')
        profile = concierge.profile(c,proposal['user_id'])
        now = app.now_iso()
        if profile:
            customer_id = profile['id']
        else:
            c.execute('INSERT OR IGNORE INTO customers(match_key,name,phone,email,created_at,updated_at) VALUES(?,?,?,?,?,?)', ('concierge:'+proposal['user_id'],fields['guest_name'],fields['phone'],fields['email'],now,now))
            customer_id = c.execute('SELECT id FROM customers WHERE match_key=?',('concierge:'+proposal['user_id'],)).fetchone()[0]
            c.execute('UPDATE concierge_customers SET customer_id=? WHERE user_id=?',(customer_id,proposal['user_id']))
        cursor = c.execute('''INSERT INTO reservations(source,guest_name,phone,email,visit_at,party_size,course_name,amount,seating_area,counter_round,duration_minutes,status,created_at,updated_at,guest_note,customer_id,public_request_id,cancellation_policy_accepted_at)
            VALUES('CONCIERGE_LINE',?,?,?,?,?,?,?,?,?,150,'CONFIRMED',?,?,?,?,?,?)''',
            (fields['guest_name'],fields['phone'],fields['email'],visit_at,party,course[0],result['amount'],room,round_number if room=='COUNTER' else None,now,now,fields['guest_note'],customer_id,'concierge:'+hashed,now))
        rid = cursor.lastrowid
        c.execute('UPDATE reservations SET celebration_items=?,plate_message=? WHERE id=?',(celebrations,plate,rid))
        c.execute('UPDATE concierge_proposals SET reservation_id=? WHERE hash=?',(rid,hashed))
        concierge.enqueue(c,proposal['user_id'],[concierge.text_message(f"ご予約を承りました。\n{slot['date']} {slot['time']}・{party}名様\n受付番号 {rid}\n前受けのお支払いはございません。ご来店を心よりお待ち申し上げております。")])
        row = dict(c.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone())
        c.commit()
    finally:
        c.close()
    import guest_service
    guest_service.sync_requests(app)
    app.deliver_confirmation(row)
    return {'reservation_id':rid,'status':'CONFIRMED','confirmed':True}
