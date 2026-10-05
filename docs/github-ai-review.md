# Pull Request AIレビュー

関連リポジトリのPull Request作成・更新時に、再利用可能なGitHub Actions workflowからGeminiを呼び出してレビューする。

## 安全上の境界

- `pull_request`イベントを使い、`pull_request_target`は使わない。
- PRのブランチをcheckout・build・実行しない。GitHub APIからdiffと変更後ファイルをデータとして読む。
- workflowとreviewer本体は信頼済みの`Yagapon_Chatbot`から取得する。
- `GITHUB_TOKEN`は`contents: read`と`pull-requests: write`だけを付与する。
- Geminiの出力を構造化し、実際に追加された行だけへコメントできるよう検証する。
- 同じhead SHAにはマーカーを付け、同じレビューを重複投稿しない。

## 導入

1. Organization secret `GOOGLE_API_KEY`を作成し、対象リポジトリだけに共有する。
2. `deploy/github/ai-review-caller.yml.example`を対象リポジトリの`.github/workflows/ai-review.yml`へコピーする。
3. 初回は参考チェックとして運用し、Required Checkにはしない。
4. 誤検知、見逃し、費用、時間を評価してから対象リポジトリを増やす。

本番運用では`@main`ではなく、レビュー済みのrelease tagまたはcommit SHAへ固定する。Private forkからのPRはGitHubの既定保護により書き込みトークンとSecretを受け取れないため、レビューは投稿されない。

## Gemini 3.8 Flashの評価

既存PRから以下を含む評価セットを作る。

- 本番障害・データ損失につながった不具合
- 認証・認可の不備
- 非同期処理、競合、エラー処理
- 正常な変更（誤検知を測るため）
- Flutter、Firebase Functions、Web、Python Bot

各PRで、人間が確認した正解指摘に対する再現率、AI指摘の適合率、重大度、該当行、費用、実行時間を記録する。最初は`medium`、重大な見逃しが多い場合だけ`high`を比較する。
