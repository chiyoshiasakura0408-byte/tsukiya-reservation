# 西天満つきや｜Square Web Payments SDK 調査・開発準備
調査日：2026-10-26（日本時間）
状態：設計・準備。アプリケーション実装、Square Sandboxへの決済要求、本番変更は未実施。
基準コミット：816be767951154b99ac9ee29149b57814b7ddbff
準備ブランチ：prep/square-web-payments-20261026

## 1. 結論と公開範囲
既存のPython/SQLiteシステムへの実装は可能と判断する。Web Payments SDKでトークンを取得し、Python側からPayments APIを呼び出す。予約・顧客DBは既存に一元化する。
初期対象は日本語 /book と英語 /book-en のカード予約。日本語の銀行振込と電話の請求書・SMSリンクは現行経路を維持する。
本書は安全性の設計案であり、実装済み・検証済みを意味しない。公開可否はSandbox検証と朝倉承認後に判断する。
SDKはページ内入力を可能にするが、カード会社の3DS認証画面・銀行アプリ切替が必要になる場合はある。「一切画面切替がない」とは約束しない。

## 2. 実測した本番構成
Render get_service/list_deploys の読み取り結果：
- サービス tsukiya-reservation / srv-dae0itv40ujc73d9sej0、指定ワークスペースと一致。
- main、autoDeploy=yes、commitで自動デプロイ。
- Python、python3 run.py、インスタンス1、Oregon、plan 0.5c-512mb。
- 永続ディスク /var/data、1GB。DATA_DIRの実設定値は未確認。
- PRプレビュー無効。今回有効化・作成していない。
- 最新live：dep-dbespebbc2fs738i05gg、上記基準コミット、2026-10-25 18:34:12 JST完了。
- 実サービス healthCheckPath は空。render.yaml は /health と記載。設定差異として記録し、今回は変更しない。
- 環境変数の読み取り機能が接続ツールにない。Dashboard /env はログイン画面だったため、存在・値は未確認。秘密値を取得・記録していない。
- 本番課金、設定変更、デプロイ、既存予約DB変更は実施していない。

## 3. 既存コードの接続
| 処理 | 根拠（基準コミット） | 現状 |
|---|---|---|
| 日本語・英語入力 | public/book.html:49 / public/book-en.html:49 | POST /api/public/reservations。請求書リンクを別タブで提示。SDKなし |
| 予約登録 | run.py:3278以降 | 入力・規約同意・ブロック顧客検証、サーバー料金計算、BEGIN IMMEDIATEで空席再確認とINSERT |
| 空席 | run.py:1142 availability_check | PENDING/INVOICED/CONFIRMED/ERRORが占席。カウンター合算、個室時間重複、停止日・seat_blocks判定 |
| 料金 | run.py:131以降 / bookable_course | 松葉66,000円/名。10月ずわい44,000円の別コースも現行存在。変更対象にしない |
| 請求書 | run.py:686 make_invoice | Customer→Order→Invoice→Publish。各処理の固定冪等キー。英語は専用Locationがあれば使用 |
| Webhook | run.py:871、1888、4049 | HMAC検証、event_id一意、invoice.payment_madeでSquare再取得しPAID確認後CONFIRMED |
| 照合 | run.py:1858 reconcile_reservations | 請求書のPAID照合・確認通知の再処理。直接決済を確定する処理はない |
| 48時間期限 | run.py:1036 expire_public_reservations | 従来WEB/PHONE請求の期限処理。直接決済を追加する際は対象分離が必須 |
| 顧客管理 | run.py:400/413 | 名前・電話/メールで顧客紐付け。既存sync_customersを再利用 |
| 確認通知 | run.py:1812 deliver_confirmation | メール/SMS送信状態の保存。SENDINGで停止する障害の復旧は追加検証が必要 |
| 朝倉LINE | owner_notifications.py:31/83 | INSERT時に予約受付イベントを記録。未決済も通知対象。直接決済の仮確保にそのまま適用すると不要通知が出る |
| 経理 | accounting_booking_notices.py | 来店日当日にCONFIRMEDになった予約のみ通知。全予約通知に拡張しない |
| 返金 | refunds.py:158以降 | 朝倉承認後。Invoice→Order→Paymentを特定するので直接決済には追加対応が必要。Location照合も既存は通常Location固定 |
| 決済後対応 | guest_service.py:186 payment_event | payment.created/updatedで確定済み予約のorder_idに紐付け。予約確定より先に受信すると未紐付けのまま重複排除され得る |

「payment.created/updatedの受信コードがある」ことと「直接決済で予約確定できる」ことを区別する。現状の当該処理は顧客対応用で、直接決済の予約確定を実装していない。
既存コードの取引中にSquare通信する箇所を新経路へコピーしない。SQLite書込ロック中の外部通信は避ける。

## 4. 日本語・英語・VIPの比較
| 経路 | 現状 | 初期方針 |
|---|---|---|
| 日本語Web | CARD請求書 / BANK | CARDのみSDK。BANKは現行48h・着金確認を維持 |
| 英語Web | CARDのみ（bank_transfer.parseもBANKを拒否） | SDK、英語UI、同じサーバー料金。言語に対応するLocationを固定保存 |
| VIP LIFF | vip_liff.py：本人・VIP承認確認後リクエスト保存。席/請求/決済なし。前受けなしの案内 | 初期導入から除外。将来は席・料金を店舗が確認後、本人認証付きページ内決済を追加可能 |
| VIP提案リンク | concierge_booking.py：本人確認・空席確認後、前受けなしでCONFIRMED | 自動的に新決済へ切替えない。既存例外を維持し、事前決済へ変更する場合は別承認 |

VIPの全額事前決済への統一は既存運用の変更になる。今回の「原則前受け」は一般Webへ適用し、VIP例外を勝手に廃止しない。

## 5. Squareの日本向け条件と費用
2026-10-26に公式資料を確認。
- Squareアカウント/Application、JPY・日本のLocation、対応カードの受付状態を確認する。通常・英語Locationを別途検証する。
- SDK公開設定にApplication IDとLocation IDを渡す。Access TokenとWebhook署名鍵はサーバーのみ。
- Secure Context（HTTPS）とCSP必須。現在のHTMLはinline scriptが多く、run.pyのsend_htmlもローダーを挿入する。単純なCSP追加で壊さず、決済画面のJS外出し/nonce化、Square接続・iframe・font等の許可を検証する。
- 最新の card.tokenize(verificationDetails) フローを採用。amount（JPYの文字列）、currencyCode、intent=CHARGE、billingContact、customerInitiated=true、sellerKeyedIn=false を設定する。架空の住所・氏名は入れない。必要項目は実装時にSDKの現行型へ照合する。
- 日本のSCA/3DS要件に対応。認証成功・失敗・中断・チャレンジなしをSandboxで確認する。旧verifyBuyer中心の新規実装は採用しない。
- サーバーがCreatePaymentを行い、COMPLETED・金額・JPY・Location・Order・reference_idを照合した場合だけ予約確定。APPROVEDは確定扱いにしない。
- 公表通常料率：eコマースAPI 3.6%、請求書3.25%。SDK追加月額を前提とする有料プラン契約は不要。個別契約料率は未確認。
| 料理代 | API 3.6% | 請求書3.25% | 差額 |
|---|---:|---:|---:|
| 66,000円（1名） | 2,376円 | 2,145円 | 231円 |
| 132,000円（2名） | 4,752円 | 4,290円 | 462円 |
| 528,000円（8名） | 19,008円 | 17,160円 | 1,848円 |

Square以外の費用は、別Sandbox用ホスティング/永続ディスクを作成する場合の費用、開発・保守。今回有料リソースを追加していない。既存Renderでの処理負荷・帯域増は未測定。
返金はRefunds APIでPayment IDを指定し、COMPLETEDの決済に対して行う。原則1年以内、受領額超過不可。既存の朝倉承認・キャンセル料計算・固定冪等キーを維持する。追加の返金手数料なしとの日本向け案内を確認。元決済手数料の返還条件は現行日本契約で未確認のため、費用見積から差し引かない。

## 6. 推奨実装設計（未実装）
### API案
既存 /api/public/reservations は残し、別経路で段階導入する。
- GET /api/public/checkout/config：有効フラグ、sandbox/production、公開Application ID・Location ID、SDK URL、localeのみ。秘密は返さない。
- POST /api/public/checkouts：入力検証、料金確定、短時間確保。本人用の推測不能なcheckout secretを発行しサーバーにはハッシュ保存。同じrequest_idでもpayloadが違う場合409。
- POST /api/public/checkouts/{id}/pay：secret認証、CSRF/Origin検証、席期限確認、決済試行確保。クライアントから金額・Location・Orderの指定を受けない。
- POST /api/public/checkouts/{id}/status：secret認証で最小限の状態照会。secretをURL/query/logへ含めない。
- 既存 /webhooks/square：署名検証を維持、直接決済用payment.created/updatedを追加。既存請求書イベントは維持。

### データ・状態
追加テーブル checkout_sessions / checkout_attempts（名称案）。
既存reservationsにPENDINGを先に保存して席を確保し、新表をreservation_id UNIQUEで紐付ける。新しい予約statusだけを追加すると現行の席集計から漏れるため、初期案は現行statusを使う。
sessions：予約ID、入力ハッシュ、秘密ハッシュ、route、environment、location_id、価格スナップショット、expires_at、state、作成/更新時刻。
attempts：session、連番、固定idempotency_key（最大45文字）、Square payment_id UNIQUE、order_id、reference_id、検証結果、送信/照合状態。
amountは円整数、JPY固定。金額・来店日・人数変更時は未送信の旧確保を終了して再入力。送信済み/結果不明では変更・別決済を拒否する。

状態：HOLD → PAYING → PAID（予約CONFIRMED）。
未送信HOLDは期限到達でEXPIRED（予約CANCELLED）。
確定的失敗はFAILEDとし席解放。再試行は空席を再確保して新トークン/新試行。
タイムアウト/5xx/切断はUNKNOWNとし、再課金も席再販売も行わず、同じ冪等キー・同一要求で照合/再送。Square上で決済不存在または終端失敗/取消が確認できた時だけ解放する。

### 一時確保と競合
- 確保時間は15分を仮案とし、朝倉承認前は固定運用にしない。サーバー時刻で判定。
- BEGIN IMMEDIATE内で既存席チェック→PENDING+session登録→COMMIT。電話/銀行/VIP/スタッフ/seat_blocksも同じ在庫として判定する。
- 支払開始と期限解放は同一DBトランザクションで競合させる。PAYINGへ移った後は期限ジョブから解放しない。
- 既存48時間expire、未決済リマインダー、取消メールから新checkout経路を明示的に除外する。既存予約に新期限を遡及適用しない。
- BEGIN IMMEDIATEを終了してからSquare通信する。原則短時間のAPI即時売上（autocomplete=true）、履行までオーソリを保持する設計にはしない。
- API送信前に試行ID・冪等キー・要求の対応を永続化。障害後も同じsource_idを含む同一要求の安全な再送ができるよう、単回トークンを必要最短期間だけ暗号化保存する方式を検証する。暗号鍵は別環境変数。PAN/CVVは一切サーバーへ送らせない。トークンを監査ログに書かず、終端後は消去する。
- 結果不明が長引けば管理画面と朝倉向けに要確認を表示。監視期限・手動照合手順を決め、無期限の無通知占席を防止する。時間だけを理由に強制解放はしない。

### 決済成功と通知
API応答、Webhook、定期照合は共通のfinalize関数へ集約。
Square GetPaymentで最新状態を取得してから、短いトランザクションでPayment ID一意保存、予約CONFIRMED、payment_source=SQUARE、payment_confirmed_at、payment_audit、確認通知キューを同時COMMITする。
重複イベント・異なるイベントID・順不同でも、payment_idと予約の状態により一度だけ確定する。DB確定前にevent_idだけ処理済みにしない。
通知失敗は予約成功を取り消さない。メール/LINEは永続outboxと再送・結果不明管理で回復し、プロセス内ロックだけに頼らない。既存SENDINGの滞留を扱う。メールの配送自体を完全に一度だけ保証できるとは表現しない。
owner_notificationsのINSERTトリガー/バックフィル/queueを新checkout仮確保から除外し、支払確定後に同じ通知経路へ接続する。既存予約受付通知は変更しない。経理は当日確定分のみという現行条件を維持。
guest_service.payment_eventは予約確定後に処理し、先に未紐付け保存されたpayment_idを再紐付けできるよう補修する。

### 返金・中止・切戻し
直接決済はsessionの検証済みpayment_id/location_idから返金。請求書経路は従来方式。英語Locationも予約保存値で検証する。
3日前から100%、無断100%、減員同様の現行規約・同意証跡を維持。通常返金は朝倉承認後。決済成功後の席矛盾は自動で満席に押し込まず、未解決として新規受付停止/朝倉通知/承認付き返金対応。
旧請求書と直接決済を同じ予約に同時発行しない。直接決済がUNKNOWNのとき請求書/振込へ切り替えない。
機能フラグOFFは新規checkout受付だけ停止。進行中決済・Webhook・照合・返金は継続する。切戻しでDB/決済履歴を削除しない。

## 7. 環境設定の準備
以下は必要性の一覧。既存本番で実際に設定されているかは全て未確認。
| 設定名 | 用途 | 取扱 |
|---|---|---|
| SQUARE_ENV | 既存環境切替 | 本番は変更しない。検証はsandbox固定 |
| SQUARE_ACCESS_TOKEN | サーバーAPI | Sandbox専用、秘密 |
| SQUARE_LOCATION_ID / SQUARE_EN_LOCATION_ID | 店舗の識別 | SandboxのJPY Locationのみ |
| SQUARE_API_VERSION | API固定版 | 現行コード既定2026-08-19。SDK対応はSandboxで検証。勝手に更新しない |
| SQUARE_WEBHOOK_SIGNATURE_KEY | 署名 | Sandbox購読専用、秘密 |
| APP_BASE_URL | Webhook署名用正規URL | Sandbox用HTTPS、購読URLと一致 |
| DATA_DIR | DB保存 | 空の検証DB、本番/var/dataを共有しない |
| SQUARE_APPLICATION_ID（新規案） | ブラウザーSDK | 公開ID。Tokenと混同しない |
| WEB_CARD_CHECKOUT_ENABLED（新規案） | 段階導入 | 既定0、未実装 |
| WEB_CARD_HOLD_SECONDS（新規案） | 仮確保期限 | 仮案900、未実装 |
| CHECKOUT_TOKEN_ENCRYPTION_KEY（新規案） | 再送用一時トークン保護 | Sandbox専用秘密、未実装 |

既存アプリ起動時は通知・期限等のバックグラウンド処理も動く。検証は本番環境変数をコピーせず、空DB、メール/SMS/LINEの偽物アダプターを実装してから起動する。単にSMTP等を空欄にしただけの無害化に頼らない。
本番customer DBを検証用に複製しない。架空のゲストのみ。SDK URL・Application ID・Token・Location・署名鍵すべてを同一Sandbox環境に統一する。
今回はRender preview無効のため、必要なら別の検証サービスを作成する（費用は事前提示）。既存ディスクを共有しない。

## 8. 実装順と合格条件（全件未実施）
1. 最新mainとの差分再確認、Sandbox設定の存在確認、安全な偽物通知アダプター、環境ガード。
2. 追加テーブルと移行、確保API、料金/停止日/通し不可/英語カード限定/ブロック顧客。
3. Payments API・永続試行・一意制約・共通finalize・Webhook・照合。
4. 新経路を旧48h/通知トリガーから分離。顧客、監査、経理当日通知、返金の対応。
5. 日本語/英語SDK画面・CSP・3DS・再表示/状態照会。
6. 以下の隔離DBテスト→Square Sandbox結合→Safari/iPhone・Chrome画面検証。
| 試験 | 合格条件 |
|---|---|
| 同じ最終個室/席へ同時予約（Web/電話/BANK/VIP） | 利用枠を超えない。予約とsessionの登録は同一トランザクション |
| 二重クリック・再送・ブラウザ再読込 | 同じ試行を返し、課金/予約/監査が増殖しない |
| request_id同一で人数・日付・料金を改ざん | 409等で拒否。サーバー価格66,000円×人数 |
| 支払開始と確保期限の同時実行 | PAYINGの席を解放しない。期限済みに課金しない |
| API送信直前/直後/成功直後のプロセス終了 | 再起動後に固定キーで照合し、成功は予約確定へ回復 |
| Square成功・応答消失 | 新しいキーで課金しない。UNKNOWN表示と照合 |
| 3DS成功/拒否/閉じる/タイムアウト | 未成功は確定せず、確定的失敗は席解放。結果不明は照合 |
| Webhook重複・別ID同内容・順不同・署名偽装 | 一度だけ確定。不正署名拒否。他店/他環境/金額/注文不一致拒否 |
| Webhook未着/DB一時ロック/メール・LINE障害 | 照合とoutboxで復旧。通知失敗が予約失敗にならない |
| 返金承認・部分返金・二重操作・結果不明 | 元決済にのみ、同一キー。英語Location正常。無承認送信なし |
| 銀行/電話48h/12h前通知/自動取消 | 既存テスト継続合格、新checkoutを誤って処理しない |
| フラグOFFと切戻し | 新規停止、処理中/成功済みの照合は続く。mainマージなし |
| 仮確保の大量作成・カード試行連打 | セッション/連絡先/IPの制限、短い期限、枠確保上限 |
| 顧客・朝倉・経理・決済後お礼 | 既存紐付け、仮確保通知なし、当日経理条件、未紐付け決済の再結合 |
既存tests/test_workflow.py、test_bank_transfer.py、test_refunds.py、test_square_deadline.py、test_owner_notifications.py、test_accounting_booking_notices.py、test_vip_liff.py、test_vip_access.py等の回帰試験が必要。今回テスト実行・合格報告はしていない。

## 9. 本番前に朝倉承認が必要な事項
- 手数料差額を踏まえた日本語/英語Webカード決済の公開。
- 一時確保15分（仮案）、未完了時の画面・席解放方針。
- 本番Application/Location/Webhook追加設定、CSPと機能フラグ変更、DB移行とバックアップ、mainへのマージ・自動デプロイ。
- 有料の独立Sandbox環境を作る場合の費用。
- VIPにも前受けを適用するか（初期は除外を推奨）。
- 公開後の実課金を伴う確認を行う場合は金額・方法を別途承認。今回実課金は禁止。
承認前にSandbox合格記録、具体的差分、運用手順、費用を提示する。本書だけを根拠に本番公開しない。

## 10. 参照資料
コードは https://github.com/chiyoshiasakura0408-byte/tsukiya-reservation/tree/816be767951154b99ac9ee29149b57814b7ddbff を基準とした。
確認対象：run.py、public/book.html、public/book-en.html、public/vip-liff.html、public/concierge-book.html、vip_liff.py、vip_access.py、vip_slots.py、concierge_booking.py、booking_policy.py、bank_transfer.py、refunds.py、guest_service.py、owner_notifications.py、accounting_booking_notices.py、README_PRODUCTION.md、requirements.txt、render.yaml、既存workflow/VIPテスト。
全リポジトリのセキュリティ監査ではない。関連経路を追跡した範囲の調査であり、実行環境の秘密値・Squareアカウント個別契約は未確認。
- https://developer.squareup.com/docs/web-payments/overview
- https://developer.squareup.com/docs/web-payments/take-card-payment
- https://developer.squareup.com/docs/web-payments/content-security-policy
- https://developer.squareup.com/docs/sca-overview
- https://developer.squareup.com/reference/square/payments-api/create-payment
- https://developer.squareup.com/docs/payments-api/refund-payments
- https://squareup.com/jp/ja/payments/our-fees
- https://squareup.com/help/jp/ja/article/6116-process-refunds
- https://render.com/docs/deploys
- https://render.com/docs/health-checks
