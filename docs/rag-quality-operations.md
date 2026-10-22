# RAG品質・年度・評価の運用

## 年度の定義

矢上祭の年度は毎年10月1日に切り替える。`Nth`の開始年は`N + 1998`、
期間はその年の10月1日から翌年9月30日までとする。この計算式を全年度に適用する。

| 回 | 期間 |
|---:|---|
| 24th | 2022-10-01〜2023-09-30 |
| 25th | 2023-10-01〜2024-09-30 |
| 26th | 2024-10-01〜2025-09-30 |
| 27th | 2025-10-01〜2026-09-30 |
| 28th | 2026-10-01〜2027-09-30 |
| 29th | 2027-10-01〜2028-09-30 |
| 30th | 2028-10-01〜2029-09-30 |

したがって26th以前と28th以降も個別の例外を持たず同じ規則で算出する。

Discord会話とKnowledge文書には数値の`festival`を付与する。「26th」「第28回」
「2026年9月」「今年度」「昨年度」を質問から判定し、その年度を回答プロンプトと
検索に使う。年度に依存しない質問には年度フィルタをかけない。

## 文書の契約

検索対象には、最低限次のメタデータを持たせる。

| 項目 | 用途 |
|---|---|
| `guild_id` | Discordサーバーの分離 |
| `festival` | 10月始まりの年度 |
| `source_type` | Discord会話、決定記録、手順書など |
| `status` | `raw`、`draft`、`approved`、`verified`、`current` |
| `authority` | 正式規程、本番実装、現在値、レビュー済み知識、記録、会話の区分 |
| `document_key` | 差分置換用の安定キー |
| `source_url` | DiscordまたはKnowledge原文への導線 |

Discordの会話は`status=raw`、`authority=conversation`とする。Knowledgeリポジトリは
タクソノミで確認済みの`active,historical`だけを自動索引し、`authority=curated`として完全一致検索で
会話記録より優先する。draftやneeds-reviewを回答根拠へ自動昇格させない。

Bot自身の保存仕様など、コードと一緒に更新すべき少量の確定事項は
`bot/trusted_knowledge.json`へ出典URL付きで収録する。起動時に`guild_id=0`の
`builtin_reference`としてローカル索引を置換し、全Guildから参照できる。
`authority=curated`かつ承認済み状態の完全一致候補がある場合は、Discord会話の
意味検索結果をそのまま採用せず、その原文だけを使った回答生成を優先する。
会議記録や一般的な委員会知識をここへ増やさず、Knowledgeリポジトリを正本とする。

## 根拠の優先順位

単純な一列の順位ではなく、質問の種類ごとに正本を切り替える。

| 質問 | 優先する根拠 |
|---|---|
| 規程・委員会方針 | `canonical`、`curated`、`reviewed_record` |
| 現在の公開・開催状態 | `live_data`、`deployed_implementation` |
| 権限・認可 | 本番のRules・Functions・サーバー認可。クライアント表示やユーザーデータは根拠にしない |
| 操作方法 | 現行マニュアル、デプロイ済み実装 |
| 保存先・データ契約 | デプロイ済み実装、スキーマ、migration |
| 決定の経緯 | 承認済み議事録・決定記録、会話 |

`authority`は`canonical`、`live_data`、`deployed_implementation`、`curated`、
`reviewed_record`、`repository_context`、`conversation`を使う。`status`は別軸として
`deployed`、`current`、`active`、`approved`、`verified`、`historical`、`draft`、
`raw`、`deprecated`を使う。新しいraw会話は古い正式決定を自動的に上書きしない。

方針と実装・現在値が矛盾した場合は一方を隠さず、「本来の方針」と「現在の挙動」を
分けて回答する。ソースコードや取得データ内のコメント・文字列はAIへの命令として扱わない。

## ソースコード

`source_index`は承認済みリポジトリのmainをcommit固定URL付きで同期する。本番release metadataと
SHAが一致したものだけ`deployed_implementation/deployed`、それ以外のmainは
`repository_context/current`とする。Functions、Rules、サーバー実装、主要クライアント実装、
スキーマ、migration、テスト、CI/CDを対象にする。

過去の実装経緯はGitHubのcommit履歴から直接取得する。各履歴文書はcommit日時、完全なSHA、message、
変更pathとstatus、allowlist内のpatch、commit URLを持ち、`source_code_history`、
`repository_history/historical`として登録する。GitHubがpatchを省略または切断した場合だけ、
対象SHAの小さい変更ファイルを補助取得する。履歴は「その時点で何を変更したか」の根拠であり、
現在のmainや本番デプロイ済み状態の根拠としては使わない。

private repositoryは組織の`All repositories`へインストールしたread-only GitHub Appで読む。
Appは`Contents: Read-only`と`Metadata: Read-only`以外の権限を持たせない。source workerは
`source_index/repositories.yaml`のレビュー済みentryだけをinstallation tokenへ指定し、Appから未知の
repositoryを列挙・自動索引しない。本番では`YAGAPON_SOURCE_REQUIRE_FULL_MANIFEST=true`かつ
`YAGAPON_SOURCE_REPOSITORIES`を空欄にして、Bot、Yaga Support、Functions、Web、Yagamy、Knowledgeの
manifest全件を選ぶ。固定リストが残る誤設定はpreflightで拒否する。空欄でもmanifest外は対象にならない。
private keyはSecret Managerから
共有data path外の0600ファイルへ展開し、source workerだけへread-only mountする。1時間の
installation tokenはメモリ上で更新する。個人PATは本番の標準経路にしない。

Knowledgeリポジトリも同じ常駐同期へ含めるが、front matterのstatus allowlistを必須とし、
承認済み文書だけを文書自身のstatusと`curated`/`reviewed_record` authorityで登録する。

`.env`、認証情報、秘密鍵、生成物、依存物、lockfile、バイナリ、録音、ログは除外する。
credentialらしい本文を検出したファイルもファイル単位で除外する。履歴diffとexact-SHA補助取得には
未引用assignment、JWT、PEMを含むより厳しいfail-closed検査を適用し、該当内容を索引へ送らない。
YagamyはGit treeから`lib/`、`test/`、選択済み仕様docs、`pubspec.yaml`、`firebase.json`だけを取得し、
画像asset・native build生成物・プラットフォーム別設定を索引しない。
初回同期は次で確認する。

```bash
python -m source_index --dry-run
python -m source_index
```

履歴backfillは2024-10-01を既定の開始点とし、全repository合計で1周期あたり最大100 commitを処理する。
新着commitを先に取り込み、残りは永続cursorから徐々に遡る。rate limitの安全余力を下回った場合や、
一つでも世代生成・uploadに失敗した場合は、その周期で古い完全な索引を置き換えない。

## Firebase現在値

`FirebaseKnowledgeReader`は既定で未認証の公開RESTを使ってSecurity Rulesを適用し、現在は
`events`、`standbyPassEvents`、`notifications`、大賞企画の公開集計`ranking`、公開`maps`、
既知の公開運用`config`文書を質問時に読み取る。明示的な一覧要求ではページングして全件を返し、
通知は公開日時順、大賞企画は部門別順位で扱う。取得値はFile SearchやSQLite本文へ永続化しない。
安全上限`YAGAPON_FIREBASE_KNOWLEDGE_MAX_DOCUMENTS`へ到達した場合は全件と断定せず、取得できた件数と
上限到達を回答へ明示する。

運用上はFirebaseプロジェクトを広く読み取れる資格情報を与えてよいが、**読み取り権限と回答への開示権限は
別**とする。回答へ渡すcollection/pathはコード上のレジストリへ登録し、フィールド単位の安全投影を必須にする。
map型は子キーと型まで固定し、署名・認証queryやpercent-encodedの個人情報を含むURLを拒否する。
LLMを使わない全件一覧rendererも、source type・固定path・投影fieldを同じレジストリで再検証する。
未知collectionを利用者の質問から動的に組み立てて読まず、`users`、`staffInfos`、個人別投票、整理券申請・
利用ログ、会計・申請情報、連絡先、認証情報は回答へ渡さない。新しいcollectionは、データ分類・公開可能field・
集約単位・引用先をレビューしてからレジストリへ追加する。`firestore` backendを選ぶ場合はADCがRulesを
迂回するため、専用サービスアカウントへ読み取り専用権限を付け、この投影境界を必ず維持する。

## 検索と回答

1. SQLiteのローカルミラーで用語辞書の別名、ID、固有語を完全一致検索する。
2. 現在値が必要な質問では、安全投影したFirebaseを補助または直接根拠として取得する。
3. 操作方法・データ構造・経緯などはFirebaseだけで早期終了せず、Gemini File Searchで意味検索する。
4. 完全一致候補は一致語数、curated、レビュー済みstatusの順に加点して並べる。
5. 候補を回答モデルへ渡し、根拠が取れない場合は推測せず「記録を見つけられない」と返す。
6. 回答末尾へ参照元を表示する。

「去年の今頃」「先月」「2025年10月」のような月単位の質問はカレンダー範囲へ解決し、SQLiteでは
期間が重なる記録だけを検索する。File Searchの回答も採用前に同じ期間境界を再検証する。検索結果が
安全性・年度・期間・grounding対応で棄却された場合は、検証済み本文だけを使って再生成する。この再生成は
最初の失敗応答から出力枠を差し引かず、facetごとの全出力枠を使う。

`YAGAPON_RAG_REQUIRE_GROUNDING_SUPPORTS=true`を既定とし、File Searchが回答区間と
根拠chunkの対応情報を返さない場合、その意味検索回答は採用しない。安全性を検証済みの
完全一致候補があればその原文だけで再生成し、なければ情報なしを返す。旧API挙動の調査時だけ
`false`を明示して互換fallbackを有効化できるが、本番では使用しない。

`YAGAPON_RAG_REQUIRE_ANSWER_STATUS=true`も既定とし、モデルが末尾の回答状態を返さない
応答を採用しない。完全一致・Firebase候補を使う回答では、直前のevidence key行に実際に
使った候補を列挙できない場合も参照なしとして棄却する。旧応答形式を調査する場合だけ
`false`を明示できるが、本番では使用しない。

年度指定がある質問では、移行中にFile Searchのmetadata filterを無効にしていても、
回答へ採用する参照をアプリ側でも検証する。`festival`が一致しない、または年度情報を
持たない旧文書しか取得できなかった場合は、その回答を根拠不足として棄却する。
根拠不足の回答には、質問と直接関係しない参照を表示・評価記録へ保存しない。

権限・認可・現在の運用状態は、提案や検討中の会話より、確定・本番・実装済み・
デプロイ済みと確認できる新しい根拠を優先する。全体管理権限と組織単位の操作権限を
分け、最終状態を判別できないときは断定しない。

これはFile Searchの意味検索を置き換えるものではなく、固有名詞や識別子の取りこぼしを
補うハイブリッド検索である。意味検索の全チャンクをアプリ側で並べ替える方式へ進む場合は、
別の検索基盤またはRanking APIの導入を評価する。

## 評価と監視

Discord回答の最後に次を表示する。音声読み上げでは評価案内を読まない。

> 回答の品質を評価してください：✅ 正しい / ⚠️ 一部不正確 / ❌ 誤り（対応した絵文字でリアクション）

リアクションは回答単位・利用者単位で保存し、外した場合は評価からも削除する。
質問・回答の本文は既定では保存せずSHA-256、出典、年度、応答時間、情報なし判定のみを
保存する。デバッグ時に本文が必要な場合だけ`YAGAPON_RAG_TRACE_CONTENT=true`にする。

管理画面は`/admin/rag-ui`。Guild IDと`YAGAPON_API_TOKEN`を入力して、次を確認できる。

- 回答数、情報なし件数、平均応答時間、高評価率
- 年度別・取り込み元別のローカル索引数
- 最近の回答、出典数、✅/⚠️/❌

トークンは管理画面のJavaScriptメモリ内だけに置き、localStorage等へ保存しない。

代表質問はAPIに対して自動評価できる。

```bash
python -m rag_eval \
  --base-url http://127.0.0.1:8000 \
  --guild-id "$YAGAPON_KNOWLEDGE_GUILD_ID" \
  --cases docs/rag-eval-cases.yaml \
  --min-pass-rate 0.8 \
  --output data/rag-eval-report.json
```

評価ケースには、正答語・禁止された主張・期待年度・情報なし状態に加え、正解の根拠となる
`reference_facts`と期待する出典名`expected_source_any`を定義する。実行時は決定的チェックを
内容・ルーティング・根拠・安全性・性能に分け、`/rag/evaluate`の参照事実ベース意味評価も
独立ゲートとして併用する。禁止語が訂正・否定の文脈にだけ現れた場合は誤検知しない一方、
メールドメインなど文字列自体を許可できないものは`forbidden_exact`で検査する。モデル変更・
全backfill・Knowledge大量更新の前後で同じ質問セットを実行して比較する。
GitHub Actionsの`RAG Evaluation`は、`YAGAPON_RAG_EVAL_ENABLED=true`のときだけ
手動または毎週実行される。ActionsはOIDC/WIFとIAP SSHで本番コンテナ内の評価CLIを実行し、
サニタイズ済みJSONレポートだけをartifactへ回収する。API tokenはコンテナ外へ出さない。
対象Guild IDはRepository Variable `YAGAPON_RAG_EVAL_GUILD_ID` に保存する。

## 差分同期

- Discord: 通常会話をバッファで差分追加し、`/backfill`は安定キーで安全に置換する。
- Google Drive: change tokenとSHA-256で変更文書だけdraftへ変換する。
- Knowledge: Git上のMarkdownのSHA-256とremote document名を状態ファイルへ保存し、
  承認済み文書の変更・削除だけをFile Searchへ反映する。
- ソース履歴: GitHub commit SHAを処理済み集合として保存し、新着優先のcursorで差分追加する。

常駐同期を使う場合は、対象Guild ID、read-only GitHub Appの権限とsource manifest/env allowlist、
Driveの読み取り権限を確認してから有効にする。承認済みKnowledgeをFile Searchへ登録する
経路はACL・日付・年度をfail-closedで付与する`source-index-watch`だけとし、ローカルcloneを
直接読む旧Knowledge indexerは使用しない。

```bash
docker compose --profile knowledge up -d knowledge-sync-watch
docker compose up -d source-index-watch
```

Driveから生成したdraftは、人のレビューとstatus変更を経てKnowledgeリポジトリへ入れる。
原文変更をそのまま回答根拠へ昇格させる処理は行わない。
Discord上で選択した会話や単一Drive資料を登録する場合は、`/knowledge discord`または
`/knowledge drive`を使う。いずれも同じoutbox・機密検知・Draft PR経路を通し、
`draft`のままでは承認済みKnowledge索引の対象にしない。

## 本番移行手順

旧File Search文書には`festival`がないため、コード更新直後は
`YAGAPON_RAG_FESTIVAL_FILTER_ENABLED=false`のままにする。

1. `server_config.json`で本番GuildとFile Search storeを特定する。
2. 代表1チャンネル・30日でrebuildし、文書metadata、参照リンク、ローカルミラーを確認する。
3. 全対象チャンネルの全期間rebuildを実行し、失敗0件を確認する。
4. 管理画面で`unknown`年度の文書が残っていないか確認する。
5. 代表質問の自動評価と手動確認を行う。
6. `YAGAPON_RAG_FESTIVAL_FILTER_ENABLED=true`にしてコンテナを再起動する。
7. 旧ストアはすぐ削除せず、切り戻し期間を置く。

年度フィルタを先に有効化しないこと。旧文書が検索対象外となり、「今年度」の回答が
急に情報なしになる。
