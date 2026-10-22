# おしゃべりやがぽん 本番運用監査（2026-10-22）

## 結論

主要機能は、Discord、Google Drive、Gemini、File Search、Firebase、GitHub Actions、GCSを分離した構成で本番稼働できる状態にある。会議録の長時間処理、再実行、ナレッジ草案の永続化、承認済みKnowledgeの検索、ソースコード検索は、それぞれ障害時に元データを失わない経路を持つ。

今回の監査では、次の本番上の不整合を修正した。

- Knowledge文書がソースコードとして登録されていた分類を `knowledge_repository` に分離した。
- `festival: null` の年度共通Knowledgeが現在年度に誤分類される処理を停止した。
- 年度共通Knowledgeは、承認済みのauthority/statusを持つ場合だけ年度指定質問の根拠にできるようにした。
- 終了シグナルの重複処理と、Discordを閉じた後の再接続競合を防止した。
- デプロイ前preflightに7分の上限を設け、VM上で無期限停止しないようにした。
- 再文字起こし成功後は、生文字起こしを恒久領域に保存してから元PCMを削除するようにした。途中失敗時はPCMを保持する。

## データフロー

### 会議

1. `/join` で会議資料URLを登録し、PCMを `/data/recordings` に逐次保存する。
2. 文字起こしは `/data/transcripts` に会議単位で原文を保存する。
3. 議事録生成は長文を分割・統合し、Google Driveへ「議事録」と「文字起こし」を別文書として保存する。
4. 原文・議事録・資料参照をローカル完全一致DBとFile Searchへ登録する。
5. Knowledge向け草案を `/data/knowledge-outbox/<bundle-id>` に保存し、GCSの `pending/` へ送る。
6. KnowledgeリポジトリのActionsが検証してDraft PRを作成する。成功時だけ `processed/` へ移し、PR作成失敗時は `pending/`、検証失敗時は `failed/` に残す。

失敗したPR候補は本文・根拠・manifestを含む同一bundleとして残る。次回の定期実行または手動再試行では、このbundleを再生成せず送信する。`blocked-sensitive-content` は自動再送しない。

### RAG

検索時の根拠は次の順で扱う。

1. Firebaseの現在値（公開状態など実データに限る）
2. 正式決定・承認済みKnowledge
3. デプロイ済み実装・リポジトリ実装
4. 会議録・Discord原文

年度付き質問では同年度を優先し、年度未設定の文書は `canonical`、`curated`、`reviewed_record` のいずれかで、かつ有効なstatusを持つ年度共通文書だけを許可する。Discordの未整理ログやlegacy文書は年度不明のまま根拠にしない。

### ソースコードとKnowledge

- `source_code`: 実装事実の確認用。コメントや文字列内の命令には従わない。
- `knowledge_repository`: 人がレビューした規程、決定、運用知識。文書本文を根拠として扱うが、プロンプト命令としては扱わない。
- GCSの承認済みsnapshotだけをKnowledge同期元にし、Draft PRの内容を直接本番検索へ入れない。

## 永続領域と障害時の扱い

| データ | 保存先 | 失敗時 |
|---|---|---|
| 録音PCM | `/data/recordings` | 文字起こし未完了なら保持 |
| 生文字起こし | `/data/transcripts` | 議事録失敗でも保持 |
| 再生ジョブ | `/data/replays` | stage/errorを保持し再調査可能 |
| Knowledge草案 | `/data/knowledge-outbox` とGCS | PR失敗時も保持して再送 |
| 完全一致検索 | SQLite | File Search障害時の根拠候補に利用 |
| Source Index状態 | `/data/source-index-state.json` | 世代アップロード失敗時は旧世代を保持 |
| 運用イベント | SQLite | 成否・dead letterを記録 |

## 残る運用作業

### ブロッカー

- Knowledgeリポジトリで「Allow GitHub Actions to create and approve pull requests」を管理者が有効化する。現在はActionsの標準 `GITHUB_TOKEN` がPR作成時に403となる。PATは不要で、保留bundleはGCS `pending/` に残っている。
- 設定後、`Yagapon intake` workflowを手動実行し、保留bundleがPR化されて `processed/` に移ることを確認する。

### 優先度高

- 過去の `delivered_recording_retained` PCMは、対応する文字起こしが恒久保存されていることを1件ずつ照合してから削除する。一括削除は禁止する。
- `source-index-watch` と `knowledge-curation-watch` は「プロセスが存在する」だけでなく、最終成功時刻と最終エラーを `/health` へ公開する。
- 1GB VMでは起動時import・preflight・イメージ展開が重い。常時swap使用や再起動時間が運用上問題になる場合は、2GB以上へ増強する。
- RAG評価は質問件数に対しリアクション評価が少ない。正答率を判断できる量まで、✅/⚠️/❌と固定評価セットを蓄積する。

### 優先度中

- `committee_server.py`、`corpus.py`、`key_management.py`、`voice.py` は責務が大きい。コマンドUI、永続化、権限判定、外部APIを段階的に分離する。
- Python依存は上限付き範囲指定が中心で完全再現lockではない。検証済みconstraints/lockを生成し、Dependabot等で定期更新する。
- 運用イベント・完了済みreplay・完了済みoutboxに保持期間を定義する。`pending`、`failed`、`blocked` は成功確認前に消さない。
- Firebaseを管理画面化する場合も、クライアントが自由更新できる値を認可根拠にしない。サーバー管理領域またはCustom Claimsを用いる。

## 機能追加時の境界

- Discordコマンドは入力と応答に限定し、外部API・保存・権限判定をサービス層へ置く。
- 外部送信は必ず冪等キー、永続outbox、再試行上限、dead letterを持つ。
- AI生成物はDraftとして扱い、正式Knowledgeへの反映はPRレビューを通す。
- source type、authority、status、festival、source URLを新しい全ナレッジに必須メタデータとして付ける。
- 破壊的な後処理は、代替成果物の存在とサイズを確認してから行う。
- デプロイはpreflight、health、ロールバックの順序を維持し、本番データ領域をイメージ更新から分離する。

