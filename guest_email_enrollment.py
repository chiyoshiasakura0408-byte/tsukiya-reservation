"""A separate, one-time passkey invitation after a web reservation is confirmed."""
import base64
from contextlib import closing
import hashlib
import hmac
import os
import time

ORIGIN = "https://tsukiya-reservation.onrender.com"
LIFETIME = 12 * 3600


def secret_for_origin(origin):
    if origin != ORIGIN and os.getenv("GUEST_EMAIL_ENROLLMENT_ENABLED") != "1":
        return None
    if os.getenv("GUEST_EMAIL_ENROLLMENT_ENABLED") == "0":
        return None
    key = os.getenv("CHECKOUT_TOKEN_ENCRYPTION_KEY", "")
    if len(key) < 32:
        return None
    return hmac.new(key.encode(), b"tsukiya-guest-email-enrollment-v1", hashlib.sha256).digest()


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS guest_email_invites (
        reservation_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL,
        identity TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
        expires INTEGER NOT NULL, state TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
    )""")


def send(app, reservation_id):
    import guest_passkeys as pk
    settings = pk.configuration(app)
    if not settings or not secret_for_origin(app.APP_BASE_URL):
        return
    _, _, secret = settings
    with closing(pk.connect(app)) as db, db:
        schema(db)
        db.execute("BEGIN IMMEDIATE")
        row = pk.identity(app, db, reservation_id)
        if not row:
            return
        booking = db.execute("SELECT confirmation_sent_at, booking_language FROM reservations WHERE id=?",
                             (reservation_id,)).fetchone()
        if (not booking["confirmation_sent_at"] or db.execute(
            "SELECT 1 FROM guest_passkey_accounts WHERE customer_id=?", (row["id"],)
        ).fetchone() or db.execute(
            "SELECT 1 FROM guest_saved_cards WHERE customer_id=?", (row["id"],)
        ).fetchone()):
            return
        identity = pk.fingerprint(app, secret, row)
        raw = hmac.new(secret, f"email-enrollment:{reservation_id}:{identity}".encode(), hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        claim = db.execute("INSERT OR IGNORE INTO guest_email_invites VALUES(?,?,?,?,?,'SENDING',0)",
                           (reservation_id, row["id"], identity, pk.digest(secret, "email-invite", token),
                            int(time.time()) + LIFETIME)).rowcount
        recipient = row["email"]
        english = booking["booking_language"] == "en"
    if not claim:
        return
    url = settings[0] + ("/passkey-enroll?lang=en#" if english else "/passkey-enroll#") + token
    subject = ("Nishitenma Tsukiya | Set up a passkey for your next booking" if english
               else "【西天満 つきや】次回のご予約に便利なパスキー登録のご案内")
    body = (
        f"Thank you for booking Nishitenma Tsukiya.\n\nSet up a passkey for your next booking:\n{url}\n\n"
        "This link expires in 12 hours and can be used once. Please do not forward it."
        if english else
        f"このたびは西天満 つきやをご予約いただき、誠にありがとうございます。\n\n"
        f"次回のご予約でお名前や連絡先の入力を省略できるよう、以下のリンクからパスキーをご登録いただけます。\n{url}\n\n"
        "リンクは12時間以内に一度だけご利用いただけます。第三者への転送はお控えください。"
    )
    try:
        app.guest_service.send_email(app, recipient, body, subject=subject)
    except Exception as exc:
        # SMTP failure may be ambiguous; do not send a duplicate automatically.
        print(f"guest-email-enrollment: delivery unconfirmed ({type(exc).__name__})", flush=True)
        state = "UNCONFIRMED"
    else:
        state = "SENT"
    with closing(pk.connect(app)) as db, db:
        db.execute("UPDATE guest_email_invites SET state=? WHERE reservation_id=?",
                   (state, reservation_id))
