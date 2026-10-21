import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())
import run
import availability_display


class AvailabilityDisplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.object(run, 'DB', self.tmp.name+'/db')
        env.start()
        self.addCleanup(env.stop)
        self.c = run.con()
        self.addCleanup(self.c.close)

    def marks(self, day='2026-12-01', party=2):
        slots = {area: {clock: run.public_party_allowed(area, party) and
            run.availability_check(self.c, area, day+'T'+clock, party,
                                   rnd if area == 'COUNTER' else None)[0]
            for clock, rnd in (('18:00', 1), ('20:30', 2))}
            for area in ('COUNTER', *run.ROOMS)}
        return availability_display.marks(run, self.c, day, slots)

    def block(self, area, seat=1):
        self.c.execute("INSERT INTO seat_blocks(visit_date,time_text,seating_area,seat_number,note,created_at) VALUES(?,?,?,?,?,?)",
                       ('2026-12-01', '18:00', area, seat, '', 'now'))

    def test_counter_threshold_before_requested_party_and_sittings(self):
        self.assertEqual(self.marks()['COUNTER']['18:00'], '○')
        for seat in range(1, 4): self.block('COUNTER', seat)
        self.assertEqual(self.marks()['COUNTER']['18:00'], '○')  # five remain
        self.block('COUNTER', 4)
        self.assertEqual(self.marks()['COUNTER']['18:00'], '△')
        self.assertEqual(self.marks(party=4)['COUNTER']['18:00'], '△')
        self.assertEqual(self.marks(party=5)['COUNTER']['18:00'], '×')
        self.assertEqual(self.marks()['COUNTER']['20:30'], '○')
        for seat in range(5, 8): self.block('COUNTER', seat)
        self.assertEqual(self.marks()['COUNTER']['18:00'], '×')

    def test_private_rooms_count_physical_rooms_and_respect_party_eligibility(self):
        self.assertEqual(self.marks()['PRIVATE']['18:00'], '○')  # all three vacant
        self.block('PRIVATE1')
        self.assertEqual(self.marks()['PRIVATE']['18:00'], '△')
        self.block('PRIVATE2')
        self.assertEqual(self.marks(party=4)['PRIVATE']['18:00'], '△')
        self.assertEqual(self.marks(party=2)['PRIVATE']['18:00'], '×')
        self.block('PRIVATE3')
        self.assertEqual(self.marks(party=4)['PRIVATE']['18:00'], '×')
        self.assertEqual(self.marks()['PRIVATE']['20:30'], '○')

    def test_confirmed_bookings_and_closure(self):
        self.c.execute("INSERT INTO reservations(source,guest_name,visit_at,party_size,seating_area,counter_round,amount,duration_minutes,status,created_at,updated_at) VALUES('DIRECT','test','2026-12-01T18:00',4,'COUNTER',1,1,150,'CONFIRMED','now','now')")
        self.assertEqual(self.marks()['COUNTER']['18:00'], '△')
        self.c.execute("UPDATE reservations SET status='CANCELLED'")
        self.assertEqual(self.marks()['COUNTER']['18:00'], '○')
        self.assertEqual(self.marks(day='2026-11-30')['COUNTER']['18:00'], '×')


if __name__ == '__main__':
    unittest.main()
