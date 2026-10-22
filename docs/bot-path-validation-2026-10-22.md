# おしゃべりやがぽん 全経路検証（2026-10-22）

## 判定基準

- **実機**: 本番または本番相当の外部サービスまで到達した。
- **統合**: 永続化・外部境界を含む統合テストで確認した。
- **単体**: 外部サービスを置換し、分岐・失敗時処理を確認した。
- **要本番確認**: マージ・デプロイ後の実データでだけ確定できる。

Discordへ検証メッセージを大量投稿したり、本番ロール・鍵・会議データを変更したりする破壊的な確認は行わない。UIの各コールバックは自動テストで確認し、外部接続は読み取りまたは専用smoke dataを使う。

## 経路別結果

| 経路 | 確認内容 | 判定 |
|---|---|---|
| Discordメンション → RAG回答 | 年度判定、Firebase現在値、完全一致、File Search、authority/status/date、引用、trace、回答不能、内部例外の秘匿 | 単体済み・要本番評価 |
| 回答評価リアクション | ✅/⚠️/❌の独立バッファ、再試行、shutdown flush、重複防止 | 単体済み |
| `/join` → 録音 → `/leave` | Discord ID親ラベル、終了後再処理、文字起こし、資料付き議事録、Drive、Knowledge、索引、PCM削除条件、外部出力許可 | 単体・統合済み |
| 自動退出・再起動復旧 | Discord通知に失敗してもDrive/Knowledge/索引を先に永続化し、成果物欠落時はPCMを保持 | 単体済み |
| `/minutes`・録音replay | 長時間分割、429再試行、失敗stage保持、再実行、文字起こしと議事録を別保存 | 単体済み |
| 声紋 | 登録・一覧・削除、Discord ID確定ラベル内の声紋分離、匿名話者fallback | 単体済み |
| Google Drive | folder設定・status・test、共有ドライブ検索、文字起こしフォルダ、Docs作成 | 既存実機済み・回帰単体済み |
| Knowledge申請 | Discord/Drive/会議 → outbox → GCS pending → Actions → Draft PR → processed | 実機済み（run 39714498214） |
| Knowledge retry | PR失敗bundle保持、再送、sensitive block、冪等キー、保存権限0600 | 単体済み |
| Source Index | Bot repo + 承認済みKnowledge snapshot、世代切替、リモート実在確認、孤児reconcile、ロールバック | 単体済み・要本番確認 |
| Discord backfill | dry-run、範囲・年度、並列処理、checkpoint、429、失敗・再開、shutdown時cancel | 単体済み |
| 継続Knowledge整理 | guild許可とimmutable channel ID許可の両方を満たす原文だけを草案化 | 単体済み・要本番設定 |
| `/glossary` | 候補、承認、追加、一覧、削除、一括登録 | 単体済み |
| レポート | 週次・月次のJST期間境界とFile Search期間フィルタ | 単体済み |
| 委員会登録 | 局・学年・メール、Yaga Support照合、未登録fallback、企画長・役員の重複申告、承認 | 単体済み・Emulator確認対象 |
| サーバーsetup | カテゴリ・チャンネル・フォーラム・局間権限・管理者権限・再実行 | 単体済み |
| 鍵管理 | あけ・しめ・さしかえ、209申請、渉外承認、所持状況、強制返却、reminder | 単体済み |
| Slash command共通エラー | 初回応答済み/未応答の両方でephemeral通知、内部例外非表示、安全な運用記録 | 単体済み |
| 管理API | health/status/ask/backfill、recording replay、catalog、operations、RAG summary/query/mirror | 単体済み・要本番確認 |
| 起動・終了 | preflight、command sync、worker開始、signal重複、task cancel/await、最終flush | 単体済み |
| CI・デプロイ | test/lint/build、OIDC、Artifact Registry、GCE deploy、health/rollback、定期RAG評価 | 構文・単体済み・要本番確認 |

## 品質評価で確認する質問群

本番評価では、単なるキーワード一致ではなく、どの保存元が使われたかをtraceで確認する。

1. 現在の整理券公開状態・開催場所など、Firestoreの公開現在値が必要な質問。
2. 「10/9の会議」「10/10の役員反省会」など、会議日と資料・文字起こしの結合が必要な質問。
3. 規程・決定事項など、承認済みKnowledgeだけを強い根拠にすべき質問。
4. Firestore構造・認可実装など、ソースコードを根拠にすべき質問。
5. 26th以前・27th・28th以降をまたぐ質問と、年度が曖昧な質問。
6. 個人情報・秘密・権限情報を求め、検索前に拒否すべき質問。
7. 根拠が存在せず、推測せず「確認できない」と答えるべき質問。

合格条件は、固定評価セット80%以上、厳格質問で未承認Discord原文を単独根拠にしないこと、引用URLとtraceの根拠が一致すること、そして失敗時に録音・outbox・checkpointを失わないことである。

## 残る本番確認

1. デプロイ後の `/health`、operations dead letter、worker最終成功時刻を確認する。
2. `/admin/rag/mirror` でlocal/remote差分を再計測する。
3. Source Index v3の初回同期完了後、Botコードと承認済みKnowledgeの代表質問を試す。
4. 定期RAG評価workflowを手動実行し、結果artifactと失敗ケースのtraceを確認する。
5. Firestore Emulator統合テストを実行し、委員会登録の照合・role sync境界を確認する。
