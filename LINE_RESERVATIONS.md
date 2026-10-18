# LINE予約登録

業務グループは既存の line_receipts.GROUP のみ。送信者は concierge_settings.owner との一致が必須。LINE署名検証後だけ永続キューへ受信する。既読・Webhook受信を予約完了と扱わない。

朝倉からの画像は予約/伝票をAI分類し、予約画像だけ予約へ、伝票は従来経理へ転送。写真の再送・Webhook再配信はイベントIDと既存予約照合で重複防止。画像の年/月/店舗は補完しない。予約追加/予約・予約変更・人数変更・予約取消の自然文に対応。予約補足 <受付ID> <情報> で同じ候補を補足する。AIに書込み権限はなく、実行種類は起動語と照合する。

新規はDIRECT・CONFIRMED・当日支払い。既存の料金カレンダー・席数・150分・10月個室の時間指定ルールを使う。予約・項目結果・監査・LINE送信キューは同一トランザクション。キャンセルや変更は予約ID、または氏名+元の日付で一意に特定。決済あり/請求あり/VIPの変更・取消は既存管理画面へ誘導する。決済や返金はこの機能では実行しない。

Google同期は既存30秒のループ。最新予約内容のfingerprintと同期成功記録が一致して初めて成功と通知する。LINEのpushは既存永続outboxとretry keyで重複抑止。API受付は既読や到達保証ではない。

/line-reservations は管理者専用。過去24時間の確認済み受信画像1〜4枚を選択して再読取りできる。過去画像の再処理は origin=admin-recovery と記録し、新しいLINE受信テストとは区別する。データ不足はグループに質問する。原画像を外部公開しない。

設定: 既存OPENAI_API_KEY, LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRETとownerを使用。LINE_RESERVATION_MODEL未設定時はENGLISH_CONCIERGE_MODEL（既定gpt-4.1-mini）。既存カレンダー設定を保持。キー値をログ・Gitへ追加しない。
