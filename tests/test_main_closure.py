import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())
import booking_policy
import run
import vip_slots


class MainClosureTests(unittest.TestCase):
    def test_all_days_and_sittings_including_november_30(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run, 'DB', tmp + '/db'):
            c = run.con()
            try:
                day = date(2026, 10, 22)
                while day <= date(2026, 11, 30):
                    for clock, rnd in (('18:00', 1), ('20:30', 2), ('19:00', 1)):
                        ok, reason = run.availability_check(c, 'COUNTER', str(day)+'T'+clock, 2, rnd)
                        self.assertFalse(ok, (day, clock))
                        self.assertIn('11月30日', reason)
                    self.assertIsNone(vip_slots.choose(run, c, str(day), 'COUNTER', 2))
                    for area, party in (('PRIVATE1', 2), ('PRIVATE2', 2), ('PRIVATE3', 4)):
                        for clock in ('18:00', '20:30'):
                            self.assertTrue(run.availability_check(c, area, str(day)+'T'+clock, party)[0])
                    day += timedelta(days=1)
                for day in ('2026-10-21', '2026-12-01', '2026-12-05'):
                    for clock, rnd in (('18:00', 1), ('20:30', 2)):
                        self.assertTrue(run.availability_check(c, 'COUNTER', day+'T'+clock, 2, rnd)[0])
                    self.assertEqual(vip_slots.choose(run, c, day, 'COUNTER', 2), [1, 2])
                self.assertIn('日曜日', booking_policy.main_restaurant_closure_reason('2026-12-06', 'COUNTER'))
                self.assertEqual(c.execute('SELECT count(*) FROM reservations').fetchone()[0], 0)
            finally:
                c.close()


if __name__ == '__main__':
    unittest.main()
