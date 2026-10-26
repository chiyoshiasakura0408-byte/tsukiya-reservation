# Square page-internal card payment — implementation status

2026-10-26. Owner requested implementation and coordinated updates to reservation guidance. Work is on a feature branch; production has not been changed.

## Implemented

- Japanese and English `/book` use `public/web-checkout.js` when enabled and configured. The existing invoice UI remains the fallback before activation. The bank choice uses its original endpoint; English remains card only. VIP exceptions remain unchanged.
- `web_checkout.py` creates a 15-minute `WEB_CARD` reservation hold using the existing seat table and `BEGIN IMMEDIATE`. Legacy telephone invoices, bank transfer and 48-hour invoice expiry retain their paths.
- Each session is bound to an opaque secret and a hash of accepted booking input. Server-owned price, currency, location, order and reference are verified. Only SDK `cnon:` tokens and confirmed `CARD` payments can confirm the booking; CASH/EXTERNAL/saved-card sources are rejected.
- Order and payment idempotency keys, an encrypted exact payment request, a persisted processing lease, API response/Webhook/reconciliation finalization, and a unique payment ID protect retries. Completed requests erase the temporary token. Raw card numbers and CVV never enter the application.
- Timeouts keep the seat and forbid new payment channels/cancellation while reconciliation replays the exact request/key. Holds not submitted for payment expire. Definitive card declines release the seat.
- Admin booking lists include the new route and checkout state. Pending holds do not trigger owner booking notifications or create customer profiles. Confirmation, owner notification and same-day-only accounting notices reuse existing paths. Direct payment refunds reuse owner approval and the persisted refund key, with the original location.
- Payment-screen CSP uses per-response nonces and Square's environment-specific resources. Confirmation emails identify the on-page card payment. English concierge guidance separates Web payment from phone invoice deadlines.
- Official Site: corresponding payment guidance prepared on four pages (Japanese home/menu, English home/course). Its release is coordinated with the backend; it is not yet published.

## Enablement configuration

Existing settings remain unchanged. Additional settings:

- `SQUARE_APPLICATION_ID`: public application ID, matching the existing Square token/environment/location.
- `CHECKOUT_TOKEN_ENCRYPTION_KEY`: secret Fernet key. Store securely as an environment variable, retain while unresolved payments exist, and never commit it.
- `WEB_CARD_CHECKOUT_ENABLED=1`: admit new embedded checkouts. Default off.
- `WEB_CARD_PRODUCTION_APPROVED=1`: additional production release gate, default off. Requires completed release checks below. Do not use it to skip testing.

Config also requires the Square token, location, Webhook signature key and HTTPS `APP_BASE_URL`. English location, if present, must belong to the same environment/currency. A feature flag switch-off stops new sessions but keeps existing sessions' payment/reconciliation paths working. Do not switch the Square environment or encryption key while sessions are unresolved.

## Verification completed

23 new isolated tests pass: price tampering, non-card source rejection, English location, payload/secret binding, concurrent last-room attempts, duplicate payments/Webhooks, ambiguous API responses, exact request replay, expiry, cancellation/invoice guards, resume, notification timing, English direct refunds, HTTP origin validation and page CSP/script serving.

Impacted legacy regression comparison used external-HTTP/socket blocking and temporary DBs. 120 pre-existing tests contain the same 19 failures/errors on original main and this change (outdated date/closure/course assertions and existing guest-service assertions). No newly failing legacy test was identified. These are not described as an all-green suite.

Python compilation, JavaScript syntax and whitespace checks pass. This is local/fake-gateway verification, not Square Sandbox certification.

Use `python3 scripts/test_offline.py test_web_checkout.py` for the new tests. The runner strips inherited service credentials and proxies and prohibits external HTTP/socket connections while permitting loopback test servers. Do not run unrestricted broad tests: some legacy tests have external integration paths.

## Not yet verified / release blockers

Square Developer sign-in succeeded. Sandbox credentials were used transiently, without displaying token values. The actual Sandbox Locations API confirmed an ACTIVE JP/JPY location. Through this implementation and a separate empty SQLite database, the real Sandbox Payments API returned PAID for 132,000 JPY, a replay returned the same PAID session, and a Square declined-card test token produced FAILED and released the seat. A Sandbox refund request was also accepted with PENDING status (not reported as completed). These are simulated Sandbox funds only; no real customer payment or notification was sent.

Actual SDK browser/3DS and signed external Webhook round-trip remain unverified. The local preview is HTTP and correctly fails Secure Context requirements; application security was not weakened to work around it. An isolated Render free service was created for HTTPS testing (tsukiya-square-sandbox-20261026, srv-dbfdsh2vcj2c73fp5mqg), using the feature branch, auto-deploy OFF and a separate ephemeral DB. It contains no production notification credentials, has no Square access token configured and embedded checkout remains OFF. Completing its environment setup is blocked because the browser's authenticated Render account does not have access to the intended workspace (tea-dadupjn40ujc73d2s28g). No production configuration, migration, main merge or production deployment has occurred.

Before production:

1. Configure an isolated Sandbox environment with a new empty DB and Sandbox-only Application ID/token/JPY locations/signature key. Never copy the production DB or notification credentials.
2. Exercise real SDK tokenization, 3DS success/rejection/cancel, foreign cards/postal code, script/CSP loading and reload recovery on iPhone/Safari and Chrome.
3. Verify signed Webhooks, API-success/response-loss, process restart, expiry-versus-pay races and refund processing. Keep notification transports fake during tests.
4. Resolve or explicitly assess the existing failing regression fixtures. Check that Site/concierge text matches the selected payment mode and preserves bank/phone/VIP rules.
5. Back up SQLite using a consistent SQLite backup, check production settings and the additive schema migration, then merge/deploy the tested feature branch and set the release flags. Main currently auto-deploys on commit, so do not push there during development.
6. Publish the matching official Site version, verify production readiness without charging a real card, and monitor unresolved payments and notification status. Any real-payment validation needs a separately bounded amount/method.

Unresolved payment states are deliberately retained for safety. Staff must investigate REVIEW cases before freeing seats or issuing a replacement invoice. Notification delivery still uses existing mail/LINE machinery and may require operator review if an external service accepts a message but the response is lost; this implementation does not promise exactly-once email delivery.
