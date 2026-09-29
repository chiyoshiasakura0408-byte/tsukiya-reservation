# 西天満つきや 予約・前受け管理

## 現在の仕様
- カウンター：8席、18:00/20:30の各部を別枠で管理。各日の各部で合計8名を超える予約を拒否。
- 個室：3室を独立管理。開始時刻は18:00/20:30、利用時間は150分。重なる予約を拒否。
- 電話予約：管理画面から登録。
- 管理画面：未ログインでは従業員用ログイン画面のみ表示。Renderの`ADMIN_TOKEN`を管理パスワードとして使用し、ログイン後のセッションは12時間で失効。従業員ごとの個別アカウントではありません。
- Square：メール予約は請求書をメール送信。電話番号のみの予約は請求書を作成し、管理画面の「SMSで送る」から端末のSMS送信画面を開いて手動で送信。`invoice.payment_made` で予約を自動確定。
- Square予約：`booking.created` / `booking.updated` Webhookを受信し、外部Square予約も取り込み可能。
- 決済完了後：SMTP設定済みなら予約確定メールを1回だけ自動送信。

## Square Webhook
`https://<Renderドメイン>/webhooks/square`

購読イベント:
- invoice.payment_made
- booking.created
- booking.updated

## Renderに直接設定する秘密情報
- SQUARE_ACCESS_TOKEN
- SQUARE_LOCATION_ID
- SQUARE_WEBHOOK_SIGNATURE_KEY
- APP_BASE_URL
- SMTP_HOST / SMTP_USER / SMTP_PASS / MAIL_FROM

秘密鍵やパスワードはGitHubへ保存しないでください。

## 注意
Square Bookings APIは予約の作成・更新を扱えますが、カウンター8席を複数組に分ける「残席」概念は飲食店向けではないため、席数制御は本システムを正とします。Squareネイティブ予約から入った予約は一旦「未割当」として取り込み、席割りを確認する運用が安全です。
