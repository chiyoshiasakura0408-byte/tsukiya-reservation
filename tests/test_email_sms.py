import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import Mock, patch

import email_sms


class EmailSmsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = SimpleNamespace(DB=self.tmp.name+'/db', TWILIO_ACCOUNT_SID='test',
            TWILIO_AUTH_TOKEN='test', TWILIO_FROM_NUMBER='TSUKIYA',
            send_sms=Mock(return_value={'sid':'SM-test','status':'queued'}))
        self.row = dict(id=67, email='guest@example.com', phone='08000000000',
                        guest_name='試験', booking_language='ja')
        self.mail = EmailMessage()
        self.mail['To'] = self.row['email']
        self.mail['Subject'] = '銀行振込のご案内'
        self.mail.set_content('変更後のご予約 4名。振込期限をご確認ください。')

    def notices(self):
        with email_sms.connect(self.app) as c:
            return [dict(r) for r in c.execute('SELECT * FROM guest_email_sms')]

    def test_guest_notice_contains_spam_and_deadline_without_seat_details(self):
        email_sms.accepted(self.app,self.row,self.mail,'bank')
        self.app.send_sms.assert_called_once()
        text = self.app.send_sms.call_args.kwargs['body']
        self.assertIn('迷惑メールフォルダ',text)
        self.assertIn('期限内',text)
        self.assertNotIn('個室',text)
        self.assertEqual(self.notices()[0]['state'],'ACCEPTED')

    def test_retries_and_concurrent_callbacks_send_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:email_sms.accepted(self.app,self.row,self.mail,'bank'),range(8)))
        email_sms.process(self.app)
        self.app.send_sms.assert_called_once()
        self.assertEqual(len(self.notices()),1)

    def test_changed_email_content_creates_new_notice(self):
        email_sms.accepted(self.app,self.row,self.mail,'bank')
        self.mail.set_content('人数変更後のご案内 3名。')
        email_sms.accepted(self.app,self.row,self.mail,'bank')
        self.assertEqual(self.app.send_sms.call_count,2)

    def test_store_email_and_missing_phone_do_not_send(self):
        self.mail.replace_header('To','store@example.com')
        email_sms.accepted(self.app,self.row,self.mail,'bank')
        self.mail.replace_header('To',self.row['email'])
        email_sms.accepted(self.app,{**self.row,'phone':''},self.mail,'bank')
        self.app.send_sms.assert_not_called()
        self.assertEqual(self.notices()[0]['state'],'SKIPPED')

    def test_ambiguous_sms_failure_is_not_retried(self):
        self.app.send_sms.side_effect=TimeoutError()
        email_sms.accepted(self.app,self.row,self.mail,'bank')
        email_sms.accepted(self.app,self.row,self.mail,'bank')
        email_sms.process(self.app)
        self.app.send_sms.assert_called_once()
        self.assertEqual(self.notices()[0]['state'],'REVIEW')

    def test_missing_config_preserves_pending_for_worker(self):
        self.app.TWILIO_AUTH_TOKEN=''
        email_sms.accepted(self.app,self.row,self.mail,'bank')
        self.app.send_sms.assert_not_called()
        self.app.TWILIO_AUTH_TOKEN='test'
        email_sms.process(self.app)
        self.app.send_sms.assert_called_once()

    def bank_setup(self):
        import smtplib
        self.app.con=lambda:sqlite3.connect(self.app.DB)
        self.app.SMTP_HOST='smtp.test'
        self.app.SMTP_USER='test'
        self.app.SMTP_PASS='test'
        self.app.MAIL_FROM='store@example.com'
        self.app.SMTP_PORT=587
        self.app.smtplib=smtplib
        self.app.cancellation_url=lambda r:''
        self.app.booking_policy=SimpleNamespace(details=lambda r:{'beverage_policy':'飲料案内'})
        self.app.annex_message=lambda r,en:''
        self.row.update(bank_due_at='2099-11-10T12:00:00+09:00',visit_at='2099-11-12T18:00',
                        party_size=4,amount=264000,bank_payer='試験')
        with self.app.con() as c:
            c.execute('CREATE TABLE reservations(id INTEGER PRIMARY KEY,bank_email_status TEXT)')
            c.execute('INSERT INTO reservations(id) VALUES(67)')

    def test_bank_smtp_failure_never_sends_email_notice_sms(self):
        import bank_transfer
        self.bank_setup()
        with patch.object(self.app.smtplib,'SMTP',side_effect=TimeoutError()):
            bank_transfer.receipt(self.app,self.row)
        self.app.send_sms.assert_not_called()
        with self.app.con() as c:
            self.assertEqual(c.execute('SELECT bank_email_status FROM reservations').fetchone()[0],'ERROR')

    def test_bank_smtp_success_automatically_sends_sms_once(self):
        import bank_transfer
        self.bank_setup()
        with patch.object(self.app.smtplib,'SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value={}
            bank_transfer.receipt(self.app,self.row)
            bank_transfer.receipt(self.app,self.row)
        self.app.send_sms.assert_called_once()
        self.assertEqual(self.notices()[0]['state'],'ACCEPTED')

    def test_english_and_no_payment_claim_for_confirmation(self):
        text=email_sms.body({**self.row,'booking_language':'en'},'confirmation')
        self.assertIn('spam/junk',text)
        self.assertNotIn('payment',text)


if __name__=='__main__':unittest.main()
