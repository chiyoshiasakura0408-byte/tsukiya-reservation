import os
import json
import sqlite3
import urllib.request
import urllib.error
import base64
import hmac
import hashlib
import smtplib

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage


BASE = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB = DATA_DIR / "tsukiya.sqlite"

PORT = int(os.getenv("PORT", "10000"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

SQUARE_TOKEN = os.getenv("SQUARE_ACCESS_TOKEN", "")
SQUARE_LOCATION_ID = os.getenv("SQUARE_LOCATION_ID", "")
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
    "CONFIRMED"
)

ROOMS = (
    "PRIVATE1",
    "PRIVATE2",
    "PRIVATE3"
)


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def con():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

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
            confirmation_sent_at TEXT,
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

    cols = {
        r["name"]
        for r in c.execute(
            "PRAGMA table_info(reservations)"
        )
    }

    wanted = {
        "seating_area": "TEXT",
        "counter_round": "INTEGER",
        "duration_minutes": "INTEGER DEFAULT 150",
        "square_booking_id": "TEXT",
        "confirmation_sent_at": "TEXT",
        "last_error": "TEXT",
    }

    for name, typ in wanted.items():
        if name not in cols:
            c.execute(
                f"""
                ALTER TABLE reservations
                ADD COLUMN {name} {typ}
                """
            )

    c.commit()
    return c


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


def send_sms(phone, payment_url):
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

    message_body = (
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


def make_invoice(r):
    if not r["email"] and not r["phone"]:
        raise RuntimeError(
            "メールアドレスまたは電話番号が必要です"
        )

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
                    SQUARE_LOCATION_ID,

                "reference_id":
                    f"tsukiya-reservation-{rid}",

                "customer_id":
                    customer["id"],

                "line_items": [
                    {
                        "name":
                            "お料理代",

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
        + timedelta(days=1)
    ).date().isoformat()

    if r["email"]:
        delivery_method = "EMAIL"
    else:
        delivery_method = "SHARE_MANUALLY"

    invoice = square(
        "/v2/invoices",
        body={
            "idempotency_key":
                f"tsukiya-invoice-{ikey}",

            "invoice": {
                "location_id":
                    SQUARE_LOCATION_ID,

                "order_id":
                    order["id"],

                "primary_recipient": {
                    "customer_id":
                        customer["id"]
                },

                "delivery_method":
                    delivery_method,

                "title":
                    "西天満 つきや お料理代",

                "description": (
                    "お振込みの際は下記口座までお願い致します。\n\n"
                    "三井住友銀行\n"
                    "堂島支店\n"
                    "(普)0655295\n"
                    "アサクラ　チヨシ"
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

    if not r["email"]:
        if not payment_url:
            raise RuntimeError(
                "Square請求書URLを取得できませんでした"
            )

        send_sms(
            r["phone"],
            payment_url
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

    active = ACTIVE_STATUSES

    if seating_area == "COUNTER":
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
            AND status IN (?,?,?)
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

        remain = (
            COUNTER_CAPACITY
            - used
        )

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
            AND status IN (?,?,?)
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

    msg["Subject"] = (
        "【西天満 つきや】"
        "ご予約確定のご案内"
    )

    msg["From"] = MAIL_FROM
    msg["To"] = r["email"]

    msg.set_content(
        f"""
{r['guest_name']} 様

このたびは西天満 つきやをご予約いただき、
誠にありがとうございます。

ご入金を確認し、
下記の内容にてご予約を確定いたしました。

ご来店日時：{r['visit_at']}
お席：{seating_label(r)}
人数：{r['party_size']}名様
お料理代：{r['amount']:,}円

当日は心を尽くしてお迎えいたします。
どうぞお気をつけてお越しくださいませ。

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
        status=200
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

        self.end_headers()

        self.wfile.write(b)

    def send_html(
        self,
        text,
        status=200
    ):
        b = text.encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

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
        return (
            bool(ADMIN_TOKEN)
            and hmac.compare_digest(
                self.headers.get(
                    "x-admin-token",
                    ""
                ),
                ADMIN_TOKEN
            )
        )

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path

        if p == "/":
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

                    "webhook_configured":
                        bool(
                            SQUARE_WEBHOOK_SIGNATURE_KEY
                            and APP_BASE_URL
                        ),

                    "sms_configured":
                        bool(
                            TWILIO_ACCOUNT_SID
                            and TWILIO_AUTH_TOKEN
                            and TWILIO_FROM_NUMBER
                        )
                }
            )

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

            rows = [
                dict(x)
                for x in c.execute(
                    """
                    SELECT *
                    FROM reservations
                    ORDER BY visit_at,id
                    """
                )
            ]

            c.close()

            return self.send_json(rows)

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

            c.close()

            return self.send_json(
                {
                    "available":
                        ok,

                    "message":
                        msg
                }
            )

        self.send_error(404)

    def do_POST(self):
        p = urlparse(
            self.path
        ).path

        if p == "/api/reservations/phone":
            if not self.auth():
                return self.send_json(
                    {
                        "error":
                            "unauthorized"
                    },
                    401
                )

            x = self.read_json()

            required = (
                "guest_name",
                "visit_at",
                "party_size",
                "amount",
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

            amount = int(
                x["amount"]
            )

            c = con()

            try:
                ok, msg = availability_check(
                    c,
                    area,
                    x["visit_at"],
                    party,
                    rnd,
                    dur
                )

            except Exception as e:
                c.close()

                return self.send_json(
                    {
                        "error":
                            str(e)
                    },
                    400
                )

            if not ok:
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
                )
                VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    "PHONE",
                    x["guest_name"],
                    x.get("phone"),
                    x.get("email"),
                    x["visit_at"],
                    party,
                    x.get("course_name")
                    or "お料理代",
                    amount,
                    area,
                    rnd,
                    dur,
                    "PENDING",
                    ts,
                    ts
                )
            )

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

            if r["square_invoice_id"]:
                out = dict(r)
                c.close()

                return self.send_json(out)

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

                return self.send_json(out)

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

            event_id = (
                event.get("event_id")
                or event.get("id")
                or hashlib.sha256(
                    raw
                ).hexdigest()
            )

            event_type = (
                event.get("type")
                or ""
            )

            c = con()

            seen = c.execute(
                """
                SELECT 1
                FROM webhook_events
                WHERE event_id=?
                """,
                (
                    event_id,
                )
            ).fetchone()

            if seen:
                c.close()

                return self.send_json(
                    {
                        "ok":
                            True,

                        "duplicate":
                            True
                    }
                )

            c.execute(
                """
                INSERT INTO webhook_events(
                    event_id,
                    event_type,
                    received_at
                )
                VALUES(
                    ?,?,?
                )
                """,
                (
                    event_id,
                    event_type,
                    now_iso()
                )
            )

            c.commit()

            if event_type == "invoice.payment_made":
                data = (
                    event.get("data")
                    or {}
                )

                obj = (
                    data.get("object")
                    or {}
                )

                invoice = (
                    obj.get("invoice")
                    or obj
                )

                invoice_id = invoice.get(
                    "id"
                )

                if invoice_id:
                    r = c.execute(
                        """
                        SELECT *
                        FROM reservations
                        WHERE square_invoice_id=?
                        """,
                        (
                            invoice_id,
                        )
                    ).fetchone()

                    if r:
                        ts = now_iso()

                        c.execute(
                            """
                            UPDATE reservations
                            SET
                                status='CONFIRMED',
                                last_error=NULL,
                                updated_at=?
                            WHERE id=?
                            """,
                            (
                                ts,
                                r["id"]
                            )
                        )

                        c.commit()

                        r = c.execute(
                            """
                            SELECT *
                            FROM reservations
                            WHERE id=?
                            """,
                            (
                                r["id"],
                            )
                        ).fetchone()

                        if not r[
                            "confirmation_sent_at"
                        ]:
                            ok, err = send_confirmation(r)

                            if ok:
                                c.execute(
                                    """
                                    UPDATE reservations
                                    SET
                                        confirmation_sent_at=?,
                                        last_error=NULL,
                                        updated_at=?
                                    WHERE id=?
                                    """,
                                    (
                                        now_iso(),
                                        now_iso(),
                                        r["id"]
                                    )
                                )

                            else:
                                c.execute(
                                    """
                                    UPDATE reservations
                                    SET
                                        last_error=?,
                                        updated_at=?
                                    WHERE id=?
                                    """,
                                    (
                                        err,
                                        now_iso(),
                                        r["id"]
                                    )
                                )

                            c.commit()

                c.close()

                return self.send_json(
                    {
                        "ok":
                            True
                    }
                )

            c.close()

            if event_type in (
                "booking.created",
                "booking.updated"
            ):
                try:
                    import_booking(event)

                except Exception as e:
                    print(
                        "booking import error:",
                        e
                    )

            return self.send_json(
                {
                    "ok":
                        True
                }
            )

        self.send_error(404)


if __name__ == "__main__":
    c = con()
    c.close()

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
