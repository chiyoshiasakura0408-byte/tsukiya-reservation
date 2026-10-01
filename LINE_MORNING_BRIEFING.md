# LINE morning briefing — pending bot connection

Requested by the owner: daily at 09:00 Asia/Tokyo, send the staff LINE group both a screenshot of that day's reservation seating chart and the morning message. Delivery is not enabled yet.

## Prepared integration

- Authenticated GET `/api/reservation-requests` defaults to the current Japan date. It returns `morning_message`, `items`, `snapshot_path`, `delivery_time`, and `delivery_enabled: false`.
- Screenshot route: `/reservations?snapshot=1&date=YYYY-MM-DD` (staff authentication required). Capture the actual page at a desktop viewport, suggested 1440 × 1000, full page. It includes the date, counter and private rooms, both seatings, status colours, and yellow request markers.
- Wait for `body[data-snapshot-ready="true"]` before capture. Do not send a login page, incomplete chart, stale image, or previous day's reservations. Abandon and flag failures for staff.
- Use the same explicit Japan date for data retrieval and screenshot capture.
- Greeting: おはようございます、本日のご予約状況をお伝え致します。
- One request: 本日ご予約の（予約名）様より、（リクエスト内容）のリクエストがございます。
- Multiple reservations with requests: append the numbered 本日のリクエスト一覧. Group multiple cake/flower choices within one reservation line. Cancelled reservations are excluded.
- No requests: greeting plus chart, without a request list. No reservations: send the empty chart plus greeting.

## Before activation

Connect the LINE bot and verify the owner-designated staff group. Configure credentials securely. Add the daily 09:00 Japan scheduler, screenshot renderer, secure short-lived image delivery, and date/group delivery deduplication. Preserve authentication for customer information; do not publicly expose the reservation page. Send text and image together, splitting long request lists only if LINE limits require it. Do not mark delivery complete if either capture or submission fails. Test with the owner's authorized destination before enabling. No LINE messages or scheduled delivery have been created by this preparation.
