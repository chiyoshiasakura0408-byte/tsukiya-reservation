"""Booking-only WebAuthn credentials. Staff sessions and payments are separate."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlsplit

from webauthn import (
    generate_authentication_options, generate_registration_options, options_to_json,
    verify_authentication_response, verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, parse_client_data_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, ResidentKeyRequirement, UserVerificationRequirement,
)

PREFIX = "/api/public/guest-passkeys/"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
OTP = re.compile(r"[0-9]{6}")
VERIFIED = UserVerificationRequirement.REQUIRED


class Rejected(Exception):
    def __init__(self, status=403):
        self.status = status


def configuration(app):
    origin = app.APP_BASE_URL
    url = urlsplit(origin)
    secret = os.getenv("GUEST_PASSKEY_SECRET", "")
    if (os.getenv("GUEST_PASSKEY_ENABLED") == "1" and len(secret) >= 32
            and url.scheme == "https" and url.hostname and url.netloc == url.hostname
            and origin == "https://" + url.hostname
            and all((app.SMTP_HOST, app.SMTP_USER, app.SMTP_PASS, app.MAIL_FROM,
                     app.TWILIO_ACCOUNT_SID, app.TWILIO_AUTH_TOKEN, app.TWILIO_FROM_NUMBER))):
        return origin, url.hostname, secret.encode()
    return None


def receipt_name(data):
    name = data.get("receipt_name", "")
    if not isinstance(name, str) or len(name) > 150 or any(ord(c) < 32 for c in name):
        raise ValueError("invalid receipt name")
    return name.strip()


def save_receipt_name(db, reservation_id, name):
    if name:
        db.execute("UPDATE reservations SET booking_receipt_name=? WHERE id=?", (name, reservation_id))


def connect(app):
    db = sqlite3.connect(app.DB, timeout=5)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS guest_passkey_accounts (
            customer_id INTEGER PRIMARY KEY,
            reservation_id INTEGER NOT NULL,
            user_handle BLOB NOT NULL UNIQUE,
            identity TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS guest_passkey_credentials (
            credential_id BLOB PRIMARY KEY NOT NULL,
            customer_id INTEGER NOT NULL UNIQUE,
            public_key BLOB NOT NULL,
            sign_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS guest_passkey_attempts (
            token TEXT PRIMARY KEY NOT NULL,
            step TEXT NOT NULL,
            payload TEXT NOT NULL,
            expires INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS guest_passkey_limits (
            bucket TEXT NOT NULL,
            window INTEGER NOT NULL,
            attempts INTEGER NOT NULL,
            PRIMARY KEY (bucket, window)
        );
    """)
    return db


def digest(secret, *values):
    return hmac.new(secret, json.dumps(values, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def value(data, field, pattern):
    result = data.get(field)
    if not isinstance(result, str) or not pattern.fullmatch(result):
        raise Rejected(400)
    return result


def issue(db, secret, step, payload, now, lifetime):
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO guest_passkey_attempts VALUES(?,?,?,?)",
               (digest(secret, "ticket", token), step, json.dumps(payload), now + lifetime))
    return token


def consume(db, secret, data, step, now):
    token = value(data, "ticket", TOKEN)
    key = digest(secret, "ticket", token)
    row = db.execute("DELETE FROM guest_passkey_attempts WHERE token=? AND step=? RETURNING *",
                     (key, step)).fetchone()
    # Consume before verification. Invalid assertions cannot be retried with the same challenge.
    db.commit()
    if not row or row["expires"] <= now:
        raise Rejected()
    return json.loads(row["payload"])


def charge(db, bucket, now, duration, limit):
    window = now // duration * duration
    count = db.execute("""INSERT INTO guest_passkey_limits VALUES(?,?,1)
        ON CONFLICT(bucket,window) DO UPDATE SET attempts=attempts+1 RETURNING attempts""",
        (bucket, window)).fetchone()[0]
    db.commit()
    if count > limit:
        raise Rejected(429)


def identity(app, db, reservation_id):
    row = db.execute("""
        SELECT c.id, c.name, c.email, c.phone,
               COALESCE(NULLIF(r.booking_receipt_name, ''), c.receipt_name) AS receipt_name,
               r.guest_name, r.email AS booking_email, r.phone AS booking_phone
        FROM reservations r JOIN customers c ON c.id=r.customer_id
        WHERE r.id=? AND r.status='CONFIRMED' AND r.source IN ('WEB','WEB_CARD')
          AND (r.course_name IS NULL OR r.course_name NOT LIKE '決済テスト%')
    """, (reservation_id,)).fetchone()
    if not row:
        return None
    name = lambda s: " ".join((s or "").split()).casefold()
    email = (row["email"] or "").strip().casefold()
    phone = app.normalize_jp_phone(row["phone"])
    if (not name(row["name"]) or name(row["name"]) != name(row["guest_name"])
            or email != (row["booking_email"] or "").strip().casefold()
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254
            or not re.fullmatch(r"\+[1-9][0-9]{8,14}", phone)
            or phone != app.normalize_jp_phone(row["booking_phone"])):
        return None
    return row


def fingerprint(app, secret, row):
    return digest(secret, "identity", row["id"], row["name"], row["email"],
                  app.normalize_jp_phone(row["phone"]))


def send_codes(app, row, email_code, sms_code):
    import guest_service
    guest_service.send_email(app, row["email"], (
        f"西天満つきや パスキー確認コード / Tsukiya passkey verification code: {email_code}\n"
        "有効期限10分。第三者に共有しないでください。 / Expires in 10 minutes. Do not share."
    ))
    app.send_sms(row["phone"], body=(
        f"西天満つきや パスキーSMS確認コード / Tsukiya passkey SMS code: {sms_code}\n"
        "有効期限10分。第三者に共有しないでください。 / Expires in 10 minutes. Do not share."
    ))


def begin_verification(app, db, secret, data, now):
    reservation_id = int(value(data, "reference", re.compile(r"[1-9][0-9]{0,17}")))
    email = value(data, "email", re.compile(r"[^\s@]{1,120}@[^\s@]{1,133}")).casefold()
    phone = app.normalize_jp_phone(value(data, "phone", re.compile(r"[+0-9 ()-]{10,24}")))
    row = identity(app, db, reservation_id)
    if not row or row["email"].strip().casefold() != email or app.normalize_jp_phone(row["phone"]) != phone:
        return {"ticket": secrets.token_urlsafe(32)}
    charge(db, digest(secret, "recipient", row["id"]), now, 3600, 3)
    charge(db, "verify-global", now, 3600, 60)
    email_code, sms_code = (f"{secrets.randbelow(1000000):06}" for _ in range(2))
    nonce = secrets.token_urlsafe(32)
    ticket = issue(db, secret, "verify", {
        "reservation_id": reservation_id, "identity": fingerprint(app, secret, row), "nonce": nonce,
        "email": digest(secret, "email-code", nonce, email_code),
        "sms": digest(secret, "sms-code", nonce, sms_code),
    }, now, 600)
    db.commit()
    try:
        send_codes(app, row, email_code, sms_code)
    except Exception:
        db.execute("DELETE FROM guest_passkey_attempts WHERE token=?", (digest(secret, "ticket", ticket),))
        db.commit()
        print("guest-passkeys: verification delivery failed", flush=True)
    return {"ticket": ticket}


def prepare_registration(app, db, secret, rp, data, now):
    email_code, sms_code = value(data, "email_code", OTP), value(data, "sms_code", OTP)
    pending = consume(db, secret, data, "verify", now)
    nonce = pending["nonce"]
    email_ok = hmac.compare_digest(pending["email"], digest(secret, "email-code", nonce, email_code))
    sms_ok = hmac.compare_digest(pending["sms"], digest(secret, "sms-code", nonce, sms_code))
    row = identity(app, db, pending["reservation_id"])
    if not (email_ok and sms_ok and row and fingerprint(app, secret, row) == pending["identity"]):
        raise Rejected()
    account = db.execute("SELECT user_handle FROM guest_passkey_accounts WHERE customer_id=?",
                         (row["id"],)).fetchone()
    handle = bytes(account["user_handle"]) if account else secrets.token_bytes(32)
    options = generate_registration_options(
        rp_id=rp, rp_name="Nishitenma Tsukiya", user_id=handle,
        user_name="Tsukiya booking guest", timeout=120000,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=VERIFIED),
    )
    ticket = issue(db, secret, "register", {
        "challenge": options.challenge.hex(), "handle": handle.hex(),
        "reservation_id": pending["reservation_id"], "identity": pending["identity"],
    }, now, 180)
    return {"ticket": ticket, "options": json.loads(options_to_json(options))}


def same_context(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("response"), dict):
        raise Rejected()
    client_data = value(raw["response"], "clientDataJSON", re.compile(r"[A-Za-z0-9_-]{1,4096}"))
    if parse_client_data_json(base64url_to_bytes(client_data)).cross_origin:
        raise Rejected()
    return raw


def register(app, db, origin, rp, secret, data, now):
    pending = consume(db, secret, data, "register", now)
    credential = verify_registration_response(
        credential=same_context(data.get("credential")), expected_challenge=bytes.fromhex(pending["challenge"]),
        expected_rp_id=rp, expected_origin=origin, require_user_verification=True,
    )
    db.execute("BEGIN IMMEDIATE")
    row = identity(app, db, pending["reservation_id"])
    if not row or fingerprint(app, secret, row) != pending["identity"]:
        raise Rejected()
    db.execute("""INSERT INTO guest_passkey_accounts VALUES(?,?,?,?)
        ON CONFLICT(customer_id) DO UPDATE SET reservation_id=excluded.reservation_id,
        user_handle=excluded.user_handle,identity=excluded.identity""",
        (row["id"], pending["reservation_id"], bytes.fromhex(pending["handle"]), pending["identity"]))
    db.execute("DELETE FROM guest_passkey_credentials WHERE customer_id=?", (row["id"],))
    db.execute("INSERT INTO guest_passkey_credentials VALUES(?,?,?,?)",
               (credential.credential_id, row["id"], credential.credential_public_key, credential.sign_count))
    result = {"ok": True}
    import guest_saved_cards
    if guest_saved_cards.enabled():
        # Enrollment's dual-channel proof can authorize storing the card for
        # this completed reservation. Reuse still requires a signed login.
        result["card_save_grant"] = guest_saved_cards.grant(
            app, db, secret, row, credential.credential_id, now, save_for=pending["reservation_id"])
    return result


def authenticate(app, db, origin, rp, secret, data, now):
    pending = consume(db, secret, data, "login", now)
    raw = same_context(data.get("credential"))
    credential_id = base64url_to_bytes(value(raw, "rawId", re.compile(r"[A-Za-z0-9_-]{1,2048}")))
    handle = base64url_to_bytes(value(raw["response"], "userHandle", TOKEN))
    db.execute("BEGIN IMMEDIATE")
    account = db.execute("""SELECT k.*, a.user_handle, a.reservation_id, a.identity
        FROM guest_passkey_credentials k JOIN guest_passkey_accounts a USING(customer_id)
        WHERE k.credential_id=? AND a.user_handle=?""", (credential_id, handle)).fetchone()
    if not account:
        raise Rejected()
    assertion = verify_authentication_response(
        credential=raw, expected_challenge=bytes.fromhex(pending["challenge"]),
        expected_rp_id=rp, expected_origin=origin, credential_public_key=account["public_key"],
        credential_current_sign_count=account["sign_count"], require_user_verification=True,
    )
    row = identity(app, db, account["reservation_id"])
    if (not row or row["id"] != account["customer_id"]
            or fingerprint(app, secret, row) != account["identity"]):
        raise Rejected()
    if data.get("forget") is True:
        db.execute("DELETE FROM guest_passkey_credentials WHERE customer_id=?", (row["id"],))
        db.execute("DELETE FROM guest_passkey_accounts WHERE customer_id=?", (row["id"],))
        return {"ok": True}
    db.execute("UPDATE guest_passkey_credentials SET sign_count=? WHERE credential_id=?",
               (assertion.new_sign_count, credential_id))
    result = {"guest_name": row["name"], "phone": row["phone"], "email": row["email"],
              "receipt_name": row["receipt_name"]}
    import guest_saved_cards
    if guest_saved_cards.enabled():
        guest_saved_cards.schema(db)
        result["card_grant"] = guest_saved_cards.grant(app, db, secret, row, credential_id, now)
        result["saved_card"] = guest_saved_cards.public_card(guest_saved_cards.card(app, db, row["id"]))
    return result


def process(app, action, data, source, settings):
    origin, rp, secret = settings
    now = int(time.time())
    with closing(connect(app)) as db, db:
        charge(db, digest(secret, "client", source), now, 300, 120)
        db.execute("DELETE FROM guest_passkey_attempts WHERE expires<=?", (now,))
        db.execute("DELETE FROM guest_passkey_limits WHERE window<?", (now - 7200,))
        db.commit()
        if action == "verify":
            charge(db, digest(secret, "verify-client", source), now, 300, 30)
            return begin_verification(app, db, secret, data, now)
        if action == "register-options":
            return prepare_registration(app, db, secret, rp, data, now)
        if action == "register":
            return register(app, db, origin, rp, secret, data, now)
        if action == "login-options":
            options = generate_authentication_options(rp_id=rp, timeout=120000, user_verification=VERIFIED)
            ticket = issue(db, secret, "login", {"challenge": options.challenge.hex()}, now, 180)
            return {"ticket": ticket, "options": json.loads(options_to_json(options))}
        if action == "login":
            return authenticate(app, db, origin, rp, secret, data, now)
        raise Rejected(404)


def handle_post(handler, app, path):
    settings = configuration(app)
    if not settings:
        return handler.send_json({"error": "unavailable"}, 404)
    if (handler.headers.get_all("Origin") != [settings[0]]
            or handler.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"
            or handler.headers.get_content_type() != "application/json"):
        return handler.send_json({"error": "invalid origin"}, 403)
    action = path.removeprefix(PREFIX)
    status = 200
    try:
        data = handler.read_json()
        if not isinstance(data, dict):
            raise Rejected(400)
        result = process(app, action, data, handler.client_address[0], settings)
    except Rejected as exc:
        status = exc.status
    except (ValueError, TypeError, WebAuthnException, sqlite3.IntegrityError):
        status = 403
    except Exception as exc:
        print(f"guest-passkeys: {type(exc).__name__}", flush=True)
        status = 503
    if action in {"verify", "register-options", "register", "login-options", "login"}:
        print(f"guest-passkeys: {action} {status}", flush=True)
    return handler.send_json(result if status == 200 else {"error": "verification unavailable"}, status)
