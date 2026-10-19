import unittest
import tempfile
import time
import os
from pathlib import Path
from unittest.mock import patch
import line_delivery as d

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Path(self.tmp.name)/'test.sqlite'
        self.pdf_patch=patch.object(d,'printable_message',return_value={'type':'text','text':'PDF'})
        self.pdf_patch.start();self.addCleanup(self.pdf_patch.stop)
        c=d.connect(self.db)
        c.execute('INSERT INTO line_sources VALUES(?,?,?,1)',(d.TARGET,'group','now'))
        c.commit();c.close()
    def tearDown(self):self.tmp.cleanup()
    def test_same_day_no_duplicate(self):
        with patch.object(d,'api',return_value={}) as api, patch.object(d,'capture',return_value={'type':'image'}),patch.object(d,'message',return_value={'type':'text','text':'test'}):
            d.deliver(self.db,10000,'admin','https://example.com','2026-10-04')
            self.assertTrue(d.deliver(self.db,10000,'admin','https://example.com','2026-10-04')['already_sent'])
            self.assertEqual(api.call_count,2)
    def test_retry_preserves_payload_and_key(self):
        with patch.object(d,'api',side_effect=[{},RuntimeError('LINE API HTTP 500'),{}]) as api,patch.object(d,'capture',return_value={'type':'image'}) as capture,patch.object(d,'message',return_value={'type':'text','text':'test'}):
            with self.assertRaises(RuntimeError):d.deliver(self.db,1,'admin','https://example.com','2026-10-04')
            d.deliver(self.db,1,'admin','https://example.com','2026-10-04')
            self.assertEqual(api.call_args_list[1],api.call_args_list[2])
            self.assertEqual(capture.call_count,2)
    def test_wrong_group_fails_closed(self):
        c=d.connect(self.db);c.execute('DELETE FROM line_sources');c.commit();c.close()
        with patch.object(d,'api') as api:
            with self.assertRaises(RuntimeError):d.deliver(self.db,1,'admin','https://example.com')
            api.assert_not_called()
    def test_image_expiry_and_signature(self):
        folder=self.db.parent/'line-images';folder.mkdir();name='a'*32+'-original.jpg';(folder/name).write_bytes(b'test')
        with patch.dict(os.environ,{'LINE_CHANNEL_SECRET':'test'}):
            exp=int(time.time())+60;sig=d.image_signature(name,exp)
            self.assertIsNotNone(d.image_path(self.db,name,exp,sig))
            self.assertIsNone(d.image_path(self.db,name,exp,'bad'))
            self.assertIsNone(d.image_path(self.db,name,0,d.image_signature(name,0)))
            self.assertIsNone(d.image_path(self.db,'../secret',exp,sig))
    def test_briefing_format_and_current_time(self):
        c=d.connect(self.db)
        c.execute("CREATE TABLE reservations(id INTEGER PRIMARY KEY,guest_name TEXT,visit_at TEXT,party_size INTEGER,seating_area TEXT,status TEXT,source TEXT,payment_confirmed_at TEXT,celebration_items TEXT,plate_message TEXT,guest_note TEXT)")
        rows = [
            ('すがの', '17:30', 4, '10/18朝倉様のカレンダー画像に基づく。10/19 17:30、4名、個室。事前決済なし・当日精算。連絡先未確認。顧客候補は本人同定できないため紐付けなし。', ''),
            ('もりせ あつこ', '18:00', 2, '10/18に朝倉様からいただいたカレンダー画像に基づく予約。10月19日18:00、2名、個室、事前決済なし・当日精算。', ''),
            ('せと', '18:00', 2, '10/18朝倉様のカレンダー画像に基づく。10/19 18:30、2名、個室。事前決済なし・当日精算。花束とケーキのご要望あり。花束・ケーキの種類、予算、プレート文言は未指定のため朝倉様へ確認。\n【開始時刻変更】2026-10-19T18:30 → 2026-10-19T18:00\n【朝倉追記】小さい花束', '花束'),
            ('May Wong', '20:30', 2, '朝倉様の10/18カレンダー画像・既承認の変更依頼に基づく。10/19 20:30、2名、個室、事前決済なし・当日精算。旧予約#11からの変更。記念日利用。', '')]
        for name, clock, guests, note, items in rows:
            c.execute("INSERT INTO reservations(guest_name,visit_at,party_size,seating_area,status,source,celebration_items,guest_note) VALUES(?,?,?,'PRIVATE1','CONFIRMED','DIRECT',?,?)", (name,'2026-10-19T'+clock,guests,items,note))
        c.commit();c.close()
        expected = """おはようございます。
2026年10月19日（月）
本日のご予約状況をお伝え致します。

合計4組・10名
＊花束とケーキのリクエストあり。

・17:30 すがの様　4名、個室
＊事前決済なし

・18:00 もりせ あつこ様　2名、個室
＊事前決済なし

・18:00 せと様　2名、個室
＊事前決済なし
＊花束とケーキのご要望あり。
＊小さい花束

・20:30 May Wong様　2名、個室
＊事前決済なし
＊記念日利用(リクエストなし)"""
        self.assertEqual(d.message(self.db,'2026-10-19')['text'], expected)
        with patch.object(d,'capture',return_value={'type':'image'}) as capture, patch.object(d,'api',return_value={}) as api:
            self.assertTrue(d.resend_briefing(self.db,'2026-10-19',charts=(1,'admin','https://example.com'))['resent'])
            self.assertTrue(d.resend_briefing(self.db,'2026-10-19',charts=(1,'admin','https://example.com'))['already_sent'])
            self.assertEqual(capture.call_count,2)
            self.assertEqual(api.call_count,1)
            self.assertEqual(len(api.call_args.args[1]['messages']),2)
            self.assertTrue(d.resend_briefing(self.db,'2026-10-19',printable='https://example.com')['resent'])
            self.assertTrue(d.resend_briefing(self.db,'2026-10-19',printable='https://example.com')['already_sent'])
            self.assertEqual(api.call_count,2)

    def test_correction_sent_once(self):
        with patch.object(d,'api',return_value={}) as api, patch.object(d,'message',return_value={'type':'text','text':'corrected'}):
            self.assertTrue(d.resend_briefing(self.db,'2026-10-19')['resent'])
            self.assertTrue(d.resend_briefing(self.db,'2026-10-19')['already_sent'])
            self.assertEqual(api.call_count,1)
            self.assertEqual(api.call_args.args[1]['to'],d.TARGET)

    def test_default_disabled(self):self.assertFalse(d.status(self.db)['delivery_enabled'])

if __name__=='__main__':unittest.main()
