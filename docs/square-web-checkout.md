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

24 new isolated tests pass: price tampering, non-card source rejection, English location, payload/secret binding, concurrent last-room attempts, duplicate payments/Webhooks, ambiguous API responses, exact request replay, expiry, cancellation/invoice guards, resume, notification timing, English direct refunds, HTTP origin validation, page CSP/script serving and 3DS issuer frame/form permissions.

Impacted legacy regression comparison used external-HTTP/socket blocking and temporary DBs. 120 pre-existing tests contain the same 19 failures/errors on original main and this change (outdated date/closure/course assertions and existing guest-service assertions). No newly failing legacy test was identified. These are not described as an all-green suite.

Python compilation, JavaScript syntax and whitespace checks pass. Automated tests use a fake gateway; the separate live Sandbox browser results follow below.

Use `python3 scripts/test_offline.py test_web_checkout.py` for the new tests. The runner strips inherited service credentials and proxies and prohibits external HTTP/socket connections while permitting loopback test servers. Do not run unrestricted broad tests: some legacy tests have external integration paths.

## Real Square Sandbox verification

Render sign-in/workspace access is resolved. The isolated HTTPS service `tsukiya-square-sandbox-20261026` (`srv-dbfdsh2vcj2c73fp5mqg`) uses the feature branch, auto-deploy OFF, a separate ephemeral DB, Sandbox credentials and no production notification credentials. Embedded checkout is enabled only there. Tested application commit: `466777c0f5c6ca3447284e8c964eb42e630a20f4`; live deploy `dep-dbfe4t2vcj2c73fq4q50`.

- Locations API: ACTIVE JP/JPY. Real Sandbox Payments API: 132,000 JPY paid, same-key replay remained the same session, declined token released the seat. Sandbox refund initially PENDING; a later read-only Refunds API lookup confirmed COMPLETED for 132,000 JPY.
- HTTPS Chrome Japanese SDK: JCB challenge card, Square simulator OTP, payment success and confirmed reservation #1 on the same page.
- HTTPS Chrome English SDK: HOLD restored after reload; foreign Visa test card requested ZIP, decline produced an unconfirmed reservation and reselecting showed the room released. JCB 3DS cancel returned to the unpaid form. A subsequent no-challenge JCB test payment confirmed reservation #3 in English.
- Sandbox subscription `wbhk_da758ddcd8ca48f79f01dce1d16474ff`: payment.created/payment.updated, API 2026-08-19. Square delivery logs showed HTTP 200 for actual test-payment events (2026-10-26 14:22 JST).
- Initial 3DS challenge was blocked by the new CSP; Sandbox issuer form/frame origins were added and the challenge then passed. Production issuers vary, so payment pages allow HTTPS form/frame origins while script nonces and script/connect origins remain restricted. No insecure HTTP/eval script exception was added.
- No real charge, production booking change, email, LINE or SMS was sent by these tests. Test cards and fictitious contacts only.

## Production readiness and remaining checks

Read-only confirmation: Render production still runs main `816be767951154b99ac9ee29149b57814b7ddbff`. Required existing Square token/location/env/signature variables are present, but the new Application ID, encryption key and enablement flags are absent. Existing Square production subscription `wbhk_b940d71ef72f4418829920f3fd5c8620` remains enabled with booking.created, booking.updated and invoice.payment_made only. It needs payment.created/payment.updated added at release, retaining existing events/signature. No production setting, schema, main merge or deployment has been changed. Official Site version 47 is saved and not published.

iPhone/Safari real-device testing remains unperformed because the available browser is Chrome. Browser-level network-loss/process-restart testing and real notification delivery remain unverified. Local tests cover request replay, concurrent last-room attempts, ambiguous responses and duplicate Webhooks. Do not represent unperformed device or delivery checks as passed.

Before production:

1. Keep Sandbox isolated; never copy production booking data or notification credentials into it. Configuration and real Sandbox checkout are now working.
2. Complete iPhone/Safari real-device testing of SDK/3DS/reload, including a rejected authentication. Chrome success/cancel, decline, ZIP and HOLD reload have passed.
3. Verify signed Webhooks, API-success/response-loss, process restart, expiry-versus-pay races and refund processing. Keep notification transports fake during tests.
4. Resolve or explicitly assess the existing failing regression fixtures. Check that Site/concierge text matches the selected payment mode and preserves bank/phone/VIP rules.
5. Back up SQLite using a consistent SQLite backup, check production settings and the additive schema migration, then merge/deploy the tested feature branch and set the release flags. Main currently auto-deploys on commit, so do not push there during development.
6. Publish the matching official Site version, verify production readiness without charging a real card, and monitor unresolved payments and notification status. Any real-payment validation needs a separately bounded amount/method.

Unresolved payment states are deliberately retained for safety. Staff must investigate REVIEW cases before freeing seats or issuing a replacement invoice. Notification delivery still uses existing mail/LINE machinery and may require operator review if an external service accepts a message but the response is lost; this implementation does not promise exactly-once email delivery.

## Fees and operator approval

Square Japan current published ecommerce API rate is 3.6% (custom merchant terms may differ): 66,000 JPY × 3.6% = 2,376 JPY per guest; 132,000 JPY = 4,752 JPY. Published invoice rate is 3.25%, so standard-rate difference is 231 JPY per 66,000 JPY guest. Square API/SDK have no additional SDK licence charge. Sandbox Render uses the free plan; no paid service upgrade has been made. Sources checked 2026-10-26: https://squareup.com/jp/ja/payments/our-fees and https://developer.squareup.com/jp/ja/online-payment-apis (the latter still contains an older JCB rate; use current official fee table/account contract for final pricing).

The owner's initial instruction reserves production release and Square production setting changes for approval. The concrete release scope is: consistent SQLite backup, preserve existing credentials, add the two payment Webhook events, add public Application ID/new encryption key/release flags, merge PR #15 and verify Render, then publish official Site version 47 together. Production validation must not charge a real card or alter an existing reservation.
