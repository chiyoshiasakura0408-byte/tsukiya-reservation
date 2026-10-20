import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())
import booking_policy
import run
import vip_slots


class MainRestaurantSundayClosureTest(unittest.TestCase):
    def test_all_sundays_and_boundary_dates(self):
        day = date(2026, 10, 21)
        sundays = 0
        while day <= date(2027, 3, 20):
            closed = booking_policy.main_restaurant_sunday_closed(day, 'COUNTER')
            self.assertEqual(closed, day.weekday() == 6, str(day))
            sundays += int(closed)
            for area in ('PRIVATE', 'PRIVATE1', 'PRIVATE2', 'PRIVATE3'):
                self.assertFalse(booking_policy.main_restaurant_sunday_closed(day, area))
            day += timedelta(days=1)
        self.assertEqual(sundays, 21)
        for day in ('2026-10-18', '2027-03-20', '2027-03-21', '2027-10-24'):
            self.assertFalse(booking_policy.main_restaurant_sunday_closed(day, 'COUNTER'))

    def test_shared_booking_validation_and_vip_capacity(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run, 'DB', tmp + '/db'):
            c = run.con()
            try:
                for day in ('2026-10-25', '2026-11-15', '2027-03-14'):
                    for clock, rnd in (('18:00', 1), ('20:30', 2), ('19:00', 2)):
                        ok, message = run.availability_check(c, 'COUNTER', day + 'T' + clock, 2, rnd)
                        self.assertFalse(ok)
                        self.assertIn('日曜日の全時間帯', message)
                    self.assertIsNone(vip_slots.choose(run, c, day, 'COUNTER', 2))
                    for area, party in (('PRIVATE1', 2), ('PRIVATE2', 2), ('PRIVATE3', 4)):
                        for clock in ('18:00', '20:30'):
                            self.assertTrue(run.availability_check(c, area, day + 'T' + clock, party)[0])
                for day in ('2026-11-14', '2026-11-16', '2027-03-20', '2027-03-21'):
                    for clock, rnd in (('18:00', 1), ('20:30', 2)):
                        self.assertTrue(run.availability_check(c, 'COUNTER', day + 'T' + clock, 2, rnd)[0])
                    self.assertEqual(vip_slots.choose(run, c, day, 'COUNTER', 2), [1, 2])
                self.assertEqual(c.execute('SELECT count(*) FROM reservations').fetchone()[0], 0)
            finally:
                c.close()


if __name__ == '__main__':
    unittest.main()
