import base64
import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

import guest_passkeys as pk
import run


def encode(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class PasskeyTests(unittest.TestCase):
    origin = "https://book.example.test"
    rp = "book.example.test"
    settings = (origin, rp, b"separate secret for tests only---")

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(directory) / "bookings.sqlite"
        self.enterContext(patch.object(run, "DB", self.db))
        with closing(run.con()) as db, db:
            db.execute("INSERT INTO customers(id,match_key,name,email,phone,receipt_name,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (1, "customer-one", "Guest One", "one@example.test", "+819011112222", "Company One", "PRIVATE", "now", "now"))
            db.execute("INSERT INTO customers(id,match_key,name,email,phone,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (2, "customer-two", "Guest Two", "two@example.test", "+819011112222", "now", "now"))
            for rid, cid, name, email in ((100, 1, "Guest One", "one@example.test"),
                                          (200, 2, "Guest Two", "two@example.test")):
                db.execute("""INSERT INTO reservations(id,customer_id,source,status,guest_name,email,phone,visit_at,party_size,amount,created_at,updated_at)
                             VALUES(?,?,'WEB_CARD','CONFIRMED',?,?,'090-1111-2222','2027-01-03T18:00',2,132000,'now','now')""",
                           (rid, cid, name, email))

    def call(self, action, **data):
        return pk.process(run, action, data, "local-test", self.settings)

    def registration_options(self, ref="100", email="one@example.test"):
        with patch.object(pk, "send_codes") as sender:
            result = self.call("verify", reference=ref, email=email, phone="090-1111-2222")
        sender.assert_called_once()
        email_code, sms_code = sender.call_args.args[-2:]
        return self.call("register-options", ticket=result["ticket"], email_code=email_code, sms_code=sms_code)

    def client_data(self, challenge, registration=False, origin=None, cross_origin=False):
        return json.dumps({
            "type": "webauthn.create" if registration else "webauthn.get",
            "challenge": challenge, "origin": origin or self.origin, "crossOrigin": cross_origin,
        }).encode()

    def enroll(self, ref="100", email="one@example.test", label=b"credential-one", **options):
        start = self.registration_options(ref, email)
        key = ec.generate_private_key(ec.SECP256R1())
        public = key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: public.x.to_bytes(32), -3: public.y.to_bytes(32)})
        rp_hash = hashlib.sha256(self.rp.encode()).digest()
        flags = b"\x45"  # User present, user verified, attested credential data.
        auth = rp_hash + flags + bytes(4) + bytes(16) + len(label).to_bytes(2) + label + cose
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        raw = {"id": encode(label), "rawId": encode(label), "type": "public-key", "response": {
            "clientDataJSON": encode(self.client_data(start["options"]["challenge"], True, **options)),
            "attestationObject": encode(attestation),
        }}
        self.assertEqual(self.call("register", ticket=start["ticket"], credential=raw), {"ok": True})
        return key, label, start["options"]["user"]["id"]

    def signed_login(self, key, label, handle, *, flags=5, count=1, **options):
        start = self.call("login-options")
        client = self.client_data(start["options"]["challenge"], **options)
        auth = hashlib.sha256(self.rp.encode()).digest() + bytes([flags]) + count.to_bytes(4)
        signature = key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"ticket": start["ticket"], "credential": {
            "id": encode(label), "rawId": encode(label), "type": "public-key", "response": {
                "clientDataJSON": encode(client), "authenticatorData": encode(auth),
                "signature": encode(signature), "userHandle": handle,
            },
        }}

    def test_signed_authentication_and_replay(self):
        params = self.signed_login(*self.enroll())
        self.assertEqual(self.call("login", **params), {
            "guest_name": "Guest One", "email": "one@example.test", "phone": "+819011112222",
            "receipt_name": "Company One",
        })
        with self.assertRaises(pk.Rejected):
            self.call("login", **params)

    def test_cross_customer_handle_cannot_read_another_profile(self):
        one = self.enroll()
        two = self.enroll("200", "two@example.test", b"credential-two")
        with self.assertRaises(pk.Rejected):
            self.call("login", **self.signed_login(one[0], one[1], two[2]))
        self.assertEqual(self.call("login", **self.signed_login(*two))["guest_name"], "Guest Two")

    def test_wrong_origin_and_missing_verification(self):
        account = self.enroll()
        for changes in ({"origin": "https://evil.example.test"}, {"flags": 1}, {"cross_origin": True}):
            with self.subTest(changes=changes), self.assertRaises((pk.Rejected, pk.WebAuthnException)):
                self.call("login", **self.signed_login(*account, **changes))

    def test_codes_are_both_required_and_consumed(self):
        with patch.object(pk, "send_codes") as sender:
            result = self.call("verify", reference="100", email="one@example.test", phone="090-1111-2222")
        email, sms = sender.call_args.args[-2:]
        wrong = f"{(int(sms) + 1) % 1000000:06}"
        for code in (wrong, sms):
            with self.assertRaises(pk.Rejected):
                self.call("register-options", ticket=result["ticket"], email_code=email, sms_code=code)

    def test_invalid_contact_and_wrong_link_never_send(self):
        with patch.object(pk, "send_codes") as sender:
            self.call("verify", reference="100", email="two@example.test", phone="090-1111-2222")
            with closing(run.con()) as db, db:
                db.execute("UPDATE reservations SET customer_id=2 WHERE id=100")
            self.call("verify", reference="100", email="one@example.test", phone="090-1111-2222")
            sender.assert_not_called()

    def test_recovery_replaces_old_key_and_deletion_leaves_booking(self):
        old = self.enroll()
        new = self.enroll(label=b"replacement")
        with self.assertRaises(pk.Rejected):
            self.call("login", **self.signed_login(*old))
        self.assertEqual(self.call("login", forget=True, **self.signed_login(*new)), {"ok": True})
        with self.assertRaises(pk.Rejected):
            self.call("login", **self.signed_login(*new))
        with closing(run.con()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reservations").fetchone()[0], 2)

    def test_contact_correction_revokes_access(self):
        account = self.enroll()
        with closing(run.con()) as db, db:
            db.execute("UPDATE customers SET email='new@example.test' WHERE id=1")
        with self.assertRaises(pk.Rejected):
            self.call("login", **self.signed_login(*account))

    def test_expired_challenge_and_rate_limit(self):
        account = self.enroll()
        params = self.signed_login(*account)
        with closing(pk.connect(run)) as db, db:
            db.execute("UPDATE guest_passkey_attempts SET expires=0")
        with self.assertRaises(pk.Rejected):
            self.call("login", **params)
        with patch.object(pk, "send_codes") as sender:
            for _ in range(2):
                self.call("verify", reference="100", email="one@example.test", phone="090-1111-2222")
            with self.assertRaises(pk.Rejected) as exc:
                self.call("verify", reference="100", email="one@example.test", phone="090-1111-2222")
            self.assertEqual(exc.exception.status, 429)
            self.assertEqual(sender.call_count, 2)


if __name__ == "__main__":
    unittest.main()
