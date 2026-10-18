# 西天満つきや Google Calendar同期

実装は既定で無効。予約テーブルを読み取るだけで、予約・決済・顧客通知APIを呼ばない。
30秒ごとに全予約を照合する。既存予約も初回対象。予約日時はAsia/Tokyoとして扱い、
VIP通し予約は当日23時まで、それ以外は保存されたduration_minutesを使う。

## 本番有効化に必要な操作

1. chiyoshiasakura0408@gmail.comでGoogle Calendarを開き、非公開の専用カレンダー
   `西天満つきや｜予約管理` を作成する。既存のメインカレンダーは変更しない。
2. Google CloudのプロジェクトでCalendar APIを有効化し、OAuthクライアントを設定する。
   承認済みリダイレクトURIに `https://tsukiya-reservation.onrender.com/calendar-sync/callback` を登録する。
   `/calendar-sync` を管理者ログイン済みのSafariで開き、Googleに接続する。
   アカウントと非公開カレンダーを検証した後、オフライン用トークンをDBと同じ永続ディスクに0600で保存する。
   必要なスコープはcalendar.events、calendar.calendars.readonly、calendar.acl.readonly、openid、userinfo.email。
   OAuthアプリがTestingのままだとrefresh tokenの有効期限に制約があるため、
   長期運用に適した公開状態・同意画面設定を確認する。
3. RenderのSecret環境変数に以下を設定する。トークンをGitHub・ログ・チャットに貼らない。
   - GOOGLE_CALENDAR_ID（専用カレンダーの @group.calendar.google.com ID）
   - GOOGLE_CALENDAR_CLIENT_ID
   - GOOGLE_CALENDAR_CLIENT_SECRET
   - GOOGLE_CALENDAR_REFRESH_TOKEN（任意。通常は接続画面が安全に保存）
   - GOOGLE_CALENDAR_SYNC_ENABLED=1
   - GOOGLE_CALENDAR_NAMESPACE=tsukiya-production（運用後は変更しない）
4. Renderへデプロイ後、calendar_sync_stateで全行のlast_errorがNULLになったことを確認する。
   reservationsとのID照合および専用カレンダー上の予定数・詳細の照合を行う。
   CANCELLED/CANCELED/EXPIREDは予定を作成しない。PENDING/INVOICED/CONFIRMED/ERRORを同期。
   削除済み予約は過去に同期した予定だけ削除する。過去の予約も初回同期対象。
5. Google CalendarとiPhone標準カレンダーのカレンダー一覧で専用カレンダーを表示する。
   新しい副カレンダーの端末同期選択はiPhone実機で確認する。

## 安全性・運用

固定の予定IDと所有者マーカーで重複と他の予定の誤更新を防ぐ。メインカレンダー指定は拒否。
予定はprivate、参加者なし、リマインダーなし、全更新でsendUpdates=none。
電話・メールの専用列は投影せず、要望文も一般的な電話・メール表記をマスクする。
自由記述に含まれる特殊な連絡先表記を完全に検出する保証はない。
既存の個人予定・予約行は変更しない。予約仕様の検証・変更は行わない。
エラーは同期専用テーブルにコードだけを保存し、30秒〜最大1時間間隔で継続再試行。
不正な予約1件で他の予約を停止しない。正常な予定も6時間ごとに再照合。
SQLiteと同じ永続ディスク上のファイルロックで並列workerの競合を防ぐ。
同期状態と予約DBは同じバックアップ対象。namespace・DBを別環境に複製して同じカレンダーへ接続しない。
Googleで削除された予定のtombstoneに所有者マーカーが残っていない場合は、安全のため
所有者不一致として停止する。手動削除・キャンセル後の復活を含む実API検証は有効化前に必要。

## 現在の検証範囲

モックと一時SQLiteで16テストを実行。初回・重複防止・変更・キャンセル・直接予約・
VIP時間・情報削減・再試行・予約行不変・メインカレンダー拒否を検証。
OAuthの管理者・同一オリジン制限、stateとブラウザcookie照合、PKCE、アカウント・権限検証、トークン0600保存も検証。
本番Google API・本番DB・iPhone実機は未検証。Renderの認証値の妥当性は接続時に確認する。

公式資料:
- https://developers.google.com/workspace/calendar/api/v3/reference/events/insert
- https://developers.google.com/workspace/calendar/api/v3/reference/events/patch
- https://developers.google.com/workspace/calendar/api/v3/reference/calendars/insert
