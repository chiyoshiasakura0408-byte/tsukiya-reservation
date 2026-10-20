# Supplies inventory

`/supplies` is the iPhone-first shared staff inventory. It is the source for weekly LINE reports; the original iCloud Numbers sheet and exported XLSX are references, not live inputs.

The verified accounting LINE group and the already-paired owner receive an initial link once. Every Monday 12:00 JST the group receives a check request. Monday 17:00 JST the owner receives the current snapshot, including unchecked and unconfigured items. Staff can submit per-branch reports at any time. `備品`, `備品管理`, `備品在庫`, or `備品チェック` in that accounting group or the paired owner's direct work-channel conversation issues a fresh link. Other sources cannot request links.

The existing durable `concierge_outbox` carries these messages using its owner/work channel and stable LINE retry keys. Acceptance means LINE API accepted the request, not that a person read it. Failures remain visible in the authenticated inventory view. There are no new Render services or external credentials.

## Access and records

- Public page shell contains no stock data. APIs require an existing staff session or an unguessable, inventory-only, eight-day bearer link. Only token hashes are stored in the access table. Tokens travel in URL fragments, then a request header; they never grant access to reservations or finance.
- POST requires same origin and an action header. No CORS is enabled. Staff must keep the inventory link inside the accounting group.
- Original 35 products are seeded once, with unknown quantities/thresholds left NULL. The original annex-only wipes are excluded in the main branch unless explicitly enabled.
- Quantities and order thresholds are validated. Outstanding orders reduce the recommendation. Unset thresholds remain explicitly unconfigured. Manual need with an optional quantity supports initial use before thresholds are agreed.
- Each item update carries a revision; conflicts roll back the complete batch. A history row retains before/after values, actor and time. Completing a report retains a snapshot. Blank quantities and stale weekly checks never count as verified zero stock.
- The feature is enabled by default as authorized. To pause scheduled messages, set `supply_state.enabled` to `0`; manual inventory editing remains available. Initial/weekly notices use unique database keys, so restarts do not repeat them.

Validation: `python3 -m unittest tests.test_supplies tests.test_line_bot tests.test_security_boundary`. Tests use isolated databases and fake LINE destinations, without sending real messages.
