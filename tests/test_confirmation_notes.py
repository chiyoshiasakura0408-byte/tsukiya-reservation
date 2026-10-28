"""Only staff-authored customer-facing notes belong in automatic confirmations."""
import json
import uuid
import unittest
from unittest.mock import patch
import test_gpt_reservations as fixtures

run = fixtures.run
NOTE = 'システムの都合上、ご予約時刻は18:00と記載されておりますが、実際のご予約は18:30で承っております。18:30にお越しください。'

class ConfirmationNotesTest(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.GPTAPITest()
        self.fixture.setUp()
    def tearDown(self):
        self.fixture.tearDown()
    def row(self, note=NOTE):
        self.fixture.body.update(confirmation_note=note,guest_note='INTERNAL OWNER NOTE - NEVER SEND')
        return self.fixture.create()
    def render(self,row):
        row=dict(row,email='guest@example.test',booking_language=row.get('booking_language','ja'))
        with patch.multiple(run,SMTP_HOST='smtp.test',SMTP_USER='test',SMTP_PASS='test',MAIL_FROM='test@example.test'), \
             patch.object(run.smtplib,'SMTP') as smtp:
            self.assertEqual(run.send_confirmation(row),(True,''))
            return smtp.return_value.__enter__.return_value.send_message.call_args.args[0].get_content()
    def test_note_persisted_and_in_email_without_internal_notes(self):
        row=self.row()
        self.assertEqual(row['confirmation_note'],NOTE)
        c=run.con()
        saved=dict(c.execute('SELECT * FROM reservations WHERE id=?',(row['id'],)).fetchone());c.close()
        self.assertEqual(saved['confirmation_note'],NOTE)
        body=self.render(saved)
        self.assertIn('ご来店日時：2099-12-05T18:00',body)
        self.assertIn('【ご予約に関する備考】\n'+NOTE,body)
        self.assertNotIn('INTERNAL OWNER NOTE',body)
        self.assertEqual(self.fixture.call('/api/gpt/reservations/'+str(row['id']))[1]['confirmation_note'],NOTE)
    def test_empty_or_legacy_note_has_no_note_heading(self):
        row=self.row('')
        row.pop('confirmation_note')
        body=self.render(row)
        self.assertNotIn('【ご予約に関する備考】',body)
        self.assertNotIn('INTERNAL OWNER NOTE',body)
    def test_english_confirmation_includes_explicit_customer_note(self):
        row=self.row();row['booking_language']='en'
        body=self.render(row)
        self.assertIn('Reservation notes:\n'+NOTE,body)
        self.assertNotIn('INTERNAL OWNER NOTE',body)
    def test_invalid_customer_note_never_saves_a_reservation(self):
        for value in (None,123,{},'x'*1001,'bad\x00note'):
            body=dict(self.fixture.body,confirmation_note=value,request_id=str(uuid.uuid4()))
            code,result=self.fixture.call('/api/gpt/reservations',body)
            self.assertEqual(code,400,result)
        c=run.con();self.assertEqual(c.execute('SELECT count(*) FROM reservations').fetchone()[0],0);c.close()

if __name__=='__main__':unittest.main()
