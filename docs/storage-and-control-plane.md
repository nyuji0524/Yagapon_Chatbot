# 保存先と管理画面化の方針

## 現在の保存先

GCEでは永続ディスクの `/var/lib/yagapon` をコンテナの `/data` にマウントする。

| データ | コンテナ内 | 用途 |
|---|---|---|
| サーバー設定 | `/data/server_config.json` | 局名、メンバー、辞書、Drive、RAG、backfill cursor |
| 録音 | `/data/recordings/<guild>/<session>/user-*.pcm` | 文字起こし成功までの復旧用音声 |
| 生文字起こし | `/data/transcripts/<guild>/<session>.md` | 議事録とは別の恒久記録 |
| RAG品質 | `/data/rag.sqlite3` | 文書索引、質問、引用、評価 |
| 運用状態 | `/data/operations.sqlite3` | ジョブ、エラー、デッドレター（本文は保存しない） |
| Knowledge送信待ち | `/data/knowledge-outbox` | GitHub Draft PR作成前後の再送可能データ |
| Drive同期状態 | `/data/drive-sync-state.json` | change token、ハッシュ、削除状態 |
| Knowledge索引状態 | `/data/knowledge-index-state.json` | GitHub文書とFile Search文書の対応 |

秘密情報は `/etc/yagapon/yagapon.env` と Google Secret Manager に置き、上記データや
将来の管理画面からは分離する。

## Firebase連携案（未実装）

Firestoreを管理画面の control plane とし、設定、ジョブ状態、同期状態、品質指標だけを
扱う案が適している。PCM音声、全文文字起こし、SQLite本体は容量・個人情報・再処理性能の
観点から永続ディスクまたはCloud Storageに残す。

- `guildConfigs/{guildId}`: 編集可能な非秘密設定
- `jobs/{jobId}`: backfill、Drive同期、録音復旧の状態
- `deadLetters/{id}`: 再処理対象とエラー分類（本文なし）
- `metrics/{period}`: 429率、成功率、遅延、RAG評価
- 管理者認証: Firebase Authentication + サーバー側Custom Claims
- 書き込み: 管理画面から直接重要設定を書かず、認証済みAPIで検証・監査ログを残す
- 移行: `server_config.json`をキャッシュ兼ロールバック元として併存し、段階的に切り替える

現時点ではこの連携は実装せず、既存JSON契約を維持する。
