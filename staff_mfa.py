"""Opt-in authenticator-app MFA for interactive staff sessions."""
import base64
from contextlib import closing
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from threading import BoundedSemaphore

import pyotp

PASSWORD_WORK = BoundedSemaphore(2)


def enabled():
    return os.getenv("STAFF_MFA_ENABLED") == "1"


def accounts():
    raw = os.getenv("STAFF_MFA_ACCOUNTS", "")
    config = json.loads(raw)
    if not isinstance(config, dict) or not config or len(config) > 30:
        raise ValueError("invalid staff MFA configuration")
    for name, account in config.items():
        if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", name) or not isinstance(account, dict)
                or not re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{64}", account.get("password_hash", ""))
                or not re.fullmatch(r"[A-Z2-7]{32}", account.get("totp_secret", ""))):
            raise ValueError("invalid staff MFA account")
        if len(base64.b32decode(account["totp_secret"])) != 20:
            raise ValueError("invalid staff MFA secret")
    return config


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1,
                          dklen=32, maxmem=64 * 1024 * 1024)


def verify(db_path, username, password, code, now=None):
    config = accounts()
    if (not isinstance(username, str) or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)
            or len(password) > 200):
        return False
    account = config.get(username)
    if not account:
        return False
    salt, expected = account["password_hash"].split(":")
    if not PASSWORD_WORK.acquire(blocking=False):
        return False
    try:
        valid_password = hmac.compare_digest(password_hash(password, bytes.fromhex(salt)).hex(), expected)
    finally:
        PASSWORD_WORK.release()
    if not valid_password:
        return False
    authenticator = pyotp.TOTP(account["totp_secret"])
    current_step = int(time.time() if now is None else now) // authenticator.interval
    step = next((candidate for candidate in (current_step, current_step - 1, current_step + 1)
                 if hmac.compare_digest(authenticator.generate_otp(candidate), code)), None)
    if step is None:
        return False
    factor_id = hashlib.sha256(account["totp_secret"].encode()).hexdigest()
    with closing(sqlite3.connect(db_path, timeout=5)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS staff_mfa_used_steps (
            factor_id TEXT PRIMARY KEY, last_step INTEGER NOT NULL
        )""")
        row = db.execute("""INSERT INTO staff_mfa_used_steps VALUES(?,?)
            ON CONFLICT(factor_id) DO UPDATE SET last_step=excluded.last_step
            WHERE last_step<excluded.last_step RETURNING last_step""", (factor_id, step)).fetchone()
    return row is not None


def session_message(expiry):
    if not enabled():
        return expiry.encode()
    version = hashlib.sha256(os.getenv("STAFF_MFA_ACCOUNTS", "").encode()).hexdigest()
    return f"staff-mfa-v1:{version}:{expiry}".encode()
