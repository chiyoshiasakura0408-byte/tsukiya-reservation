# 西天満つきや 予約・前受け管理

## 現在の仕様
- お客様用予約ページ：`/book`。11月10日〜翌年3月20日を対象に、人数別の空席を7日間の○/×カレンダーで公開。松葉蟹おまかせコースは1名60,000円（税込）で、人数×単価の全額をSquare請求書で案内します。Squareの全額入金確認まで予約は確定しません。WEB申込は受付から48時間経過後、Squareで未払いを確認して請求書を停止し、自動取消で席を空けます。5分ごとの確認に加え、空席照会と新規申込の直前にも処理します。Squareの状態が不明な場合は取消を保留します。決済管理画面から手動取消もできます。
- 管理画面：`/`が決済管理、`/reservations`が日別予約管理。どちらも従業員ログインが必要です。お客様用ページから顧客情報や管理操作は閲覧できません。
- カウンター：8席、18:00/20:30の各部を別枠で管理。各日の各部で合計8名を超える予約を拒否。
- 個室：3室を独立管理。開始時刻は18:00/20:30、利用時間は150分。重なる予約を拒否。
- 電話予約：管理画面から登録。
- 管理画面：未ログインでは従業員用ログイン画面のみ表示。従業員ログインパスワードは`7777`。`ADMIN_TOKEN`は変更せずセッション署名に使用し、ログイン後のセッションは12時間で失効。従業員ごとの個別アカウントではありません。
- Square：全額一括払いのみ。請求書には残額全額を求める支払要求を1件だけ設定し、分割払いの受付は設けません。メール予約は請求書をメール送信。電話番号のみの予約は請求書を作成し、管理画面の「SMSで送る」から端末のSMS送信画面を開いて手動で送信。請求書が全額支払済み（`PAID`）の場合に予約を自動確定します。
- 銀行振込：管理画面の「Square未決済者・銀行入金確認」にSquare請求書の現在の決済状態を表示します。銀行口座への直接入金は自動検知しません。従業員が通帳・明細で全額着金を照合したらチェックボックスを選び、振込記録と確認者名を残します。その時点でもSquare請求書が`UNPAID`の場合だけカード決済受付を停止して予約を確定します。Square側に一部入金や状態不明が記録された例外時は手動で調査します。
- Square予約：`booking.created` / `booking.updated` Webhookを受信し、外部Square予約も取り込み可能。
- TableCheck連携：未実装。TableCheckの予約は自動取得・自動同期されません。併用する場合は管理画面へ手動登録して残席の重複を確認してください。
- 決済完了後：SMTP設定済みなら予約確定メールを送信。失敗時は管理画面から再送可能です。電話番号のみの場合は「確定SMSを作成」から端末で手動送信します。

## 本番設定と確認
1. Render Blueprintで`render.yaml`を読み込み、永続ディスクを有効にしてデプロイします。`ADMIN_TOKEN`はRenderが生成する秘密値で、従業員には共有しません。値を変更するとログイン済みセッションが無効になります。従業員ログインは`7777`です。
2. `APP_BASE_URL`に公開URL（末尾のスラッシュなし）を設定し、Squareの本番アクセストークンとロケーションID、Webhook署名キーをRenderの秘密環境変数に設定します。請求書メールを使う場合はSMTPの各値も設定します。
3. SquareのWebhook通知先を下記URLにして3イベントを購読します。テスト予約を作成し、請求書の送信、全額入金、確定メールまたは手動SMSまで確認します。銀行振込は明細を照合した後にのみ操作します。
4. `/health`の応答、Renderログ、SQLiteファイルの永続ディスクへの保存を確認します。バックアップはRenderの永続ディスクのスナップショット等で運用してください。

`ADMIN_TOKEN`はSquareのトークンではありません。顧客や従業員へ共有しないでください。ログインパスワード`7777`は短い共通番号のため、本番ではアクセス元制限などの保護が必要です。従業員別の権限分離はありません。

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
- SQUARE_EN_LOCATION_ID（任意。英語の請求画面が必要な場合、Squareで希望言語をEnglishに設定した別店舗のロケーションIDを指定。未設定なら通常のロケーションで英語の品目・件名・説明を送信しますが、Square共通UIの言語は保証されません）
- ADMIN_TOKEN（Blueprintが生成。任意の強い値に変更可能）

秘密鍵やパスワードはGitHubへ保存しないでください。

## 注意
Square Bookings APIは予約の作成・更新を扱えますが、カウンター8席を複数組に分ける「残席」概念は飲食店向けではないため、席数制御は本システムを正とします。Squareネイティブ予約から入った予約は一旦「未割当」として取り込み、席割りを確認する運用が安全です。


## Refund approval gate (2026-10-07)

Cancellation releases seats but no longer authorizes a new card refund. Both guest and staff cancellations create `AWAITING_APPROVAL`. The payment console shows an owner approval dialog with guest, reservation, cancellation reason, fee and amount. Approval requires the existing staff session plus a separate owner-only password; this is an additional password, not MFA.

Set `REFUND_APPROVAL_SECRET` in Render's secret environment settings to a unique randomly generated password of at least 20 characters, different from `ADMIN_TOKEN` and the staff PIN. Deliver it only to the owner through a secure channel, not chat, source code or a shared iPad password store. With the setting absent, short or reused, approval fails closed and card refunds remain waiting. No credential is generated or set by this code change. Five failed owner-password attempts block approval for 15 minutes within the running process; this counter resets on restart and does not replace perimeter rate limiting.

Approval records owner label, time and exact amount in the refund row. Repeated approval is rejected; amount mismatch requires a refresh. The existing worker verifies Square payment/amount/location and reuses the persisted idempotency key. Legacy unsubmitted `QUEUED` rows move to approval waiting. Legacy `SUBMITTING` rows without an approval are not resubmitted: they become manual review because Square may already have received them. Existing `PENDING` rows only query the known refund result. Never restart an uncertain refund under a new key.

Bank refunds remain manual. This change does not alter Square dashboard permissions, authenticate the owner's physical identity, prevent server-admin compromise, or undo a refund already submitted. Staff can still cancel reservations; this gate separates the monetary action.

Deployment must include the UI and server together. Restart the old worker when deploying. Before owner approval is enabled, review existing waiting/manual refunds in Square. This implementation has not been production deployed solely by committing it.
