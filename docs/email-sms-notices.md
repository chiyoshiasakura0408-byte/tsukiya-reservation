# Guest email notification SMS

After the reservation system's SMTP server accepts a guest email, send an SMS
to the phone recorded on that reservation. The SMS asks the guest to check their
registered email address and spam/junk folder. Bank/payment reminders also ask
the guest to observe the deadline in the email. English bookings use English.
No room number, seat number, account details or private notes appear in the SMS.

Covered: reservation confirmation (including changed confirmation content), bank
instructions, payment reminders, expiry cancellation, guest refund updates, and
guest-service emails. Staff/owner emails, emails sent manually in Gmail, and
emails sent externally by Square are not intercepted.

No historical backfill or customer messages are generated during deployment.
Missing phone numbers are recorded as SKIPPED. Existing production Twilio
configuration and send_sms are reused without exposing credentials.

guest_email_sms records an event fingerprint (reservation, recipient, type,
subject and body), phone snapshot, notification body and state. Identical
callbacks cannot send twice, including concurrent calls; changed email content
can notify again. PENDING events dispatch immediately, or on the existing
five-minute worker when SMS configuration becomes available. ACCEPTED means
Twilio returned a message SID, not confirmed handset delivery. API failures or
interrupted sends are held as REVIEW and must be checked against Twilio logs
before a staff member retries. SMS failure does not change email success.
