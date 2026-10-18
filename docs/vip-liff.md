# VIP LIFF reservation requests

Endpoint: `/concierge/vip`. Four in-page steps: date → time → party → confirmation.
The only submission writes a `concierge_requests` row and the existing owner request outbox, atomically. No seat, payment, invoice, or normal/English booking records are changed. LINE ID tokens are verified by LINE and checked against the existing approved VIP/customer link on every API call. Tokens are never stored.

## Activation

1. Under the **same LINE provider** as the VIP Messaging API account (`@387reour`), create a LINE Login channel. Channel creation requires the account owner's approval of LINE Developers Agreement.
2. Add a LIFF application: Full size, endpoint `https://tsukiya-reservation.onrender.com/concierge/vip`, `openid` scope. No `chat_message.write`, email or profile permissions are needed. Publish the Login channel when ready.
3. Via the existing authenticated `/api/concierge/configure` endpoint and `X-Tsukiya-Action: concierge`, save `{"action":"liff","liff_id":"...","channel_id":"..."}`. Both IDs are public; do not store the channel secret in this app. Environment alternatives are `VIP_LIFF_ID` and `VIP_LINE_LOGIN_CHANNEL_ID`.
4. Verify the page with an approved VIP account in LINE: month arrows, unopened date, time boundaries 17:00 and 21:00, party 1 and 8, back navigation, review and request. A deliberately submitted live request will notify the owner; label it as an operational test and coordinate before sending.
5. Run the existing configure action `{"action":"install-menu"}`. It installs the approved black/ivory menu with Reservations pointing directly to the LIFF URL. The installer validates bot identity, backups the original default menu, and checks the installed image and actions.

Before LIFF configuration, the screen explicitly says it is being prepared and the existing reservation menu flow remains available. After configuration, old calendar/date/party commands return the LIFF launch button. Existing issued booking proposals are unchanged.

## Verification

`python -m unittest discover -s tests -p 'test_vip_liff.py' -q`

Tests cover request/admin integration, no reservation/payment writes, identity rejection, revoked VIPs, time/party boundaries, unopened future dates, concurrent deduplication, transaction rollback, HTTP authentication and protected configuration.

Baseline full suite at `2cf8a4b`: 217 tests, four pre-existing failures/errors (paused booking routes and guest service stock notification). The initial LIFF full-suite run added six passing tests with the same four failures/errors. A seventh HTTP integration test also passes. LINE-device testing remains gated on LINE Login registration.
