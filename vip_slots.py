"""A single reservation holds both rounds without doubling its price."""
import json
import re
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
    if area=='COUNTER':
        free=set(range(1,9))
        for clock in ('18:00','20:30'):
            assigned,blocked=counter_assignments(app,c,day,clock)
            free-=set(assigned)|blocked
        return sorted(free)[:party] if len(free)>=party else None
    if area not in app.ROOMS or not app.public_party_allowed(area,party):return None
    return [] if all(app.availability_check(c,area,day+'T'+clock,party,None,150)[0] for clock in ('18:00','20:30')) else None


def valid_start(app, day, clock, course):
    if not isinstance(clock,str) or not re.fullmatch(r'(18|19|20|21|22):[0-5][0-9]',clock):return False
    return app.bookable_course(day)==app.PUBLIC_COURSES.get(course) and datetime.fromisoformat(str(day)+'T'+clock)>datetime.now(app.concierge.JST).replace(tzinfo=None)


def availability(app,day,party):
    c=app.con()
    try:
        result=[]
        for key,course in app.PUBLIC_COURSES.items():
            if course!=app.bookable_course(day):continue
            if datetime.fromisoformat(str(day)+'T23:00')<=datetime.now(app.concierge.JST).replace(tzinfo=None):continue
            for area in ('COUNTER','PRIVATE'):
                rooms=('COUNTER',) if area=='COUNTER' else app.ROOMS
                if any(app.public_party_allowed(room,party) and choose(app,c,str(day),room,party) is not None for room in rooms):
                    result.append({'course':key,'area':area,'time':'18:00','through':True})
        return result
    finally:c.close()
