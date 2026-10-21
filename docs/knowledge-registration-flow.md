# Discord・資料のKnowledge登録フロー

## 目的

通常のDiscord会話は検索用の生コーパスであり、その時点の提案、雑談、誤りも含む。
Google Driveの原本も、更新されたという理由だけで現在の正式運用にはできない。
そこで、残す価値のある出典を明示的に申請し、人の確認を経てKnowledgeへ昇格する。

```text
DiscordメッセージURL ─┐
                      ├─ draft + manifest ─ 機密検知 ─ Draft PR ─ 人のレビュー ─ merge
Google Drive資料URL ──┘          │                 │
                                 └─ 永続outbox      └─ 検出時は送信停止

merge後の文書 ─ status確認 ─ Knowledge差分索引 ─ RAGの整理済み根拠
```

## Discordから登録する

サーバー管理者が、登録の中心となるメッセージの「メッセージリンクをコピー」を使う。

```text
/knowledge discord message_url:<URL> title:<任意> context_messages:21
```

- URLと同じGuildだけを許可する。
- 登録可能Guildが未設定の場合はコマンドを拒否し、暗黙に全Guildを許可しない。
- 実行者が閲覧・履歴閲覧でき、やがぽんも読めるチャンネルだけを取得する。
- `/ignore`対象のチャンネルと、その配下スレッドは登録しない。
- `YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS`または`YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS`
  に一致する選考等の機微チャンネルは登録しない（既定の語は`選考`）。
- 選択メッセージと前後を最大50件まで出典リンク、日時、発言者ID付きで保存する。
- Discord添付URLは期限付きクエリを除いて記録する。ファイル本体は自動転載しない。
- 発言は`draft`であり、最終決定や現在の運用として自動採用しない。

取得範囲に必要な前提がない場合は、より適切な中心メッセージを選ぶか、PRで出典を追加する。
非公開チャンネルの内容を申請する前に、Knowledgeリポジトリを閲覧できる人の範囲を確認する。

## Google Driveから登録する

```text
/knowledge drive drive_url:<Google Docs・Sheets・Slides・DriveファイルURL>
```

- 明示された1ファイルだけを読み取る。フォルダ全体はこのコマンドの対象外。
- Google Docs、Sheets、Slides、テキスト、Markdown、CSV、JSONをテキスト化できる。
- AIは要約、事実候補、判断候補、アクション候補、未確認事項、整理先候補を作る。
- 資料内の命令文はデータとして扱い、AIやBotへの指示として実行しない。
- APIキー、GitHub token、秘密鍵らしき値はモデル入力前に除外し、除外件数を記録する。
- 原本本文をGitへそのまま複製せず、Drive URL、file ID、更新日時、SHA-256を残す。

BotのGoogle資格情報に対象ファイルの閲覧権限が必要である。コマンドは`drive.readonly`を優先する。
現在の資格情報がこのscopeを持たない場合だけ`drive.file + documents`へ限定フォールバックする。
現在の`drive.file` OAuthでは、
やがぽんが作成した資料やアプリへ明示的に許可された資料を読める。任意の既存資料・共有ドライブを
対象にする場合はDrive read-only scopeで再認証する。サービスアカウントを使う場合は対象資料または
共有ドライブへの明示共有を使う。
個人の広いOAuth tokenをGitHubやDiscordへ貼らない。

## 配信状態と再送

`/knowledge status`で直近10件を確認する。

| 状態 | 意味 | 対応 |
|---|---|---|
| `pending` | outbox保存済み、GitHub資格情報なし | サービス資格情報を設定して再送 |
| `published` | Draft PR作成済み | PRを人が確認 |
| `failed` | GitHub API等で送信失敗 | 原因修正後に再送 |
| `blocked-sensitive-content` | 認証情報らしき内容を検出 | outbox上で管理者が確認・除去。コマンドからの再送は禁止 |

再送は次のコマンドで、保存済みファイルを再生成せず使用する。

```text
/knowledge retry bundle_id:<statusに表示されたID>
```

申請IDは内容ハッシュを含む。同じ原本でも内容が変われば別申請となり、過去PRを黙って上書きしない。

## Draft PRで確認すること

1. 原本リンク、取得範囲、日時、年度が正しい。
2. 非公開情報、個人情報、認証情報をKnowledgeへ保管してよい。
3. 提案、決定、実装、本番反映、後日の振り返りが混ざっていない。
4. 既存の正本がある場合は重複を作らず、追記または参照にする。
5. `sources/`の一次資料から、必要に応じて`years/`、`knowledge/`、`roles/`、`checklists/`へ整理する。
6. 内容を確認できる関係者がレビューし、DraftをReadyにしてからマージする。

Knowledge用AIレビューは真偽を決めず、front matter、分類、状態、年度、出典、manifest、
機密情報など構造を確認する。内容の承認は人が行う。

## 本番設定

Draft PRを作るサービス資格情報には、対象Knowledgeリポジトリだけの
`Contents: Read and write`と`Pull requests: Read and write`を付ける。

```dotenv
YAGAPON_KNOWLEDGE_REPOSITORY=YagamiFes-IT/YagamiFes-IT-Knowledge
YAGAPON_KNOWLEDGE_BASE_BRANCH=main
YAGAPON_KNOWLEDGE_GITHUB_TOKEN=<fine-grained service credential>
YAGAPON_KNOWLEDGE_OUTBOX=/data/knowledge-outbox
YAGAPON_KNOWLEDGE_GUILD_ID=<IT Division Guild ID>
YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS=<登録を許可するGuild ID。複数はカンマ区切り>
YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS=<選考等の除外チャンネルID>
YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS=選考
```

個人用の`gh`認証を本番VMへコピーしない。Secretは`/etc/yagapon/yagapon.env`または
Secret Managerに置き、リポジトリ、outbox、ログ、管理画面へ保存しない。
