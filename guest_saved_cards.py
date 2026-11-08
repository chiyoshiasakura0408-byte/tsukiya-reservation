"""Optional Square card-on-file for verified guest accounts."""
import hashlib
import json
import os
import re
import time
from threading import Lock

import guest_passkeys

SAVE_LOCKS = tuple(Lock() for _ in range(16))


def enabled():
    return os.getenv("GUEST_SAVED_CARDS_ENABLED") == "1" or (
        os.getenv("SQUARE_ENV") == "sandbox"
        and os.getenv("APP_BASE_URL", "").rstrip("/") == "https://tsukiya-square-sandbox-20261026.onrender.com"
    )


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS guest_saved_cards (
        environment TEXT NOT NULL, customer_id INTEGER NOT NULL, square_customer TEXT NOT NULL,
        square_card TEXT NOT NULL, brand TEXT NOT NULL, last4 TEXT NOT NULL, saved_at INTEGER NOT NULL,
        PRIMARY KEY(environment, customer_id)
    )""")


def grant(app, db, secret, row, credential_id, now):
    return guest_passkeys.issue(db, secret, "saved-card", {
        "customer": row["id"], "credential": credential_id.hex(),
        "identity": guest_passkeys.fingerprint(app, secret, row),
    }, now, 1200)


def authorize(app, db, token, reservation=None):
    settings = guest_passkeys.configuration(app)
    if not enabled() or not settings or not isinstance(token, str) or not guest_passkeys.TOKEN.fullmatch(token):
        raise guest_passkeys.Rejected()
    secret = settings[2]
    attempt = db.execute("""SELECT payload FROM guest_passkey_attempts
        WHERE token=? AND step='saved-card' AND expires>?""",
        (guest_passkeys.digest(secret, "ticket", token), int(time.time()))).fetchone()
    if not attempt:
        raise guest_passkeys.Rejected()
    data = json.loads(attempt["payload"])
    current = db.execute("""SELECT a.reservation_id FROM guest_passkey_accounts a
        JOIN guest_passkey_credentials k USING(customer_id)
        WHERE a.customer_id=? AND k.credential_id=? AND a.identity=?""",
        (data["customer"], bytes.fromhex(data["credential"]), data["identity"])).fetchone()
    row = guest_passkeys.identity(app, db, current["reservation_id"]) if current else None
    if not row or row["id"] != data["customer"] or guest_passkeys.fingerprint(app, secret, row) != data["identity"]:
        raise guest_passkeys.Rejected()
    if reservation and (
        reservation["guest_name"].strip() != row["name"].strip()
        or reservation["email"].strip().casefold() != row["email"].strip().casefold()
        or app.normalize_jp_phone(reservation["phone"]) != app.normalize_jp_phone(row["phone"])
    ):
        raise guest_passkeys.Rejected()
    return row


def card(app, db, customer_id):
    return db.execute("""SELECT * FROM guest_saved_cards
        WHERE environment=? AND customer_id=?""", (app.SQUARE_ENV, customer_id)).fetchone()


def public_card(row):
    if row:
        return {"id": row["square_card"], "brand": row["brand"], "last4": row["last4"]}
    return None


def square_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,100}", value):
        raise ValueError("invalid Square reference")
    return value


def save(app, db, row, checkout):
    existing = card(app, db, row["id"])
    if existing:
        return public_card(existing)
    # Stable merchant-local reference; never match a card holder by email/phone alone.
    namespace = hashlib.sha256((app.APP_BASE_URL + ":" + str(row["id"])).encode()).hexdigest()[:32]
    customer = app.square("/v2/customers", body={"idempotency_key": "gc-" + namespace,
        "reference_id": "tsukiya-" + namespace, "given_name": "Tsukiya online guest"})["customer"]
    customer_id = square_id(customer["id"])
    payment_id = square_id(checkout["payment_id"])
    saved = app.square("/v2/cards", body={"idempotency_key": "gs-" + checkout["id"],
        "source_id": payment_id, "card": {"customer_id": customer_id}})["card"]
    card_id = square_id(saved["id"])
    if saved.get("customer_id") != customer_id or not saved.get("enabled") or not re.fullmatch(r"[0-9]{4}", saved.get("last_4") or ""):
        raise ValueError("Square card mismatch")
    db.execute("""INSERT INTO guest_saved_cards VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(environment, customer_id) DO NOTHING""",
        (app.SQUARE_ENV, row["id"], customer_id, card_id, saved.get("card_brand") or "CARD",
         saved["last_4"], int(time.time())))
    return public_card(card(app, db, row["id"]))
