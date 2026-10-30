import web_checkout
import reservation_changes
import gpt_reservations
import bank_transfer
import cancellation_emails
import booking_blocks
from email.utils import formataddr, parseaddr
import calendar_sync
import calendar_auth
import business_cards
import tablecheck_import
import refunds
import marketing
import line_bot
import concierge
import english_concierge
import booking_policy
import availability_display
import concierge_booking
import vip_access
import vip_slots
import instagram_concierge
import guest_service
import line_delivery
import supplies
import drinks
import finance_links
import line_receipts
import line_reservations
import sys
import os
import re
from functools import lru_cache
import json
import sqlite3
import urllib.request
import urllib.error
import base64
import hmac
import hashlib
import smtplib
import threading
import time
from collections import deque

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo


BASE = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB = DATA_DIR / "tsukiya.sqlite"

PORT = int(os.getenv("PORT", "10000"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
REFUND_APPROVAL_SECRET = os.getenv("REFUND_APPROVAL_SECRET", "")
STAFF_LOGIN_PIN = "7777"
SESSION_COOKIE = "tsukiya_session"
SESSION_SECONDS = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
LOGIN_FAILURES = {}
LOGIN_LOCK = threading.Lock()
SMS_LOCK = threading.Lock()
EMAIL_LOCK = threading.Lock()
RECONCILE_LOCK = threading.Lock()
REMINDER_LOCK = threading.Lock()

SQUARE_TOKEN = os.getenv("SQUARE_ACCESS_TOKEN", "")
SQUARE_LOCATION_ID = os.getenv("SQUARE_LOCATION_ID", "")
SQUARE_EN_LOCATION_ID = os.getenv("SQUARE_EN_LOCATION_ID", "")
SQUARE_ENV = os.getenv("SQUARE_ENV", "production")
SQUARE_API_VERSION = os.getenv("SQUARE_API_VERSION", "2026-08-19")

APP_BASE_URL = os.getenv(
    "APP_BASE_URL",
    ""
).strip().rstrip("/")

SQUARE_WEBHOOK_SIGNATURE_KEY = os.getenv(
    "SQUARE_WEBHOOK_SIGNATURE_KEY",
    ""
).strip()

COUNTER_CAPACITY = int(
    os.getenv("COUNTER_CAPACITY", "8")
)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER)

def mail_sender():
    return formataddr(("西天満つきや", parseaddr(MAIL_FROM)[1]))


TWILIO_ACCOUNT_SID = os.getenv(
    "TWILIO_ACCOUNT_SID",
    ""
)

TWILIO_AUTH_TOKEN = os.getenv(
    "TWILIO_AUTH_TOKEN",
    ""
)

TWILIO_FROM_NUMBER = os.getenv(
    "TWILIO_FROM_NUMBER",
    ""
)


ACTIVE_STATUSES = (
    "PENDING",
    "INVOICED",
    "CONFIRMED",
    "ERROR"
)

ROOMS = (
    "PRIVATE1",
    "PRIVATE2",
    "PRIVATE3"
)

PUBLIC_COURSE_PRICE = 66000  # 税込・1名あたり
PUBLIC_PAYMENT_HOURS = 48
NEW_PAYMENT_HOURS = 24
PUBLIC_COURSES = {
    "zuwaigani-oct": ("ずわい蟹おまかせコース", 44000),
    "matsuba-seko": ("松葉蟹おまかせコース", PUBLIC_COURSE_PRICE),
    "matsuba-fukahire": ("松葉蟹おまかせコース", PUBLIC_COURSE_PRICE),
}


def public_party_allowed(area, party):
    if area == "COUNTER":
        return 2 <= party <= COUNTER_CAPACITY
    if area in ("PRIVATE1", "PRIVATE2"):
        return 2 <= party <= 4
    if area == "PRIVATE3":
        return 4 <= party <= 8
    return False


def guest_requests(x):
    """Validate optional staff notes and celebration requests without changing course price."""
    note = x.get("guest_note", "")
    plate = x.get("plate_message", "")
    items = x.get("celebration_items", [])
    if not isinstance(note, str) or not isinstance(plate, str) or not isinstance(items, list):
        raise ValueError("備考の入力形式が不正です")
    allowed = {"花束", "ホールケーキ", "カットケーキ", "ホールケーキ（小）", "ホールケーキ（大）", "花束（小）", "花束（大）"}
    if len(items) > 5 or any(not isinstance(item, str) or item not in allowed for item in items):
        raise ValueError("お祝い項目を確認してください")
    if len(note) > 1000 or len(plate) > 120:
        raise ValueError("備考またはプレートの文字数が長すぎます")
    if plate.strip() and not any("ケーキ" in item for item in items):
        raise ValueError("プレートはケーキ選択時に入力してください")
    return note.strip(), ",".join(dict.fromkeys(items)), plate.strip()


def public_slot_allowed(day, time_text, course=None):
    """Validate the date-specific course before accepting a public booking."""
    actual = bookable_course(day)
    if actual is None or (course is not None and course != bookable_course_key(day)):
        return False
    try:
        visit = datetime.fromisoformat(f"{day.isoformat()}T{time_text}")
    except ValueError:
        return False
    if time_text not in ("18:00", "20:30"):
        return False
    now_jp = datetime.now(timezone(timedelta(hours=9)))
    return day >= now_jp.date() + timedelta(days=2) and visit > now_jp.replace(tzinfo=None)


def staff_slot_allowed(day, time_text):
    """Staff may accept prior-day and same-day bookings before service begins."""
    if bookable_course(day) is None or time_text not in ("18:00", "20:30"):
        return False
    now_jp = datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None)
    return datetime.fromisoformat(f"{day.isoformat()}T{time_text}") > now_jp


def bookable_course_key(day):
    if date(2026, 10, 22) <= day <= date(2026, 11, 9):
        return "zuwaigani-oct"
    if date(2026, 10, 15) <= day <= date(2026, 11, 9):
        return 'tarabagani'
    if day.month == 11 and day.day >= 10 or day.month == 12:
        return 'matsuba-seko'
    if day.month in (1, 2) or day.month == 3 and day.day <= 20:
        return 'matsuba-fukahire'
    return None


def bookable_course(day):
    """Return the server-owned price and name for the date-specific course."""
    return PUBLIC_COURSES.get(bookable_course_key(day))


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def con():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    refunds.schema(c)
    reservation_changes.schema(c)
    marketing.schema(c)

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS reservations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            guest_name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            visit_at TEXT NOT NULL,
            party_size INTEGER NOT NULL,
            course_name TEXT,
            booking_language TEXT DEFAULT 'ja',
            amount INTEGER NOT NULL,
            seating_area TEXT,
            counter_round INTEGER,
            duration_minutes INTEGER DEFAULT 150,
            status TEXT NOT NULL DEFAULT 'PENDING',
            square_customer_id TEXT,
            square_order_id TEXT,
            square_invoice_id TEXT,
            square_invoice_url TEXT,
            square_booking_id TEXT,
            public_request_id TEXT,
            cancellation_policy_accepted_at TEXT,
            confirmation_sent_at TEXT,
            payment_source TEXT,
            payment_confirmed_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS webhook_events(
            event_id TEXT PRIMARY KEY,
            event_type TEXT,
            received_at TEXT NOT NULL
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reservation_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            amount INTEGER NOT NULL,
            reference TEXT NOT NULL,
            confirmed_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    c.execute("""CREATE TABLE IF NOT EXISTS reservation_note_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reservation_id INTEGER NOT NULL,
        old_note TEXT NOT NULL,
        new_note TEXT NOT NULL,
        changed_at TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS seat_blocks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        visit_date TEXT NOT NULL,
        time_text TEXT NOT NULL,
        seating_area TEXT NOT NULL,
        seat_number INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(visit_date,time_text,seating_area,seat_number)
    )""")

    cols = {
        r["name"]
        for r in c.execute(
            "PRAGMA table_info(reservations)"
        )
    }

    bank_transfer.schema(c)
    web_checkout.schema(c)
    wanted = {
        "payment_method": "TEXT NOT NULL DEFAULT 'CARD'",
        "bank_payer": "TEXT",
        "bank_planned_date": "TEXT",
        "bank_due_at": "TEXT",
        "invoice_payment_due_at": "TEXT",
        "bank_email_status": "TEXT",
        "confirmation_email_status": "TEXT",
        "confirmation_sms_status": "TEXT",
        "confirmation_sms_sid": "TEXT",
        "confirmation_sms_sent_at": "TEXT",
        "confirmation_sms_error": "TEXT",
        "confirmation_sms_correction_status": "TEXT",
        "confirmation_sms_correction_sid": "TEXT",
        "confirmation_sms_correction_sent_at": "TEXT",
        "confirmation_sms_correction_error": "TEXT",
        "confirmation_sms_correction_body": "TEXT",
        "confirmation_sms_correction_original_sid": "TEXT",
        "invoice_sms_status": "TEXT",
        "invoice_sms_sid": "TEXT",
        "invoice_sms_sent_at": "TEXT",
        "invoice_sms_error": "TEXT",
        "seating_area": "TEXT",
        "counter_round": "INTEGER",
        "duration_minutes": "INTEGER DEFAULT 150",
        "vip_through": "INTEGER NOT NULL DEFAULT 0",
        "vip_booking": "INTEGER NOT NULL DEFAULT 0",
        "vip_seats": "TEXT NOT NULL DEFAULT '[]'",
        "square_booking_id": "TEXT",
        "public_request_id": "TEXT",
        "cancellation_policy_accepted_at": "TEXT",
        "cancellation_reason": "TEXT",
        "cancelled_at": "TEXT",
        "confirmation_sent_at": "TEXT",
        "payment_source": "TEXT",
        "payment_confirmed_at": "TEXT",
        "last_error": "TEXT",
        "booking_language": "TEXT DEFAULT 'ja'",
        "acquisition_source": "TEXT",
        "staff_seen_at": "TEXT",
        "invoice_issued_at": "TEXT",
        "reminder_status": "TEXT",
        "reminder_sent_at": "TEXT",
        "reminder_error": "TEXT",
        "square_reminder_policy": "TEXT",
        "guest_note": "TEXT",
        "confirmation_note": "TEXT NOT NULL DEFAULT ''",
        "celebration_items": "TEXT",
        "plate_message": "TEXT",
        "customer_id": "INTEGER",
        "visit_note": "TEXT",
        "companions": "TEXT",
    }

    migrate_seen = "staff_seen_at" not in cols
    migrate_reminder = "reminder_status" not in cols
    migrate_square_policy = "square_reminder_policy" not in cols

    for name, typ in wanted.items():
        if name not in cols:
            c.execute(
                f"""
                ALTER TABLE reservations
                ADD COLUMN {name} {typ}
                """
            )

    if migrate_seen:
        c.execute("UPDATE reservations SET staff_seen_at=created_at WHERE staff_seen_at IS NULL")
    if migrate_reminder:
        c.execute("UPDATE reservations SET reminder_status='SKIPPED_LEGACY' WHERE square_invoice_id IS NOT NULL")

    if migrate_square_policy:
        # Existing live unpaid invoices were handled before this policy rollout.
        # Preserve their receipts and avoid replaying external/manual reminders.
        c.execute("UPDATE reservations SET reminder_status='SKIPPED_LEGACY' "
                  "WHERE square_invoice_id IS NOT NULL AND reminder_status IS NULL")

    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS public_request_id_unique "
        "ON reservations(public_request_id)"
    )
    c.execute("""CREATE TABLE IF NOT EXISTS customers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_key TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        company_name TEXT NOT NULL DEFAULT '',
        receipt_name TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        allergies TEXT NOT NULL DEFAULT '',
        disliked_foods TEXT NOT NULL DEFAULT '',
        preferred_seat TEXT NOT NULL DEFAULT '',
        preferred_drinks TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    booking_blocks.schema(c)
    business_cards.init(c)
    tablecheck_import.schema(c)
    customer_cols = {row["name"] for row in c.execute("PRAGMA table_info(customers)")}
    for field in ("allergies", "disliked_foods", "preferred_seat", "preferred_drinks", "preferred_crab", "return_transport", "alcohol_service", "soft_drink_only"):
        if field not in customer_cols:
            c.execute(f"ALTER TABLE customers ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
    c.execute("CREATE INDEX IF NOT EXISTS reservations_customer_idx ON reservations(customer_id)")
    concierge.owner_notifications.capture_schema(c)
    cancellation_emails.schema(c)

    c.commit()
    return c


def customer_match_key(row):
    name = " ".join((row["guest_name"] or "").split()).casefold()
    digits = re.sub(r"\D", "", row["phone"] or "")
    if digits.startswith("81") and len(digits) in (12, 13):
        digits = "0" + digits[2:]
    email = (row["email"] or "").strip().casefold()
    if digits:
        return f"phone:{digits}:name:{name}"
    if email:
        return f"email:{email}:name:{name}"
    return f"reservation:{row['id']}"


def sync_customers(c):
    """Link legacy and new bookings without merging people on a shared phone alone."""
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("UPDATE reservations SET customer_id=NULL WHERE customer_id IS NOT NULL AND course_name LIKE '決済テスト%'")
        for row in c.execute("""SELECT id,guest_name,phone,email FROM reservations
            WHERE customer_id IS NULL AND (source!='WEB_CARD' OR status='CONFIRMED') AND (course_name IS NULL OR course_name NOT LIKE '決済テスト%')
            ORDER BY id""").fetchall():
            key = customer_match_key(row)
            now = now_iso()
            c.execute("""INSERT OR IGNORE INTO customers(match_key,name,phone,email,created_at,updated_at)
                         VALUES(?,?,?,?,?,?)""", (key, row["guest_name"], row["phone"] or "", row["email"] or "", now, now))
            customer = c.execute("SELECT id FROM customers WHERE match_key=?", (key,)).fetchone()
            c.execute("UPDATE reservations SET customer_id=? WHERE id=?", (customer["id"], row["id"]))
        c.commit()
    except Exception:
        c.rollback()
        raise


def square(path, body=None, method=None):
    if not SQUARE_TOKEN or not SQUARE_LOCATION_ID:
        raise RuntimeError(
            "Square認証情報が未設定です"
        )

    if SQUARE_ENV == "sandbox":
        host = "https://connect.squareupsandbox.com"
    else:
        host = "https://connect.squareup.com"

    data = (
        None
        if body is None
        else json.dumps(
            body
        ).encode("utf-8")
    )

    req = urllib.request.Request(
        host + path,
        data=data,
        method=(
            method
            or (
                "POST"
                if body is not None
                else "GET"
            )
        )
    )

    req.add_header(
        "Authorization",
        f"Bearer {SQUARE_TOKEN}"
    )

    req.add_header(
        "Square-Version",
        SQUARE_API_VERSION
    )

    req.add_header(
        "Content-Type",
        "application/json"
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=30
        ) as r:
            raw = r.read().decode("utf-8")

            return (
                json.loads(raw)
                if raw
                else {}
            )

    except urllib.error.HTTPError as e:
        raise RuntimeError(
            e.read().decode("utf-8")
        )


def normalize_jp_phone(phone):
    p = "".join(
        ch
        for ch in str(phone or "")
        if ch.isdigit() or ch == "+"
    )

    if p.startswith("0"):
        return "+81" + p[1:]

    return p


def send_sms(phone, payment_url=None, *, body=None):
    if not TWILIO_ACCOUNT_SID:
        raise RuntimeError(
            "TWILIO_ACCOUNT_SID が未設定です"
        )

    if not TWILIO_AUTH_TOKEN:
        raise RuntimeError(
            "TWILIO_AUTH_TOKEN が未設定です"
        )

    if not TWILIO_FROM_NUMBER:
        raise RuntimeError(
            "TWILIO_FROM_NUMBER が未設定です"
        )

    if not phone:
        raise RuntimeError(
            "電話番号がありません"
        )

    to_number = normalize_jp_phone(phone)

    message_body = body if body is not None else (
        "西天満つきやです。\n"
        "ご予約いただき誠にありがとうございます。\n\n"
        "下記よりお料理代のお支払いをお願いいたします。\n"
        f"{payment_url}\n\n"
        "お振込みの際は下記口座までお願い致します。\n\n"
        "三井住友銀行\n"
        "堂島支店\n"
        "(普)0655295\n"
        "アサクラ　チヨシ"
    )

    form = urlencode(
        {
            "To": to_number,
            "From": TWILIO_FROM_NUMBER,
            "Body": message_body
        }
    ).encode("utf-8")

    url = (
        "https://api.twilio.com/"
        "2010-04-01/Accounts/"
        f"{TWILIO_ACCOUNT_SID}/"
        "Messages.json"
    )

    req = urllib.request.Request(
        url,
        data=form,
        method="POST"
    )

    credentials = (
        f"{TWILIO_ACCOUNT_SID}:"
        f"{TWILIO_AUTH_TOKEN}"
    ).encode("utf-8")

    auth = base64.b64encode(
        credentials
    ).decode("utf-8")

    req.add_header(
        "Authorization",
        f"Basic {auth}"
    )

    req.add_header(
        "Content-Type",
        "application/x-www-form-urlencoded"
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=30
        ) as res:
            return json.loads(
                res.read().decode("utf-8")
            )

    except urllib.error.HTTPError as e:
        raise RuntimeError(
            e.read().decode("utf-8")
        )


def send_invoice_sms(reservation):
    """Submit a phone-only invoice once; retain ambiguous failures for manual review."""
    if reservation.get("email") or not reservation.get("phone"):
        return reservation
    rid = reservation["id"]
    with SMS_LOCK:
        c = con()
        try:
            row = dict(c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
            if row["status"] != "INVOICED" or not row.get("square_invoice_url"):
                return row
            if row.get("invoice_sms_status") in ("QUEUED", "SENDING", "ERROR"):
                return row
            if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
                c.execute("UPDATE reservations SET invoice_sms_status='NOT_CONFIGURED',"
                          "invoice_sms_error=? WHERE id=?", ("TwilioのSMS送信設定が未完了です", rid))
                c.commit()
            else:
                c.execute("UPDATE reservations SET invoice_sms_status='SENDING',invoice_sms_error=NULL WHERE id=?", (rid,))
                c.commit()
                try:
                    arrival_day = date.fromisoformat(row["visit_at"][:10])
                    received_day = parse_dt(row["created_at"]).astimezone(timezone(timedelta(hours=9))).date()
                    if row["source"] == "PHONE" and received_day == arrival_day - timedelta(days=1):
                        sms_text = (f"西天満つきやです。\\n前日電話予約のお料理代金を下記よりお支払いください。\\n"
                                    f"お支払い期限：{arrival_day:%Y年%m月%d日} 08:00（日本時間）\\n"
                                    "期限までに決済を確認できない場合、ご予約は取り消しとなります。\\n"
                                    f"{row['square_invoice_url']}")
                        result = send_sms(row["phone"], body=sms_text)
                    else:
                        result = send_sms(row["phone"], row["square_invoice_url"])
                    if not result.get("sid") or result.get("status") in ("failed", "undelivered", "canceled"):
                        raise RuntimeError("SMS送信を受け付けられませんでした")
                    c.execute("UPDATE reservations SET invoice_sms_status='QUEUED',invoice_sms_sid=?,"
                              "invoice_sms_sent_at=? WHERE id=?", (result["sid"], now_iso(), rid))
                except Exception:
                    c.execute("UPDATE reservations SET invoice_sms_status='ERROR',invoice_sms_error=? WHERE id=?",
                              ("SMS送信を確認できません。Twilioの送信履歴を確認してください", rid))
                c.commit()
            return dict(c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
        finally:
            c.close()


def confirmation_sms_body(row):
    """Use the explicitly customer-facing note, never the internal booking note."""
    direct = row["source"] in ("DIRECT", "CONCIERGE_LINE")
    intro = ("ご予約を確定いたしました。お料理代は当日店舗にてお支払いください。" if direct
             else "ご入金を確認し、ご予約を確定いたしました。")
    note = str(dict(row).get("confirmation_note") or "").strip()
    body = (f"西天満つきやです。{row['guest_name']}様\n{intro}\n"
            f"日時：{row['visit_at'].replace('T', ' ')}\nお席：{customer_seating_label(row)}\n"
            f"人数：{row['party_size']}名様\n")
    if note:
        body += "【ご予約に関する備考】\n" + note + "\n"
    else:
        body += ("ご指定の時刻から23時までお過ごしいただけます。" if row.get('vip_through')
                 else "当店は一斉スタートでお料理をご提供します。ご来店時間をお守りください。") + "\n"
    body += "当日は心を尽くしてお迎えいたします。"
    venue = annex_message(row)
    if venue:
        if note:
            venue = venue.replace("までお越しください。", "でのご案内です。")
        body += "\n" + venue
    return body


def send_confirmation_sms_correction(reservation, expected_sid):
    """Send one auditable correction; retain the original SMS and never replay unknown results."""
    rid = reservation["id"]
    with SMS_LOCK:
        c = con()
        try:
            row = dict(c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
            if (row["source"] != "DIRECT" or row["status"] != "CONFIRMED" or not row.get("phone")
                    or not row.get("confirmation_note") or not expected_sid
                    or row.get("confirmation_sms_sid") != expected_sid):
                raise ValueError("元のSMSと予約内容を確認してください")
            if row.get("confirmation_sms_correction_status"):
                return row
            if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
                raise ValueError("TwilioのSMS送信設定が未完了です")
            day = date.fromisoformat(row["visit_at"][:10])
            body = (f"西天満つきやです。{row['guest_name']}様\n先ほどのSMSのご案内を訂正いたします。\n"
                    f"ご予約日：{day:%Y年%m月%d日}\nお席：{customer_seating_label(row)}\n人数：{row['party_size']}名様\n"
                    "先ほどのSMSに入金確認済みとの記載がございましたが、お料理代は当日精算で承っております。\n"
                    "【ご予約時刻について】\n" + row["confirmation_note"].strip())
            venue = annex_message(row).replace("までお越しください。", "でのご案内です。")
            if venue:
                body += "\n" + venue
            claimed = c.execute("UPDATE reservations SET confirmation_sms_correction_status='SENDING',"
                                "confirmation_sms_correction_body=?,confirmation_sms_correction_original_sid=? "
                                "WHERE id=? AND confirmation_sms_correction_status IS NULL",
                                (body, expected_sid, rid)).rowcount
            c.commit()
            if claimed:
                try:
                    result = send_sms(row["phone"], body=body)
                    if not result.get("sid") or result.get("status") in ("failed", "undelivered", "canceled"):
                        raise RuntimeError("SMS送信を受け付けられませんでした")
                    c.execute("UPDATE reservations SET confirmation_sms_correction_status='QUEUED',"
                              "confirmation_sms_correction_sid=?,confirmation_sms_correction_sent_at=? WHERE id=?",
                              (result["sid"], now_iso(), rid))
                except Exception:
                    c.execute("UPDATE reservations SET confirmation_sms_correction_status='ERROR',"
                              "confirmation_sms_correction_error=? WHERE id=?",
                              ("訂正SMSの結果を確認できません。送信履歴を確認してください。自動再送は行いません。", rid))
                c.commit()
            return dict(c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
        finally:
            c.close()


def send_confirmation_sms(reservation):
    """Claim a confirmed phone-only notification before contacting Twilio."""
    if reservation.get("email") or not reservation.get("phone"):
        return reservation
    rid = reservation["id"]
    with SMS_LOCK:
        c = con()
        try:
            row = dict(c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
            if row["status"] != "CONFIRMED":
                return row
            if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
                c.execute("UPDATE reservations SET confirmation_sms_status='NOT_CONFIGURED',confirmation_sms_error=? "
                          "WHERE id=? AND (confirmation_sms_status IS NULL OR confirmation_sms_status='NOT_CONFIGURED')",
                          ("TwilioのSMS送信設定が未完了です", rid))
                c.commit()
            else:
                claimed = c.execute("UPDATE reservations SET confirmation_sms_status='SENDING',confirmation_sms_error=NULL "
                                    "WHERE id=? AND status='CONFIRMED' AND "
                                    "(confirmation_sms_status IS NULL OR confirmation_sms_status='NOT_CONFIGURED')", (rid,)).rowcount
                c.commit()
                if claimed:
                    body = confirmation_sms_body(row)
                    try:
                        result = send_sms(row["phone"], body=body)
                        if not result.get("sid") or result.get("status") in ("failed", "undelivered", "canceled"):
                            raise RuntimeError("SMS送信を受け付けられませんでした")
                        c.execute("UPDATE reservations SET confirmation_sms_status='QUEUED',confirmation_sms_sid=?,"
                                  "confirmation_sms_sent_at=? WHERE id=?", (result["sid"], now_iso(), rid))
                    except Exception:
                        c.execute("UPDATE reservations SET confirmation_sms_status='ERROR',confirmation_sms_error=? WHERE id=?",
                                  ("SMS送信を確認できません。Twilioの送信履歴を確認してください", rid))
                    c.commit()
            return dict(c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
        finally:
            c.close()


def make_invoice(r):
    if r["source"] == web_checkout.SOURCE:
        raise ValueError("Webカード決済のため請求書は発行しません")
    if dict(r).get("payment_method") == "BANK":
        raise ValueError("銀行振込希望のためカード請求は作成しません")
    if not r["email"] and not r["phone"]:
        raise RuntimeError(
            "メールアドレスまたは電話番号が必要です"
        )

    # Persist the new phone term before contacting Square so retries keep the
    # exact same deadline. Previously issued invoices keep their agreed term.
    r = dict(r)
    if r['source'] == 'PHONE':
        c = con()
        try:
            c.execute('BEGIN IMMEDIATE')
            saved = c.execute('SELECT * FROM reservations WHERE id=?', (r['id'],)).fetchone()
            if saved and not saved['square_invoice_id']:
                deadline = saved['invoice_payment_due_at'] or new_phone_payment_deadline(r).isoformat()
                c.execute('UPDATE reservations SET invoice_payment_due_at=? WHERE id=?', (deadline,r['id']))
                r['invoice_payment_due_at'] = deadline
            c.commit()
        finally:
            c.close()

    english = r["booking_language"] == "en"
    location_id = SQUARE_EN_LOCATION_ID if english and SQUARE_EN_LOCATION_ID else SQUARE_LOCATION_ID
    rid = str(r["id"])

    created = (
        (r["created_at"] or now_iso())
        .replace(":", "")
        .replace("+", "")
        .replace(".", "")
        .replace("-", "")
    )

    ikey = f"{rid}-{created}"

    customer_body = {
        "idempotency_key":
            f"tsukiya-customer-{ikey}",

        "given_name":
            r["guest_name"],

        "reference_id":
            f"tsukiya-reservation-{rid}"
    }

    if r["email"]:
        customer_body[
            "email_address"
        ] = r["email"]

    if r["phone"]:
        customer_body[
            "phone_number"
        ] = normalize_jp_phone(
            r["phone"]
        )

    customer = square(
        "/v2/customers",
        body=customer_body
    )["customer"]

    order = square(
        "/v2/orders",
        body={
            "idempotency_key":
                f"tsukiya-order-{ikey}",

            "order": {
                "location_id":
                    location_id,

                "reference_id":
                    f"tsukiya-reservation-{rid}",

                "customer_id":
                    customer["id"],

                "line_items": [
                    {
                        "name":
                            "Crab omakase course" if english else "お料理代",

                        "quantity":
                            "1",

                        "base_price_money": {
                            "amount":
                                int(r["amount"]),

                            "currency":
                                "JPY"
                        }
                    }
                ]
            }
        }
    )["order"]

    now_jp = datetime.now(timezone(timedelta(hours=9)))
    phone_day = date.fromisoformat(str(r["visit_at"])[:10]) if r["source"] == "PHONE" else None
    immediate_phone = phone_day is not None and phone_day == now_jp.date() + timedelta(days=1)
    due = (phone_day if immediate_phone else
           (datetime.now(timezone.utc) + timedelta(days=3)).date()).isoformat()

    phone_deadline_ja = (f"前日電話予約のお支払期限：{phone_day:%Y年%m月%d日} 08:00（日本時間）。未決済の場合、予約は取り消しとなります。\n\n" if immediate_phone else "")
    phone_deadline_en = (f"Phone booking payment deadline: {phone_day:%Y-%m-%d} at 08:00 JST. Unpaid reservations will be canceled.\n\n" if immediate_phone else "")
    if r.get('invoice_payment_due_at'):
        phone_due = datetime.fromisoformat(r['invoice_payment_due_at']).astimezone(ZoneInfo('Asia/Tokyo'))
        due = phone_due.date().isoformat()
        term = 'お支払期限 / Payment deadline: ' + phone_due.isoformat(timespec='seconds') + ' (JST)\n'
        phone_deadline_ja = term + '請求発行から24時間以内にお支払いください（前日電話予約は来店当日08:00まで）。未決済の場合は予約取消となります。\n\n'
        phone_deadline_en = term + 'Please pay within 24 hours of invoice issuance (by 08:00 JST on the visit day for day-before bookings). Unpaid reservations will be canceled.\n\n'
    delivery_method = "EMAIL" if r["email"] else "SHARE_MANUALLY"

    invoice = square(
        "/v2/invoices",
        body={
            "idempotency_key":
                f"tsukiya-invoice-{ikey}",

            "invoice": {
                "location_id":
                    location_id,

                "order_id":
                    order["id"],

                "primary_recipient": {
                    "customer_id":
                        customer["id"]
                },

                "delivery_method":
                    delivery_method,

                "title":
                    "Nishitenma Tsukiya | Crab Omakase" if english else "西天満 つきや お料理代",

                "description": (
                    phone_deadline_en + "Full prepayment for your crab omakase reservation. "
                    "Your reservation is confirmed after full payment. "
                    + booking_policy.details(r)["beverage_policy_en"] + "\n\n"
                    "For bank transfers:\nSumitomo Mitsui Banking Corporation\n"
                    "Dojima Branch\nOrdinary account 0655295\n"
                    "Account name: ASAKURA CHIYOSHI"
                    + ("\n\n" + annex_message(r, True) if annex_message(r, True) else "")
                ) if english else (
                    phone_deadline_ja + booking_policy.details(r)["beverage_policy"] + "\n\n" + "お振込みの際は下記口座までお願い致します。\n\n"
                    "三井住友銀行\n"
                    "堂島支店\n"
                    "(普)0655295\n"
                    "アサクラ　チヨシ"
                    + ("\n\n" + annex_message(r) if annex_message(r) else "")
                ),

                "payment_requests": [
                    {
                        "request_type":
                            "BALANCE",

                        "due_date":
                            due
                    }
                ],

                "accepted_payment_methods": {
                    "card": True
                }
            }
        }
    )["invoice"]

    published = square(
        f"/v2/invoices/{invoice['id']}/publish",
        body={
            "version":
                invoice["version"],

            "idempotency_key":
                f"tsukiya-publish-{ikey}"
        }
    )["invoice"]

    payment_url = (
        published.get("public_url")
        or published.get("invoice_url")
        or ""
    )

    if not r["email"] and not payment_url:
        raise RuntimeError(
            "Square請求書URLを取得できませんでした"
        )

    return (
        customer["id"],
        order["id"],
        published["id"],
        payment_url
    )


def verify_square(raw, signature):
    if (
        not SQUARE_WEBHOOK_SIGNATURE_KEY
        or not signature
    ):
        return False

    notification_url = (
        APP_BASE_URL.rstrip("/")
        + "/webhooks/square"
    )

    try:
        body = raw.decode("utf-8")

        message = (
            notification_url
            + body
        ).encode("utf-8")

        calculated_signature = (
            base64.b64encode(
                hmac.new(
                    SQUARE_WEBHOOK_SIGNATURE_KEY
                    .strip()
                    .encode("utf-8"),

                    message,

                    hashlib.sha256
                ).digest()
            ).decode("utf-8")
        )

        return hmac.compare_digest(
            calculated_signature,
            signature.strip()
        )

    except Exception as e:
        print(
            "Square webhook signature error:",
            e
        )

        return False


def parse_dt(s):
    return datetime.fromisoformat(
        str(s).replace(
            "Z",
            "+00:00"
        )
    )


def new_phone_payment_deadline(reservation, now=None):
    now = now or datetime.now(timezone.utc)
    deadline = now + timedelta(hours=NEW_PAYMENT_HOURS)
    visit_day = date.fromisoformat(reservation['visit_at'][:10])
    if visit_day == now.astimezone(ZoneInfo('Asia/Tokyo')).date() + timedelta(days=1):
        deadline = min(deadline, datetime.combine(visit_day, datetime.min.time(),
                       tzinfo=ZoneInfo('Asia/Tokyo')) + timedelta(hours=8))
    return deadline.astimezone(timezone.utc).replace(microsecond=0)


def square_payment_deadline(invoice, reservation=None):
    """Square due_date expires at the end of that date in invoice.timezone.

    Official Invoice.timezone example: due March 9 -> March 10 00:00 local.
    Preserve the explicit 08:00 JST term on day-before PHONE invoices.
    Missing/ambiguous schedules never fall back to reservation creation time.
    """
    if not isinstance(invoice, dict) or not invoice.get("timezone"):
        return None
    requests = invoice.get("payment_requests") or []
    if len(requests) != 1 or requests[0].get("request_type") != "BALANCE":
        return None
    try:
        day = date.fromisoformat(requests[0]["due_date"])
        deadline = datetime.combine(day + timedelta(days=1), datetime.min.time(),
                                    tzinfo=ZoneInfo(invoice["timezone"]))
        r = reservation or {}
        if r.get('source') == 'PHONE' and r.get('invoice_payment_due_at'):
            explicit = datetime.fromisoformat(r['invoice_payment_due_at'])
            if explicit.tzinfo is None:
                return None
            explicit = explicit.astimezone(ZoneInfo('Asia/Tokyo'))
            term = 'お支払期限 / Payment deadline: ' + explicit.isoformat(timespec='seconds') + ' (JST)'
            if day != explicit.date() or term not in invoice.get('description', '').splitlines():
                return None
            return explicit
        if r.get("source") == "PHONE" and r.get("visit_at") and r.get("created_at"):
            visit_day = date.fromisoformat(r["visit_at"][:10])
            created_day = parse_dt(r["created_at"]).astimezone(ZoneInfo("Asia/Tokyo")).date()
            description = invoice.get("description", "")
            if (day == visit_day and created_day == visit_day - timedelta(days=1)
                    and ("08:00（日本時間）" in description or "08:00 JST" in description)):
                deadline = datetime.combine(day, datetime.min.time(),
                                            tzinfo=ZoneInfo("Asia/Tokyo")) + timedelta(hours=8)
        return deadline
    except (ValueError, TypeError, KeyError):
        return None


def send_payment_reminder(reservation, invoice):
    """Send the approved email once in the final 12 hours of the Square term."""
    deadline = square_payment_deadline(invoice, reservation)
    now = datetime.now(timezone.utc)
    if (reservation.get("status") != "INVOICED" or not isinstance(invoice, dict)
            or invoice.get("status") != "UNPAID"
            or invoice.get("id") != reservation.get("square_invoice_id")
            or deadline is None or not deadline - timedelta(hours=12) <= now < deadline
            or not (reservation.get("email") and SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_FROM)):
        return False
    recipient = (invoice.get("primary_recipient") or {}).get("email_address", "")
    if recipient.strip().lower() != reservation["email"].strip().lower():
        return False
    with REMINDER_LOCK:
        c = con()
        try:
            claimed = c.execute(
                "UPDATE reservations SET reminder_status='SENDING',reminder_error=NULL "
                "WHERE id=? AND status='INVOICED' AND square_invoice_id=? AND reminder_status IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM reservations other WHERE other.square_invoice_id=? "
                "AND other.id!=? AND other.reminder_status IS NOT NULL)",
                (reservation["id"], invoice["id"], invoice["id"], reservation["id"])
            ).rowcount
            c.commit()
            if not claimed:
                return False
        finally:
            c.close()
        visit = parse_dt(reservation["visit_at"])
        text_body = (
            f"{reservation['guest_name']}様\n\n"
            "この度は西天満つきやをご予約いただき、誠にありがとうございます。\n\n"
            f"{visit.month}月{visit.day}日{visit:%H:%M}・{reservation['party_size']}名様のご予約につきまして、"
            f"現在、事前決済{reservation['amount']:,}円のお支払いが確認できておりません。\n\n"
            "当店では、事前決済の完了をもってご予約確定となります。恐れ入りますが、先にお送りしたSquareの請求案内をご確認のうえ、お手続きをお願いいたします。\n\n"
            "お支払期限までに決済が完了しない場合、ご予約はお取り消しとなりますので、あらかじめご了承ください。\n\n"
            "請求案内が見当たらない場合や、お支払いができない場合は、このメールへご返信ください。行き違いでお手続き済みの場合はご容赦ください。\n\n"
            "西天満つきや"
        )
        try:
            # Re-read both authorities immediately before the external send.
            c = con()
            current = c.execute("SELECT status,square_invoice_id,email,amount,visit_at,party_size FROM reservations WHERE id=?",
                                (reservation["id"],)).fetchone()
            c.close()
            fresh = square(f"/v2/invoices/{invoice['id']}")["invoice"]
            fresh_deadline = square_payment_deadline(fresh, reservation)
            if (not current or current["status"] != "INVOICED"
                    or current["square_invoice_id"] != invoice["id"]
                    or any(current[key] != reservation.get(key) for key in ("email", "amount", "visit_at", "party_size"))
                    or fresh.get("id") != invoice["id"] or fresh.get("status") != "UNPAID"
                    or fresh_deadline != deadline
                    or not deadline - timedelta(hours=12) <= datetime.now(timezone.utc) < deadline
                    or (fresh.get("primary_recipient") or {}).get("email_address", "").strip().lower() != recipient.strip().lower()):
                raise ValueError("予約・Square請求の状態が変更されました")
            message = EmailMessage()
            message["Subject"] = "【西天満つきや】ご予約の事前決済について"
            message["From"] = mail_sender()
            message["To"] = recipient
            message.set_content(text_body)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(message)
        except Exception as exc:
            # Ambiguous send failures remain claimed to prevent duplicate delivery.
            c = con()
            c.execute("UPDATE reservations SET reminder_status='ERROR',reminder_error=? WHERE id=?",
                      (type(exc).__name__, reservation["id"]))
            c.commit(); c.close()
            return False
        c = con()
        c.execute("UPDATE reservations SET reminder_status='SENT',reminder_sent_at=? WHERE id=?",
                  (now_iso(), reservation["id"]))
        c.commit(); c.close()
        return True


def expire_public_reservations():
    """Release expired public holds only after Square confirms an invoice is unpaid."""
    web_checkout.expire(sys.modules[__name__])
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=PUBLIC_PAYMENT_HOURS)).isoformat()
    now_jp = datetime.now(timezone(timedelta(hours=9)))
    c = con()
    expired = 0
    try:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            "SELECT id,square_invoice_id,source,visit_at,created_at,invoice_issued_at,invoice_payment_due_at,reminder_status,payment_method "
            "FROM reservations WHERE "
            "((source='WEB' AND payment_method!='BANK' AND square_invoice_id IS NULL AND created_at<=?) "
            "OR (source IN ('WEB','PHONE') AND square_invoice_id IS NOT NULL "
            "AND COALESCE(invoice_issued_at,created_at)<=? "
            "AND reminder_status IN ('SENT','SKIPPED_LEGACY')) "
            "OR (source='PHONE' AND square_invoice_id IS NOT NULL AND invoice_payment_due_at<=?) "
            "OR (source='PHONE' AND square_invoice_id IS NOT NULL "
            "AND substr(visit_at,1,10)<=?)) "
            "AND status IN ('PENDING','INVOICED','ERROR') "
            "ORDER BY id LIMIT 50", (cutoff, cutoff, now_iso(), now_jp.date().isoformat())
        ).fetchall()
        for row in rows:
            # Only PHONE reservations taken the calendar day before service have the
            # special 08:00 JST deadline. Older bookings keep the existing 48h rules.
            visit_day = date.fromisoformat(row["visit_at"][:10])
            created_day = parse_dt(row["created_at"]).astimezone(
                timezone(timedelta(hours=9))).date()
            phone_8am = (
                row["source"] == "PHONE"
                and created_day == visit_day - timedelta(days=1)
                and now_jp >= datetime.combine(
                    visit_day, datetime.min.time(), tzinfo=timezone(timedelta(hours=9))
                ) + timedelta(hours=8)
            )
            normal_expired = (
                row['source'] == 'PHONE' and row['invoice_payment_due_at']
                and parse_dt(row['invoice_payment_due_at']) <= datetime.now(timezone.utc)
            ) or (
                row["source"] == "WEB"
                and row["payment_method"] != "BANK"
                and not row["square_invoice_id"]
                and row["created_at"] <= cutoff
            ) or (
                row["source"] in ("WEB", "PHONE")
                and row["square_invoice_id"]
                and (row["invoice_issued_at"] or row["created_at"]) <= cutoff
                and row["reminder_status"] in ("SENT", "SKIPPED_LEGACY")
            )
            if not phone_8am and not normal_expired:
                continue
            iid = row["square_invoice_id"]
            if iid:
                try:
                    invoice = square(f"/v2/invoices/{iid}")["invoice"]
                    status = invoice["status"]
                    if status == "UNPAID":
                        deadline = square_payment_deadline(invoice, dict(row))
                        if deadline is None or now_jp < deadline:
                            continue
                        square(f"/v2/invoices/{iid}/cancel",
                               body={"version": invoice["version"]})
                    elif status not in ("CANCELED", "CANCELLED"):
                        continue
                except Exception as exc:
                    print(f"Expiry check failed for reservation {row['id']}: {exc}")
                    continue
            c.execute("UPDATE reservations SET status='CANCELLED',staff_seen_at=NULL,cancellation_reason='PAYMENT_EXPIRED',cancelled_at=?,updated_at=? WHERE id=?",
                      (now_iso(), now_iso(), row["id"]))
            expired += 1
        c.commit()
    finally:
        c.close()
    return expired


def expiry_loop():
    while True:
        try:
            refunds.process(sys.modules[__name__])
            reconcile_reservations()
            expire_public_reservations()
            bank_transfer.overdue(sys.modules[__name__])
        except Exception as exc:
            print(f"Public reservation expiry failed: {exc}")
        try:
            cancellation_emails.process(sys.modules[__name__])
        except Exception as exc:
            print(f"Cancellation email worker failed: {type(exc).__name__}")
        time.sleep(300)


def october_private_time(visit_at, area):
    """October 2026 staff-entered private bookings may use a custom start."""
    visit = parse_dt(visit_at)
    return (visit.year == 2026 and visit.month == 10
            and area in ("PRIVATE", *ROOMS)
            and 17 <= visit.hour < 23
            and visit > datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None))


def approved_staff_time_exception(data, direct):
    """Owner-approved, single-party legacy reservation exception (2026-10-18)."""
    if not direct or data.get("seating_area") not in ("PRIVATE", *ROOMS):
        return False
    key = "|".join((str(data.get("email", "")).strip().lower(),
                    str(data.get("visit_at", "")), str(data.get("party_size", ""))))
    return (hashlib.sha256(key.encode()).hexdigest() == "88bd040da22421392f8e5a516df10cbbcb5745b1527e59d1808713ebcc81c879"
            and parse_dt(data["visit_at"]) > datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None))


def availability_check(
    c,
    seating_area,
    visit_at,
    party_size,
    counter_round=None,
    duration_minutes=150,
    exclude_id=None,
    allow_custom_start=False,
    allow_room_exception=False,
    restore_existing=False
):
    if not seating_area:
        return (
            False,
            "席を選択してください"
        )

    if party_size < 1:
        return (
            False,
            "人数が不正です"
        )

    visit = parse_dt(visit_at)

    closure_reason = booking_policy.main_restaurant_closure_reason(visit.date(), seating_area)
    if closure_reason and not restore_existing:
        return False, closure_reason

    if seating_area == "PRIVATE":
        eligible = [area for area in ROOMS if public_party_allowed(area, party_size)]
        if not eligible:
            return False, "個室は2〜8名で選択してください"
        for room in eligible:
            ok, _ = availability_check(c, room, visit_at, party_size, None, duration_minutes, exclude_id, allow_custom_start)
            if ok:
                return True, f"個室{room[-1]}に空きがあります"
        return False, "ご希望の人数で利用できる個室は満室です"

    if seating_area == "COUNTER" or seating_area in ROOMS:
        slot = (visit.hour, visit.minute)
        if slot not in ((18, 0), (20, 30)) and not october_private_time(visit_at, seating_area) and not (allow_custom_start and seating_area in ROOMS and 18 <= visit.hour < 23):
            return (False, "開始時刻は18:00または20:30を選択してください")
        if duration_minutes != 150:
            return (False, "利用時間は150分です")
        expected_round = 1 if slot == (18, 0) else 2
        if seating_area == "COUNTER" and counter_round != expected_round:
            return (False, "カウンターの部と開始時刻が一致しません")

    active = ACTIVE_STATUSES

    if seating_area == "COUNTER":
        if party_size > COUNTER_CAPACITY:
            return False, "カウンターの人数が席数を超えています"
        if counter_round not in (1, 2):
            return (
                False,
                "カウンター回転を選択してください"
            )

        params = [
            visit.date().isoformat(),
            counter_round,
            *active
        ]

        sql = """
            SELECT
                COALESCE(
                    SUM(party_size),
                    0
                ) n
            FROM reservations
            WHERE
                substr(visit_at,1,10)=?
            AND seating_area='COUNTER'
            AND (counter_round=? OR vip_through=1)
            AND status IN (?,?,?,?)
        """

        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(
                exclude_id
            )

        used = int(
            c.execute(
                sql,
                params
            ).fetchone()["n"]
        )

        blocked = c.execute("SELECT COUNT(*) n FROM seat_blocks WHERE visit_date=? AND time_text=? AND seating_area='COUNTER'", (visit.date().isoformat(), visit.strftime('%H:%M'))).fetchone()["n"]
        remain = COUNTER_CAPACITY - used - blocked

        if party_size > remain:
            return (
                False,
                f"カウンター残り{max(remain, 0)}席です"
            )

        return (
            True,
            f"残席{remain - party_size}"
        )

    if seating_area in ROOMS:
        if not public_party_allowed(seating_area, party_size) and not (allow_room_exception and seating_area == "PRIVATE3" and 2 <= party_size <= 3):
            return False, "この個室の利用人数の範囲外です"
        if c.execute("SELECT 1 FROM seat_blocks WHERE visit_date=? AND time_text=? AND seating_area=?", (visit.date().isoformat(), visit.strftime('%H:%M'), seating_area)).fetchone():
            return False, "この個室はブロックされています"
        end = (
            visit
            + timedelta(
                minutes=duration_minutes
            )
        )

        if visit.year == 2026 and visit.month == 10:
            for block in c.execute("SELECT time_text FROM seat_blocks WHERE visit_date=? AND seating_area=?", (visit.date().isoformat(), seating_area)):
                block_start = parse_dt(visit.date().isoformat() + "T" + block["time_text"])
                if visit < block_start + timedelta(minutes=150) and block_start < end:
                    return False, "この個室はブロックされています"

        rows = c.execute(
            """
            SELECT
                id,
                visit_at,
                duration_minutes, vip_through
            FROM reservations
            WHERE seating_area=?
            AND status IN (?,?,?,?)
            """,
            (
                seating_area,
                *active
            )
        ).fetchall()

        for row in rows:
            if (
                exclude_id is not None
                and row["id"] == exclude_id
            ):
                continue

            other_start = parse_dt(
                row["visit_at"]
            )

            if row["vip_through"]:
                other_start=other_start.replace(hour=18,minute=0)
            other_end = (
                other_start
                + timedelta(
                    minutes=int(
                        300 if row["vip_through"] else row["duration_minutes"]
                        or 150
                    )
                )
            )

            if (
                visit < other_end
                and other_start < end
            ):
                return (
                    False,
                    "この個室は同時間帯に予約があります"
                )

        return (
            True,
            "予約可能です"
        )

    if seating_area == "UNASSIGNED":
        return (
            True,
            "未割当です"
        )

    return (
        False,
        "席の指定が不正です"
    )


class ReservationNoteConflict(ValueError):
    pass


def bank_confirmation_record(data):
    """Owner-checked transfers need only the final authenticated confirmation."""
    if data.get('owner_confirmed') is True:
        return '朝倉確認済み・最終チェックで確定', '朝倉'
    reference, staff = data.get('reference'), data.get('confirmed_by')
    if (not isinstance(reference, str) or not reference.strip() or len(reference) > 200
            or not isinstance(staff, str) or not staff.strip() or len(staff) > 100):
        raise ValueError('朝倉確認済みの最終チェックが必要です')
    return reference.strip(), staff.strip()


def restore_paid_bank_reservation(c, rid, data):
    """Restore an expired bank booking under the caller's write transaction; no invoice."""
    r = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    if (not r or r['status'] != 'CANCELLED' or r['payment_method'] != 'BANK'
            or r['square_invoice_id']
            or r['payment_source'] or r['payment_confirmed_at']):
        raise ValueError('取消済みの銀行振込予約のみ復活できます')
    if (data.get('paid') is not True or type(data.get('amount')) is not int
            or data['amount'] != r['amount'] or r['amount'] <= 0):
        raise ValueError('全額入金の確認と正しい金額が必要です')
    reference, staff = bank_confirmation_record(data)
    if data.get('expected_visit_at') != r['visit_at']:
        raise ValueError('予約が更新されています。再読み込みしてください')
    visit = parse_dt(r['visit_at'])
    if visit.tzinfo is None:
        visit = visit.replace(tzinfo=ZoneInfo('Asia/Tokyo'))
    if visit <= datetime.now(timezone.utc):
        raise ValueError('過去の予約は復活できません')
    if r['seating_area'] not in ('COUNTER', *ROOMS) or r['vip_through']:
        raise ValueError('通常のお席の予約のみ復活できます')
    ok, message = availability_check(c, r['seating_area'], r['visit_at'], r['party_size'],
                                     r['counter_round'], int(r['duration_minutes'] or 150), rid,
                                     restore_existing=True)
    if not ok:
        raise ValueError(message)
    ts = now_iso()
    old_note = r['guest_note'] or ''
    note = old_note + f'\n【振込済み予約復活】{ts}／{staff.strip()}／{reference.strip()}／追加請求なし'
    c.execute("INSERT INTO reservation_note_history(reservation_id,old_note,new_note,changed_at) VALUES(?,?,?,?)",
              (rid, old_note, note, ts))
    c.execute("INSERT INTO payment_audit(reservation_id,source,amount,reference,confirmed_by,created_at) VALUES(?,?,?,?,?,?)",
              (rid, 'BANK', r['amount'], reference.strip(), staff.strip(), ts))
    c.execute("UPDATE reservations SET status='CONFIRMED',payment_source='BANK',payment_confirmed_at=?,"
              "cancellation_reason=NULL,cancelled_at=NULL,staff_seen_at=NULL,guest_note=?,last_error=NULL,updated_at=? WHERE id=?",
              (ts, note, ts, rid))
    return dict(c.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone())


def change_reservation_note(c, rid, data):
    """Edit the booking note only; never send confirmations or change seat/payment data."""
    note = data.get("guest_note")
    expected = data.get("expected_guest_note")
    if not isinstance(note, str) or not isinstance(expected, str):
        raise ValueError("備考の入力形式が不正です")
    if len(note) > 5000 or "\x00" in note:
        raise ValueError("備考は5000文字以内で入力してください")
    r = c.execute("SELECT guest_note FROM reservations WHERE id=?", (rid,)).fetchone()
    if not r:
        raise ValueError("予約が見つかりません")
    old = r["guest_note"] or ""
    if old != expected:
        raise ReservationNoteConflict("別の画面で備考が更新されています。予約を開き直して確認してください")
    note = note.strip()
    if note != old:
        ts = now_iso()
        c.execute("INSERT INTO reservation_note_history(reservation_id,old_note,new_note,changed_at) VALUES(?,?,?,?)",
                  (rid, old, note, ts))
        c.execute("UPDATE reservations SET guest_note=?,updated_at=? WHERE id=?", (note, ts, rid))
    return {"ok": True, "id": rid, "guest_note": note}


def change_direct_room(c, rid, data):
    """Atomically move a confirmed direct booking without recreating it."""
    r = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    if not r or r["source"] != "DIRECT" or r["status"] != "CONFIRMED" or r["vip_through"] or r["square_invoice_id"]:
        raise ValueError("この予約は個室変更の対象外です")
    if (data.get("expected_seating_area") != r["seating_area"]
            or data.get("expected_visit_at") != r["visit_at"]
            or data.get("expected_party_size") != r["party_size"]):
        raise ValueError("予約が更新されています。再読み込みしてください")
    room = data.get("seating_area")
    if r["seating_area"] not in ROOMS or room not in ROOMS or not public_party_allowed(room, r["party_size"]):
        raise ValueError("人数に対応する個室を選択してください")
    ok, message = availability_check(c, room, r["visit_at"], r["party_size"], None,
                                     int(r["duration_minutes"] or 150), rid)
    if not ok:
        raise ValueError(message)
    c.execute("UPDATE reservations SET seating_area=?,updated_at=? WHERE id=?", (room, now_iso(), rid))
    return {"ok": True, "id": rid, "seating_area": room}


def change_direct_time(c, rid, data):
    """Change only the clock of an unpaid direct reservation, retaining its identity."""
    r = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    if not r:
        raise ValueError("予約が見つかりません")
    if r["source"] != "DIRECT" or r["status"] != "CONFIRMED" or r["vip_through"] or r["square_invoice_id"] or r["payment_confirmed_at"]:
        raise ValueError("この予約は時刻変更の対象外です")
    if data.get("expected_visit_at") != r["visit_at"]:
        raise ValueError("予約が更新されています。再読み込みしてください")
    clock = data.get("time", "")
    if not isinstance(clock, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
        raise ValueError("時刻を確認してください")
    visit = r["visit_at"][:10] + "T" + clock
    if parse_dt(visit) <= datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None):
        raise ValueError("過去の時刻には変更できません")
    rnd = (1 if clock == "18:00" else 2) if r["seating_area"] == "COUNTER" else None
    ok, message = availability_check(c, r["seating_area"], visit, r["party_size"], rnd, int(r["duration_minutes"] or 150), rid)
    if not ok:
        raise ValueError(message)
    if visit != r["visit_at"]:
        note = (r["guest_note"] or "") + "\n【開始時刻変更】" + r["visit_at"] + " → " + visit
        c.execute("UPDATE reservations SET visit_at=?,counter_round=?,guest_note=?,updated_at=? WHERE id=?", (visit, rnd, note, now_iso(), rid))
    return {"ok": True, "visit_at": visit}


def change_direct_party(c, rid, data):
    r = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    pending_bank = bool(r and r["payment_method"] == "BANK" and r["status"] == "PENDING"
                        and not r["payment_source"] and not r["square_order_id"])
    direct = bool(r and r["source"] == "DIRECT" and r["status"] == "CONFIRMED")
    if not r or not (direct or pending_bank) or r["vip_through"] or r["square_invoice_id"] or r["payment_confirmed_at"] or r["source"] == web_checkout.SOURCE:
        raise ValueError("この予約は人数変更の対象外です")
    if data.get("expected_party_size") != r["party_size"] or data.get("expected_visit_at") != r["visit_at"] or data.get("expected_amount") != r["amount"]:
        raise ValueError("予約が更新されています。再読み込みしてください")
    party = data.get("party_size")
    if type(party) is not int or not 1 <= party <= 8:
        raise ValueError("人数を確認してください")
    if parse_dt(r["visit_at"]) <= datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None):
        raise ValueError("過去の予約は変更できません")
    if not r["party_size"] or int(r["amount"]) % r["party_size"]:
        raise ValueError("1名様の料金を確認してください")
    ok, message = availability_check(c, r["seating_area"], r["visit_at"], party, r["counter_round"], int(r["duration_minutes"] or 150), rid,
                                     allow_custom_start=bool(r["seating_area"] in ROOMS), restore_existing=pending_bank)
    if not ok:
        raise ValueError(message)
    amount = int(r["amount"]) // r["party_size"] * party
    if party != r["party_size"]:
        note = (r["guest_note"] or "") + f"\n【人数変更】{r['party_size']}名 → {party}名（料理代 {r['amount']}円 → {amount}円）"
        c.execute("UPDATE reservations SET party_size=?,amount=?,guest_note=?,updated_at=? WHERE id=?", (party, amount, note, now_iso(), rid))
    return {"ok": True, "party_size": party, "amount": amount}


def nearby_availability(c, visit_at, area, party_size, limit=6):
    """Offer actual free seats, prioritizing the requested day and time."""
    visit = parse_dt(visit_at)
    if area not in ("COUNTER", "PRIVATE", *ROOMS) or party_size < 1:
        raise ValueError("席または人数が不正です")
    candidates = []
    areas = ([*ROOMS, "COUNTER"] if area == "PRIVATE" else [area] + [a for a in ("COUNTER", *ROOMS) if a != area])
    times = [visit.strftime("%H:%M")] + [t for t in ("18:00", "20:30") if t != visit.strftime("%H:%M")]
    for offset in [0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6, 7, -7]:
        day = (visit + timedelta(days=offset)).date()
        if day < datetime.now(timezone(timedelta(hours=9))).date():
            continue
        for time_text in times:
            if time_text not in ("18:00", "20:30"):
                continue
            proposed = f"{day.isoformat()}T{time_text}"
            if datetime.fromisoformat(proposed) <= datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None):
                continue
            for candidate_area in areas:
                if proposed == visit_at and candidate_area == area:
                    continue
                rnd = (1 if time_text == "18:00" else 2) if candidate_area == "COUNTER" else None
                ok, _ = availability_check(c, candidate_area, proposed, party_size, rnd)
                if ok:
                    candidates.append({"visit_at": proposed, "seating_area": candidate_area,
                                       "counter_round": rnd})
                if len(candidates) >= limit:
                    return candidates
    return candidates


def seating_label(r):
    area = r["seating_area"]

    if area == "COUNTER":
        if dict(r).get("vip_through"):
            return "カウンター 1部・2部通し"
        return (
            f"カウンター "
            f"{r['counter_round'] or ''}部"
        )

    if area == "PRIVATE1":
        return "個室1"

    if area == "PRIVATE2":
        return "個室2"

    if area == "PRIVATE3":
        return "個室3"

    return "未割当"


def customer_seating_label(r):
    return "個室" if str(r["seating_area"]).startswith("PRIVATE") else seating_label(r)


def annex_message(r, english=False):
    if not str(r["seating_area"]).startswith("PRIVATE"):
        return ""
    return 'Private rooms are in a separate building from the main restaurant. Please come to Tsukiya at Nishi-Tenma, Bettei (Annex), 3-8-7 Nishitenma, Kita-ku, Osaka.' if english else '個室は本店とは別の建物でのご案内となります。西天満つきや 別邸（大阪市北区西天満3-8-7）までお越しください。'


def cancellation_token(r):
    if not ADMIN_TOKEN:
        return ""
    data = "customer-cancel-v1|" + "|".join(str(r[k] or "") for k in ("id", "created_at", "email", "visit_at"))
    return str(r["id"]) + "." + hmac.new(ADMIN_TOKEN.encode(), data.encode(), hashlib.sha256).hexdigest()


def cancellation_url(r):
    token = cancellation_token(r)
    return APP_BASE_URL + "/cancel-reservation#" + token if token and APP_BASE_URL.startswith("https://") else ""


def customer_cancellation(token, confirm=False, accepted=False, expected_fee=None):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]{1,18}\.[0-9a-f]{64}", token):
        return 403, {"error": "リンクが無効です。店舗へお問い合わせください。"}
    c = con()
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM reservations WHERE id=?", (int(token.split(".")[0]),)).fetchone()
        if not r or not hmac.compare_digest(cancellation_token(r), token):
            return 403, {"error": "リンクが無効です。店舗へお問い合わせください。"}
        visit = datetime.fromisoformat(r["visit_at"])
        if visit.tzinfo is None:
            visit = visit.replace(tzinfo=timezone(timedelta(hours=9)))
        now = datetime.now(timezone(timedelta(hours=9)))
        if now >= visit:
            return 410, {"error": "オンラインでのお手続き期限を過ぎています。店舗へお問い合わせください。"}
        if r["status"] == "CANCELLED":
            return 200, {"cancelled": True, "refund": refunds.public(c, r["id"])}
        if r["status"] != "CONFIRMED":
            return 409, {"error": "この予約はオンラインでキャンセルできません。店舗へお問い合わせください。"}
        if reservation_changes.records(c, r['id']):
            return 409, {"error": "人数変更の差額請求があるため、キャンセルは店舗へお問い合わせください。"}
        fee = r["amount"] if now.date() >= visit.astimezone(now.tzinfo).date() - timedelta(days=3) else 0
        if confirm:
            if accepted is not True or expected_fee != fee:
                return 400, {"error": "キャンセル規定をご確認ください。"}
            c.execute("UPDATE reservations SET status='CANCELLED',staff_seen_at=NULL,cancellation_reason='CUSTOMER',cancelled_at=?,updated_at=? WHERE id=? AND status='CONFIRMED'", (now_iso(), now_iso(), r["id"]))
            refunds.enqueue(c, r, fee, now_iso())
            c.commit()
            return 200, {"cancelled": True, "fee": fee, "refund": refunds.public(c, r["id"])}
        return 200, {"cancelled": False, "name": r["guest_name"], "visit_at": r["visit_at"], "party_size": r["party_size"], "amount": r["amount"], "paid": r["amount"] if r["payment_source"] in ("SQUARE", "BANK") else 0, "fee": fee}
    finally:
        c.close()


def send_confirmation(r):
    if (
        not SMTP_HOST
        or not SMTP_USER
        or not SMTP_PASS
        or not MAIL_FROM
    ):
        return (
            False,
            "SMTP未設定"
        )

    if not r["email"]:
        return (
            False,
            "メールアドレス未設定"
        )

    msg = EmailMessage()

    policy = booking_policy.details(r)
    customer_note = str(dict(r).get("confirmation_note") or "").strip()
    note_ja = "【ご予約に関する備考】\n" + customer_note if customer_note else ""
    note_en = "Reservation notes:\n" + customer_note if customer_note else ""
    english = r["booking_language"] == "en"
    direct = r["source"] in ("DIRECT", "CONCIERGE_LINE")
    confirmation_ja = "下記の内容にてご予約を確定いたしました。\nお料理代は当日店舗にてお支払いください。" if direct else "ご入金を確認し、\n下記の内容にてご予約を確定いたしました。"
    confirmation_en = "Your reservation is confirmed. Please pay for your course at the restaurant on the day of your visit." if direct else "We have received your full payment and confirmed your reservation."
    if r["source"] == web_checkout.SOURCE:
        confirmation_ja = "公式予約画面でのお料理代の全額カード決済を確認し、下記の内容にてご予約を確定いたしました。"
        confirmation_en = "We have received your full card payment through our reservation page and confirmed your reservation."
    msg["Subject"] = ("Nishitenma Tsukiya | Reservation Confirmed" if english
                      else "【西天満 つきや】ご予約確定のご案内")

    msg["From"] = mail_sender()
    msg["To"] = r["email"]

    chat_data = 'guest-chat-v1|' + '|'.join(str(r[k] or '') for k in ('id','created_at','email','visit_at'))
    chat_proof = str(r['id']) + '.' + hmac.new(ADMIN_TOKEN.encode(), chat_data.encode(), hashlib.sha256).hexdigest() if ADMIN_TOKEN else ''
    chat_link = APP_BASE_URL + '/concierge/en#visit=' + chat_proof if chat_proof and APP_BASE_URL.startswith('https://') else ''
    staff_en = ('Talk to staff on the day of your visit (Japan time):\n' + chat_link + '\nSend messages in English; Mr Asakura replies via LINE with translation. Please allow time during service.') if chat_link else ''
    cancel_link = cancellation_url(r)
    cancel_ja = ("ご予約のキャンセルはこちら\n" + cancel_link + "\nリンク先で内容とキャンセル規定をご確認のうえ、お手続きください。キャンセル規定に基づき返金対象額を計算します。Squareカード決済は店舗責任者の確認・承認後に返金し、銀行振込・処理できない場合は店舗で対応します。カード明細への反映には通常さらに2〜7営業日ほどかかる場合があります。") if cancel_link else "キャンセルをご希望の場合は店舗へお問い合わせください。"
    cancel_en = ("Cancel your reservation:\n" + cancel_link + "\nReview the cancellation policy before confirming. Eligible Square card payments are refunded after approval by the restaurant owner. Bank transfers and exceptions require assistance from the restaurant. Card statements may take a further 2–7 business days to reflect refunds.") if cancel_link else "Please contact the restaurant to cancel your reservation."
    if english:
        seat_en = "Private Room" if str(r["seating_area"]).startswith("PRIVATE") else "Counter"
        msg.set_content(
            f"""Dear {r['guest_name']},

Thank you for choosing Nishitenma Tsukiya.
{confirmation_en}

Date and time: {r['visit_at']} (Japan time)
Seating: {seat_en}
Guests: {r['party_size']}
{'Course price (pay at restaurant)' if direct else 'Course payment'}: JPY {r['amount']:,}
{policy['beverage_policy_en']}

{note_en}

{annex_message(r, True)}

We look forward to welcoming you. Please arrive on time, as each seating begins together.

{staff_en}

{cancel_en}

Nishitenma Tsukiya
"""
        )
    else:
        msg.set_content(
        f"""
{r['guest_name']} 様

このたびは西天満 つきやをご予約いただき、
誠にありがとうございます。

{confirmation_ja}

ご来店日時：{r['visit_at']}
お席：{customer_seating_label(r)}
人数：{r['party_size']}名様
お料理代：{r['amount']:,}円
予約区分：{policy['reservation_category']}
{policy['beverage_policy']}

{note_ja}

{annex_message(r)}

{"1部・2部通し予約です。ご指定の時刻から23時までお過ごしいただけます。" if dict(r).get("vip_through") else "当店では皆様一斉にお料理のご提供を開始いたします。ご来店時間をお守りくださいますようお願い申し上げます。"}

当日は心を尽くしてお迎えいたします。
どうぞお気をつけてお越しくださいませ。

{cancel_ja}

西天満 つきや
"""
        )

    try:
        with smtplib.SMTP(
            SMTP_HOST,
            SMTP_PORT,
            timeout=30
        ) as s:
            s.starttls()

            s.login(
                SMTP_USER,
                SMTP_PASS
            )

            s.send_message(
                msg
            )

        return (
            True,
            ""
        )

    except Exception as e:
        return (
            False,
            str(e)
        )


def import_booking(event):
    data = (
        event.get("data")
        or {}
    )

    obj = (
        data.get("object")
        or {}
    )

    booking = (
        obj.get("booking")
        or obj
    )

    booking_id = booking.get("id")

    if not booking_id:
        return

    start_at = (
        booking.get("start_at")
        or now_iso()
    )

    customer_id = booking.get(
        "customer_id"
    )

    guest_name = "Square予約"
    email = ""
    phone = ""

    if customer_id:
        try:
            customer = square(
                f"/v2/customers/{customer_id}"
            )["customer"]

            guest_name = (
                customer.get("given_name")
                or customer.get("family_name")
                or "Square予約"
            )

            email = (
                customer.get("email_address")
                or ""
            )

            phone = (
                customer.get("phone_number")
                or ""
            )

        except Exception:
            pass

    duration = 150

    segs = (
        booking.get(
            "appointment_segments"
        )
        or []
    )

    if segs:
        try:
            duration = max(
                1,
                int(
                    sum(
                        int(
                            x.get(
                                "duration_minutes"
                            )
                            or 0
                        )
                        for x in segs
                    )
                )
            )

        except Exception:
            duration = 150

    c = con()

    existing = c.execute(
        """
        SELECT *
        FROM reservations
        WHERE square_booking_id=?
        """,
        (
            booking_id,
        )
    ).fetchone()

    if not existing:
        try:
            booking_blocks.ensure_allowed(c, phone, email)
        except booking_blocks.BookingBlocked:
            print("Square booking import refused by customer booking restriction:", booking_id)
            c.close()
            return

    ts = now_iso()

    if existing:
        c.execute(
            """
            UPDATE reservations
            SET
                guest_name=?,
                phone=?,
                email=?,
                visit_at=?,
                duration_minutes=?,
                updated_at=?
            WHERE id=?
            """,
            (
                guest_name,
                phone,
                email,
                start_at,
                duration,
                ts,
                existing["id"]
            )
        )

    else:
        c.execute(
            """
            INSERT INTO reservations(
                source,
                guest_name,
                phone,
                email,
                visit_at,
                party_size,
                course_name,
                amount,
                seating_area,
                counter_round,
                duration_minutes,
                status,
                square_booking_id,
                created_at,
                updated_at
            )
            VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                "SQUARE",
                guest_name,
                phone,
                email,
                start_at,
                1,
                "お料理代",
                0,
                "UNASSIGNED",
                None,
                duration,
                "PENDING",
                booking_id,
                ts,
                ts
            )
        )

    c.commit()
    c.close()


def deliver_confirmation(reservation, retry_failed=False):
    check = con()
    try:
        if reservation_changes.summary(check, reservation['id'])['additional_unpaid']:
            return dict(reservation)
    finally:
        check.close()
    result = send_confirmation_sms(reservation)
    if not result.get("email") or result["status"] != "CONFIRMED":
        return result
    rid = result["id"]
    with EMAIL_LOCK:
        c = con()
        try:
            row = dict(c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
            if row.get("confirmation_sent_at"):
                return row
            if retry_failed and row.get("confirmation_email_status") == "ERROR":
                c.execute("UPDATE reservations SET confirmation_email_status=NULL WHERE id=? AND confirmation_sent_at IS NULL AND confirmation_email_status='ERROR'", (rid,))
                c.commit()
            if not (SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_FROM):
                c.execute("UPDATE reservations SET confirmation_email_status='NOT_CONFIGURED',last_error='SMTP未設定' WHERE id=? AND (confirmation_email_status IS NULL OR confirmation_email_status='NOT_CONFIGURED')", (rid,))
                c.commit()
            else:
                claimed = c.execute("UPDATE reservations SET confirmation_email_status='SENDING' WHERE id=? AND confirmation_sent_at IS NULL AND (confirmation_email_status IS NULL OR confirmation_email_status='NOT_CONFIGURED')", (rid,)).rowcount
                c.commit()
                if claimed:
                    ok, error = send_confirmation(row)
                    c.execute("UPDATE reservations SET confirmation_email_status=?,confirmation_sent_at=?,last_error=? WHERE id=?", ("SENT" if ok else "ERROR", now_iso() if ok else None, None if ok else error, rid))
                    c.commit()
            return dict(c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
        finally:
            c.close()


def refresh_sms_delivery(row):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        return
    for prefix in ("invoice_sms", "confirmation_sms"):
        sid = row.get(prefix + "_sid")
        if not sid or row.get(prefix + "_status") != "QUEUED":
            continue
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages/{sid}.json"
        auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Basic " + auth}), timeout=30) as response:
            result = json.load(response)
        if result.get("status") in ("failed", "undelivered", "canceled"):
            c = con()
            c.execute(f"UPDATE reservations SET {prefix}_status='ERROR',{prefix}_error=? WHERE id=?", ("SMS配信失敗。Twilioの送信履歴を確認してください", row["id"]))
            c.commit(); c.close()


def reconcile_reservations():
    """Repair missed payment events and unclaimed notifications without duplicate sends."""
    if not RECONCILE_LOCK.acquire(blocking=False):
        return
    try:
        web_checkout.reconcile(sys.modules[__name__])
        reservation_changes.reconcile(sys.modules[__name__])
        c = con()
        rows = [dict(r) for r in c.execute("SELECT * FROM reservations WHERE status IN ('INVOICED','CONFIRMED') ORDER BY id")]
        c.close()
        for row in rows:
            try:
                refresh_sms_delivery(row)
            except Exception as exc:
                print(f"SMS delivery check failed for reservation {row['id']}: {type(exc).__name__}")
            try:
                if row["status"] == "INVOICED" and row.get("square_invoice_id"):
                    invoice = square(f"/v2/invoices/{row['square_invoice_id']}")["invoice"]
                    if invoice.get("status") == "PAID":
                        event = {"event_id": "reconcile-paid-" + invoice["id"], "type": "invoice.payment_made", "data": {"object": {"invoice": invoice}}}
                        process_square_event(event, json.dumps(event).encode())
                    else:
                        send_invoice_sms(row)
                        send_payment_reminder(row, invoice)
                elif row["status"] == "CONFIRMED":
                    deliver_confirmation(row)
            except Exception as exc:
                print(f"Reconciliation failed for reservation {row['id']}: {type(exc).__name__}")
    finally:
        RECONCILE_LOCK.release()


def process_square_event(event, raw):
    event_id = event.get("event_id") or event.get("id") or hashlib.sha256(raw).hexdigest()
    event_type = event.get("type") or ""

    # Import first so a failed Square lookup can be retried by the webhook sender.
    if event_type in ("booking.created", "booking.updated"):
        import_booking(event)

    payment = ((event.get("data") or {}).get("object") or {}).get("payment") or {}
    if event_type in ("payment.created", "payment.updated") and str(payment.get("reference_id", "")).startswith("wc:"):
        web_checkout.finalize(sys.modules[__name__], payment)
    guest_service.payment_event(sys.modules[__name__], event)
    c = con()
    confirmed = None
    try:
        c.execute("BEGIN IMMEDIATE")
        if c.execute(
            "SELECT 1 FROM webhook_events WHERE event_id=?", (event_id,)
        ).fetchone():
            c.rollback()
            return True

        if event_type == "invoice.payment_made":
            obj = (event.get("data") or {}).get("object") or {}
            invoice = obj.get("invoice") or obj
            # A payment event can also represent an installment. Confirm only
            # when Square reports the invoice itself paid in full.
            if invoice.get("status") == "PAID" and invoice.get("id"):
                row = c.execute(
                    "SELECT * FROM reservations WHERE square_invoice_id=?",
                    (invoice["id"],)
                ).fetchone()
                if row and row["status"] in ("PENDING", "INVOICED", "ERROR"):
                    # Confirm against Square's current state, rather than an old
                    # webhook snapshot. A bank-confirmed reservation stays bank-paid.
                    current = square(f"/v2/invoices/{invoice['id']}")["invoice"]
                    if current.get("status") != "PAID":
                        row = None
                if row and row["status"] in ("PENDING", "INVOICED", "ERROR"):
                    c.execute(
                        "UPDATE reservations SET status='CONFIRMED', "
                        "payment_source='SQUARE', payment_confirmed_at=?, "
                        "last_error=NULL, updated_at=? WHERE id=?",
                        (now_iso(), now_iso(), row["id"])
                    )
                    confirmed = dict(c.execute(
                        "SELECT * FROM reservations WHERE id=?", (row["id"],)
                    ).fetchone())

        c.execute(
            "INSERT INTO webhook_events(event_id,event_type,received_at) VALUES(?,?,?)",
            (event_id, event_type, now_iso())
        )
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

    if confirmed:
        deliver_confirmation(confirmed)

    return False


@lru_cache(maxsize=3)
def crab_video(number):
    return base64.b64decode((BASE / "public" / "assets" / f"crab-{number}.mp4.b64").read_text(), validate=True)


def concierge_availability(day, party, through=False):
    expire_public_reservations()
    if through:
        return vip_slots.availability(sys.modules[__name__],day,party)
    slots = []
    c = con()
    try:
        for course in PUBLIC_COURSES:
            for clock, round_number in (("18:00", 1), ("20:30", 2)):
                if not public_slot_allowed(day, clock, course):
                    continue
                for area in ("COUNTER", "PRIVATE"):
                    rooms = ("COUNTER",) if area == "COUNTER" else ROOMS
                    if any(public_party_allowed(room, party) and availability_check(c, room, str(day)+"T"+clock, party, round_number if room == "COUNTER" else None, 150)[0] for room in rooms):
                        slots.append({"course": course, "area": area, "time": clock})
        return slots
    finally:
        c.close()


class Handler(
    BaseHTTPRequestHandler
):

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def end_headers(self):
        if SQUARE_ENV == "sandbox":
            self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        if urlparse(self.path).path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(
        self,
        fmt,
        *args
    ):
        if self.path.startswith("/line-image/"):
            return
        print(
            "%s - - [%s] %s"
            % (
                self.address_string(),
                self.log_date_time_string(),
                fmt % args
            )
        )

    def send_json(
        self,
        obj,
        status=200,
        headers=None
    ):
        b = json.dumps(
            obj,
            ensure_ascii=False,
            default=str
        ).encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(b))
        )

        for key, value in (headers or {}).items():
            self.send_header(key, value)

        self.end_headers()

        self.wfile.write(b)

    def send_html(
        self,
        text,
        status=200,
        loader=True
    ):
        if loader and "</head>" in text and "/crab-loader.js" not in text:
            text = text.replace("</head>", "<script>" + (BASE / "public" / "crab-loader.js").read_text() + "</script></head>", 1)
        if 'aria-label="管理画面"' in text:
            text += "<script>" + (BASE / "public" / "refund-badge.js").read_text() + "</script>"
        payment_page = urlparse(self.path).path in ("/book", "/book-en", "/book.html", "/book-en.html")
        if payment_page:
            text, payment_nonce = web_checkout.secure_page(text)
        b = text.encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

        self.send_header("Cache-Control", "no-store")
        if payment_page:
            self.send_header("Content-Security-Policy", web_checkout.csp(payment_nonce, SQUARE_ENV == "sandbox"))
        self.send_header("Referrer-Policy", "no-referrer")

        self.send_header(
            "Content-Length",
            str(len(b))
        )

        self.end_headers()

        self.wfile.write(b)

    def read_raw(self):
        n = int(
            self.headers.get(
                "Content-Length",
                "0"
            )
        )

        return self.rfile.read(n)

    def read_json(self):
        raw = self.read_raw()

        if not raw:
            return {}

        return json.loads(
            raw.decode("utf-8")
        )

    def auth(self):
        if not ADMIN_TOKEN:
            return False

        if hmac.compare_digest(
            self.headers.get("x-admin-token", ""),
            ADMIN_TOKEN
        ):
            return True

        cookies = self.headers.get("Cookie", "").split(";")
        for cookie in cookies:
            name, _, value = cookie.strip().partition("=")
            if name != SESSION_COOKIE:
                continue
            try:
                expiry, signature = value.split(".", 1)
                if int(expiry) <= int(datetime.now(timezone.utc).timestamp()):
                    return False
                expected = hmac.new(
                    ADMIN_TOKEN.encode(), expiry.encode(), hashlib.sha256
                ).hexdigest()
                return hmac.compare_digest(signature, expected)
            except (ValueError, TypeError):
                return False
        return False

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def cookie_header(self, value, max_age):
        secure = (
            APP_BASE_URL.startswith("https://")
            or self.headers.get("X-Forwarded-Proto", "") == "https"
        )
        return (
            f"{SESSION_COOKIE}={value}; HttpOnly; SameSite=Strict; "
            f"Path=/; Max-Age={max_age}"
            + ("; Secure" if secure else "")
        )

    def log_request(self, code="-", size="-"):
        self.log_message('"%s %s %s" %s %s', self.command, urlparse(self.path).path, self.request_version, str(code), str(size))

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path

        if gpt_reservations.handle_get(self, sys.modules[__name__], p, parse_qs(u.query)):
            return

        if p == "/api/public/checkout-config":
            lang = parse_qs(u.query).get("language", ["ja"])[0]
            return self.send_json(web_checkout.config(sys.modules[__name__], lang))

        if p == "/drink-capacity":
            return self.send_html((BASE / "public" / "drink-capacity.html").read_text(), loader=False)
        if p in ("/supplies", "/drinks"):
            return self.send_html((BASE / "public" / (p[1:] + ".html")).read_text(), loader=False)
        if p in ("/api/supplies", "/api/drinks", "/api/drink-capacity"):
            if not self.auth() and not supplies.authorized(DB, self.headers.get("X-Supplies-Token", "")):
                return self.send_json({"error": "経理LINEから新しいリンクを開いてください"}, 401)
            import drink_capacity
            inventory = drink_capacity if p == "/api/drink-capacity" else drinks if p == "/api/drinks" else supplies
            return self.send_json(inventory.view(DB), headers={"Cache-Control": "no-store"})

        if calendar_auth.handle_get(self, p, parse_qs(u.query), DB, APP_BASE_URL):
            return

        if p in ("/line-reservations", "/api/line/reservations/status") or p.startswith("/api/line/reservations/image/"):
            if not self.auth():
                return self.redirect("/login") if p == "/line-reservations" else self.send_json({"error":"unauthorized"},401)
            if p == "/line-reservations":
                return self.send_html((BASE / "public" / "line-reservations.html").read_text(),loader=False)
            if p == "/api/line/reservations/status":
                return self.send_json(line_reservations.status(sys.modules[__name__]),headers={"Cache-Control":"no-store"})
            try:
                content,mime=line_reservations.image_bytes(DB,p.rsplit('/',1)[-1])
            except Exception:
                return self.send_json({"error":"画像を取得できません。LINEで再送してください"},404)
            self.send_response(200)
            self.send_header("Content-Type",mime)
            self.send_header("Cache-Control","private, no-store")
            self.send_header("Content-Length",str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if p == "/vip-invite-qr":
            c=concierge.connect(DB)
            try:
                token=parse_qs(u.query).get('token',[''])[0]
                content=vip_access.invite_qr(c,token)
            except ValueError:
                return self.send_json({"error":"招待は使用済み、または期限切れです。"},404)
            finally:c.close()
            self.send_response(200)
            self.send_header("Content-Type","image/png")
            self.send_header("Cache-Control","private, no-store")
            self.send_header("Content-Length",str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if p in ("/attribution.js", "/web-checkout.js"):
            b = (BASE / "public" / p[1:]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
            return
        if p == "/marketing":
            return self.redirect("/reservations")
        if p in ("/api/marketing", "/api/marketing/archives"):
            if not self.auth():
                return self.send_json({"error":"unauthorized"},401)
            c=con()
            try:
                return self.send_json((marketing.archives if p.endswith("/archives") else marketing.report)(c,parse_qs(u.query)),headers={"Cache-Control":"no-store"})
            except ValueError as exc:
                return self.send_json({"error":str(exc)},400)
            finally: c.close()

        if p.startswith("/concierge-media/"):
            name = p.rsplit("/",1)[-1]
            if not re.fullmatch(r"[0-9a-f]{32}\.jpg", name):
                return self.send_error(404)
            path = Path(DB).parent / "concierge-media" / name
            if not path.is_file():
                return self.send_error(404)
            content=path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type","image/jpeg")
            self.send_header("Content-Length",str(len(content)))
            self.send_header("X-Content-Type-Options","nosniff")
            self.end_headers()
            self.wfile.write(content)
            return
        if p == "/api/guest-service/status":
            if not self.auth():
                return self.send_json({"error":"unauthorized"},401)
            return self.send_json(guest_service.status(sys.modules[__name__]),headers={"Cache-Control":"no-store"})
        if p == "/webhooks/instagram":
            challenge = instagram_concierge.verify(parse_qs(u.query))
            if challenge is None:
                return self.send_json({"error": "verification failed"}, 403)
            content = challenge.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
            return
        if p == "/api/public/course-catalog":
            return self.send_json({**concierge.concierge_menu.catalog(), "booking_paused": False}, headers={"Access-Control-Allow-Origin": "*"})
        if p == "/concierge/en":
            return self.send_html((BASE / "public" / "concierge-en.html").read_text(encoding="utf-8"), loader=False)
        if p == "/concierge/vip":
            return self.send_html((BASE / "public" / "vip-liff.html").read_text(), loader=False)
        if p == "/api/vip-liff/config":
            import vip_liff
            c=concierge.connect(DB)
            try:
                cfg=vip_liff.config(c)
                return self.send_json({'liff_id':cfg['liff_id'], 'configured':bool(cfg['liff_id'] and cfg['channel_id'])},headers={"Cache-Control":"no-store"})
            finally:c.close()
        if p == "/concierge/book":
            return self.send_html((BASE / "public" / "concierge-book.html").read_text(), loader=False)
        if p == "/concierge":
            if not self.auth():
                return self.redirect("/login")
            return self.send_html((BASE / "public" / "concierge.html").read_text(), loader=False)
        if p == "/api/vip-access":
            if not self.auth():return self.send_json({"error":"unauthorized"},401)
            c=concierge.connect(DB)
            try:return self.send_json([dict(r) for r in c.execute("SELECT a.*,c.name AS customer_name FROM vip_access a LEFT JOIN customers c ON c.id=a.customer_id ORDER BY a.updated DESC")])
            finally:c.close()
        if p == "/api/concierge/diagnostics":
            if not self.auth():
                return self.send_json({"error":"unauthorized"},401)
            import concierge_diagnostics
            return self.send_json(concierge_diagnostics.check(DB),headers={"Cache-Control":"no-store"})
        if p == "/api/reservation-notifications":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            q = parse_qs(u.query)
            rid = (q.get("reservation_id") or [""])[0]
            if rid and not rid.isdigit():
                return self.send_json({"error": "invalid reservation id"}, 400)
            c = concierge.connect(DB)
            try:
                return self.send_json(concierge.owner_notifications.status(c, int(rid) if rid else None),
                                      headers={"Cache-Control": "no-store"})
            finally:
                c.close()
        if p == "/api/concierge/status":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            return self.send_json(concierge.status(DB), headers={"Cache-Control": "no-store"})
        if p == "/webhooks/concierge":
            return self.send_json({"service": "tsukiya-customer-concierge", "version": 2})

        if p.startswith("/line-image/"):
            q = parse_qs(u.query)
            path = line_delivery.image_path(DB, p.rsplit("/",1)[-1], (q.get("expires") or [""])[0], (q.get("sig") or [""])[0])
            if not path:
                return self.send_error(404)
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf" if path.suffix == ".pdf" else "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/line-settings":
            if not self.auth():
                return self.redirect("/login")
            return self.send_html((BASE / "public" / "line-settings.html").read_text(), loader=False)

        if p == "/api/line/status":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            import staff_faq
            return self.send_json({**line_bot.status(DB), **line_delivery.status(DB), **line_receipts.status(DB),
                                   "staff_faq": staff_faq.status(DB)}, headers={"Cache-Control": "no-store"})

        if p == "/webhooks/line":
            return self.send_json({"service": "tsukiya-line-webhook", "version": 1,
                                   "method": "POST", "delivery_enabled": False})

        if p == "/api/refunds":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            c = con()
            rows = [dict(r) for r in c.execute("SELECT f.*,r.guest_name,r.visit_at,r.cancellation_reason FROM cancellation_refunds f JOIN reservations r ON r.id=f.reservation_id ORDER BY f.created_at DESC")]
            c.close()
            return self.send_json({"refunds": rows, "action_required_count": refunds.action_required_count(rows), "guide": refunds.GUIDE, "approval_configured": refunds.approval_configured(sys.modules[__name__])}, 200, {"Cache-Control": "no-store"})
        if p == "/cancel-reservation":
            return self.send_html((BASE / "public" / "cancel.html").read_text(encoding="utf-8"), loader=False)
        if p == "/loading-test":
            return self.send_html((BASE / "public" / "loading-test.html").read_text())
        if p == "/crab-loader.js":
            b = (BASE / "public" / "crab-loader.js").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
            return
        if p in ("/assets/crab-1.mp4", "/assets/crab-2.mp4", "/assets/crab-3.mp4"):
            b = crab_video(int(p[-5])); total = len(b); start = 0; end = total - 1
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if not match or not any(match.groups()):
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{total}"); self.end_headers(); return
                if match[1]:
                    start = int(match[1]); end = min(int(match[2]) if match[2] else total - 1, total - 1)
                else:
                    start = max(0, total - int(match[2]))
                if start > end or start >= total:
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{total}"); self.end_headers(); return
            self.send_response(206 if requested else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(end - start + 1))
            if requested: self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers(); self.wfile.write(b[start:end+1]); return

        if p in ("/book", "/book-test"):
            if p == "/book-test" and not self.auth():
                return self.redirect("/login")
            english = p == "/book" and (parse_qs(u.query).get("lang") or [""])[0] == "en"
            page = (BASE / "public" / ("book-en.html" if english else "book.html")).read_text(encoding="utf-8")
            if p == "/book-test":
                page = page.replace("<body>", "<body><p>決済テスト専用・1予約 1円（税込）</p>")
                page = page.replace("selected.party_size*selected.price_per_person", "1")
                page = page.replace("/api/public/reservations", "/api/test/reservations")
            return self.send_html(page)

        if p == "/api/public/availability":
            expire_public_reservations()
            q = parse_qs(u.query)
            course = (q.get("course") or [None])[0]
            if course is not None and course not in PUBLIC_COURSES:
                return self.send_json({"error": "コースを選び直してください"}, 400)
            try:
                start = date.fromisoformat((q.get("start") or [""])[0])
                party = int((q.get("party_size") or ["2"])[0])
            except (ValueError, TypeError):
                return self.send_json({"error": "日付または人数が不正です"}, 400)
            if not 2 <= party <= 8:
                return self.send_json({"error": "2〜8名で選択してください"}, 400)
            today = datetime.now(timezone(timedelta(hours=9))).date()
            if start < today or start > today + timedelta(days=365):
                return self.send_json({"error": "表示できる日付の範囲外です"}, 400)
            c = con()
            try:
                days = []
                for offset in range(7):
                    day = start + timedelta(days=offset)
                    slots = {}
                    for area in ("COUNTER", *ROOMS):
                        slots[area] = {}
                        for time_text, round_number in (("18:00", 1), ("20:30", 2)):
                            allowed = public_slot_allowed(day, time_text, course)
                            available = allowed and public_party_allowed(area, party) and availability_check(
                                c, area, f"{day.isoformat()}T{time_text}", party,
                                round_number if area == "COUNTER" else None, 150
                            )[0]
                            slots[area][time_text] = available
                    priced = bookable_course(day)
                    days.append({"date": day.isoformat(), "slots": slots,
                                 "marks": availability_display.marks(sys.modules[__name__], c, day, slots),
                                 "course_name": priced[0] if priced else None,
                                 "price_per_person": priced[1] if priced else None})
            finally:
                c.close()
            return self.send_json({
                "days": days, "price_per_person": days[0]["price_per_person"],
                "course_name": days[0]["course_name"]
            })

        if p == "/":
            if not self.auth():
                return self.redirect("/login")

            f = (
                BASE
                / "public"
                / "index.html"
            )

            if not f.exists():
                f = (
                    BASE
                    / "index.html"
                )

            if not f.exists():
                return self.send_html(
                    "<h1>Tsukiya Reservation</h1>"
                )

            return self.send_html(
                f.read_text(
                    encoding="utf-8"
                )
            )

        if p == "/reservations":
            if not self.auth():
                return self.redirect("/login")
            f = BASE / "public" / "reservations.html"
            return self.send_html(f.read_text(encoding="utf-8"))

        if p == "/customers":
            if not self.auth():
                return self.redirect("/login")
            return self.send_html((BASE / "public" / "customers.html").read_text(encoding="utf-8").replace('<script src="/public/business-cards.js"></script>', "<script>" + (BASE / "public" / "business-cards.js").read_text(encoding="utf-8") + "</script>"))

        card_path = re.fullmatch(r"/api/customers/(\d+)/square-cards", p)
        if card_path:
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            c = con()
            try:
                ids = [r[0] for r in c.execute("SELECT DISTINCT square_customer_id FROM reservations WHERE customer_id=? AND square_customer_id IS NOT NULL AND square_customer_id!=''", (int(card_path[1]),))]
            finally:
                c.close()
            if len(ids) != 1:
                return self.send_json({"status": "not_linked" if not ids else "needs_matching", "cards": [], "message": "Square顧客との対応をSquare管理画面で確認してください。カード未登録と断定するものではありません。"}, headers={"Cache-Control": "no-store"})
            try:
                cards = square("/v2/cards?" + urlencode({"customer_id": ids[0], "include_disabled": "false"})).get("cards", [])
                safe = [{"brand": r.get("card_brand"), "last4": r.get("last_4")} for r in cards if r.get("customer_id") == ids[0] and r.get("enabled") is True]
                return self.send_json({"status": "checked", "cards": safe, "message": "保存済みカードの確認のみです。請求は実行していません。"}, headers={"Cache-Control": "no-store"})
            except Exception:
                return self.send_json({"error": "Squareのカード情報を確認できませんでした。"}, 502)

        if p == "/api/customers" or re.fullmatch(r"/api/customers/\d+", p):
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            c = con()
            try:
                sync_customers(c)
                if p == "/api/customers":
                    rows = [dict(row) for row in c.execute("""SELECT c.id,c.name,c.company_name,c.receipt_name,c.phone,c.email,c.note,
                        c.allergies,c.disliked_foods,c.preferred_seat,c.preferred_drinks,c.preferred_crab,c.return_transport,c.alcohol_service,c.soft_drink_only,
                        COUNT(CASE WHEN r.status='CONFIRMED' AND r.visit_at < ? THEN 1 END) AS visit_count,
                        MAX(CASE WHEN r.status='CONFIRMED' AND r.visit_at < ? THEN r.visit_at END) AS last_visit
                        FROM customers c LEFT JOIN reservations r ON r.customer_id=c.id
                        GROUP BY c.id ORDER BY c.id DESC""", (datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M"),)*2)]
                    booking_blocks.annotate(c, rows)
                    tablecheck_import.visit_summaries(c, rows, datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M"))
                    return self.send_json(rows)
                customer_id = int(p.rsplit("/", 1)[1])
                row = c.execute("""SELECT id,name,company_name,receipt_name,phone,email,note,
                    allergies,disliked_foods,preferred_seat,preferred_drinks,preferred_crab,return_transport,alcohol_service,soft_drink_only FROM customers WHERE id=?""", (customer_id,)).fetchone()
                if not row:
                    return self.send_json({"error": "顧客が見つかりません"}, 404)
                visits = [dict(v) for v in c.execute("""SELECT id,visit_at,party_size,course_name,seating_area,status,
                    amount,guest_note,celebration_items,plate_message,visit_note,companions
                    FROM reservations WHERE customer_id=? ORDER BY visit_at DESC,id DESC""", (customer_id,))]
                for visit in visits:
                    visit["receipts"] = finance_links.for_reservation(c,visit["id"])
                customer = tablecheck_import.visit_summaries(c, [dict(row)], datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M"))[0]
                customer["booking_block"] = booking_blocks.state(c, customer_id)
                booking_blocks.annotate(c, [customer])
                return self.send_json({"customer": customer, "visits": visits, "business_cards": business_cards.list_for(c, customer_id), "tablecheck_imports": tablecheck_import.detail(c, customer_id)})
            finally:
                c.close()

        if p == "/login":
            if self.auth():
                return self.redirect("/")
            f = BASE / "public" / "login.html"
            return self.send_html(f.read_text(encoding="utf-8"))

        if p == "/api/diagnostics/memory":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            import memory_diagnostics
            return self.send_json(memory_diagnostics.status(), headers={"Cache-Control": "no-store"})
        if p == "/health":
            return self.send_json(
                {
                    "ok":
                        True,

                    "square_configured":
                        bool(
                            SQUARE_TOKEN
                            and SQUARE_LOCATION_ID
                        ),

                    "release": os.getenv("RENDER_GIT_COMMIT", ""),
                    "english_square_location_configured":
                        bool(SQUARE_EN_LOCATION_ID),

                    "webhook_configured":
                        bool(
                            SQUARE_WEBHOOK_SIGNATURE_KEY
                            and APP_BASE_URL
                        ),

                    "email_configured":
                        bool(SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_FROM),

                    "sms_configured":
                        bool(
                            TWILIO_ACCOUNT_SID
                            and TWILIO_AUTH_TOKEN
                            and TWILIO_FROM_NUMBER
                        )
                }
            )

        if p == "/api/reservation-requests":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            today_jp = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
            try:
                day = date.fromisoformat((parse_qs(u.query).get("date") or [today_jp])[0]).isoformat()
            except ValueError:
                return self.send_json({"error": "日付が不正です"}, 400)
            c = con()
            try:
                rows = c.execute("SELECT id,guest_name,visit_at,status,celebration_items,plate_message FROM reservations WHERE substr(visit_at,1,10)=? AND status!='CANCELLED' AND COALESCE(celebration_items,'')!='' ORDER BY visit_at,id", (day,)).fetchall()
                items = []
                for row in rows:
                    requests = [x.strip() for x in row["celebration_items"].split(",") if "花束" in x or "ケーキ" in x]
                    if requests:
                        items.append({"reservation_id": row["id"], "visit_at": row["visit_at"], "status": row["status"], "guest_name": row["guest_name"], "requests": requests, "plate_message": row["plate_message"] or "", "message": f"{'本日' if day == today_jp else day}ご予約の{row['guest_name']}様より、{'・'.join(requests)}のリクエストがあります。予約詳細からご確認ください。"})
                greeting = "おはようございます、本日のご予約状況をお伝え致します。"
                request_lines = [f"本日ご予約の{item['guest_name']}様より、{'・'.join(item['requests'])}のリクエストがございます。" for item in items]
                if len(request_lines) > 1:
                    request_text = "本日のリクエスト一覧\n" + "\n".join(f"{i}. {text}" for i, text in enumerate(request_lines, 1))
                else:
                    request_text = request_lines[0] if request_lines else ""
                return self.send_json({"date": day, "timezone": "Asia/Tokyo", "items": items,
                    "morning_message": greeting + ("\n\n" + request_text if request_text else ""),
                    "snapshot_path": f"/reservations?snapshot=1&date={day}",
                    "delivery_time": "09:00", "delivery_enabled": False})
            finally:
                c.close()

        if p == "/api/reservations":
            if not self.auth():
                return self.send_json(
                    {
                        "error":
                            "unauthorized"
                    },
                    401
                )

            c = con()
            sync_customers(c)

            rows = [
                dict(x)
                for x in c.execute(
                    """
                    SELECT r.*,c.allergies AS customer_allergies,c.disliked_foods AS customer_disliked_foods,
                        c.preferred_seat AS customer_preferred_seat,c.preferred_drinks AS customer_preferred_drinks,
                        ce.state AS cancellation_email_status,ce.accepted_at AS cancellation_email_sent_at,ce.error AS cancellation_email_error
                    FROM reservations r LEFT JOIN customers c ON c.id=r.customer_id
                    LEFT JOIN cancellation_emails ce ON ce.reservation_id=r.id
                    ORDER BY r.visit_at,r.id
                    """
                )
            ]

            for row in rows:
                # Acquisition data is internal reporting only, not staff UI data.
                row.pop("acquisition_source", None)
                if row["source"] == web_checkout.SOURCE:
                    checkout = c.execute("SELECT state,expires,last_error FROM web_checkouts WHERE reservation_id=?",(row["id"],)).fetchone()
                    if checkout:
                        row["web_checkout_state"] = checkout["state"]
                        row["web_checkout_expires"] = checkout["expires"]
                        row["web_checkout_attention"] = checkout["last_error"]
                row.update(reservation_changes.summary(c,row["id"]))
                row["receipts"] = finance_links.for_reservation(c,row["id"])
                row.update(booking_policy.details(row))
            c.close()

            return self.send_json(rows)

        if p == "/api/phone-course":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            q = parse_qs(u.query)
            try:
                day = date.fromisoformat(q.get("date", [""])[0])
                party = int(q.get("party_size", ["0"])[0])
                if not 1 <= party <= 20:
                    raise ValueError()
            except ValueError:
                return self.send_json({"error": "来店日・人数が不正です"}, 400)
            course = bookable_course(day) if staff_slot_allowed(day, "18:00") or staff_slot_allowed(day, "20:30") else None
            return self.send_json({"available": bool(course),
                                   "course_name": course[0] if course else None,
                                   "amount": course[1] * party if course else None})

        if p == "/api/unpaid-invoices":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            c = con()
            try:
                rows = [dict(x) for x in c.execute(
                    "SELECT id,guest_name,visit_at,amount,square_invoice_id,payment_method,bank_payer,bank_planned_date,bank_due_at,bank_email_status,source,created_at,reminder_status,reminder_sent_at "
                    "FROM reservations WHERE (status='INVOICED' AND square_invoice_id IS NOT NULL) OR (payment_method='BANK' AND status='PENDING') "
                    "ORDER BY visit_at,id"
                )]
            finally:
                c.close()
            result = []
            for row in rows:
                if row["payment_method"] == "BANK" and not row["square_invoice_id"]:
                    row["square_status"]="BANK_PENDING"
                    result.append(row)
                    continue
                try:
                    invoice = square(f"/v2/invoices/{row['square_invoice_id']}")["invoice"]
                    row["square_status"] = invoice["status"]
                    deadline = square_payment_deadline(invoice, row)
                    row["square_due_at"] = deadline.isoformat() if deadline else None
                    row["square_invoice_number"] = invoice.get("invoice_number")
                except Exception:
                    row["square_status"] = "UNKNOWN"
                result.append(row)
            return self.send_json(result)

        if p == "/api/availability":
            if not self.auth():
                return self.send_json(
                    {
                        "error":
                            "unauthorized"
                    },
                    401
                )

            q = parse_qs(u.query)

            area = (
                q.get("seating_area")
                or [""]
            )[0]

            visit = (
                q.get("visit_at")
                or [""]
            )[0]

            party = int(
                (
                    q.get("party_size")
                    or ["1"]
                )[0]
            )

            rnd_raw = (
                q.get("counter_round")
                or [""]
            )[0]

            rnd = (
                int(rnd_raw)
                if rnd_raw
                else None
            )

            dur = int(
                (
                    q.get("duration_minutes")
                    or ["150"]
                )[0]
            )

            c = con()

            try:
                if (q.get("time_specified") or [""])[0] == "1" and not october_private_time(visit, area):
                    room, _, _ = vip_slots.staff_slot(sys.modules[__name__],c,area,visit,party)
                    ok, msg = True, f"個室{room[-1]}に空きがあります（23:00まで）"
                else:
                    ok, msg = availability_check(c,area,visit,party,rnd,dur)

            except Exception as e:
                ok = False
                msg = str(e)

            try:
                suggestions = nearby_availability(c, visit, area, party) if not ok and area in ("COUNTER", "PRIVATE", *ROOMS) and party > 0 else []
            except (ValueError, OverflowError):
                suggestions = []
            c.close()

            return self.send_json(
                {
                    "available":
                        ok,

                    "message":
                        msg,
                    "suggestions": suggestions
                }
            )

        if p == "/api/seat-blocks":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            try:
                day = date.fromisoformat((parse_qs(u.query).get("date") or [""])[0]).isoformat()
            except ValueError:
                return self.send_json({"error": "日付が不正です"}, 400)
            c = con()
            try:
                return self.send_json([dict(r) for r in c.execute("SELECT * FROM seat_blocks WHERE visit_date=? ORDER BY time_text,seating_area,seat_number", (day,))])
            finally:
                c.close()

        self.send_error(404)

    def do_POST(self):
        p = urlparse(

            self.path
        ).path

        # Reject ambiguous framing before any handler reads or acts on a body.
        lengths = self.headers.get_all("Content-Length", [])
        limit = 4096 if p == "/api/login" else 12 * 1024 * 1024
        if (self.headers.get("Transfer-Encoding") is not None
                or len(lengths) > 1
                or (lengths and not re.fullmatch(r"[0-9]{1,10}", lengths[0]))):
            self.close_connection = True
            return self.send_json({"error": "invalid request framing"}, 400)
        if lengths and int(lengths[0]) > limit:
            self.close_connection = True
            return self.send_json({"error": "request too large"}, 413)

        if gpt_reservations.handle_post(self, sys.modules[__name__], p):
            return

        checkout_match = re.fullmatch(r"/api/public/checkouts(?:/([0-9a-f]{32})/(pay|status|cancel)|/(lookup))?", p)
        if checkout_match:
            # Same-origin only; no third-party POSTs or cross-origin fallback.
            origin = self.headers.get("Origin", "")
            if origin != APP_BASE_URL or self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                return self.send_json({"error":"invalid origin"}, 403)
            try:
                if not 0 < int(self.headers.get("Content-Length", "0")) <= 8192:
                    return self.send_json({"error":"入力が長すぎます"}, 413)
                data = self.read_json()
                if not isinstance(data, dict):
                    raise ValueError()
                sid, action, lookup = checkout_match.groups()
                if lookup:
                    result = web_checkout.lookup(sys.modules[__name__], data.get("request_id"), data.get("checkout_secret"))
                elif not action:
                    result = web_checkout.create(sys.modules[__name__], data)
                elif action == "pay":
                    result = web_checkout.pay(sys.modules[__name__], sid, data.get("checkout_secret"), data.get("source_id"))
                elif action == "cancel":
                    result = web_checkout.abandon(sys.modules[__name__], sid, data.get("checkout_secret"))
                else:
                    result = web_checkout.status(sys.modules[__name__], sid, data.get("checkout_secret"))
                return self.send_json(result)
            except web_checkout.CheckoutError as exc:
                return self.send_json({"error":str(exc)}, exc.status)
            except booking_blocks.BookingBlocked as exc:
                return self.send_json({"error":str(exc)},403)
            except (ValueError, TypeError, UnicodeError):
                return self.send_json({"error":"入力内容をご確認ください"},400)
            except Exception:
                return self.send_json({"error":"処理結果を確認できません。再度支払わず、画面で状態をご確認ください。"},503)

        if calendar_auth.handle_post(self, p, APP_BASE_URL):
            return

        if p in ("/api/supplies/save", "/api/supplies/report", "/api/drinks/save", "/api/drinks/report", "/api/drink-capacity/save"):
            if not self.auth() and not supplies.authorized(DB, self.headers.get("X-Supplies-Token", "")):
                return self.send_json({"error": "経理LINEから新しいリンクを開いてください"}, 401)
            if self.headers.get("Origin") != APP_BASE_URL.rstrip('/') or self.headers.get("X-Tsukiya-Action") != "supplies":
                return self.send_json({"error": "画面を開き直してください"}, 403)
            if not lengths or int(lengths[0]) > 60000:
                return self.send_json({"error": "入力が長すぎます"}, 413)
            try:
                x = self.read_json()
                import drink_capacity
                inventory = drink_capacity if p.startswith('/api/drink-capacity/') else drinks if p.startswith('/api/drinks/') else supplies
                result = inventory.save(DB, x) if p.endswith('/save') else inventory.submit(DB, x, APP_BASE_URL)
                return self.send_json(result)
            except supplies.Conflict as exc:
                return self.send_json({"error": str(exc)}, 409)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self.send_json({"error": str(exc)}, 400)

        if p == "/api/line/reservations/room-exception":
            if not self.auth():return self.send_json({"error":"unauthorized"},401)
            if self.headers.get("X-Tsukiya-Action") != "line-booking" or self.headers.get("Origin") != APP_BASE_URL.rstrip('/'):
                return self.send_json({"error":"invalid origin or action"},403)
            try:
                return self.send_json(line_reservations.approve_room_exception(sys.modules[__name__],self.read_json().get("item_id")))
            except ValueError as e:return self.send_json({"error":str(e)},400)

        if p in ("/api/line/reservations/recover", "/api/line/reservations/note"):
            if not self.auth():return self.send_json({"error":"unauthorized"},401)
            if self.headers.get("X-Tsukiya-Action") != "line-booking" or self.headers.get("Origin") != APP_BASE_URL.rstrip('/'):
                return self.send_json({"error":"invalid origin or action"},403)
            try:
                if p.endswith('/note'):
                    return self.send_json(line_reservations.admin_note(sys.modules[__name__],self.read_json()))
                line_reservations.recover(sys.modules[__name__],self.read_json().get("message_ids"))
                return self.send_json({"queued":True})
            except ValueError as e:return self.send_json({"error":str(e)},400)

        if p in ("/api/customers/tablecheck/preview", "/api/customers/tablecheck/import"):
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            origin = self.headers.get("Origin", "")
            if (self.headers.get("X-Tsukiya-Action") != "tablecheck-import"
                    or (origin and origin != APP_BASE_URL.rstrip('/'))):
                return self.send_json({"error": "invalid origin or action"}, 403)
            if not lengths or not 0 < int(lengths[0]) <= 12 * 1024 * 1024:
                self.close_connection = True
                return self.send_json({"error": "invalid size"}, 413)
            try:
                self.connection.settimeout(60)
                result = tablecheck_import.handle(sys.modules[__name__], self.read_json(), p.endswith('/import'))
                return self.send_json(result, headers={"Cache-Control": "no-store"})
            except (ValueError, TypeError, UnicodeDecodeError) as exc:
                return self.send_json({"error": str(exc)}, 400)
            except Exception:
                return self.send_json({"error": "取込を完了できませんでした。データはまとめて取り消されました。"}, 503)

        if p in ("/api/public/marketing-event", "/api/marketing/update"):
            private = p == "/api/marketing/update"
            if private and not self.auth():
                return self.send_json({"error":"unauthorized"},401)
            allowed = {APP_BASE_URL.rstrip('/'), "https://tsukiya-reservation.onrender.com",
                       "https://nishitenma-tsukiya-home.chiyoshi-a-0408.chatgpt.site"}
            origin=self.headers.get("Origin", "")
            if origin and origin not in allowed or (private and self.headers.get("X-Tsukiya-Action") != "marketing"):
                return self.send_json({"error":"invalid origin or action"},403)
            if not private and not origin:
                return self.send_json({"error":"origin required"},403)
            if not lengths or not 0 < int(lengths[0]) <= 1024:
                self.close_connection=True
                return self.send_json({"error":"invalid size"},400)
            c=con()
            try:
                self.connection.settimeout(10)
                data=self.read_json()
                if private: marketing.update(c,data,now_iso())
                elif not marketing.record(c,data,now_iso()):
                    return self.send_json({"error":"rate limited"},429)
                c.commit()
                return self.send_json({"ok":True},headers={"Cache-Control":"no-store"})
            except (ValueError,TypeError,UnicodeDecodeError) as exc:
                c.rollback()
                return self.send_json({"error":"invalid analytics request"},400)
            finally: c.close()

        if p == "/api/public/english-concierge":
            if self.headers.get("X-Tsukiya-Action") != "english-concierge":
                return self.send_json({"error": "Invalid request."}, 403)
            if not lengths or not 0 < int(lengths[0]) <= 12000:
                self.close_connection = True
                return self.send_json({"error": "Your enquiry is too long."}, 413)
            try:
                self.connection.settimeout(15)
                code, result = english_concierge.handle(DB, self.read_json(), self.client_address[0], APP_BASE_URL)
                return self.send_json(result, code)
            except (ValueError, TypeError, UnicodeDecodeError):
                return self.send_json({"error": "Please check your name, enquiry and consent, then try again."}, 400)
            except Exception:
                return self.send_json({"error": "We could not process your enquiry. Please try again."}, 503)

        if p == "/api/guest-service/configure":
            if not self.auth():
                return self.send_json({"error":"unauthorized"},401)
            if self.headers.get("X-Tsukiya-Action") != "guest-service":
                return self.send_json({"error":"invalid request"},403)
            try:
                if self.headers.get("Transfer-Encoding") or not 0 < int(self.headers.get("Content-Length", "0")) <= 12*1024*1024:
                    return self.send_json({"error":"入力が長すぎます"},400)
                self.connection.settimeout(30)
                data=self.read_json()
                return self.send_json(guest_service.configure(sys.modules[__name__],data),headers={"Cache-Control":"no-store"})
            except (ValueError,TypeError,AttributeError) as exc:
                return self.send_json({"error":str(exc)},400)
            except Exception:
                return self.send_json({"error":"処理を完了できませんでした"},503)

        if p == "/api/vip-access/invite":
            if not self.auth():return self.send_json({"error":"unauthorized"},401)
            if self.headers.get("X-Tsukiya-Action")!="vip-access":return self.send_json({"error":"invalid request"},403)
            c=concierge.connect(DB)
            try:
                with c:
                    result=vip_access.create_invite(c,self.read_json())
                    token=result['command'].split(' ',1)[1]
                    result.update(vip_access.invite_details(c,token,APP_BASE_URL))
                return self.send_json(result,headers={"Cache-Control":"no-store"})
            except (ValueError,TypeError,AttributeError) as exc:return self.send_json({"error":str(exc)},400)
            finally:c.close()

        if p == "/api/vip-access/invite-send":
            if not self.auth():return self.send_json({"error":"unauthorized"},401)
            if self.headers.get("X-Tsukiya-Action")!="vip-access":return self.send_json({"error":"invalid request"},403)
            c=concierge.connect(DB)
            try:
                with c:
                    c.execute('BEGIN IMMEDIATE')
                    result=vip_access.send_invite_to_owner(c,self.read_json().get('token'),APP_BASE_URL)
                return self.send_json(result,headers={"Cache-Control":"no-store"})
            except (ValueError,TypeError,AttributeError) as exc:return self.send_json({"error":str(exc)},400)
            finally:c.close()

        if p == "/api/vip-access/decision":
            if not self.auth():return self.send_json({"error":"unauthorized"},401)
            if self.headers.get("X-Tsukiya-Action")!="vip-access":return self.send_json({"error":"invalid request"},403)
            c=concierge.connect(DB)
            try:
                with c:
                    result=vip_access.request_decision(c,self.read_json(),APP_BASE_URL)
                return self.send_json(result)
            except (ValueError,TypeError,AttributeError) as exc:return self.send_json({"error":str(exc)},400)
            finally:c.close()

        if p in ("/api/vip-liff/profile", "/api/vip-liff/course", "/api/vip-liff/request"):
            headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer"}
            try:
                if self.headers.get("Transfer-Encoding") or not 0 < int(self.headers.get("Content-Length","0")) <= 16384:
                    return self.send_json({"error":"invalid request"},400,headers=headers)
                if self.headers.get("X-Tsukiya-Action") != "vip-liff":
                    return self.send_json({"error":"invalid request"},403,headers=headers)
                self.connection.settimeout(10)
                import vip_liff
                return self.send_json(vip_liff.handle(DB,p.rsplit("/",1)[-1],self.read_json(),APP_BASE_URL),headers=headers)
            except PermissionError as exc:return self.send_json({"error":str(exc)},403,headers=headers)
            except (ValueError,TypeError,AttributeError) as exc:return self.send_json({"error":str(exc)},400,headers=headers)
            except Exception:return self.send_json({"error":"一時的に接続できません。同じ画面から再度送信してください。"},503,headers=headers)

        if p in ("/api/concierge/booking-info", "/api/concierge/book", "/api/concierge/verify"):
            try:
                if self.headers.get("Transfer-Encoding") or not 0 < int(self.headers.get("Content-Length", "0")) <= 16384:
                    return self.send_json({"error": "invalid request"}, 400)
                self.connection.settimeout(10)
                data = self.read_json()
                if self.headers.get("X-Tsukiya-Action") != "vip-booking":
                    return self.send_json({"error":"invalid request"},403)
                cookies = dict(v.strip().split("=",1) for v in self.headers.get("Cookie","").split(";") if "=" in v)
                browser = cookies.get("tsukiya_vip","")
                headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer"}
                if p.endswith("/verify"):
                    result,new_browser=concierge_booking.verify(sys.modules[__name__],data.get("token"),browser)
                    if new_browser:
                        headers["Set-Cookie"]="tsukiya_vip="+new_browser+"; Path=/api/concierge; HttpOnly; SameSite=Strict; Max-Age=600"+("; Secure" if APP_BASE_URL.startswith("https://") else "")
                else:
                    result = concierge_booking.booking(sys.modules[__name__], data.get("token"), data if p.endswith("/book") else None, browser)
                return self.send_json(result, headers=headers)
            except PermissionError as exc:
                return self.send_json({"error":str(exc)},403)
            except (ValueError, TypeError, AttributeError) as exc:
                return self.send_json({"error": str(exc)}, 400)
            except Exception:
                return self.send_json({"error": "一時的に処理できません。同じ画面から再度ご確認ください。"}, 503)

        if p in ("/webhooks/concierge", "/webhooks/instagram", "/api/concierge/configure"):
            admin = p == "/api/concierge/configure"
            if admin and not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            if admin and self.headers.get("X-Tsukiya-Action") != "concierge":
                return self.send_json({"error": "invalid request"}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 1024*1024:
                    return self.send_json({"error": "invalid request"}, 400)
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.send_json({"error": "invalid request"}, 400)
                if admin:
                    result = concierge.configure(DB, json.loads(raw))
                    return self.send_json(result, headers={"Cache-Control": "no-store"})
                if p == "/webhooks/instagram":
                    code, result = instagram_concierge.receive(DB, raw, self.headers.get("X-Hub-Signature-256", ""), concierge_availability, PUBLIC_COURSES, APP_BASE_URL or "https://tsukiya-reservation.onrender.com")
                    return self.send_json(result, code)
                code, result = concierge.receive(DB, raw, self.headers.get("X-Line-Signature", ""), concierge_availability, PUBLIC_COURSES, APP_BASE_URL or "https://tsukiya-reservation.onrender.com")
                return self.send_json(result, code)
            except (ValueError, TypeError, AttributeError) as exc:
                return self.send_json({"error": str(exc) if admin else "invalid request"}, 400)
            except Exception:
                return self.send_json({"error": "temporarily unavailable"}, 503)

        if p == "/api/line/receipt-bridge":
            if not self.auth():
                return self.send_json({"error":"unauthorized"},401)
            if self.headers.get("X-Tsukiya-Action") != "line-settings":
                return self.send_json({"error":"invalid request"},403)
            try:
                length=int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 16384:
                    return self.send_json({"error":"invalid request"},400)
                self.connection.settimeout(10)
                data=json.loads(self.rfile.read(length))
                line_receipts.configure(DB,data.get("secret"),data.get("receiver_token"))
                return self.send_json({"ok":True})
            except (ValueError,TypeError,AttributeError):
                return self.send_json({"error":"invalid request"},400)

        if p == "/api/finance/receipt-link":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 32768:
                    return self.send_json({"error":"invalid request"},400)
                self.connection.settimeout(15)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.send_json({"error":"invalid request"},400)
                code, result = finance_links.sync(DB,raw,self.headers.get("X-Tsukiya-Receipt-Signature", ""),con,sync_customers)
                return self.send_json(result,code,headers={"Cache-Control":"no-store"})
            except Exception:
                return self.send_json({"error":"receipt linking unavailable"},503)

        if p == "/api/line/shift-messages":
            try:
                import shift_messages
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 24000:
                    return self.send_json({"error":"invalid request"},400)
                self.connection.settimeout(15)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.send_json({"error":"invalid request"},400)
                code, result = shift_messages.handle(DB, raw, self.headers.get("X-Tsukiya-Shift-Signature", ""))
                return self.send_json(result, code, headers={"Cache-Control":"no-store"})
            except Exception:
                return self.send_json({"error":"shift transport unavailable"},503)

        if p == "/api/line/receipt-content":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 512:
                    return self.send_json({"error":"invalid request"},400)
                self.connection.settimeout(15)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.send_json({"error":"invalid request"},400)
                code, data, mime = line_receipts.content(DB,raw,self.headers.get("X-Tsukiya-Receipt-Signature", ""))
                if code != 200:
                    return self.send_json({"error":"receipt unavailable"},code)
                self.send_response(200)
                self.send_header("Content-Type",mime)
                self.send_header("Content-Length",str(len(data)))
                self.send_header("Cache-Control","private, no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception:
                return self.send_json({"error":"receipt unavailable"},503)

        if p in ("/api/line/test", "/api/line/enable", "/api/line/disable", "/api/line/resend-briefing", "/api/line/resend-charts", "/api/line/resend-printable"):
            if not self.auth():
                return self.send_json({"error":"unauthorized"},401)
            if self.headers.get("X-Tsukiya-Action") != "line-settings":
                return self.send_json({"error":"invalid request"},403)
            try:
                if p.endswith("resend-printable"):
                    result = line_delivery.resend_briefing(DB, printable=APP_BASE_URL)
                elif p.endswith("resend-charts"):
                    result = line_delivery.resend_briefing(DB, charts=(PORT, ADMIN_TOKEN, APP_BASE_URL))
                elif p.endswith("resend-briefing"):
                    result = line_delivery.resend_briefing(DB)
                elif p.endswith("test"):
                    result = line_delivery.deliver(DB, PORT, ADMIN_TOKEN, APP_BASE_URL)
                else:
                    if p.endswith("enable"):
                        info = line_delivery.status(DB)
                        if not any(r['state']=='SENT' for r in info['deliveries']):
                            return self.send_json({"error":"先にテスト送信を完了してください"},409)
                    line_delivery.enable(DB,p.endswith("enable"))
                    result = {"ok":True}
                return self.send_json(result)
            except Exception as e:
                return self.send_json({"error":str(e) if isinstance(e,RuntimeError) else type(e).__name__},503)

        if p == "/webhooks/line":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or length <= 0:
                    return self.send_json({"error": "invalid content length"}, 400)
                if length > line_bot.MAX_BODY:
                    return self.send_json({"error": "payload too large"}, 413)
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.send_json({"error": "incomplete body"}, 400)
                status, result = line_bot.receive(DB, raw, self.headers.get("x-line-signature", ""))
            except (ValueError, TypeError):
                return self.send_json({"error": "invalid request"}, 400)
            except Exception:
                return self.send_json({"error": "webhook temporarily unavailable"}, 503)
            return self.send_json(result, status, {"Cache-Control": "no-store"})

        if p in ("/api/public/cancellation/preview", "/api/public/cancellation/confirm"):
            try:
                if int(self.headers.get("Content-Length", "0")) > 2048:
                    return self.send_json({"error": "入力が長すぎます"}, 413)
                x = self.read_json()
                if not isinstance(x, dict):
                    raise ValueError()
                status, data = customer_cancellation(x.get("token"), p.endswith("/confirm"), x.get("accepted"), x.get("expected_fee"))
            except (ValueError, TypeError, json.JSONDecodeError):
                return self.send_json({"error": "入力内容を確認してください"}, 400)
            return self.send_json(data, status, {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

        if p in ("/api/business-cards/scan", "/api/business-cards/save"):
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            if self.headers.get("X-Tsukiya-Action") != "business-cards":
                return self.send_json({"error": "invalid request"}, 403)
            try:
                if self.headers.get("Transfer-Encoding") or not 0 < int(self.headers.get("Content-Length", "0")) <= 9*1024*1024:
                    raise ValueError("写真のサイズが大きすぎます")
                self.connection.settimeout(60)
                data = self.read_json()
                if not isinstance(data, dict): raise ValueError("入力内容を確認してください")
                result = business_cards.scan(data) if p.endswith("/scan") else business_cards.save(sys.modules[__name__], data)
                return self.send_json(result, headers={"Cache-Control": "no-store"})
            except (ValueError, TypeError) as exc:
                return self.send_json({"error": str(exc)}, 400)
            except Exception:
                return self.send_json({"error": "名刺の処理を完了できませんでした"}, 503)

        note_change = re.fullmatch(r"/api/reservations/(\d+)/note", p)
        if note_change:
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            if (not APP_BASE_URL or self.headers.get("X-Tsukiya-Action") != "edit-reservation-note"
                    or self.headers.get("Origin") != APP_BASE_URL.rstrip('/')):
                return self.send_json({"error": "予約管理画面を開き直してください"}, 403)
            if not lengths or not 0 < int(lengths[0]) <= 64000:
                return self.send_json({"error": "備考の入力が長すぎます"}, 413)
            c = con()
            try:
                data = self.read_json()
                if not isinstance(data, dict):
                    raise ValueError("入力内容を確認してください")
                c.execute("BEGIN IMMEDIATE")
                result = change_reservation_note(c, int(note_change[1]), data)
                c.commit()
                return self.send_json(result)
            except ReservationNoteConflict as exc:
                c.rollback()
                return self.send_json({"error": str(exc)}, 409)
            except (ValueError, TypeError) as exc:
                c.rollback()
                return self.send_json({"error": str(exc)}, 400)
            finally:
                c.close()

        paid_change = re.fullmatch(r"/api/reservations/(\d+)/(paid-party|difference-invoice)", p)
        if paid_change:
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            if (not APP_BASE_URL or self.headers.get("Origin") != APP_BASE_URL.rstrip('/')
                    or self.headers.get("X-Tsukiya-Action") != "paid-reservation-change"):
                return self.send_json({"error": "予約管理画面を開き直してください"}, 403)
            if not lengths or not 0 < int(lengths[0]) <= 64000:
                return self.send_json({"error": "入力が長すぎます"}, 413)
            c = con()
            try:
                data = self.read_json()
                if not isinstance(data, dict):
                    raise ValueError("入力内容を確認してください")
                rid = int(paid_change[1])
                if paid_change[2] == 'paid-party':
                    reservation_changes.verify_original(sys.modules[__name__], c, rid)
                    c.execute('BEGIN IMMEDIATE')
                    result = reservation_changes.change(sys.modules[__name__], c, rid, data)
                    c.commit()
                else:
                    result = reservation_changes.issue(sys.modules[__name__], rid, data.get('change_id'))
                return self.send_json(result)
            except (ValueError, TypeError) as exc:
                c.rollback()
                return self.send_json({"error": str(exc)}, 400)
            except Exception:
                c.rollback()
                return self.send_json({"error": "処理結果を確認できません。予約詳細を開き直してください。同じ差額請求を再確認できます。"}, 502)
            finally:
                c.close()

        time_change = re.fullmatch(r"/api/reservations/(\d+)/(time|party|room)", p)
        if time_change:
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            if self.headers.get("X-Tsukiya-Action") != "change-time":
                return self.send_json({"error": "invalid request"}, 403)
            c = con()
            try:
                data = self.read_json()
                if not isinstance(data, dict):
                    raise ValueError("入力内容を確認してください")
                c.execute("BEGIN IMMEDIATE")
                result = {"time": change_direct_time, "party": change_direct_party, "room": change_direct_room}[time_change[2]](c, int(time_change[1]), data)
                c.commit()
                return self.send_json(result)
            except (ValueError, TypeError) as exc:
                c.rollback()
                return self.send_json({"error": str(exc)}, 400)
            finally:
                c.close()

        block_path = re.fullmatch(r"/api/customers/(\d+)/booking-block", p)
        if block_path:
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            if (self.headers.get("X-Tsukiya-Action") != "booking-block"
                    or (APP_BASE_URL and self.headers.get("Origin") != APP_BASE_URL.rstrip('/'))):
                return self.send_json({"error": "顧客管理画面を開き直してください"}, 403)
            c = con()
            try:
                data = self.read_json()
                c.execute("BEGIN IMMEDIATE")
                result = booking_blocks.set_state(c, int(block_path[1]), data)
                c.commit()
                return self.send_json({"ok": True, "booking_block": result})
            except (ValueError, TypeError) as exc:
                c.rollback()
                return self.send_json({"error": str(exc)}, 400)
            finally:
                c.close()

        customer_path = re.fullmatch(r"/api/customers/(\d+)(?:/visits/(\d+))?", p)
        if customer_path:
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            try:
                x = self.read_json()
                if not isinstance(x, dict):
                    raise ValueError("入力内容が不正です")
                visit_id = customer_path[2]
                fields = ({"visit_note": 2000, "companions": 500} if visit_id else
                          {"name": 100, "company_name": 150, "receipt_name": 150,
                           "phone": 50, "email": 254, "note": 2000,
                           "allergies": 1000, "disliked_foods": 1000,
                           "preferred_seat": 500, "preferred_drinks": 1000, "preferred_crab": 1000, "return_transport": 1000, "alcohol_service": 10, "soft_drink_only": 1})
                if not x or any(k not in fields or not isinstance(v, str) or len(v) > fields[k]
                                for k, v in x.items()):
                    raise ValueError("入力項目または文字数を確認してください")
                values = {k: v.strip() for k, v in x.items()}
                if "soft_drink_only" in values and values["soft_drink_only"] not in ("0","1"):
                    raise ValueError("ソフトドリンク欄を確認してください")
                if "alcohol_service" in values and values["alcohol_service"] not in ("","可","不可"):
                    raise ValueError("飲酒案内の可否を確認してください")
                if "name" in values and not values["name"]:
                    raise ValueError("名前を入力してください")
                if "email" in values and values["email"] and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values["email"]):
                    raise ValueError("メールアドレスを確認してください")
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_json({"error": str(exc)}, 400)
            c = con()
            try:
                sync_customers(c)
                if visit_id:
                    target = c.execute("SELECT id FROM reservations WHERE id=? AND customer_id=?",
                                       (int(visit_id), int(customer_path[1]))).fetchone()
                    table = "reservations"
                else:
                    target = c.execute("SELECT * FROM customers WHERE id=?", (int(customer_path[1]),)).fetchone()
                    table = "customers"
                if not target:
                    return self.send_json({"error": "対象が見つかりません"}, 404)
                if not visit_id:
                    booking_blocks.retain_contacts(c, target)
                if not visit_id and any(k in values for k in ("name", "phone", "email")):
                    merged = {"guest_name": values.get("name", target["name"]),
                              "phone": values.get("phone", target["phone"]),
                              "email": values.get("email", target["email"]), "id": target["id"]}
                    values["match_key"] = customer_match_key(merged)
                changes = ",".join(f"{key}=?" for key in values)
                if not visit_id:
                    changes += ",updated_at=?"
                try:
                    c.execute(f"UPDATE {table} SET {changes} WHERE id=?",
                              (*values.values(), *((now_iso(),) if not visit_id else ()), target["id"]))
                except sqlite3.IntegrityError:
                    return self.send_json({"error": "同じ名前と連絡先の顧客が既にあります"}, 409)
                c.commit()
                return self.send_json({"ok": True})
            finally:
                c.close()

        if p in ("/api/seat-blocks", "/api/seat-blocks/remove"):
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            x = self.read_json()
            c = con()
            try:
                c.execute("BEGIN IMMEDIATE")
                if p.endswith("/remove"):
                    block_id = x.get("id")
                    if type(block_id) is not int or block_id < 1:
                        return self.send_json({"error": "ブロックIDが不正です"}, 400)
                    removed = c.execute("DELETE FROM seat_blocks WHERE id=?", (block_id,)).rowcount
                    c.commit()
                    return self.send_json({"ok": True, "removed": removed})
                day = date.fromisoformat(x.get("date", "")).isoformat()
                items = x.get("blocks")
                if not isinstance(items, list) or not 1 <= len(items) <= 16:
                    raise ValueError("1〜16席を選択してください")
                if day < datetime.now(timezone(timedelta(hours=9))).date().isoformat():
                    raise ValueError("過去の日付はブロックできません")
                for item in items:
                    time_text = item.get("time")
                    area = item.get("seating_area")
                    num = item.get("seat_number")
                    if time_text not in ("18:00", "20:30") or area not in ("COUNTER", *ROOMS) or type(num) is not int or (area == "COUNTER" and not 1 <= num <= 8) or (area != "COUNTER" and num != 0):
                        raise ValueError("席の指定が不正です")
                    if f"{day}T{time_text}" <= datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M"):
                        raise ValueError("開始済みの時間はブロックできません")
                    if c.execute("SELECT 1 FROM seat_blocks WHERE visit_date=? AND time_text=? AND seating_area=? AND seat_number=?", (day, time_text, area, num)).fetchone():
                        raise ValueError("選択した席はすでにブロックされています")
                    if area == "COUNTER":
                        assigned,_ = vip_slots.counter_assignments(sys.modules[__name__],c,day,time_text)
                        if num in assigned:
                            raise ValueError("予約済みの席はブロックできません")
                    elif c.execute("SELECT 1 FROM reservations WHERE substr(visit_at,1,10)=? AND (substr(visit_at,12,5)=? OR vip_through=1) AND seating_area=? AND status IN (?,?,?,?)", (day, time_text, area, *ACTIVE_STATUSES)).fetchone():
                        raise ValueError("予約済みの個室はブロックできません")
                    c.execute("INSERT INTO seat_blocks(visit_date,time_text,seating_area,seat_number,note,created_at) VALUES(?,?,?,?,?,?)", (day,time_text,area,num,str(x.get("note") or "")[:200],now_iso()))
                c.commit()
                return self.send_json({"ok": True, "created": len(items)})
            except (ValueError, TypeError, AttributeError, sqlite3.IntegrityError) as exc:
                c.rollback()
                return self.send_json({"error": str(exc)}, 400)
            finally:
                c.close()

        if p == "/api/reservations/mark-read":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            x = self.read_json()
            ids = x.get("ids", []) if isinstance(x, dict) else []
            if not isinstance(ids, list) or len(ids) > 1000 or any(type(i) is not int or i < 1 for i in ids):
                return self.send_json({"error": "予約IDが不正です"}, 400)
            if not ids:
                return self.send_json({"ok": True, "marked": 0})
            c = con()
            try:
                placeholders = ",".join("?" for _ in ids)
                result = c.execute(f"UPDATE reservations SET staff_seen_at=? WHERE staff_seen_at IS NULL AND id IN ({placeholders})", (now_iso(), *ids))
                c.commit()
                return self.send_json({"ok": True, "marked": result.rowcount})
            finally:
                c.close()

        if p in ("/api/public/reservations", "/api/test/reservations"):
            test_booking = p == "/api/test/reservations"
            if test_booking and not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            expire_public_reservations()
            try:
                body_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.send_json({"error": "入力形式が不正です"}, 400)
            if body_length > 4096:
                return self.send_json({"error": "入力が長すぎます"}, 413)
            try:
                x = self.read_json()
                if not isinstance(x, dict):
                    raise ValueError("invalid booking")
                payment_method, bank_payer, bank_planned_date = bank_transfer.parse(x)
                if test_booking and payment_method == "BANK":
                    raise ValueError("bank test disabled")
                booking_policy.require_public(x)
                day = date.fromisoformat(str(x.get("date", "")))
                time_text = str(x.get("time", ""))
                area = str(x.get("seating_area", ""))
                party = int(x.get("party_size"))
                request_id = str(x.get("request_id", ""))
                policy_accepted = x.get("cancellation_policy_accepted") is True
                course = x.get("course")
                booking_language = x.get("booking_language", "ja")
                name = str(x.get("guest_name", "")).strip()
                email = str(x.get("email", "")).strip().lower()
                phone = str(x.get("phone", "")).strip()
                guest_note, celebration_items, plate_message = guest_requests(x)
            except (ValueError, TypeError, json.JSONDecodeError):
                return self.send_json({"error": "入力内容を確認してください"}, 400)
            if not test_booking and payment_method == "CARD" and web_checkout.config(sys.modules[__name__])["enabled"]:
                return self.send_json({"error":"お支払い画面が更新されました。ページを再読み込みしてください。"},409)
            if payment_method == "BANK":
                if not bank_transfer.ready(DB):
                    return self.send_json({"error":"振込受付の通知設定を確認中です。店舗へご連絡ください"},503)
            elif not SQUARE_TOKEN or not SQUARE_LOCATION_ID:
                return self.send_json({"error":"現在お申し込みを受け付けられません"},503)
            today_jp = datetime.now(timezone(timedelta(hours=9))).date()
            if not policy_accepted:
                return self.send_json({"error": "キャンセルポリシーへの同意が必要です"}, 400)
            if course is not None and course not in PUBLIC_COURSES:
                return self.send_json({"error": "コースを選び直してください"}, 400)
            if booking_language not in ("ja", "en"):
                return self.send_json({"error": "Invalid language"}, 400)
            if (not public_party_allowed(area, party)
                    or day > today_jp + timedelta(days=365)
                    or not public_slot_allowed(day, time_text, course)
                    or len(name) < 1 or len(name) > 80
                    or len(email) > 254 or email.count("@") != 1
                    or len(phone) < 10 or len(phone) > 20
                    or not all(ch.isdigit() or ch in "+- ()" for ch in phone)
                    or sum(ch.isdigit() for ch in phone) < 10
                    or len(request_id) != 36
                    or any(ch not in "0123456789abcdef-" for ch in request_id)):
                return self.send_json({"error": "日付・人数・連絡先を確認してください"}, 400)
            c = con()
            try:
                c.execute("BEGIN IMMEDIATE")
                existing = c.execute(
                    "SELECT * FROM reservations "
                    "WHERE public_request_id=?", (request_id,)
                ).fetchone()
                if existing:
                    c.rollback()
                    if existing["payment_method"] == "BANK" and existing["status"] != "CANCELLED":
                        return self.send_json(bank_transfer.response(existing))
                    if existing["square_invoice_url"] and existing["status"] != "CANCELLED":
                        return self.send_json({
                            "reservation_id": existing["id"],
                            "payment_url": existing["square_invoice_url"]
                        })
                    return self.send_json({"error": "申し込みを処理中です"}, 409)
                try:
                    booking_blocks.ensure_allowed(c, phone, email)
                except booking_blocks.BookingBlocked as exc:
                    c.rollback()
                    return self.send_json({"error": str(exc)}, 403)
                recent = c.execute(
                    "SELECT COUNT(*) n FROM reservations WHERE source='WEB' "
                    "AND email=? AND created_at>=?",
                    (email, (datetime.now(timezone.utc)-timedelta(days=1)).isoformat())
                ).fetchone()["n"]
                if recent >= 3:
                    c.rollback()
                    return self.send_json({"error": "申込回数の上限です。店舗へご連絡ください"}, 429)
                visit_at = f"{day.isoformat()}T{time_text}"
                ok, message = availability_check(
                    c, area, visit_at, party,
                    1 if time_text == "18:00" else 2 if area == "COUNTER" else None,
                    150
                )
                if not ok:
                    c.rollback()
                    return self.send_json({"error": message}, 409)
                ts = now_iso()
                cur = c.execute(
                    "INSERT INTO reservations(source,guest_name,phone,email,visit_at,"
                    "party_size,course_name,booking_language,amount,seating_area,counter_round,"
                    "duration_minutes,status,public_request_id,cancellation_policy_accepted_at,created_at,updated_at,guest_note,celebration_items,plate_message) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("WEB", name, phone, email, visit_at, party,
                     "決済テスト（お料理のご予約ではありません）" if test_booking else (
                         bookable_course(day)[0]),
                     booking_language,
                     1 if test_booking else party * bookable_course(day)[1],
                     area, (1 if time_text == "18:00" else 2) if area == "COUNTER" else None,
                     150, "PENDING", request_id, ts, ts, ts, guest_note, celebration_items, plate_message)
                )
                rid = cur.lastrowid
                if payment_method == "BANK":
                    due=(datetime.fromisoformat(ts)+timedelta(hours=NEW_PAYMENT_HOURS)).isoformat()
                    c.execute("UPDATE reservations SET payment_method='BANK',bank_payer=?,bank_planned_date=?,bank_due_at=? WHERE id=?",(bank_payer,bank_planned_date,due,rid))
                    bank_transfer.enqueue(c,c.execute("SELECT * FROM reservations WHERE id=?",(rid,)).fetchone(),sys.modules[__name__])
                marketing.bind(c,rid,x.get("attribution"),ts,test_booking)
                c.commit()
                row = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
            finally:
                c.close()
            if payment_method == "BANK":
                bank_transfer.receipt(sys.modules[__name__],row)
                return self.send_json(bank_transfer.response(row))
            try:
                customer_id, order_id, invoice_id, payment_url = make_invoice(row)
                c = con()
                try:
                    c.execute(
                        "UPDATE reservations SET square_customer_id=?,square_order_id=?,"
                        "square_invoice_id=?,square_invoice_url=?,status='INVOICED',"
                        "updated_at=? WHERE id=?",
                        (customer_id, order_id, invoice_id, payment_url, now_iso(), rid)
                    )
                    c.commit()
                finally:
                    c.close()
                return self.send_json({"reservation_id": rid, "payment_url": payment_url})
            except Exception as error:
                c = con()
                try:
                    c.execute("UPDATE reservations SET status='ERROR',last_error=?,"
                              "updated_at=? WHERE id=?", (str(error), now_iso(), rid))
                    c.commit()
                finally:
                    c.close()
                return self.send_json({
                    "error": "請求書を作成できませんでした。店舗へご連絡ください",
                    "reservation_id": rid
                }, 503)

        if p == "/api/reconcile":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            reconcile_reservations()
            return self.send_json({"ok": True})

        approval_match = re.fullmatch(r"/api/refunds/(\d+)/approve", p)
        if approval_match:
            if not self.auth():
                return self.send_json({"error":"unauthorized"}, 401)
            if self.headers.get('X-Tsukiya-Action') != 'approve-refund':
                return self.send_json({"error":"invalid request"}, 403)
            if int(self.headers.get('Content-Length', '0')) > 4096:
                return self.send_json({"error":"入力が長すぎます"}, 413)
            try:
                data = self.read_json()
                if not isinstance(data, dict):
                    raise ValueError()
                status, result = refunds.approve(sys.modules[__name__], int(approval_match[1]), data.get('secret'), data.get('amount'))
            except (ValueError, UnicodeError):
                return self.send_json({"error":"入力内容を確認してください"}, 400)
            return self.send_json(result, status)

        if p == "/api/login":
            if not ADMIN_TOKEN:
                return self.send_json({"error": "管理者パスワードが未設定です"}, 503)
            ip = self.client_address[0]
            with LOGIN_LOCK:
                failures = LOGIN_FAILURES.get(ip, deque())
                cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
                while failures and failures[0] < cutoff:
                    failures.popleft()
                LOGIN_FAILURES[ip] = failures
                limited = len(failures) >= LOGIN_MAX_FAILURES
            if limited:
                return self.send_json({"error": "ログイン試行が多すぎます。15分後に再試行してください"}, 429)
            try:
                credentials = self.read_json()
                if not isinstance(credentials, dict) or not isinstance(credentials.get("password"), str):
                    raise ValueError("invalid credentials")
                supplied = credentials["password"]
            except (ValueError, UnicodeError):
                return self.send_json({"error": "入力内容を確認してください"}, 400)
            if not hmac.compare_digest(supplied.encode("utf-8"), STAFF_LOGIN_PIN.encode("utf-8")):
                with LOGIN_LOCK:
                    LOGIN_FAILURES.setdefault(ip, deque()).append(time.monotonic())
                return self.send_json({"error": "パスワードが違います"}, 401)
            with LOGIN_LOCK:
                LOGIN_FAILURES.pop(ip, None)
            expiry = str(int(datetime.now(timezone.utc).timestamp()) + SESSION_SECONDS)
            signature = hmac.new(
                ADMIN_TOKEN.encode(), expiry.encode(), hashlib.sha256
            ).hexdigest()
            return self.send_json(
                {"ok": True},
                headers={
                    "Set-Cookie": self.cookie_header(
                        f"{expiry}.{signature}", SESSION_SECONDS
                    ),
                    "Cache-Control": "no-store"
                }
            )

        if p == "/api/logout":
            return self.send_json(
                {"ok": True},
                headers={
                    "Set-Cookie": self.cookie_header("", 0),
                    "Cache-Control": "no-store"
                }
            )

        correction_path = re.fullmatch(r"/api/reservations/(\d+)/send-confirmation-correction", p)
        if correction_path:
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            if (not APP_BASE_URL or self.headers.get("Origin") != APP_BASE_URL
                    or self.headers.get("X-Tsukiya-Action") != "correct-confirmation-sms"):
                return self.send_json({"error": "管理画面を開き直してください"}, 403)
            try:
                if not 0 < int(self.headers.get("Content-Length", "0")) <= 4096:
                    raise ValueError("入力内容を確認してください")
                payload = self.read_json()
                if not isinstance(payload, dict) or not isinstance(payload.get("expected_sms_sid"), str):
                    raise ValueError("元のSMSを確認してください")
                c = con()
                try:
                    row = c.execute("SELECT * FROM reservations WHERE id=?", (int(correction_path[1]),)).fetchone()
                    if not row:
                        return self.send_json({"error": "not found"}, 404)
                    saved = dict(row)
                finally:
                    c.close()
                result = send_confirmation_sms_correction(saved, payload["expected_sms_sid"])
                return self.send_json({"ok": result.get("confirmation_sms_correction_status") == "QUEUED",
                                       "status": result.get("confirmation_sms_correction_status"),
                                       "error": result.get("confirmation_sms_correction_error")})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self.send_json({"error": str(exc)}, 400)

        if p.startswith("/api/reservations/") and p.endswith("/send-confirmation"):
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            try:
                rid = int(p.split("/")[3])
            except ValueError:
                return self.send_json({"error": "bad id"}, 400)
            c = con()
            try:
                row = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
                if not row:
                    return self.send_json({"error": "not found"}, 404)
                if row["status"] != "CONFIRMED":
                    return self.send_json({"error": "確定済みの予約のみ送信できます"}, 409)
                saved = dict(row)
            finally:
                c.close()
            result = deliver_confirmation(saved, retry_failed=True)
            ok = bool(result.get("confirmation_sent_at") if result.get("email") else result.get("confirmation_sms_status") == "QUEUED")
            return self.send_json({"ok": ok, "error": result.get("last_error") if result.get("email") else result.get("confirmation_sms_error")})

        if re.fullmatch(r"/api/reservations/\d+/restore-bank-payment", p):
            if not self.auth():
                return self.send_json({'error': 'unauthorized'}, 401)
            if (not APP_BASE_URL or self.headers.get('Origin') != APP_BASE_URL.rstrip('/')
                    or self.headers.get('X-Tsukiya-Action') != 'restore-bank-payment'):
                return self.send_json({'error': '予約管理画面を開き直してください'}, 403)
            try:
                details = self.read_json()
                if not isinstance(details, dict):
                    raise ValueError('入力内容を確認してください')
                c = con()
                try:
                    c.execute('BEGIN IMMEDIATE')
                    restored = restore_paid_bank_reservation(c, int(p.split('/')[3]), details)
                    c.commit()
                finally:
                    c.close()
            except (ValueError, TypeError) as exc:
                return self.send_json({'error': str(exc)}, 409)
            # Record and occupy the original booking only. Never create or send an invoice.
            return self.send_json({'ok': True, 'id': restored['id'], 'status': restored['status'],
                                   'payment_source': restored['payment_source']})

        if p.startswith("/api/reservations/") and p.endswith("/confirm-bank-payment"):
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            try:
                rid = int(p.split("/")[3])
                details = self.read_json()
                if not isinstance(details, dict):
                    raise ValueError('入力形式が不正です')
                amount = details.get("amount")
                if type(amount) is not int or amount <= 0:
                    raise ValueError('金額が不正です')
                reference, staff = bank_confirmation_record(details)
            except (ValueError, TypeError):
                return self.send_json({"error": "金額と朝倉確認済みの最終チェックを確認してください"}, 400)
            c = con()
            
            try:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
                if not row:
                    return self.send_json({"error": "not found"}, 404)
                bank_pending = row["payment_method"] == "BANK" and row["status"] == "PENDING" and not row["square_invoice_id"]
                if not bank_pending and (row["status"] != "INVOICED" or not row["square_invoice_id"]):
                    return self.send_json({"error": "請求済みの予約のみ確認できます"}, 409)
                if amount != row["amount"]:
                    return self.send_json({"error": "振込金額が前受け金額と一致しません"}, 409)

                if not bank_pending:
                    # Stop card collection before recording a direct bank transfer.
                    try:
                        invoice_id = row["square_invoice_id"]
                        current = square(f"/v2/invoices/{invoice_id}")["invoice"]
                        if current["status"] != "UNPAID":
                            return self.send_json({"error": "Square請求書が未決済ではありません。状態を確認してください"}, 409)
                        square(
                            f"/v2/invoices/{invoice_id}/cancel",
                            body={"version": current["version"]}
                        )
                    except Exception as e:
                        return self.send_json({"error": f"Square請求書を停止できません: {e}"}, 502)

                
                fresh = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
                if fresh["status"] != ("PENDING" if bank_pending else "INVOICED"):
                    c.rollback()
                    return self.send_json({"error": "予約の状態が変わりました。再確認してください"}, 409)
                ts = now_iso()
                c.execute(
                    "UPDATE reservations SET status='CONFIRMED', payment_source='BANK', "
                    "payment_confirmed_at=?, last_error=NULL, updated_at=? WHERE id=?",
                    (ts, ts, rid)
                )
                c.execute(
                    "INSERT INTO payment_audit(reservation_id,source,amount,reference,confirmed_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (rid, "BANK", amount, reference, staff, ts)
                )
                if bank_pending:
                    bank_transfer.enqueue(c,row,sys.modules[__name__],"confirmed")
                c.commit()
                confirmed = dict(c.execute(
                    "SELECT * FROM reservations WHERE id=?", (rid,)
                ).fetchone())
            finally:
                c.close()

            confirmed = deliver_confirmation(confirmed)
            return self.send_json({"ok": True, "confirmation_email_sent": bool(confirmed.get("confirmation_sent_at"))})

        if p.startswith("/api/reservations/") and p.endswith("/cancel"):
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            try:
                rid = int(p.split("/")[3])
            except ValueError:
                return self.send_json({"error": "bad id"}, 400)
            c = con()
            try:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
                if not row:
                    return self.send_json({"error": "not found"}, 404)
                if row["status"] == "CANCELLED":
                    return self.send_json({"ok": True})
                if row["status"] not in ("PENDING", "INVOICED", "ERROR", "CONFIRMED"):
                    return self.send_json({"error": "この予約はキャンセルできません"}, 409)
                if reservation_changes.records(c, rid):
                    return self.send_json({"error": "差額請求がある予約です。Squareで元決済と差額請求を確認して店舗対応してください。"}, 409)
                try:
                    web_checkout.cancel_hold(c,row)
                except web_checkout.CheckoutError as exc:
                    return self.send_json({"error":str(exc)},exc.status)
                if row["square_invoice_id"]:
                    try:
                        iid = row["square_invoice_id"]
                        current = square(f"/v2/invoices/{iid}")["invoice"]
                        if current["status"] == "UNPAID":
                            square(f"/v2/invoices/{iid}/cancel",
                                   body={"version": current["version"]})
                        elif row["status"] == "CONFIRMED" and current["status"] in ("PAID", "CANCELED", "CANCELLED", "REFUNDED", "PARTIALLY_REFUNDED"):
                            pass  # Refund job is queued after the cancellation is committed.
                        else:
                            return self.send_json({"error": "Squareの支払状態を確認してください"}, 409)
                    except Exception:
                        return self.send_json({"error": "請求書を停止できませんでした"}, 502)
                c.execute("UPDATE reservations SET status='CANCELLED',staff_seen_at=NULL,cancellation_reason='MANUAL',cancelled_at=?,updated_at=? WHERE id=?",
                          (now_iso(), now_iso(), rid))
                if row["payment_method"] == "BANK":
                    bank_transfer.enqueue(c,row,sys.modules[__name__],"cancelled")
                if row["status"] == "CONFIRMED":
                    now = datetime.now(timezone(timedelta(hours=9)))
                    visit = datetime.fromisoformat(row["visit_at"])
                    if visit.tzinfo is None:
                        visit = visit.replace(tzinfo=now.tzinfo)
                    fee = row["amount"] if now.date() >= visit.astimezone(now.tzinfo).date() - timedelta(days=3) else 0
                    refunds.enqueue(c, row, fee, now_iso())
                c.commit()
                return self.send_json({"ok": True})
            finally:
                c.close()

        if p in ("/api/reservations/phone", "/api/reservations/direct"):
            direct = p.endswith("/direct")
            if not self.auth():
                return self.send_json(
                    {
                        "error":
                            "unauthorized"
                    },
                    401
                )

            try:
                x = self.read_json()
                if not isinstance(x, dict):
                    raise ValueError("入力形式が不正です")
                confirmation_note = x.get("confirmation_note", "")
                if not isinstance(confirmation_note, str) or len(confirmation_note) > 1000 or "\x00" in confirmation_note:
                    raise ValueError("確定メール備考は1000文字以内で入力してください")
                confirmation_note = confirmation_note.strip()
                linked_customer = x.get("customer_id")
                if linked_customer is not None and (type(linked_customer) is not int or linked_customer < 1):
                    raise ValueError("顧客を選び直してください")
                request_id = str(x.get("request_id") or "")
                if request_id and not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", request_id):
                    raise ValueError("画面を開き直して登録してください")
                if direct:
                    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", request_id):
                        raise ValueError("画面を開き直して登録してください")
                    email = str(x.get("email") or "").strip()
                    if email and (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
                        raise ValueError("確定メールを送るメールアドレスを入力してください")
                    if not isinstance(x.get("guest_name"), str) or not 1 <= len(x["guest_name"].strip()) <= 80:
                        raise ValueError("お名前を入力してください")
                    x["email"] = email
                    x["guest_name"] = x["guest_name"].strip()
                    int(x.get("party_size", 0))
                    int(x.get("counter_round") or 0)
                    int(x.get("duration_minutes") or 150)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self.send_json({"error": str(exc)}, 400)

            try:
                guest_note, celebration_items, plate_message = guest_requests(x)
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)

            required = (
                "guest_name",
                "visit_at",
                "party_size",
                "seating_area"
            )

            if any(
                x.get(k) in (None, "")
                for k in required
            ):
                return self.send_json(
                    {
                        "error":
                            "必須項目が不足しています"
                    },
                    400
                )

            if (
                not direct
                and not x.get("email")
                and not x.get("phone")
            ):
                return self.send_json(
                    {
                        "error":
                            "メールアドレスまたは電話番号が必要です"
                    },
                    400
                )

            area = x["seating_area"]

            rnd = (
                int(
                    x.get(
                        "counter_round"
                    )
                    or 0
                )
                or None
            )

            dur = 150  # Ordinary reservations cannot hold extra sittings via client input.

            party = int(
                x["party_size"]
            )

            try:
                day = parse_dt(x["visit_at"]).date()
            except ValueError:
                return self.send_json({"error": "来店日が不正です"}, 400)
            time_text = parse_dt(x["visit_at"]).strftime("%H:%M")
            time_specified = x.get("time_specified") is True
            through = False
            approved_exception = approved_staff_time_exception(x, direct)
            course = bookable_course(day) if time_specified or staff_slot_allowed(day, time_text) else None
            today_jp = datetime.now(timezone(timedelta(hours=9))).date()
            same_day_phone = not direct and day == today_jp
            same_day_direct = direct or same_day_phone
            if not course or not 1 <= party <= 20:
                return self.send_json({"error": "この来店日に予約可能なコース・人数がありません"}, 400)
            amount = course[1] * party

            c = con()

            try:
                c.execute("BEGIN IMMEDIATE")
                if not direct and request_id:
                    existing = c.execute("SELECT * FROM reservations WHERE public_request_id=?", ("phone:" + request_id,)).fetchone()
                    if existing:
                        c.rollback()
                        c.close()
                        return self.send_json(dict(existing))
                if direct:
                    existing = c.execute("SELECT * FROM reservations WHERE public_request_id=?", ("direct:" + request_id,)).fetchone()
                    if existing:
                        c.rollback()
                        c.close()
                        return self.send_json(dict(existing))
                    if linked_customer is not None and not c.execute("SELECT id FROM customers WHERE id=?", (linked_customer,)).fetchone():
                        raise ValueError("顧客が見つかりません")
                booking_blocks.ensure_allowed(c, x.get("phone"), x.get("email"), linked_customer, staff=True)
                if time_specified and not october_private_time(x["visit_at"], area) and not approved_exception:
                    if time_text != "20:30" and not booking_policy.approved_customer(c, linked_customer):
                        raise ValueError("時間指定・通し予約は承認済みVIP顧客の選択が必要です。直接予約から顧客を選択してください。")
                    area,dur,through = vip_slots.staff_slot(sys.modules[__name__],c,area,x["visit_at"],party)
                    rnd = None
                    ok,msg = True,"個室を確保できます"
                else:
                    if area == "PRIVATE":
                        area = next((room for room in ROOMS if public_party_allowed(room, party) and availability_check(c, room, x["visit_at"], party, None, dur, allow_custom_start=approved_exception)[0]), "PRIVATE")
                    ok,msg = availability_check(c,area,x["visit_at"],party,rnd,dur,allow_custom_start=approved_exception)

            except Exception as e:
                c.rollback()
                c.close()

                return self.send_json(
                    {
                        "error":
                            str(e)
                    },
                    400
                )

            if not ok:
                c.rollback()
                c.close()

                return self.send_json(
                    {
                        "error":
                            msg
                    },
                    409
                )

            ts = now_iso()

            cur = c.execute(
                """
                INSERT INTO reservations(
                    source,
                    guest_name,
                    phone,
                    email,
                    visit_at,
                    party_size,
                    course_name,
                    amount,
                    seating_area,
                    counter_round,
                    duration_minutes,
                    status,
                    created_at,
                    updated_at
                    ,guest_note,celebration_items,plate_message
                )
                VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    "DIRECT" if same_day_direct else "PHONE",
                    x["guest_name"],
                    x.get("phone"),
                    x.get("email"),
                    x["visit_at"],
                    party,
                    course[0],
                    amount,
                    area,
                    rnd,
                    dur,
                    "CONFIRMED" if same_day_direct else "PENDING",
                    ts,
                    ts,
                    guest_note,
                    celebration_items,
                    plate_message
                )
            )

            c.execute("UPDATE reservations SET confirmation_note=? WHERE id=?", (confirmation_note, cur.lastrowid))
            c.execute("UPDATE reservations SET vip_through=?,vip_booking=?,customer_id=? WHERE id=?",(int(through),int(booking_policy.approved_customer(c,linked_customer)),linked_customer,cur.lastrowid))

            if not direct and request_id:
                c.execute("UPDATE reservations SET public_request_id=? WHERE id=?", ("phone:" + request_id, cur.lastrowid))
            if direct:
                if linked_customer is None:
                    key = customer_match_key({"id": cur.lastrowid, "guest_name": x["guest_name"], "phone": x.get("phone"), "email": x.get("email")})
                    c.execute("INSERT OR IGNORE INTO customers(match_key,name,phone,email,created_at,updated_at) VALUES(?,?,?,?,?,?)", (key,x["guest_name"],x.get("phone") or "",x["email"],ts,ts))
                    linked_customer = c.execute("SELECT id FROM customers WHERE match_key=?", (key,)).fetchone()["id"]
                c.execute("UPDATE reservations SET customer_id=?,public_request_id=?,staff_seen_at=? WHERE id=?", (linked_customer,"direct:" + request_id,ts,cur.lastrowid))
            c.commit()

            row = dict(
                c.execute(
                    """
                    SELECT *
                    FROM reservations
                    WHERE id=?
                    """,
                    (
                        cur.lastrowid,
                    )
                ).fetchone()
            )

            c.close()

            if same_day_direct and row.get("email"):
                row = deliver_confirmation(row)
            elif not same_day_direct:
                # The reservation is already saved: return its ID even if invoicing fails,
                # so staff retry the invoice rather than registering another reservation.
                c = con()
                try:
                    try:
                        cid, oid, iid, url = make_invoice(row)
                    except Exception as exc:
                        c.execute("UPDATE reservations SET status='ERROR',last_error=?,updated_at=? WHERE id=?",
                                  (str(exc), now_iso(), row["id"]))
                    else:
                        ts = now_iso()
                        c.execute("UPDATE reservations SET square_customer_id=?,square_order_id=?,"
                                  "square_invoice_id=?,square_invoice_url=?,status='INVOICED',"
                                  "invoice_issued_at=?,last_error=NULL,updated_at=? WHERE id=?",
                                  (cid, oid, iid, url, ts, ts, row["id"]))
                    c.commit()
                    row = dict(c.execute("SELECT * FROM reservations WHERE id=?", (row["id"],)).fetchone())
                finally:
                    c.close()
                if row["status"] == "INVOICED":
                    row = send_invoice_sms(row)
            return self.send_json(row)

        if (
            p.startswith(
                "/api/reservations/"
            )
            and p.endswith(
                "/send-invoice"
            )
        ):
            if not self.auth():
                return self.send_json(
                    {
                        "error":
                            "unauthorized"
                    },
                    401
                )

            try:
                rid = int(
                    p.split("/")[3]
                )

            except Exception:
                return self.send_json(
                    {
                        "error":
                            "bad id"
                    },
                    400
                )

            c = con()

            r = c.execute(
                """
                SELECT *
                FROM reservations
                WHERE id=?
                """,
                (
                    rid,
                )
            ).fetchone()

            if not r:
                c.close()

                return self.send_json(
                    {
                        "error":
                            "not found"
                    },
                    404
                )

            if r["source"] == web_checkout.SOURCE:
                c.close()
                return self.send_json({"error":"画面内カード決済のため請求書は発行しません"},409)
            if r["payment_method"] == "BANK":
                c.close()
                return self.send_json({"error":"銀行振込希望のためカード請求は発行しません"},409)

            if r["source"] == "DIRECT":
                c.close()
                return self.send_json({"error": "直接予約は前受け請求を送信しません（当日精算）"}, 409)

            if r["square_invoice_id"]:
                out = dict(r)
                c.close()

                return self.send_json(send_invoice_sms(out))

            try:
                (
                    cid,
                    oid,
                    iid,
                    url
                ) = make_invoice(r)

                ts = now_iso()

                c.execute(
                    """
                    UPDATE reservations
                    SET
                        square_customer_id=?,
                        square_order_id=?,
                        square_invoice_id=?,
                        square_invoice_url=?,
                        status='INVOICED',
                        invoice_issued_at=?,
                        last_error=NULL,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        cid,
                        oid,
                        iid,
                        url,
                        ts,
                        ts,
                        rid
                    )
                )

                c.commit()

                out = dict(
                    c.execute(
                        """
                        SELECT *
                        FROM reservations
                        WHERE id=?
                        """,
                        (
                            rid,
                        )
                    ).fetchone()
                )

                c.close()

                return self.send_json(send_invoice_sms(out))

            except Exception as e:
                ts = now_iso()

                c.execute(
                    """
                    UPDATE reservations
                    SET
                        status=?,
                        last_error=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        "ERROR",
                        str(e),
                        ts,
                        rid
                    )
                )

                c.commit()
                c.close()

                return self.send_json(
                    {
                        "error":
                            str(e)
                    },
                    500
                )

        if p == "/webhooks/square":
            raw = self.read_raw()

            signature = self.headers.get(
                "x-square-hmacsha256-signature",
                ""
            )

            if not verify_square(
                raw,
                signature
            ):
                return self.send_json(
                    {
                        "error":
                            "invalid signature"
                    },
                    403
                )

            try:
                event = json.loads(
                    raw.decode("utf-8")
                )

            except Exception:
                return self.send_json(
                    {
                        "error":
                            "invalid json"
                    },
                    400
                )

            try:
                duplicate = process_square_event(event, raw)
            except Exception as e:
                print("Square webhook processing error:", e)
                return self.send_json({"error": "processing failed"}, 500)

            return self.send_json({"ok": True, "duplicate": duplicate})

        self.send_error(404)


if __name__ == "__main__":
    c = con()
    c.close()
    import memory_diagnostics
    memory_diagnostics.start(sys.modules[__name__])
    threading.Thread(target=guest_service.loop, args=(sys.modules[__name__],), daemon=True).start()
    threading.Thread(target=concierge.loop, args=(DB,), daemon=True).start()
    threading.Thread(target=line_reservations.loop, args=(sys.modules[__name__],), daemon=True).start()
    threading.Thread(target=calendar_sync.loop, args=(DB,), daemon=True).start()
    threading.Thread(target=expiry_loop, daemon=True).start()
    threading.Thread(target=marketing.archive_loop, args=(sys.modules[__name__],), daemon=True).start()
    threading.Thread(target=line_receipts.loop, args=(DB,), daemon=True).start()
    threading.Thread(target=line_delivery.loop, args=(DB, PORT, ADMIN_TOKEN, APP_BASE_URL), daemon=True).start()
    threading.Thread(target=supplies.loop, args=(DB, APP_BASE_URL), daemon=True).start()

    print(
        f"Tsukiya reservation server starting on :{PORT}"
    )

    ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        Handler
    ).serve_forever()







