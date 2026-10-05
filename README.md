# おしゃべりやがぽん v2

慶應義塾大学 矢上祭実行委員会の専属AI Discord Bot。
Discordの会話を学習し、RAG（検索拡張生成）で質問に回答。ボイスチャンネルでの議事録自動作成やコードレビューなど、委員会活動を幅広くサポートする。

## 主な機能

### 会話学習 & RAG回答
- Discordの会話をバッチ学習（100メッセージ or 2時間ごと）
- Gemini File Search（コーパス）によるRAG検索で質問に回答
- メンション or DMで質問可能（DMは登録メンバーのみ）
- 1日400クエリのレート制限

### ボイスチャンネル
- **3つのモード**: listen（聞き専）/ meeting（参加者）/ chat（おしゃべり）
- **音声録音 & 文字起こし**: pycord + Gemini Audio APIで自動文字起こし
- **リアルタイム応答**: meeting/chatモードでは会話に参加（15秒間隔）
- **TTS読み上げ**: edge-tts（ja-JP-NanamiNeural）でVCに音声出力
- **議事録自動生成**: `/leave`時にGeminiで構造化議事録を作成 → Google Docsに保存

### 声紋登録
- `/voiceprint register` でVCで10秒間録音、Geminiで音声検証
- 対面会議の録音時に話者識別に活用
- メンバー情報と紐づけて管理

### コードレビュー（移行中）
- 現行はpush WebhookをトリガーにGeminiがコードレビュー
- 今後はPull Request用の再利用可能なGitHub Actions workflowへ移行
- コード差分 + リポジトリ全体のコンテキストを参照
- 重大度別の指摘事項（Critical / Major / Minor / Trivial）
- Discordチャンネルに自動投稿

### レポート
- `/report weekly` - 週次レポート生成
- `/report monthly` - 月間報告書生成
- Google Docsにリッチフォーマットで保存

### スマートリアクション
- Geminiが会話を感情判定（interesting / surprised / funny）
- 10個中2〜3個にリアクションがつく程度の感度
- サーバー独自の絵文字にも対応

### 名言Bot
- `/meigen` - 過去の会話から名言をピックアップ
- 特定ユーザーの名言も検索可能

### 語録辞書
- `/glossary bulk` で一括登録（CSV形式 / →形式）
- ひらがな読み・別名に対応
- RAG回答・音声文字起こし・議事録の精度向上に活用

### Google Drive連携
- Google Apps Script経由でGoogleドキュメントを作成
- 議事録・レポートをリッチフォーマットで保存
- Noto Sans JP、見出し色分け、コードブロック対応

## コマンド一覧

| コマンド | 説明 |
|---------|------|
| `/setup` | 初期設定ウィザード（1メッセージ完結型） |
| `/reset` | 設定リセット（管理者のみ） |
| `/status` | 現在の設定状況を表示 |
| `/backfill [days] [channel]` | 過去ログを取り込み |
| `/ignore` | 現在のチャンネルを学習対象から除外 |
| `/join <mode>` | VCに参加（listen/meeting/chat） |
| `/leave` | VCから退出（議事録生成） |
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
| `/glossary list` | 語録一覧 |
| `/glossary delete <term>` | 語録から削除 |
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
│   ├── reports.py       # 週次・月次レポート生成
│   ├── gdrive.py        # Google Drive連携（Apps Script経由）
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
    ├── routes.py        # /health, /status, /ask, /backfill
    └── github_webhook.py# GitHub Webhook + コードレビュー
```

## 技術スタック

- **Discord**: py-cord 2.7+ (DAVE voice receive patch適用)
- **AI**: Google Gemini（用途別モデルへの移行中）
- **TTS**: edge-tts（ja-JP-NanamiNeural）
- **API**: FastAPI + uvicorn（同一asyncioループ）
- **Drive**: Google Apps Script経由
- **インフラ**: Docker Compose + GCE

## セットアップ

### 1. 環境変数

`.env` ファイルを作成：

```
GOOGLE_API_KEY=your_gemini_api_key
DISCORD_TOKEN=your_discord_bot_token
GITHUB_WEBHOOK_SECRET=your_webhook_secret
YAGAPON_API_TOKEN=generate_a_long_random_token
API_HOST=http://your-server-ip:8000
API_PORT=8000
GOOGLE_APPS_SCRIPT_URL=https://script.google.com/macros/s/xxx/exec
YAGAPON_CONFIG_PATH=/data/server_config.json
YAGAPON_VOICEPRINT_DIR=/data/voiceprints
```

`YAGAPON_API_TOKEN` は `/status`、`/ask`、`/backfill` APIのBearer認証に使う。`GITHUB_WEBHOOK_SECRET` と別の値を設定する。

### 2. Dockerで起動

```bash
mkdir -p data
docker compose up --build
```

コンテナにはffmpeg、libopus、音声受信用Pycordの固定コミットが含まれる。設定と音声サンプルは`data/`に永続化される。

バックグラウンド起動：

```bash
docker compose up -d
docker compose logs -f yagapon
```

GCEではsystemdからComposeを管理し、再起動後も自動復旧させる。詳しくは[デプロイ手順](deploy/README.md)を参照。

### 3. Discord設定

1. Discordサーバーで `/setup` を実行
2. ウィザードに従って設定を進める

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

Google共有ドライブからKnowledge用draftを作る手順は[Drive同期設計](docs/google-drive-knowledge-sync.md)を参照。

## マルチギルド対応

複数の局（サーバー）で同時利用可能。各サーバーごとに独立したコーパス・設定・メンバー管理。

## ライセンス

Private - 矢上祭実行委員会 IT局
