"""First-party attribution: no raw referrers, IPs or customer data in event storage."""
import re
import time
import threading
from collections import deque
from datetime import datetime, date, timedelta, timezone

SOURCES = {'google_maps':'Googleマップ','google_search':'Google検索','google_ads':'Google広告',
           'instagram':'Instagram','line':'LINE','other':'その他の外部サイト','unknown':'直接・不明'}
EVENTS = {'visit','booking_click','booking_start'}
LOCK = threading.Lock()
RECENT = deque()

def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS marketing_sessions(
        id TEXT PRIMARY KEY, source TEXT NOT NULL, created_at TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS marketing_events(
        session_id TEXT NOT NULL, event TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(session_id,event))''')
    c.execute('CREATE INDEX IF NOT EXISTS marketing_event_date ON marketing_events(created_at)')
    c.execute('''CREATE TABLE IF NOT EXISTS marketing_reservations(
        reservation_id INTEGER PRIMARY KEY, source TEXT NOT NULL, session_id TEXT,
        excluded INTEGER NOT NULL DEFAULT 0, attended_at TEXT)''')

def session_id(value):
    return value if isinstance(value,str) and re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',value) else None

def record(c, data, now):
    if not isinstance(data,dict): raise ValueError('invalid event')
    sid = session_id(data.get('session_id'))
    source = data.get('source')
    event = data.get('event')
    if not sid or source not in SOURCES or event not in EVENTS: raise ValueError('invalid event')
    with LOCK:
        t=time.monotonic()
        while RECENT and RECENT[0]<t-60: RECENT.popleft()
        if len(RECENT)>=300: return False
        RECENT.append(t)
    c.execute('INSERT OR IGNORE INTO marketing_sessions VALUES(?,?,?)',(sid,source,now))
    c.execute('INSERT OR IGNORE INTO marketing_events VALUES(?,?,?)',(sid,event,now))
    return True

def bind(c, rid, data, now, test=False):
    data = data if isinstance(data,dict) else {}
    sid=session_id(data.get('session_id'))
    source=data.get('source') if data.get('source') in SOURCES else 'unknown'
    if sid:
        c.execute('INSERT OR IGNORE INTO marketing_sessions VALUES(?,?,?)',(sid,source,now))
        source=c.execute('SELECT source FROM marketing_sessions WHERE id=?',(sid,)).fetchone()['source']
    else: source='unknown'
    c.execute('INSERT OR IGNORE INTO marketing_reservations(reservation_id,source,session_id,excluded) VALUES(?,?,?,?)',
              (rid,source,sid,int(test)))

def period(q):
    today=datetime.now(timezone(timedelta(hours=9))).date()
    start=date.fromisoformat(q.get('start',[today.replace(day=1).isoformat()])[0])
    end=date.fromisoformat(q.get('end',[today.isoformat()])[0])
    if end<start or (end-start).days>366: raise ValueError('期間は最大367日です')
    tz=timezone(timedelta(hours=9))
    lo=datetime.combine(start,datetime.min.time(),tz).astimezone(timezone.utc).isoformat()
    hi=datetime.combine(end+timedelta(days=1),datetime.min.time(),tz).astimezone(timezone.utc).isoformat()
    return start.isoformat(),end.isoformat(),lo,hi

def report(c,q):
    start,end,lo,hi=period(q)
    rows={s:dict(source=s,label=label,visit=0,booking_click=0,booking_start=0,requests=0,paid=0,
        paid_guests=0,paid_amount=0,confirmed=0,confirmed_guests=0,cancelled=0,refunded_amount=0,
        refund_pending_amount=0,attended=0,attended_guests=0) for s,label in SOURCES.items()}
    for r in c.execute('''SELECT s.source,e.event,COUNT(*) n FROM marketing_events e
        JOIN marketing_sessions s ON s.id=e.session_id WHERE e.created_at>=? AND e.created_at<?
        AND NOT EXISTS(SELECT 1 FROM marketing_reservations m WHERE m.session_id=s.id AND m.excluded=1)
        GROUP BY s.source,e.event''',(lo,hi)):
        rows[r['source']][r['event']]=r['n']
    bookings=c.execute('''SELECT r.id,r.source booking_source,r.created_at,r.visit_at,r.party_size,r.amount,
        r.status,r.course_name,r.payment_source,r.payment_confirmed_at,COALESCE(m.source,'unknown') source,
        COALESCE(m.excluded,0) excluded,m.attended_at,f.amount refund_amount,f.status refund_status
        FROM reservations r LEFT JOIN marketing_reservations m ON m.reservation_id=r.id
        LEFT JOIN cancellation_refunds f ON f.reservation_id=r.id
        WHERE r.created_at>=? AND r.created_at<? ORDER BY r.created_at DESC''',(lo,hi)).fetchall()
    details=[]
    for r in bookings:
        r=dict(r)
        # Historic one-yen test payments are never production acquisition.
        excluded=bool(r['excluded'] or r['amount']<=1 or 'テスト' in str(r['course_name'] or ''))
        details.append(dict(id=r['id'],source=r['source'],booking_source=r['booking_source'],created_at=r['created_at'],
            visit_at=r['visit_at'],status=r['status'],excluded=excluded,attended=bool(r['attended_at'])))
        if excluded: continue
        row=rows.get(r['source'],rows['unknown'])
        row['requests']+=1
        paid=bool(r['payment_confirmed_at'] and r['payment_source'] in ('SQUARE','BANK'))
        if paid:
            row['paid']+=1; row['paid_guests']+=r['party_size']; row['paid_amount']+=r['amount']
        if r['status']=='CONFIRMED':
            row['confirmed']+=1; row['confirmed_guests']+=r['party_size']
        if r['status']=='CANCELLED': row['cancelled']+=1
        if r['refund_status']=='COMPLETED': row['refunded_amount']+=r['refund_amount'] or 0
        elif r['refund_status'] and r['refund_status']!='NONE': row['refund_pending_amount']+=r['refund_amount'] or 0
        if r['attended_at']:
            row['attended']+=1;row['attended_guests']+=r['party_size']
    return dict(start=start,end=end,rows=list(rows.values()),reservations=details,
                basis='日本時間の予約受付日別。訪問・クリック・予約開始はイベント発生日別。入金額は前受金であり来店日売上ではありません。直接予約の確定は入金に含めません。')

def update(c,data,now):
    if not isinstance(data,dict) or type(data.get('id')) is not int: raise ValueError('予約IDが不正です')
    r=c.execute('SELECT * FROM reservations WHERE id=?',(data['id'],)).fetchone()
    if not r: raise ValueError('予約が見つかりません')
    c.execute("INSERT OR IGNORE INTO marketing_reservations(reservation_id,source) VALUES(?,'unknown')",(r['id'],))
    action=data.get('action')
    if action=='exclude':
        if type(data.get('value')) is not bool: raise ValueError('設定が不正です')
        c.execute('UPDATE marketing_reservations SET excluded=? WHERE reservation_id=?',(int(data['value']),r['id']))
    elif action=='attend':
        if type(data.get('value')) is not bool: raise ValueError('設定が不正です')
        if data['value'] and (r['status']!='CONFIRMED' or r['visit_at'][:10]>datetime.now(timezone(timedelta(hours=9))).date().isoformat()):
            raise ValueError('本日以前の確定予約のみ来店確認できます')
        c.execute('UPDATE marketing_reservations SET attended_at=? WHERE reservation_id=?',(now if data['value'] else None,r['id']))
    else: raise ValueError('操作が不正です')
