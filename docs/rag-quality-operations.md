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
| `authority` | 会話記録か、レビュー済み知識か |
| `document_key` | 差分置換用の安定キー |
| `source_url` | DiscordまたはKnowledge原文への導線 |

Discordの会話は`status=raw`、`authority=conversation`とする。Knowledgeリポジトリは
`approved,verified,current`だけを自動索引し、`authority=curated`として完全一致検索で
会話記録より優先する。draftやneeds-reviewを回答根拠へ自動昇格させない。

## 検索と回答

1. Gemini File Searchで意味検索を行う。
2. SQLiteのローカルミラーで用語辞書の別名、ID、固有語を完全一致検索する。
3. 完全一致候補は一致語数、curated、レビュー済みstatusの順に加点して並べる。
4. 両方の候補を回答モデルへ渡し、根拠が取れない場合は推測せず「記録を見つけられない」と返す。
5. 回答末尾へ参照元を表示する。

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
  --token "$YAGAPON_API_TOKEN" \
  --guild-id "$YAGAPON_KNOWLEDGE_GUILD_ID" \
  --cases docs/rag-eval-cases.yaml \
  --output data/rag-eval-report.json
```

サンプルの`docs/rag-eval-cases.example.yaml`を複製し、正答に必ず含む語、禁止語、
期待年度、出典必須、情報なし質問を実データに合わせて定義する。モデル変更・全backfill・
Knowledge大量更新の前後で同じ質問セットを実行して比較する。

## 差分同期

- Discord: 通常会話をバッファで差分追加し、`/backfill`は安定キーで安全に置換する。
- Google Drive: change tokenとSHA-256で変更文書だけdraftへ変換する。
- Knowledge: Git上のMarkdownのSHA-256とremote document名を状態ファイルへ保存し、
  承認済み文書の変更・削除だけをFile Searchへ反映する。

常駐同期を使う場合は、対象Guild ID、Knowledgeリポジトリの読み取り専用mount、Driveの
読み取り権限を確認してから有効にする。

```bash
docker compose --profile knowledge up -d knowledge-sync-watch knowledge-index-watch
```

Driveから生成したdraftは、人のレビューとstatus変更を経てKnowledgeリポジトリへ入れる。
原文変更をそのまま回答根拠へ昇格させる処理は行わない。

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
