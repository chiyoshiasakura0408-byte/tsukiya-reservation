"""A single reservation holds both rounds without doubling its price."""
import json
import re
import booking_policy
from datetime import datetime, timedelta


def counter_assignments(app,c,day,clock):
    blocked={r[0] for r in c.execute("SELECT seat_number FROM seat_blocks WHERE visit_date=? AND time_text=? AND seating_area='COUNTER'",(day,clock))}
    rows=c.execute("SELECT * FROM reservations WHERE substr(visit_at,1,10)=? AND seating_area='COUNTER' AND (substr(visit_at,12,5)=? OR vip_through=1) AND status IN (?,?,?,?) ORDER BY id",(day,clock,*app.ACTIVE_STATUSES)).fetchall()
    assigned={}
    for row in rows:
        if row['vip_through']:
            for n in json.loads(row['vip_seats'] or '[]'):assigned[n]=row['id']
    for row in rows:
        if not row['vip_through']:
            free=[n for n in range(1,9) if n not in blocked and n not in assigned]
            for n in free[:row['party_size']]:assigned[n]=row['id']
    return assigned,blocked


def choose(app,c,day,area,party):
    if booking_policy.main_restaurant_sunday_closed(day,area):
        return None
    if area=='COUNTER':
        free=set(range(1,9))
        for clock in ('18:00','20:30'):
            assigned,blocked=counter_assignments(app,c,day,clock)
            free-=set(assigned)|blocked
        return sorted(free)[:party] if len(free)>=party else None
    if area not in app.ROOMS or not app.public_party_allowed(area,party):return None
    return [] if all(app.availability_check(c,area,day+'T'+clock,party,None,150)[0] for clock in ('18:00','20:30')) else None


def valid_start(app, day, clock, course):
    if not isinstance(clock,str) or not re.fullmatch(r'(?:(17|18|19|20):[0-5][0-9]|21:00)',clock):return False
    return app.bookable_course_key(day)==course and datetime.fromisoformat(str(day)+'T'+clock)>datetime.now(app.concierge.JST).replace(tzinfo=None)


def availability(app,day,party):
    c=app.con()
    try:
        result=[]
        for key,course in app.PUBLIC_COURSES.items():
            if key!=app.bookable_course_key(day):continue
            if datetime.fromisoformat(str(day)+'T23:00')<=datetime.now(app.concierge.JST).replace(tzinfo=None):continue
            for area in ('PRIVATE',):
                rooms=('COUNTER',) if area=='COUNTER' else app.ROOMS
                if any(app.public_party_allowed(room,party) and choose(app,c,str(day),room,party) is not None for room in rooms):
                    result.append({'course':key,'area':area,'time':'18:00','through':True})
        return result
    finally:c.close()


def staff_slot(app,c,area,visit_at,party):
    """Explicit staff time selection; private rooms only, 20:30 stays ordinary."""
    if area not in ('PRIVATE',*app.ROOMS):
        raise ValueError('時間指定・通し予約は個室のみです。カウンターは1部・2部をお選びください。')
    if not isinstance(visit_at,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}',visit_at):
        raise ValueError('来店日時を確認してください。')
    visit=datetime.fromisoformat(visit_at)
    course=app.bookable_course(visit.date())
    key=app.bookable_course_key(visit.date())
    clock=visit_at[11:]
    if not key or not valid_start(app,visit.date(),clock,key):
        raise ValueError('受付期間内の17:00〜21:00の未来の時刻を指定してください。')
    through=clock!='20:30'
    rooms=app.ROOMS if area=='PRIVATE' else (area,)
    room=next((r for r in rooms if app.public_party_allowed(r,party) and
        (choose(app,c,str(visit.date()),r,party) is not None if through else
         app.availability_check(c,r,visit_at,party,None,150)[0])),None)
    if not room:
        raise ValueError('ご希望の時間帯を確保できる個室がありません。')
    return room,23*60-visit.hour*60-visit.minute if through else 150,through
