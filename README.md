# おしゃべりやがぽん v2

慶應義塾大学 矢上祭実行委員会の専属AI Discord Bot。
Discordの会話を学習し、RAG（検索拡張生成）で質問に回答。ボイスチャンネルでの議事録自動作成やコードレビューなど、委員会活動を幅広くサポートする。

## 主な機能

### 会話学習 & RAG回答
- Discordの会話をバッチ学習（100メッセージ or 2時間ごと）
- Gemini File Search + ローカル完全一致索引によるハイブリッド検索
- 正式文書・本番実装・GitHubソース・Firebase現在値・会話を質問種別ごとに順位付け
- 10月1日切替の矢上祭年度、根拠リンク、「情報なし」判定
- ✅/⚠️/❌の回答評価と認証付き品質ダッシュボード
- メンション or DMで質問可能（DMは登録メンバーのみ）
- 1日400クエリのレート制限

### ボイスチャンネル
- **3つのモード**: listen（聞き専）/ meeting（参加者）/ chat（おしゃべり）
- **音声録音 & 文字起こし**: pycord + Gemini Audio APIで自動文字起こし
- **Discord確定話者ラベル**: ユーザー別受信トラックをサーバー表示名へ確実に紐付け
- **終了後の全体再処理**: `/leave`時に録音先頭から再文字起こしし、成功時だけ途中結果を置換
- **リアルタイム応答**: meeting/chatモードでは会話に参加（15秒間隔）
- **TTS読み上げ**: edge-tts（ja-JP-NanamiNeural）でVCに音声出力
- **議事録自動生成**: `/leave`時にGeminiで構造化議事録を作成 → Google Docsに保存

### 声紋による話者識別
- `/voiceprint register` でVCから10秒間の登録音声を取得し、音質を検証
- `/minutes` でアップロードした対面・混合音声を登録音声と照合
- Discord VCの録音は受信トラックのuser_idを確定情報として使い、声紋推定を行わない
- 十分一致した登録者だけ名前を表示し、未一致話者は `Speaker N` のまま保持
- 声紋照合は確率的なため、公開前に人が話者ラベルを確認

### コードレビュー
- Pull Request作成・更新時に再利用可能なGitHub Actions workflowから実行
- コード差分 + リポジトリ全体のコンテキストを参照
- 重大度別の指摘事項（Critical / Major / Minor / Trivial）
- 指摘はPull Requestのレビューとして投稿（Discordへのpush通知は行わない）

### レポート
- `/report weekly` - 週次レポート生成
- `/report monthly` - 月間報告書生成
- Google Docsへ保存

### スマートリアクション
- Geminiが会話を感情判定（interesting / surprised / funny）
- 10個中2〜3個にリアクションがつく程度の感度
- サーバー独自の絵文字にも対応

### 名言Bot
- `/meigen` - 過去の会話から名言をピックアップ
- 特定ユーザーの名言も検索可能

### 語録辞書
- `/glossary bulk` で一括登録（CSV形式 / →形式）
- `/glossary candidates` で文書抽出候補を確認し、`/glossary approve` で正式登録
- ひらがな読み・別名に対応
- RAG回答・音声文字起こし・議事録の精度向上に活用

### Google Drive連携
- Application Default CredentialsからDrive API・Docs APIへ直接接続
- 議事録名は `yymmdd{会議名}`、文字起こしは保存先直下の `文字起こし` フォルダへ同名で保存
- 議事録は見本に合わせた青緑の見出し、問題・対応などの小見出し、作成者表記、やがぽん画像を含むGoogle Docsとして保存
- 文字起こし・レポートはMarkdown見出しと箇条書きをGoogle Docsの段落スタイルに変換し、Noto Sans JPで保存

### 委員会サーバー・鍵管理
- `/committee_setup` で28th向けロール、カテゴリ、案内、ロール選択を重複なく整備
- 実行者にはDiscordのAdministrator権限を持つ `サーバー管理者` ロールを付与
- 表示名は本人がサーバープロフィールで変更し、所属局・学年を選んでから慶應メールで登録。登録後だけ企画長・役員を個別申請でき、兼任にも対応
- 慶應メールはサーバー資格情報でYaga Supportのusers/staffInfosと照合し、UID確定は非公開申請の管理者承認後に行う。未登録・照合保留でもDiscord登録は継続
- 全28通りの局間連絡は関係する2局と執行部だけが見える専用チャンネルを用意し、用途をトピックと固定案内に表示
- 5種類の部室・倉庫鍵を「あけ」「しめ」「さしかえ」ボタンで記録
- 矢上部室（209）は利用者に応じて必要な確認フローを自動表示
- 鍵の状態・履歴はSQLiteへ永続化し、固定の貸出状況パネル、一定時間後に消える操作通知、長時間所持への確認と報告失敗時の自動再送に対応

## コマンド一覧

| コマンド | 説明 |
|---------|------|
| `/setup` | 初期設定ウィザード（1メッセージ完結型） |
| `/committee_setup` | 28th委員会サーバーのロール・カテゴリ・案内を冪等に整備 |
| `/committee_roles` | 登録後に企画長・役員（副局長・局長）を個別申告 |
| `/guide` | やがぽんと委員会サーバーの使い方をリッチ表示 |
| `/key_status` | 部室・倉庫鍵の現在状況を表示 |
| `/key_force_return <key>` | 鍵の記録を管理者が返却済みに修正 |
| `/reset` | 設定リセット（管理者のみ） |
| `/status` | 現在の設定状況を表示 |
| `/backfill [mode] [days] [channel]` | 差分更新（推奨）または指定期間を安全に再構築 |
| `/ignore` | 現在のチャンネルを学習対象から除外 |
| `/join <mode> [document_url]` | VCに参加。`drive_set`配下のGoogle Docsを会議資料として任意で紐付け |
| `/leave` | VCから退出（議事録生成） |
| `/recording list` | 再生可能な保存録音・文字起こしを確認（Bot所有者のみ） |
| `/recording replay <session_id>` | `/leave` 相当の議事録生成を元データ非破壊で再実行 |
| `/member sync` | サーバーメンバーをロールから自動登録 |
| `/member roles` | 役職・担当・学年のロールを分類 |
| `/member register <name> [@user]` | 呼び名を設定 |
| `/member list` | 登録メンバー一覧 |
| `/meigen [@user]` | 名言を表示 |
| `/report weekly` | 週次レポート生成 |
| `/report monthly` | 月間報告書生成 |
| `/voiceprint register [@user]` | 声紋を登録 |
| `/voiceprint list` | 声紋登録済み一覧 |
| `/voiceprint delete [@user]` | 声紋を削除 |
| `/glossary add <term> <def> [reading] [aliases]` | 語録に追加 |
| `/glossary bulk` | 語録を一括登録 |
| `/glossary candidates` | 文書から抽出した確認待ち用語を表示 |
| `/glossary approve <term>` | 候補を確認済み語録として登録 |
| `/glossary list` | 語録一覧 |
| `/glossary delete <term>` | 語録から削除 |
| `/knowledge discord <message_url>` | Discordメッセージと周辺会話をKnowledgeの確認待ち資料として申請（管理者のみ） |
| `/knowledge drive <drive_url>` | Drive資料を要約・構造化してKnowledgeの確認待ち資料として申請（管理者のみ） |
| `/knowledge status` | 最近の登録申請、機密検知、Draft PR作成状況を確認（管理者のみ） |
| `/knowledge retry <bundle_id>` | 永続outboxに残った申請を再送（管理者のみ） |
| `/corpus delete` | コーパスを完全削除 |

## アーキテクチャ

```
Yagapon_Chatbot/
├── main.py              # エントリポイント（Bot + FastAPI同時起動）
├── Dockerfile           # Python・ffmpeg・音声受信依存を固定
├── compose.yaml         # 常駐・再起動・永続データ・ログ設定
├── requirements.txt
├── .env                 # 環境変数（非公開）
├── bot/
│   ├── client.py        # YagaPon Botクラス（pycord）
│   ├── config.py        # サーバーごとの設定管理
│   ├── corpus.py        # コーパス管理・バッチ学習・RAGクエリ
│   ├── voice.py         # VC録音・文字起こし・リアルタイム応答
│   ├── tts.py           # edge-tts音声生成
│   ├── reactions.py     # スマートリアクション
│   ├── key_management.py# 鍵の状態・承認・通知・Discord UI
│   ├── reports.py       # 週次・月次レポート生成
│   ├── gdrive.py        # Drive API・Docs API連携
│   └── commands/
│       ├── setup.py     # /setup ウィザード
│       ├── reset.py     # /reset
│       ├── status.py    # /status
│       ├── backfill.py  # /backfill
│       ├── ignore.py    # /ignore
│       ├── voice_cmd.py # /join, /leave
│       ├── member.py    # /member サブコマンド群
│       ├── meigen.py    # /meigen
│       ├── report.py    # /report サブコマンド群
│       ├── voiceprint.py# /voiceprint サブコマンド群
│       ├── glossary.py  # /glossary サブコマンド群
│       └── corpus_cmd.py# /corpus サブコマンド群
└── api/
    ├── server.py        # FastAPIアプリ
    └── routes.py        # /health, /status, /ask, /backfill
```

## 技術スタック

- **Discord**: py-cord 2.7+ (DAVE voice receive patch適用)
- **AI**: Google Gemini（用途別モデルへの移行中）
- **TTS**: edge-tts（ja-JP-NanamiNeural）
- **API**: FastAPI + uvicorn（同一asyncioループ）
- **Drive**: Google Drive API + Google Docs API（ADC）
- **インフラ**: Docker Compose + GCE

## セットアップ

### 1. 環境変数

`.env` ファイルを作成：

```
GOOGLE_API_KEY=your_gemini_api_key
YAGAPON_RAG_MODEL=gemini-3.8-flash
YAGAPON_RESPONSE_MODEL=gemini-3.8-flash
YAGAPON_FAST_MODEL=gemini-3.1-flash-lite
YAGAPON_TRANSCRIBE_MODEL=gemini-3.5-transcribe
YAGAPON_TRANSCRIBE_FALLBACK_MODEL=gemini-3.8-flash
YAGAPON_AUDIO_MODEL=gemini-3.8-flash
YAGAPON_DAILY_QUERY_LIMIT=400
YAGAPON_BACKFILL_CONCURRENCY=3
YAGAPON_RAG_DB_PATH=/data/rag.sqlite3
YAGAPON_KEY_DB_PATH=/data/key-management.sqlite3
YAGAPON_KEY_REMINDER_HOURS=8
YAGAPON_KEY_REMINDER_REPEAT_HOURS=12
YAGAPON_KEY_NOTIFICATION_TTL_SECONDS=300
YAGAPON_RAG_TRACE_CONTENT=false
YAGAPON_RAG_FESTIVAL_FILTER_ENABLED=false
YAGAPON_MINUTES_IMAGE_URL=https://raw.githubusercontent.com/nyuji0524/Yagapon_Chatbot/main/assets/yagapon_minutes.png
DISCORD_TOKEN=your_discord_bot_token
YAGAPON_API_TOKEN=generate_a_long_random_token
API_HOST=http://your-server-ip:8000
API_PORT=8000
GOOGLE_OAUTH_AUTHORIZED_USER_JSON=
GOOGLE_SERVICE_ACCOUNT_JSON=
YAGAPON_CONFIG_PATH=/data/server_config.json
YAGAPON_CATALOG_PATH=/data/knowledge-catalog.json
YAGAPON_CATALOG_MODEL=gemini-3.1-flash-lite
YAGAPON_CATALOG_PASSES=2
YAGAPON_VOICEPRINT_DIR=/data/voiceprints
YAGAPON_RECORDING_DIR=/data/recordings
YAGAPON_TRANSCRIPT_DIR=/data/transcripts
YAGAPON_REPLAY_DIR=/data/replays
YAGAPON_KNOWLEDGE_OUTBOX=/data/knowledge-outbox
YAGAPON_KNOWLEDGE_REPOSITORY=YagamiFes-IT/YagamiFes-IT-Knowledge
YAGAPON_KNOWLEDGE_BASE_BRANCH=main
YAGAPON_KNOWLEDGE_GCS_BUCKET=yagamiatlas-yagapon-knowledge-intake
YAGAPON_KNOWLEDGE_INCLUDE_MINUTES=true
YAGAPON_KNOWLEDGE_CURATION_ENABLED=false
YAGAPON_KNOWLEDGE_CURATION_BATCH=3
YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS=
YAGAPON_KNOWLEDGE_CURATION_STATE=/data/knowledge-curation.sqlite3
YAGAPON_KNOWLEDGE_CURATION_MODEL_CALLS_PER_RUN=6
YAGAPON_KNOWLEDGE_CURATION_GENERATIONS_PER_RUN=3
YAGAPON_KNOWLEDGE_CURATION_MAX_OPEN_BUNDLES=5
YAGAPON_KNOWLEDGE_BACKFILL_STATE=
YAGAPON_KNOWLEDGE_BACKFILL_MAX_AGE_SECONDS=28800
YAGAPON_MAX_RECORDING_BYTES=4294967296
YAGAPON_RECORDING_FREE_SPACE_RESERVE_BYTES=2147483648
YAGAPON_RECORDING_RETENTION_DAYS=7
YAGAPON_KNOWLEDGE_GUILD_ID=your_discord_guild_id
YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS=your_discord_guild_id
YAGAPON_MEETING_EXPORT_CHANNEL_IDS=
YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS=
YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS=選考
YAGAPON_SOURCE_INDEX_ENABLED=false
YAGAPON_SOURCE_INDEX_STATE=/data/source-index-state.json
YAGAPON_SOURCE_GITHUB_APP_ID=
YAGAPON_SOURCE_GITHUB_APP_INSTALLATION_ID=
YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_FILE=
YAGAPON_SOURCE_GITHUB_TOKEN=
YAGAPON_SOURCE_GITHUB_TOKEN_FILE=
YAGAPON_SOURCE_REPOSITORIES=nyuji0524/Yagapon_Chatbot,YagamiFes-IT/Yaga_Support,YagamiFes-IT/yagamifes-firebase,YagamiFes-IT/Yagamifes_Web_2025,YagamiFes-IT/YagamiFes-IT-Knowledge
YAGAPON_SOURCE_HISTORY_SINCE=2024-10-01T00:00:00+09:00
YAGAPON_SOURCE_HISTORY_COMMITS_PER_CYCLE=100
YAGAPON_FIREBASE_KNOWLEDGE_ENABLED=false
YAGAPON_FIREBASE_KNOWLEDGE_BACKEND=public
YAGAPON_FIREBASE_KNOWLEDGE_PROJECT=yagamy26th
YAGAPON_FIREBASE_KNOWLEDGE_API_KEY=
```

Firebase現在値の`public` backendは未認証RESTを使うため、Firestore Security Rulesが適用される。
`firestore`を指定した場合はADCがRulesを迂回するため、専用サービスアカウントと最小権限を使う。
非公開リポジトリをソース索引へ含める場合、本番では対象リポジトリだけへインストールした
read-only GitHub Appを使う。権限は`Contents: Read-only`と自動付与される`Metadata: Read-only`だけにし、
private keyはSecret Managerから0600のファイルとしてmountする。`YAGAPON_SOURCE_GITHUB_TOKEN`は
ローカル移行時の互換経路に限り、個人の広権限な`gh` OAuth tokenは本番へ置かない。

会議の共有Drive・Knowledgeへの自動外部化は、`@everyone`が元チャンネルを同等に
閲覧できる場合、または閲覧者の一致を運用確認したチャンネル/カテゴリIDを
`YAGAPON_MEETING_EXPORT_CHANNEL_IDS`へ明示した場合だけ行う。未指定の限定チャンネルは、
ローカル文字起こしとDiscord ACL付きFile Searchに保存する。

`YAGAPON_MINUTES_IMAGE_URL` はGoogle Docs APIから認証なしで取得できる公開URLが必要です。
既定値は公開リポジトリ上の画像です。リポジトリを非公開化する場合は、Cloud Storageなどの
公開配信URLへ必ず変更してください。

RAGと回答生成はGemini 3.8 Flash、音声認識はGemini 3.5 Transcribe、
短い要約やリアクション判定はFlash-Liteを既定値にする。モデル名は環境変数で
切り戻せる。TTSは引き続きedge-ttsを使う。料金前提と予算ガードは
[`docs/ai-model-budget.md`](docs/ai-model-budget.md)を参照。

`YAGAPON_API_TOKEN` は `/status`、`/ask`、`/backfill` APIのBearer認証に使う。
Driveへ人間のGoogleアカウントとして保存する場合は、OAuthのauthorized-user JSONを
`GOOGLE_OAUTH_AUTHORIZED_USER_JSON`へ直接設定するか、コンテナ内のread-onlyファイルパスを
指定する。この値はサービスアカウントとADCより優先される。GCEではSecret Managerから
`/data/google-drive-oauth.json`へ配置し、Compose変数`YAGAPON_GOOGLE_OAUTH_FILE`で参照する。
`GOOGLE_SERVICE_ACCOUNT_JSON`は移行中の互換用途だけに残し、新規のJSON鍵は発行しない。

### 2. Dockerで起動

```bash
mkdir -p data
docker compose up --build
```

コンテナにはffmpeg、libopus、音声受信用Pycordの固定コミットが含まれる。設定と音声サンプルは`data/`に永続化される。VC録音は`data/recordings/`へPCMを逐次保存し、メモリ量を録音時間に依存させない。逐語文字起こしは議事録とは別に`data/transcripts/`とGoogle Docsへ保存する。正常配信後は生PCMを即時削除し、文字起こし失敗や容量上限に達した録音だけを復旧用に保持する。配信済み録音のメタデータは既定7日で削除するが、独立保存した文字起こしは削除しない。

`/recording replay`は、保存済み文字起こし、または障害復旧用に残っているPCMを使い、元の録音やオフセットを更新せずに長時間議事録を再生成する。結果は実行先Discordチャンネルと、設定済みの場合はGoogle Driveへ出力する。テスト出力はKnowledgeに自動投稿しない。

VC終了時には生文字起こしとAI要約を`data/knowledge-outbox/`へ必ず保存する。本番では
`YAGAPON_KNOWLEDGE_GCS_BUCKET`へcreate-onlyで候補を送り、Knowledgeリポジトリ自身の
GitHub Actionsが専用ブランチとDraft PRを作る。BotへGitHub tokenは渡さない。
詳細なAI議事録は`YAGAPON_KNOWLEDGE_INCLUDE_MINUTES=true`の場合だけ含める。
キュー送信に失敗してもoutboxを削除しない。

Discord会話とDrive資料の正式ナレッジ化は、無条件の自動昇格ではなく
[`/knowledge`登録フロー](docs/knowledge-registration-flow.md)を使う。登録内容は必ず`status: draft`で
outboxへ残り、機密検知を通過した場合だけDraft PRになる。PR上で原本、公開範囲、年度、分類、
既存正本との重複を人が確認し、マージ後に承認済み状態へ変更された文書だけをRAGの強い根拠にする。

バックグラウンド起動：

```bash
docker compose up -d
docker compose logs -f yagapon
```

GCEではsystemdからComposeを管理し、再起動後も自動復旧させる。詳しくは[デプロイ手順](deploy/README.md)を参照。

### 3. Discord設定

1. Discordサーバーで `/setup` を実行し、RAG・メンバー同期・履歴取込みを設定
2. 28th委員会サーバーでは `/committee_setup` を実行し、ロール・カテゴリ・案内・鍵管理を整備

`/committee_setup` は同じカテゴリ内の同名リソースを再利用し、案内メッセージと権限を更新する。
既存チャンネルやロールを削除せず、再実行しても重複作成しない。局間専用チャンネルは関係する
2局と執行部だけに表示される。矢上部室（209）で確認が必要な操作は非公開の `#209利用申請` へ
届き、渉外局または執行部が承認・却下する。`#報告` の一時通知は既定で5分後に削除され、
`#鍵管理状況` は固定された現在状況だけを更新する。

Yaga Support照合は `YAGAPON_SUPPORT_BACKEND` で明示的に切り替える。既定の `disabled` では
Firestoreへ接続しない。開発時は次の手順でFirestore Emulatorとテストデータを起動できる。

```bash
docker compose --profile support-emulator up -d support-firestore
docker compose --profile support-emulator run --rm support-emulator-seed
YAGAPON_SUPPORT_BACKEND=emulator \
YAGAPON_SUPPORT_FIREBASE_PROJECT=demo-yagapon-support \
YAGAPON_SUPPORT_FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 \
YAGAPON_SUPPORT_ROLE_SYNC_ENABLED=true \
  .venv/bin/pytest -q tests/test_support_directory_emulator.py
docker compose --profile support-emulator down
```

Emulator UIは `http://127.0.0.1:4000` で確認できる。本番移行時だけバックエンドを `firestore`、
プロジェクトを本番IDへ変更し、GCEサービスアカウントへ必要最小限のFirestore IAMを付与する。
`YAGAPON_SUPPORT_ROLE_SYNC_ENABLED=true` の場合だけ、局・学年の再照合と管理者承認が完了した
既存利用者へ `矢上祭実行委員` ロールをサーバー側から追記する。クライアントの自己申告だけを
根拠にYaga Support権限を変更しない。

## 開発・テスト

Python 3.12を使用する。

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
ruff check .
pytest -q
```

Pull Requestと`main`へのpushでは、GitHub Actionsがlint、単体テスト、Dockerイメージのビルドを確認する。
`main`のCI成功後はArtifact Registryへイメージをpushし、Compute Engine上のDocker Composeを
自動更新する。初期IAM・Variables設定は[デプロイ手順](deploy/README.md)を参照。

Google共有ドライブからKnowledge用draftを作る手順は[Drive同期設計](docs/google-drive-knowledge-sync.md)を参照。
Discord・Drive資料を選択してKnowledgeへ申請する手順は[Knowledge登録フロー](docs/knowledge-registration-flow.md)を参照。
File Searchの文書設計、backfill手順、監査結果は[ナレッジ品質とbackfill運用](docs/file-search-operations.md)を参照。
年度別検索、品質ダッシュボード、評価セット、Knowledge差分索引の運用は
[RAG品質・年度・評価](docs/rag-quality-operations.md)を参照。
既存文書から用語・人名候補を一括生成し、管理画面から確認・編集するための仕様は
[用語・人名カタログ](docs/knowledge-catalog.md)を参照。

承認済みGitHubリポジトリのソース同期は`python -m source_index --dry-run`で対象を確認してから
`YAGAPON_SOURCE_INDEX_ENABLED=true`にする。privateリポジトリはread-only GitHub Appの
App ID、installation ID、private-keyファイルで読み取る。installation tokenは短命で自動更新され、
Knowledgeへの書き込みには使わない。`*_FILE`を使えばSecret Managerから0600でmountした値を
環境変数へ展開せずに利用できる。Knowledgeリポジトリはfront matterが`active`または
`historical`の文書だけを`curated`として同期し、`draft`と
`needs-review`は検索対象へ昇格させない。tokenは必要なリポジトリだけに限定し、本文やログへ出力しない。

現在のmainはcommit固定URL付きの`source_code`として、過去の変更はcommit日時・SHA・変更path・diffを
持つ`source_code_history`として索引する。履歴は一度に全件取得せず、新着を優先しながら永続cursorで
2024-10-01以降へ遡る。履歴は実装経緯の根拠であり、`deployed_implementation`とは扱わない。

Firebaseの現在値照会はFile Searchへ保存せず、質問時にallowlist済みの公開運用コレクションだけを
読み取る。`support/functions/users`、申請、整理券取得者、利用ログ、会計情報は対象外である。

## マルチギルド対応

複数の局（サーバー）で同時利用可能。各サーバーごとに独立したコーパス・設定・メンバー管理。

## ライセンス

Private - 矢上祭実行委員会 IT局
