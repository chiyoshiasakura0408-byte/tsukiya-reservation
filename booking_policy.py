"""Server-owned reservation category and beverage policy; never a client price override."""
from datetime import date, datetime


def main_restaurant_sunday_closed(day, seating_area):
    """Owner instruction: no main-restaurant Sunday bookings through 2027-03-20."""
    if seating_area != 'COUNTER':
        return False
    day = date.fromisoformat(str(day)[:10])
    return date(2026, 10, 21) <= day <= date(2027, 3, 20) and day.weekday() == 6


def details(reservation):
    r = dict(reservation)
    through = bool(r.get('vip_through'))
    vip = through or bool(r.get('vip_booking')) or r.get('source') == 'CONCIERGE_LINE'
    category = 'VIP通し予約' if through else 'VIP通常予約' if vip else 'Englishコンシェルジュ' if r.get('booking_language') == 'en' else '通常予約'
    try:
        day = datetime.fromisoformat(r.get('visit_at', '')).date()
        winter = day.month in (1, 2, 12) or day.month == 11 and day.day >= 10 or day.month == 3 and day.day <= 20
    except (ValueError, TypeError):
        winter = False
    included = winter and not through
    if through:
        ja = 'VIP通し予約は冬季ドリンクオールイン適用外です。お飲み物代は別途、当日精算いたします。'
        en = 'The winter beverage inclusion does not apply to VIP extended reservations. All beverages are charged separately at the restaurant.'
    elif included:
        ja = '冬季コースは所定のお飲み物代込みです。一部別料金のお飲み物は当日精算いたします。'
        en = 'The winter course includes designated beverages. Some drinks carry an additional charge, payable at the restaurant.'
    else:
        ja = 'お飲み物代は別途、当日精算いたします。'
        en = 'Beverages are charged separately at the restaurant.'
    return {'reservation_category': category, 'beverages_included': included, 'beverage_policy': ja, 'beverage_policy_en': en}


def approved_customer(c, customer_id):
    if type(customer_id) is not int or c.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('vip_access','concierge_customers')").fetchone()[0] != 2:
        return False
    return bool(c.execute("SELECT 1 FROM vip_access a JOIN concierge_customers u ON u.user_id=a.user_id WHERE a.customer_id=? AND a.status='approved' AND u.active=1 AND u.customer_id=a.customer_id", (customer_id,)).fetchone())


def require_public(data):
    if any(data.get(key) not in (None, False, 0, '', 150 if key == 'duration_minutes' else None) for key in ('through', 'vip_through', 'time_specified', 'duration_minutes')):
        raise ValueError('通し予約はVIP専用です。通常予約は18:00・20:30のみです。')
