import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())
import run


class PendingBankPartyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = patch.object(run, 'DB', self.tmp.name + '/db')
        self.db.start()
        self.c = run.con()
        self.c.execute("""INSERT INTO reservations
            (id,source,guest_name,visit_at,party_size,seating_area,amount,status,
             payment_method,bank_payer,bank_due_at,bank_email_status,course_name,
             created_at,updated_at,duration_minutes)
            VALUES(67,'WEB','guest','2099-11-14T18:00',2,'PRIVATE1',132000,
                   'PENDING','BANK','payer','2099-10-31T21:02:14+09:00','SENT',
                   '松葉蟹おまかせコース','now','now',150)""")
        self.c.commit()
        self.data = dict(party_size=4, expected_party_size=2,
                         expected_visit_at='2099-11-14T18:00', expected_amount=132000)

    def tearDown(self):
        self.c.close()
        self.db.stop()
        self.tmp.cleanup()

    def change(self):
        self.c.execute('BEGIN IMMEDIATE')
        try:
            result = run.change_direct_party(self.c, 67, self.data)
            self.c.commit()
            return result
        except Exception:
            self.c.rollback()
            raise

    def test_changes_amount_preserving_unpaid_booking_and_deadline(self):
        before = dict(self.c.execute('SELECT * FROM reservations WHERE id=67').fetchone())
        with patch.object(run, 'square') as square, patch.object(run.smtplib, 'SMTP') as mail:
            self.assertEqual(self.change()['amount'], 264000)
            square.assert_not_called()
            mail.assert_not_called()
        after = dict(self.c.execute('SELECT * FROM reservations WHERE id=67').fetchone())
        self.assertEqual(after['party_size'], 4)
        for key in before.keys() - {'party_size','amount','guest_note','updated_at'}:
            self.assertEqual(before[key], after[key], key)
        with self.assertRaises(ValueError):
            self.change()  # stale request cannot overwrite the new amount

    def test_rejects_room_capacity_and_paid_or_card_reservations(self):
        self.data['party_size'] = 5
        with self.assertRaises(ValueError):
            self.change()
        self.data['party_size'] = 4
        for field, value in [('status','CONFIRMED'), ('payment_method','CARD'),
                             ('payment_source','BANK'), ('square_order_id','order'),
                             ('square_invoice_id','invoice'), ('source','WEB_CARD')]:
            with self.subTest(field=field):
                self.c.execute(f'UPDATE reservations SET {field}=? WHERE id=67', (value,))
                self.c.commit()
                with self.assertRaises(ValueError):
                    self.change()
                self.c.execute(f'UPDATE reservations SET {field}=? WHERE id=67',
                               ({'status':'PENDING','payment_method':'BANK','source':'WEB'}.get(field),))
                self.c.commit()
