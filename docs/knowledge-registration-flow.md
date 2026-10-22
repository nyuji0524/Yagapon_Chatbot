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

会議では`/join`に`drive_set`配下のGoogle Docs URLを渡せる。終了時に資料、Discord音声の生文字起こし、
要約、議事録を同じ会議bundleへ保存し、資料にあった予定と会議中に確定した内容を区別する。

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
| `pending` | outbox保存済み、配信先未設定 | 配信先を設定して再送 |
| `queued` | 非公開intakeへ送信済み、Draft PR作成待ち | 通常は待機。滞留時はActionsとGCSを確認 |
| `published` | Draft PR作成済み | PRを人が確認 |
| `failed` | GitHub API等で送信失敗 | 原因修正後に再送 |
| `blocked-sensitive-content` | 認証情報らしき内容を検出 | outbox上で管理者が確認・除去。コマンドからの再送は禁止 |

再送は次のコマンドで、保存済みファイルを再生成せず使用する。

```text
/knowledge retry bundle_id:<statusに表示されたID>
```

申請IDは内容ハッシュを含む。同じ原本でも内容が変われば別申請となり、過去PRを黙って上書きしない。

## 定期更新と即時更新

本番では、固定Guild allowlist内でBotが閲覧できるDiscord原文を動的に列挙して差分取得し、その完了checkpointを
確認してから議題別Knowledge整理を行う。どちらも6時間周期とし、失敗またはcheckpoint未完了時は
15分以内の短周期pollへ切り替える。配送再送はSQLiteの`next_attempt_at`（決定的jitterを含む）まで
短周期pollを続ける。自動整理が生成するものは確認待ちDraft PRであり、自動マージしない。

サーバー管理者は、同じ経路をDiscordから即時実行できる。

```text
/knowledge refresh mode:<Discord差分取得 → Knowledge整理（推奨）>
/knowledge refresh mode:<全期間を年度別に再構築 → Knowledge整理>
/knowledge refresh mode:<Knowledge整理のみ>
```

- `full`相当の推奨モードは、Discord差分取得が全件完了した場合だけ整理へ進む。
- 全期間モードは最古メッセージから現在までを、10月1日〜翌9月30日の矢上祭年度ごとに分割して
  再構築する。結果Embedにも`26th`、`27th`、`28th`のように年度別件数を表示する。
- 整理のみの場合も、最新の正常なbackfill checkpointがなければfail closedで保留する。
- 定期処理または別の手動処理と重なった場合は、同じプロセス内のasync lockとプロセス間file lockで
  二重実行を防ぐ。
- コマンドは即時に受付を返し、長時間処理の結果を実行チャンネルへEmbedで通知する。
- 対象Guildはコマンド引数で変更できず、環境変数のimmutable Guild ID allowlistだけを使う。
  Channel設定が空なら、Botが履歴を読めるTextChannelとpublicなactive/archived threadを動的に列挙する。
  `/ignore`、`YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS`、deny名称は常に除外する。private threadは自動列挙しない。
- `YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS`を設定した場合は従来どおり狭い対象へ限定できる。明示した
  Forum親はpublic投稿へ展開し、private threadはそのthread ID自体を明示した場合だけ扱う。
- Raw索引とGitHub Knowledgeへの外部化は別境界である。全readable履歴は`access_scope=channel`のまま
  RAGへ登録する。リアルタイム受信は、その時点でguild全員が読めると検証できた場合だけ自動Draft候補にする。
  定期・差分backfill由来の過去ログは、投稿当時のACLをDiscordから証明できないため`auto_promote=false`
  のままにする。管理者が全期間モードを明示実行した場合だけ、実行元Guildの現在ACLを再評価し、全員が
  読める履歴へ`auto_promote=true`を付ける。同時に再構築される他の設定済みGuildへこの同意は波及しない。
  `YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS`は、役職限定チャンネルも対象にできる意図的な固定overrideである。
  値は正のDiscord channel IDだけを許可し、不正値や正しいIDとの混在は空設定へ読み替えず整理全体を保留する。

### 旧outboxを移行する

現行の再送情報を持たない旧bundleは、まず書き込みを行わない診断を実行する。

```bash
python -m bot.knowledge_outbox_migrate --bundle-id <旧bundle ID>
```

診断は文字起こしのSHA-256、宣言済み成果物、機密パターン、移行先の衝突を確認する。
`eligible=true`を確認し、元ディレクトリを別媒体へ退避した後だけ`--apply`を付ける。

```bash
python -m bot.knowledge_outbox_migrate --bundle-id <旧bundle ID> --apply
```

`--apply`もGCS送信やPR作成は行わない。元bundleを変更・削除せず、現行再送形式の新bundleを
原子的に作るだけである。新bundleを再度確認してから`/knowledge retry`で明示的に送信し、
`processed/`とDraft PR、ファイルハッシュが一致するまで旧bundleを保持する。

## Draft PRで確認すること

1. 原本リンク、取得範囲、日時、年度が正しい。
2. 非公開情報、個人情報、認証情報をKnowledgeへ保管してよい。
3. 提案、決定、実装、本番反映、後日の振り返りが混ざっていない。
4. 既存の正本がある場合は重複を作らず、追記または参照にする。
5. `sources/`の一次資料から、必要に応じて`years/`、`knowledge/`、`roles/`、`checklists/`へ整理する。
6. 内容を確認できる関係者がレビューし、DraftをReadyにしてからマージする。

Knowledge用AIレビューは真偽を決めず、front matter、分類、状態、年度、出典、manifest、
機密情報など構造を確認する。内容の承認は人が行う。

## Discordログの継続整理

`knowledge-curation-watch`はFile Searchへ登録済みのDiscord会話・会議文字起こしを少数ずつ読み、
継続して更新する価値があるプロジェクト、機能、運用、課題、方針を議題単位にまとめる。主資料の固有語で
同年度のDiscord、デプロイ済みソースコード、承認済みKnowledgeを別枠で検索し、Situation、Environment、
現在状態、前回差分、コードマップ、時系列、Decision、問題・原因・対応・結果、当時の理由、後日の振り返り、
推測、矛盾、未確認事項を分離する。各主張は`D01`（Discord）、`C01`（Code）、`K01`（Knowledge）のような
証拠IDを持ち、草案末尾のdocument_key・原本リンクと照合できる。

議題IDは`festival + bureau + 正規化したtopic`からコード側で決定し、モデルが年度、局、保存先を自由に
指定することはできない。別名はSQLite台帳で同じ議題へ解決する。証拠のdocument_key、本文SHA-256、
公開範囲からfingerprintを作り、証拠が変わった場合だけdirtyにする。承認済みKnowledgeへ同じtopic_idが
反映されるまでは同一議題の次のDraftを出さず、未処理Draftの増殖を防ぐ。

保存先は`years/{festival}th/bureaus/{bureau}/decisions/candidates/`で、IT、総務、装飾、ステージ、
屋外、室内、広報、渉外の8局を同じように処理する。複数局名を含むチャンネルは`cross-bureau`、
全体会議等は`committee`、判定不能は`unclassified`へ置く。分類はDiscordカテゴリ・チャンネル名で行い、
AIが自由に選んだ局名を権限や保存先の根拠にしない。どの候補もDraft PR止まりで自動マージしない。
証拠の公開範囲は積集合で判定する。同じ1チャンネルとguild資料の組み合わせはchannel扱い、異なる
非公開チャンネル、`restricted`、公開範囲不明の資料が1件でも混じればfail closedで送信しない。

処理済み文書、別名、最新fingerprint、配送状態、再試行時刻は
`YAGAPON_KNOWLEDGE_CURATION_STATE`のSQLiteへ保存する。旧JSON台帳を同じパスで検出した場合は
`.legacy.json`へ退避して処理済み文書を移行する。`pending`と`failed`は完了扱いにせず、決定的jitter付きの
指数backoffで既存outbox bundleを再送する。1回のモデル呼び出し数、生成数、未処理bundle数には上限を設ける。

## 本番設定

Botは専用GCS intake bucketへのObject Creatorだけを持つ。Knowledgeリポジトリの
GitHub ActionsはOIDC/WIFで同bucketを読み、リポジトリ自身の`GITHUB_TOKEN`でDraft PRを作る。

```dotenv
YAGAPON_KNOWLEDGE_REPOSITORY=YagamiFes-IT/YagamiFes-IT-Knowledge
YAGAPON_KNOWLEDGE_BASE_BRANCH=main
YAGAPON_KNOWLEDGE_GCS_BUCKET=yagamiatlas-yagapon-knowledge-intake
YAGAPON_KNOWLEDGE_SNAPSHOT_BUCKET=yagamiatlas-yagapon-knowledge-approved
YAGAPON_KNOWLEDGE_OUTBOX=/data/knowledge-outbox
YAGAPON_KNOWLEDGE_GUILD_ID=<IT Division Guild ID>
YAGAPON_KNOWLEDGE_ALLOWED_GUILD_IDS=<登録を許可するGuild ID。複数はカンマ区切り>
YAGAPON_KNOWLEDGE_DENY_CHANNEL_IDS=<選考等の除外チャンネルID>
YAGAPON_KNOWLEDGE_DENY_CHANNEL_TERMS=選考
YAGAPON_KNOWLEDGE_CURATION_ENABLED=false
YAGAPON_KNOWLEDGE_CURATION_BATCH=3
YAGAPON_KNOWLEDGE_CURATION_STATE=/data/knowledge-curation.sqlite3
YAGAPON_KNOWLEDGE_PIPELINE_LOCK=/data/knowledge-pipeline.lock
YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS=
YAGAPON_KNOWLEDGE_CURATION_MODEL_CALLS_PER_RUN=6
YAGAPON_KNOWLEDGE_CURATION_GENERATIONS_PER_RUN=3
YAGAPON_KNOWLEDGE_CURATION_MAX_OPEN_BUNDLES=5
YAGAPON_KNOWLEDGE_CURATION_INTERVAL_SECONDS=21600
YAGAPON_KNOWLEDGE_CURATION_RETRY_SECONDS=900
YAGAPON_KNOWLEDGE_BACKFILL_STATE=/data/scheduled-backfill-state.json
YAGAPON_KNOWLEDGE_BACKFILL_MAX_AGE_SECONDS=28800
YAGAPON_SCHEDULED_BACKFILL_ENABLED=false
YAGAPON_SCHEDULED_BACKFILL_GUILD_IDS=<差分取得を許可するGuild ID>
YAGAPON_SCHEDULED_BACKFILL_CHANNEL_IDS=<任意の限定Channel/Forum親ID。空なら動的全対象>
YAGAPON_SCHEDULED_BACKFILL_STATE=/data/scheduled-backfill-state.json
YAGAPON_SCHEDULED_BACKFILL_INTERVAL_SECONDS=21600
YAGAPON_SCHEDULED_BACKFILL_RETRY_SECONDS=900
YAGAPON_SCHEDULED_BACKFILL_INITIAL_DELAY_SECONDS=300
```

schedulerを使わない単独curatorだけは`YAGAPON_KNOWLEDGE_BACKFILL_STATE`未設定で独立実行できる。
scheduler有効時と権限者による全期間取得時は同設定が必須であり、解決後のパスがproducer側の
`YAGAPON_SCHEDULED_BACKFILL_STATE`と一致しなければ、全期間取得も整理も開始せずfail closedとする。
一致する設定では、
指定時刻以内に完了したschema v2のcheckpointについて、対象guildがすべて`status=completed`、
`failures=[]`、`channels_completed == channels_total`の場合だけ整理を開始する。ファイル欠落、古い時刻、
`failed`、`deferred`、`interrupted`、一部チャンネル失敗はいずれもfail closedとなり、古いコーパスから
新しい草案を生成しない。workerへscheduler設定が渡されている場合はschedulerが有効で、かつ
checkpoint内のGuild inventoryと、動的に検出したeligible/completed inventoryが整合することも必須とする。
schedulerは取得開始時のbot-readableな`eligible_channel_ids`とは別に、全Guildの処理完了直前にACLを
再評価した`current_curation_eligible_channel_ids`（botが現在読め、ignore/deny/protectedでない実対象）と
`current_public_curation_channel_ids`（そのうちguild全員へ公開中の実対象）を保存する。整理側は固定overrideが
空の場合、保存済み`auto_promote=true`と最新current-public inventoryの両方を要求する。公開チャンネルを
役職限定へ変更すると、以前の資料に`auto_promote=true`が残っていても次の正常checkpointから候補資格を失う。
固定overrideは公開・`auto_promote`条件だけを意図的に上書きし、最新current-eligible inventoryには必ず
含まれなければならない。したがって役職限定は許可できるが、後からignore/protected/閲覧不能になった対象は
送信しない。
キャッシュ外のpublic archived threadは完了直前に現在の親から再列挙し、削除済みthreadや親が解決できない
threadは古い取得時objectへfallbackせず除外する。整理workerは再送時にもbundleの全出典を現行RagStoreから
再読込し、digest一致とcurrent-public資格を
再確認する。欠落・変更・権限失効があれば外部送信せず`blocked-access`として保留する。
初回送信でも、モデル生成後かつbundle作成直前に全出典を現行RagStoreへ再bindし、本文hash、trusted metadata、
最新ACLを再確認する。長文のvirtual segmentは元documentを再読込して同じ区間を再構成し、元documentの
key/versionを再送lineageへ保存する。
新規bundleは全出典lineageのschema・完全性・件数をSQLiteへ保存する。upgrade前のbundleやlineage metadataが
欠けたbundleは、主資料だけの記録を完全な出典一覧とみなさず、再送せず`retry_blocked`として保留する。
schedulerの無効化や
設定変更前の成功checkpointは再利用しない。

個人用の`gh`認証やPATを本番VMへコピーしない。キューobjectは機密検査済みの候補だけを含み、
パイプライン処理後に`processed/`へ移動する。Draft PRは人が確認するまでマージしない。
KnowledgeリポジトリのOrganization/Repository Actions設定では、Workflow permissionsをRead and writeにし、
`Allow GitHub Actions to create and approve pull requests`を有効にする。これを確認するまでは
`YAGAPON_KNOWLEDGE_CURATION_ENABLED=false`のままにする。

Discord会話は検索上 `access_scope=channel` を維持する。`YAGAPON_KNOWLEDGE_CURATION_CHANNEL_IDS`が
空の場合、取り込み時にguild全員が読めることをDiscord権限から検証して`auto_promote=true`を付け、かつ
最新checkpointでも全員に公開中と確認できた資料だけが非公開Knowledgeリポジトリの確認待ち草案になる。
IDを設定した場合はその明示subsetだけに限定し、current-public判定と`auto_promote`を意図的にoverrideするため、
役職限定チャンネルも対象にできる。
deny対象と`restricted`資料は、どちらの場合も自動送信しない。

定期・差分バックフィルは、過去の投稿時点のACLをDiscordから復元できないため、履歴を自動では
`auto_promote=true`にしない。管理者が`/knowledge refresh historical`を明示実行した場合に限り、
実行元Guildの現在Discord ACLを再評価し、`@everyone`が閲覧と履歴参照の両方を持つ非保護チャンネルの
履歴だけを候補化する。明示overrideを除く役職限定の通常チャンネルは検索コーパスだけに残り、private
threadはthread ID自体をbackfill設定へ明示した場合だけ検索対象になる。`/ignore`とdeny/protected対象は
検索取得からも除外する。これらをGCSやGitHubへ自動送信しない。

承認後にKnowledge PRがmainへ入ると、`source-index-watch`がcommit SHAの変更を検出する。
front matterのstatusが`active`または`historical`の文書だけを
File Searchへ差分反映する。`draft`と`needs-review`はマージされても索引対象外である。
