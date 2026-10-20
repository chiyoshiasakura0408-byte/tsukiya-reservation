"""Run on the authorized Render service. Default is strictly read-only.

python scripts/owner_notice_recovery.py --reservation-id 29
python scripts/owner_notice_recovery.py --quota
python scripts/owner_notice_recovery.py --reservation-id 29 --resume \
    --evidence 'Incident evidence reference, timestamps, request IDs and reviewer'

Only use --confirmed-not-accepted after establishing that ALL previous sends
were rejected. It is not a substitute for missing/ambiguous historical evidence.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import owner_notifications


def inspect(db, reservation_id):
    c = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {'reservation_id': reservation_id, 'read_only': True}
        if {'concierge_outbox', 'owner_booking_notices'} <= tables:
            rows = [dict(r) for r in c.execute('''SELECT o.* FROM concierge_outbox o
                JOIN owner_booking_notices n ON n.outbox_id=o.id WHERE n.reservation_id=?''', (reservation_id,))]
            for row in rows:
                row.pop('payload', None)
                row.pop('user_id', None)
            result['notices'] = rows
            result['attempts'] = [dict(r) for r in c.execute('''SELECT a.* FROM owner_notice_attempts a
                JOIN owner_booking_notices n ON n.outbox_id=a.outbox_id WHERE n.reservation_id=?
                ORDER BY a.attempt''', (reservation_id,))] if 'owner_notice_attempts' in tables else []
        if 'reservation_message_links' in tables:
            result['accepted_message_ids'] = [r[0] for r in c.execute(
                'SELECT message_id FROM reservation_message_links WHERE reservation_id=?', (reservation_id,))]
        return result
    finally:
        c.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(Path(os.getenv('DATA_DIR', '.')) / 'tsukiya.sqlite'))
    parser.add_argument('--reservation-id', type=int, default=29)
    parser.add_argument('--quota', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--confirmed-not-accepted', action='store_true')
    parser.add_argument('--evidence', default='')
    args = parser.parse_args()
    if args.quota:
        print(json.dumps(owner_notifications.quota(), ensure_ascii=False, indent=2))
        return
    if not args.resume:
        print(json.dumps(inspect(args.db, args.reservation_id), ensure_ascii=False, indent=2))
        return
    quota = owner_notifications.quota()
    if not quota.get('available'):
        parser.error('業務LINEの配信枠を確認できません。再送は実施していません')
    import concierge
    c = concierge.connect(args.db)
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            owner_notifications.resume(c, args.reservation_id, args.evidence, args.confirmed_not_accepted)
            c.execute("DELETE FROM owner_notice_channel WHERE id=1 AND reason='monthly_quota'")
            # New bookings held before their first attempt can resume safely.
            c.execute("""UPDATE concierge_outbox SET state='pending',error=NULL,error_code=NULL,next_try=?
                WHERE kind='owner-new-booking' AND state='held' AND error_code='monthly_quota'
                AND attempts=0 AND first_try IS NULL""", (time.time(),))
        print(json.dumps({'reservation_id': args.reservation_id, 'state': 'pending',
                          'api_accepted': False, 'message': '再開待ち。API受付は別途確認してください'}, ensure_ascii=False))
    finally:
        c.close()


if __name__ == '__main__':
    main()
