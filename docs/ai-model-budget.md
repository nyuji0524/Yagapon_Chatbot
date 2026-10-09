# AIモデル構成と月額予算

2026年10月5日時点のGemini Developer API有料枠を前提にする。実請求は、
File Searchが返す文書量、thinking token、音声の長さによって変動する。

## 採用構成

| 処理 | 既定モデル | コスト制御 |
| --- | --- | --- |
| RAG回答、音声会話の回答 | `gemini-3.8-flash` | thinking `low`、回答上限1,024/256 token |
| 議事録の整形、週次レポート | `gemini-3.8-flash` | thinking `medium`、出力上限4,096 token |
| 音声文字起こし | `gemini-3.5-transcribe` | 会議ファイルだけ話者分離、音声は処理後削除 |
| 要約、月次統合、リアクション | `gemini-3.1-flash-lite` | thinking `low`、用途別出力上限 |
| TTS | `edge-tts` | Gemini API課金なし |

全モデル名は環境変数で上書きできる。RAGは1ギルド1日400回を既定上限とし、
`YAGAPON_DAILY_QUERY_LIMIT`で運用予算に合わせて下げられる。
主要API応答のinput/output/thinking tokenは`AI usage`ログとして記録する。

## 単価

- Gemini 3.8 Flash: 2026年末まで入力 `$0.75`、出力 `$3.75` / 100万token。
  2027年1月1日から入力 `$1.50`、出力 `$7.50`。
- Gemini 3.5 Transcribe: 音声入力と文字出力を合わせた目安 `$0.005/分`。
- Gemini 3.1 Flash-Lite: 入力 `$0.25`、出力 `$1.50` / 100万token。
- File Search: 初回索引のembeddingは `$0.15` / 100万token。保存とクエリ時
  embeddingは無料で、取得された文書tokenは通常の入力tokenとして課金される。

## 利用量別の概算

RAG 1回を入力4,000 token（取得文書込み）、出力500 token（thinking込み）として
計算する。音声会話の回答は1回あたり入力1,000、出力100 tokenとする。

| 月間利用 | 2026年末まで | 2027年以降 | 内訳の例 |
| --- | ---: | ---: | --- |
| 小規模 | 約 `$8` | 約 `$13` | RAG 30回/日、音声10時間、音声回答300回 |
| 標準 | 約 `$31` | 約 `$46` | RAG 100回/日、音声50時間、音声回答1,000回 |
| 上限寄り | 約 `$92` | 約 `$154` | RAG 400回/日、音声100時間、音声回答3,000回 |

要約、レポート、索引作成は利用量に依存するため表には含めない。通常運用では
月数ドル程度の予備費を加え、当初は月額 `$25`、`$50`、`$100` に通知を設定する。
`$50`を超えたらRAGの日次上限を100以下へ下げ、取得文書量と不要な自動レポートを
確認する。予算通知は課金停止ではないため、必要ならAPI側のquotaも併用する。

## 品質と安全性

- 会議録音はTranscribeの話者分離を使い、声紋から実名を推定しない。
- 話者ラベルと実名の対応は参加者が確認する。
- 話者分離が失敗した場合は通常文字起こしへ一度だけ切り替える。
- RAGの回答モデルは安価なモデルへ落とさず、要約など低リスク処理だけ
  Flash-Liteへ分離する。
- モデル障害時は環境変数を変更して切り戻す。TTSは現行のedge-ttsを維持する。

## 価格資料

- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/transcribe
- https://ai.google.dev/gemini-api/docs/file-search
