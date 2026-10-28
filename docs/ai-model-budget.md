# AIモデル構成と月額予算

2026年10月5日時点のGemini Developer API有料枠を前提にする。実請求は、
File Searchが返す文書量、thinking token、音声の長さによって変動する。

## 採用構成

通常運用は `YAGAPON_AI_PROFILE=budget` とし、Gemini API全体を月1,000円以内へ
近づける。予算確保後は `YAGAPON_AI_PROFILE=quality` へ変更して再起動するだけで、
RAG・音声回答・議事録を3.8中心へ戻せる。個別の `YAGAPON_*_MODEL` が設定されて
いる場合は、profileよりその値を優先する。

| 処理 | budget | quality | コスト制御 |
| --- | --- | --- | --- |
| RAG回答、音声会話 | `gemini-3.1-flash-lite` | `gemini-3.8-flash` | 通常1,024、総括4,096 tokenまで |
| 議事録の整形 | `gemini-3.5-flash-lite` | `gemini-3.8-flash` | 文字起こしとは独立して変更可能 |
| 音声文字起こし | `gemini-3.5-transcribe` | 同左 | 必要な会議だけ実行 |
| Knowledge整理 | `gemini-3.1-flash-lite` | `gemini-3.5-flash-lite` | Batch API、1回2判定・1生成まで |
| GitHub AIレビュー | `gemini-3.1-flash-lite` | workflow inputで`gemini-3.8-flash` | 既定thinking `low` |
| TTS | `edge-tts` | 同左 | Gemini API課金なし |

全モデル名は環境変数で上書きできる。RAGは1ギルド1日50回を既定上限とし、
`YAGAPON_DAILY_QUERY_LIMIT`で運用予算に合わせて下げられる。
主要API応答のinput/output/thinking tokenは`AI usage`ログとして記録する。

## 単価

- Gemini 3.8 Flash: 2026年末まで入力 `$0.75`、出力 `$3.75` / 100万token。
  2027年1月1日から入力 `$1.50`、出力 `$7.50`。
- Gemini 3.5 Transcribe: 音声入力と文字出力を合わせた目安 `$0.005/分`。
- Gemini 3.5 Flash-Lite: 入力 `$0.30`、出力 `$2.50` / 100万token。
- Gemini 3.1 Flash-Lite: 入力 `$0.25`、出力 `$1.50` / 100万token。
  2.5 Flash-Liteはこのプロジェクトでは新規利用不可のため使わない。
- File Search: 初回索引のembeddingは `$0.15` / 100万token。保存とクエリ時
  embeddingは無料で、取得された文書tokenは通常の入力tokenとして課金される。

## 月1,000円の運用境界

- File Search取得件数は通常8、総括12を既定にし、入力tokenを抑える。
- Knowledge整理は6時間ごとでも、1回あたりモデル判定2件・生成1件までにする。
- RAGは1ギルド1日50回まで。必要なら30回へ下げる。
- Cloud Billingでは `yagamiatlas` のGemini APIだけを対象に、月1,000円の
  **spend cap budget** を設定する。これは通知だけのbudgetと異なり、上限到達後の
  Gemini API利用を当月末まで停止する。費用集計には遅延があるため、少額の超過は
  起こり得る。
- 既に当月費用が1,000円を超えている月にcapを作ると即時停止するため、次月1日に
  設定するか、当月は現在額に近い暫定capを使う。

## 品質と安全性

- 会議録音はTranscribeの話者分離を使い、声紋から実名を推定しない。
- 話者ラベルと実名の対応は参加者が確認する。
- 話者分離が失敗した場合は通常文字起こしへ一度だけ切り替える。
- budget profileでも検索・Firebase・引用監査は維持し、モデルだけに事実判定を
  任せない。品質低下は定期RAG評価で検知する。
- 予算が付いたらprofileをqualityへ変更する。コード変更や再ビルドは不要。

## 価格資料

- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/transcribe
- https://ai.google.dev/gemini-api/docs/file-search
