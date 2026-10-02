import refunds
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


BASE = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB = DATA_DIR / "tsukiya.sqlite"

PORT = int(os.getenv("PORT", "10000"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
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

PUBLIC_COURSE_PRICE = 60000  # 税込・1名あたり
PUBLIC_PAYMENT_HOURS = 48
PUBLIC_COURSES = {
    "matsuba-seko": ("松葉蟹、せこ蟹おまかせコース", PUBLIC_COURSE_PRICE),
    "matsuba-fukahire": ("松葉蟹と名物ふかひれあんかけおまかせコース", PUBLIC_COURSE_PRICE),
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
    """The public course is offered annually from Nov 10 through Mar 20."""
    if not (day.month > 11 or (day.month == 11 and day.day >= 10)
            or day.month < 3 or (day.month == 3 and day.day <= 20)):
        return False
    if course == "matsuba-seko" and day.month not in (11, 12):
        return False
    if course == "matsuba-fukahire" and day.month not in (1, 2, 3):
        return False
    if course is not None and course not in PUBLIC_COURSES:
        return False
    try:
        visit = datetime.fromisoformat(f"{day.isoformat()}T{time_text}")
    except ValueError:
        return False
    if time_text not in ("18:00", "20:30"):
        return False
    return visit > datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None)


def bookable_course(day):
    """Return the currently priced course for a staff phone reservation."""
    if day.month == 11 and day.day >= 10 or day.month == 12:
        return PUBLIC_COURSES["matsuba-seko"]
    if day.month in (1, 2) or day.month == 3 and day.day <= 20:
        return PUBLIC_COURSES["matsuba-fukahire"]
    return None


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def con():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    refunds.schema(c)

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

    wanted = {
        "confirmation_email_status": "TEXT",
        "confirmation_sms_status": "TEXT",
        "confirmation_sms_sid": "TEXT",
        "confirmation_sms_sent_at": "TEXT",
        "confirmation_sms_error": "TEXT",
        "invoice_sms_status": "TEXT",
        "invoice_sms_sid": "TEXT",
        "invoice_sms_sent_at": "TEXT",
        "invoice_sms_error": "TEXT",
        "seating_area": "TEXT",
        "counter_round": "INTEGER",
        "duration_minutes": "INTEGER DEFAULT 150",
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
        "staff_seen_at": "TEXT",
        "invoice_issued_at": "TEXT",
        "reminder_status": "TEXT",
        "reminder_sent_at": "TEXT",
        "reminder_error": "TEXT",
        "guest_note": "TEXT",
        "celebration_items": "TEXT",
        "plate_message": "TEXT",
        "customer_id": "INTEGER",
        "visit_note": "TEXT",
        "companions": "TEXT",
    }

    migrate_seen = "staff_seen_at" not in cols
    migrate_reminder = "reminder_status" not in cols

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
    customer_cols = {row["name"] for row in c.execute("PRAGMA table_info(customers)")}
    for field in ("allergies", "disliked_foods", "preferred_seat", "preferred_drinks"):
        if field not in customer_cols:
            c.execute(f"ALTER TABLE customers ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
    c.execute("CREATE INDEX IF NOT EXISTS reservations_customer_idx ON reservations(customer_id)")

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
            WHERE customer_id IS NULL AND (course_name IS NULL OR course_name NOT LIKE '決済テスト%')
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
                    body = (f"西天満つきやです。{row['guest_name']}様\nご入金を確認し、ご予約を確定いたしました。\n"
                            f"日時：{row['visit_at'].replace('T', ' ')}\nお席：{customer_seating_label(row)}\n"
                            f"人数：{row['party_size']}名様\n当店は一斉スタートでお料理をご提供します。ご来店時間をお守りください。\n当日は心を尽くしてお迎えいたします。")
                    try:
                        result = send_sms(row["phone"], body=body + ("\n" + annex_message(row) if annex_message(row) else ""))
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
    if not r["email"] and not r["phone"]:
        raise RuntimeError(
            "メールアドレスまたは電話番号が必要です"
        )

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

    due = (
        datetime.now(timezone.utc)
        + timedelta(days=3)
    ).date().isoformat()

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
                    "Full prepayment for your crab omakase reservation. "
                    "Your reservation is confirmed after full payment. "
                    "Beverages are paid for at the restaurant.\n\n"
                    "For bank transfers:\nSumitomo Mitsui Banking Corporation\n"
                    "Dojima Branch\nOrdinary account 0655295\n"
                    "Account name: ASAKURA CHIYOSHI"
                    + ("\n\n" + annex_message(r, True) if annex_message(r, True) else "")
                ) if english else (
                    "お振込みの際は下記口座までお願い致します。\n\n"
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


def send_payment_reminder(reservation, invoice_status):
    """Send one 24-hour warning only for a confirmed unpaid Square invoice."""
    issued = reservation.get("invoice_issued_at")
    if (reservation.get("status") != "INVOICED" or invoice_status != "UNPAID"
            or not issued or not reservation.get("square_invoice_url")
            or parse_dt(issued) > datetime.now(timezone.utc) - timedelta(hours=24)):
        return False
    if not (reservation.get("email") and SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_FROM
            or reservation.get("phone") and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        return False
    with REMINDER_LOCK:
        c = con()
        try:
            claimed = c.execute(
                "UPDATE reservations SET reminder_status='SENDING',reminder_error=NULL "
                "WHERE id=? AND status='INVOICED' AND reminder_status IS NULL",
                (reservation["id"],)
            ).rowcount
            c.commit()
            if not claimed:
                return False
        finally:
            c.close()
        deadline = (parse_dt(issued) + timedelta(hours=48)).astimezone(timezone(timedelta(hours=9)))
        english = reservation.get("booking_language") == "en"
        if english:
            text_body = (
                f"Dear {reservation['guest_name']},\n\n"
                "We have not yet confirmed full payment for your reservation at Tsukiya at Nishi-Tenma. "
                "Please find your invoice again below:\n"
                f"{reservation['square_invoice_url']}\n\n"
                f"If full payment is not confirmed by {deadline:%Y-%m-%d %H:%M} JST, "
                "your reservation will be canceled. If you have already paid by bank transfer, "
                "please contact us so we can verify your payment.\n\nTsukiya at Nishi-Tenma"
            )
        else:
            text_body = (
                f"{reservation['guest_name']} 様\n\n西天満つきやでございます。"
                "ご予約のお料理代のお支払いが、現時点では確認できておりません。\n"
                "請求書を再度ご案内いたします。\n"
                f"{reservation['square_invoice_url']}\n\n"
                f"{deadline:%Y年%m月%d日 %H:%M}までにお支払いが確認できない場合、"
                "ご予約は取り消しとなります。\n"
                "すでにお振り込み済みの場合は、行き違いのご案内となりますので店舗へご連絡ください。\n\n"
                "西天満 つきや"
            )
        try:
            if reservation.get("email") and SMTP_HOST and SMTP_USER and SMTP_PASS and MAIL_FROM:
                message = EmailMessage()
                message["Subject"] = "Tsukiya | Payment reminder" if english else "【西天満つきや】お支払いの再案内"
                message["From"] = MAIL_FROM
                message["To"] = reservation["email"]
                message.set_content(text_body)
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                    server.starttls()
                    server.login(SMTP_USER, SMTP_PASS)
                    server.send_message(message)
            else:
                result = send_sms(reservation["phone"], body=text_body)
                if not result.get("sid") or result.get("status") in ("failed", "undelivered", "canceled"):
                    raise RuntimeError("SMS送信を確認できませんでした")
        except Exception as exc:
            # An ambiguous SMTP/Twilio failure must not automatically send a duplicate.
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
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=PUBLIC_PAYMENT_HOURS)).isoformat()
    c = con()
    expired = 0
    try:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            "SELECT id,square_invoice_id FROM reservations WHERE "
            "((source='WEB' AND square_invoice_id IS NULL AND created_at<=?) "
            "OR (source IN ('WEB','PHONE') AND square_invoice_id IS NOT NULL "
            "AND COALESCE(invoice_issued_at,created_at)<=? "
            "AND reminder_status IN ('SENT','SKIPPED_LEGACY'))) "
            "AND status IN ('PENDING','INVOICED','ERROR') "
            "ORDER BY id LIMIT 50", (cutoff, cutoff)
        ).fetchall()
        for row in rows:
            iid = row["square_invoice_id"]
            if iid:
                try:
                    invoice = square(f"/v2/invoices/{iid}")["invoice"]
                    status = invoice["status"]
                    if status == "UNPAID":
                        square(f"/v2/invoices/{iid}/cancel",
                               body={"version": invoice["version"]})
                    elif status not in ("CANCELED", "CANCELLED"):
                        continue
                except Exception as exc:
                    print(f"Expiry check failed for reservation {row['id']}: {exc}")
                    continue
            c.execute("UPDATE reservations SET status='CANCELLED',cancellation_reason='PAYMENT_EXPIRED',cancelled_at=?,updated_at=? WHERE id=?",
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
        except Exception as exc:
            print(f"Public reservation expiry failed: {exc}")
        time.sleep(300)


def availability_check(
    c,
    seating_area,
    visit_at,
    party_size,
    counter_round=None,
    duration_minutes=150,
    exclude_id=None
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

    if seating_area == "PRIVATE":
        eligible = [area for area in ROOMS if public_party_allowed(area, party_size)]
        if not eligible:
            return False, "個室は2〜8名で選択してください"
        for room in eligible:
            ok, _ = availability_check(c, room, visit_at, party_size, None, duration_minutes, exclude_id)
            if ok:
                return True, f"個室{room[-1]}に空きがあります"
        return False, "ご希望の人数で利用できる個室は満室です"

    if seating_area == "COUNTER" or seating_area in ROOMS:
        slot = (visit.hour, visit.minute)
        if slot not in ((18, 0), (20, 30)):
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
            AND counter_round=?
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
        if not public_party_allowed(seating_area, party_size):
            return False, "この個室の利用人数の範囲外です"
        if c.execute("SELECT 1 FROM seat_blocks WHERE visit_date=? AND time_text=? AND seating_area=?", (visit.date().isoformat(), visit.strftime('%H:%M'), seating_area)).fetchone():
            return False, "この個室はブロックされています"
        end = (
            visit
            + timedelta(
                minutes=duration_minutes
            )
        )

        rows = c.execute(
            """
            SELECT
                id,
                visit_at,
                duration_minutes
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

            other_end = (
                other_start
                + timedelta(
                    minutes=int(
                        row["duration_minutes"]
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
        fee = r["amount"] if now.date() >= visit.astimezone(now.tzinfo).date() - timedelta(days=3) else 0
        if confirm:
            if accepted is not True or expected_fee != fee:
                return 400, {"error": "キャンセル規定をご確認ください。"}
            c.execute("UPDATE reservations SET status='CANCELLED',cancellation_reason='CUSTOMER',cancelled_at=?,updated_at=? WHERE id=? AND status='CONFIRMED'", (now_iso(), now_iso(), r["id"]))
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

    english = r["booking_language"] == "en"
    direct = r["source"] == "DIRECT"
    confirmation_ja = "下記の内容にてご予約を確定いたしました。\nお料理代・お飲み物代は当日店舗にてお支払いください。" if direct else "ご入金を確認し、\n下記の内容にてご予約を確定いたしました。"
    confirmation_en = "Your reservation is confirmed. Please pay for your course and beverages at the restaurant on the day of your visit." if direct else "We have received your full payment and confirmed your reservation."
    msg["Subject"] = ("Nishitenma Tsukiya | Reservation Confirmed" if english
                      else "【西天満 つきや】ご予約確定のご案内")

    msg["From"] = MAIL_FROM
    msg["To"] = r["email"]

    cancel_link = cancellation_url(r)
    cancel_ja = ("ご予約のキャンセルはこちら\n" + cancel_link + "\nリンク先で内容とキャンセル規定をご確認のうえ、お手続きください。キャンセル規定に基づき返金対象額を計算します。Squareカード決済は原則自動返金し、銀行振込・処理できない場合は店舗で対応します。カード明細への反映には通常さらに2〜7営業日ほどかかる場合があります。") if cancel_link else "キャンセルをご希望の場合は店舗へお問い合わせください。"
    cancel_en = ("Cancel your reservation:\n" + cancel_link + "\nReview the cancellation policy before confirming. Eligible Square card payments are refunded automatically. Bank transfers and exceptions require assistance from the restaurant. Card statements may take a further 2–7 business days to reflect refunds.") if cancel_link else "Please contact the restaurant to cancel your reservation."
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

{annex_message(r, True)}

We look forward to welcoming you. Please arrive on time, as each seating begins together.

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

{annex_message(r)}

当店では皆様一斉にお料理のご提供を開始いたします。
ご来店時間をお守りくださいますようお願い申し上げます。

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
                        send_payment_reminder(row, invoice.get("status"))
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


class Handler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        fmt,
        *args
    ):
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
        b = text.encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

        self.send_header("Cache-Control", "no-store")
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

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path

        if p == "/api/refunds":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            c = con()
            rows = [dict(r) for r in c.execute("SELECT f.*,r.guest_name FROM cancellation_refunds f JOIN reservations r ON r.id=f.reservation_id ORDER BY f.created_at DESC")]
            c.close()
            return self.send_json({"refunds": rows, "guide": refunds.GUIDE}, 200, {"Cache-Control": "no-store"})
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
                page = page.replace("1名様 60,000円（税込）", "決済テスト専用・1予約 1円（税込）")
                page = page.replace("selected.party_size*60000", "1")
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
                    days.append({"date": day.isoformat(), "slots": slots})
            finally:
                c.close()
            return self.send_json({
                "days": days, "price_per_person": PUBLIC_COURSE_PRICE,
                "course_name": PUBLIC_COURSES[course][0] if course else "松葉蟹おまかせコース"
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
            return self.send_html((BASE / "public" / "customers.html").read_text(encoding="utf-8"))

        if p == "/api/customers" or re.fullmatch(r"/api/customers/\d+", p):
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            c = con()
            try:
                sync_customers(c)
                if p == "/api/customers":
                    rows = [dict(row) for row in c.execute("""SELECT c.id,c.name,c.company_name,c.receipt_name,c.phone,c.email,c.note,
                        c.allergies,c.disliked_foods,c.preferred_seat,c.preferred_drinks,
                        COUNT(CASE WHEN r.status='CONFIRMED' AND r.visit_at < ? THEN 1 END) AS visit_count,
                        MAX(CASE WHEN r.status='CONFIRMED' AND r.visit_at < ? THEN r.visit_at END) AS last_visit
                        FROM customers c LEFT JOIN reservations r ON r.customer_id=c.id
                        GROUP BY c.id HAVING COUNT(r.id)>0 ORDER BY c.id DESC""", (datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M"),)*2)]
                    return self.send_json(rows)
                customer_id = int(p.rsplit("/", 1)[1])
                row = c.execute("""SELECT id,name,company_name,receipt_name,phone,email,note,
                    allergies,disliked_foods,preferred_seat,preferred_drinks FROM customers WHERE id=?""", (customer_id,)).fetchone()
                if not row:
                    return self.send_json({"error": "顧客が見つかりません"}, 404)
                visits = [dict(v) for v in c.execute("""SELECT id,visit_at,party_size,course_name,seating_area,status,
                    amount,guest_note,celebration_items,plate_message,visit_note,companions
                    FROM reservations WHERE customer_id=? ORDER BY visit_at DESC,id DESC""", (customer_id,))]
                return self.send_json({"customer": dict(row), "visits": visits})
            finally:
                c.close()

        if p == "/login":
            if self.auth():
                return self.redirect("/")
            f = BASE / "public" / "login.html"
            return self.send_html(f.read_text(encoding="utf-8"))

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
                        c.preferred_seat AS customer_preferred_seat,c.preferred_drinks AS customer_preferred_drinks
                    FROM reservations r LEFT JOIN customers c ON c.id=r.customer_id
                    ORDER BY r.visit_at,r.id
                    """
                )
            ]

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
            course = bookable_course(day) if public_slot_allowed(day, "18:00") or public_slot_allowed(day, "20:30") else None
            return self.send_json({"available": bool(course),
                                   "course_name": course[0] if course else None,
                                   "amount": course[1] * party if course else None})

        if p == "/api/unpaid-invoices":
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            c = con()
            try:
                rows = [dict(x) for x in c.execute(
                    "SELECT id,guest_name,visit_at,amount,square_invoice_id "
                    "FROM reservations WHERE status='INVOICED' AND square_invoice_id IS NOT NULL "
                    "ORDER BY visit_at,id"
                )]
            finally:
                c.close()
            result = []
            for row in rows:
                try:
                    invoice = square(f"/v2/invoices/{row['square_invoice_id']}")["invoice"]
                    row["square_status"] = invoice["status"]
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
                ok, msg = availability_check(
                    c,
                    area,
                    visit,
                    party,
                    rnd,
                    dur
                )

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
                           "preferred_seat": 500, "preferred_drinks": 1000})
                if not x or any(k not in fields or not isinstance(v, str) or len(v) > fields[k]
                                for k, v in x.items()):
                    raise ValueError("入力項目または文字数を確認してください")
                values = {k: v.strip() for k, v in x.items()}
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
                        occupied = c.execute("SELECT COALESCE(SUM(party_size),0) n FROM reservations WHERE substr(visit_at,1,10)=? AND substr(visit_at,12,5)=? AND seating_area='COUNTER' AND status IN (?,?,?,?)", (day, time_text, *ACTIVE_STATUSES)).fetchone()["n"]
                        existing = {r["seat_number"] for r in c.execute("SELECT seat_number FROM seat_blocks WHERE visit_date=? AND time_text=? AND seating_area='COUNTER'", (day, time_text))}
                        assigned = set([n for n in range(1, 9) if n not in existing][:int(occupied)])
                        if num in assigned:
                            raise ValueError("予約済みの席はブロックできません")
                    elif c.execute("SELECT 1 FROM reservations WHERE substr(visit_at,1,10)=? AND substr(visit_at,12,5)=? AND seating_area=? AND status IN (?,?,?,?)", (day, time_text, area, *ACTIVE_STATUSES)).fetchone():
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
            if not SQUARE_TOKEN or not SQUARE_LOCATION_ID:
                return self.send_json({"error": "現在お申し込みを受け付けられません"}, 503)
            try:
                x = self.read_json()
                if not isinstance(x, dict):
                    raise ValueError("invalid booking")
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
                    "SELECT id,square_invoice_url,status FROM reservations "
                    "WHERE public_request_id=?", (request_id,)
                ).fetchone()
                if existing:
                    c.rollback()
                    if existing["square_invoice_url"] and existing["status"] != "CANCELLED":
                        return self.send_json({
                            "reservation_id": existing["id"],
                            "payment_url": existing["square_invoice_url"]
                        })
                    return self.send_json({"error": "申し込みを処理中です"}, 409)
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
                         PUBLIC_COURSES[course][0] if course else "松葉蟹おまかせコース"),
                     booking_language,
                     1 if test_booking else party * PUBLIC_COURSE_PRICE,
                     area, (1 if time_text == "18:00" else 2) if area == "COUNTER" else None,
                     150, "PENDING", request_id, ts, ts, ts, guest_note, celebration_items, plate_message)
                )
                rid = cur.lastrowid
                c.commit()
                row = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
            finally:
                c.close()
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
            supplied = str(self.read_json().get("password") or "")
            if not hmac.compare_digest(supplied, STAFF_LOGIN_PIN):
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

        if p.startswith("/api/reservations/") and p.endswith("/confirm-bank-payment"):
            if not self.auth():
                return self.send_json({"error": "unauthorized"}, 401)
            try:
                rid = int(p.split("/")[3])
                details = self.read_json()
                amount = int(details.get("amount"))
                reference = str(details.get("reference") or "").strip()
                staff = str(details.get("confirmed_by") or "").strip()
            except (ValueError, TypeError):
                return self.send_json({"error": "金額と振込記録を入力してください"}, 400)
            if not reference or not staff or len(reference) > 200 or len(staff) > 100:
                return self.send_json({"error": "振込記録と確認者名が必要です"}, 400)
            c = con()
            
            try:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
                if not row:
                    return self.send_json({"error": "not found"}, 404)
                if row["status"] != "INVOICED" or not row["square_invoice_id"]:
                    return self.send_json({"error": "請求済みの予約のみ確認できます"}, 409)
                if amount != row["amount"]:
                    return self.send_json({"error": "振込金額が前受け金額と一致しません"}, 409)

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
                if fresh["status"] != "INVOICED":
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
                c.execute("UPDATE reservations SET status='CANCELLED',cancellation_reason='MANUAL',cancelled_at=?,updated_at=? WHERE id=?",
                          (now_iso(), now_iso(), rid))
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
                linked_customer = x.get("customer_id") if direct else None
                if linked_customer is not None and (type(linked_customer) is not int or linked_customer < 1):
                    raise ValueError("顧客を選び直してください")
                request_id = str(x.get("request_id") or "")
                if direct:
                    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", request_id):
                        raise ValueError("画面を開き直して登録してください")
                    email = str(x.get("email") or "").strip()
                    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
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
                not x.get("email")
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

            dur = int(
                x.get(
                    "duration_minutes"
                )
                or 150
            )

            party = int(
                x["party_size"]
            )

            try:
                day = parse_dt(x["visit_at"]).date()
            except ValueError:
                return self.send_json({"error": "来店日が不正です"}, 400)
            time_text = parse_dt(x["visit_at"]).strftime("%H:%M")
            course = bookable_course(day) if public_slot_allowed(day, time_text) else None
            if not course or not 1 <= party <= 20:
                return self.send_json({"error": "この来店日に予約可能なコース・人数がありません"}, 400)
            amount = course[1] * party

            c = con()

            try:
                c.execute("BEGIN IMMEDIATE")
                if direct:
                    existing = c.execute("SELECT * FROM reservations WHERE public_request_id=?", ("direct:" + request_id,)).fetchone()
                    if existing:
                        c.rollback()
                        c.close()
                        return self.send_json(dict(existing))
                    if linked_customer is not None and not c.execute("SELECT id FROM customers WHERE id=?", (linked_customer,)).fetchone():
                        raise ValueError("顧客が見つかりません")
                if area == "PRIVATE":
                    area = next((room for room in ROOMS if public_party_allowed(room, party) and availability_check(c, room, x["visit_at"], party, None, dur)[0]), "PRIVATE")
                ok, msg = availability_check(
                    c,
                    area,
                    x["visit_at"],
                    party,
                    rnd,
                    dur
                )

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
                    "DIRECT" if direct else "PHONE",
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
                    "CONFIRMED" if direct else "PENDING",
                    ts,
                    ts,
                    guest_note,
                    celebration_items,
                    plate_message
                )
            )

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

            if direct:
                row = deliver_confirmation(row)
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
    threading.Thread(target=expiry_loop, daemon=True).start()

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
