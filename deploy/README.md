# GCEへのDockerデプロイ

現行の`nohup python main.py`を、Docker Composeを管理するsystemdサービスへ移行する。切り替え時まで現行プロセスを停止しない。

## 配置

- アプリケーション: `/opt/yagapon`
- Secret環境ファイル: `/etc/yagapon/yagapon.env`（rootのみ読取）
- 永続データ: `/var/lib/yagapon`
- systemd unit: `/etc/systemd/system/yagapon.service`

`server_config.json`と`voiceprints/`は永続データへ移す。コピー後に所有者をコンテナのUID `10001`へ変更する。

## 初回切り替え

1. GCEのスナップショットと`server_config.json`、`voiceprints/`のバックアップを取得する。
2. Docker EngineとCompose pluginを導入する。
3. `/opt/yagapon`へレビュー済みcommitを配置する。
4. `/etc/yagapon/yagapon.env`へSecretを設定し、権限を`0600`にする。
5. `/var/lib/yagapon`へ既存データをコピーする。
6. `/etc/yagapon/deploy.env`へArtifact Registryの初期イメージとCompose変数を設定する。
7. 一時的に別ポートで起動し、`/health`とDiscord接続を確認する。
8. 現行nohupプロセスを停止し、systemdサービスを有効化する。
9. Discordコマンド、RAG質問、Drive保存、音声参加を確認する。

```bash
sudo install -d -m 0750 /etc/yagapon /var/lib/yagapon /opt/yagapon
sudo install -m 0644 deploy/systemd/yagapon.service /etc/systemd/system/yagapon.service
sudo tee /etc/yagapon/deploy.env >/dev/null <<'EOF'
YAGAPON_IMAGE=asia-northeast1-docker.pkg.dev/PROJECT/REPOSITORY/yagapon-chatbot:INITIAL_SHA
YAGAPON_ENV_FILE=/etc/yagapon/yagapon.env
YAGAPON_DATA_PATH=/var/lib/yagapon
YAGAPON_BIND_ADDRESS=127.0.0.1
YAGAPON_HOST_PORT=8000
YAGAPON_GCP_PROJECT_ID=PROJECT
YAGAPON_GOOGLE_OAUTH_SECRET=yagapon-google-drive-oauth
YAGAPON_GOOGLE_OAUTH_FILE=/data/google-drive-oauth.json
YAGAPON_KNOWLEDGE_GCS_BUCKET=PROJECT-yagapon-knowledge-intake
YAGAPON_KNOWLEDGE_INTAKE_DISPATCH_ENABLED=true
YAGAPON_KNOWLEDGE_INTAKE_WORKFLOW=yagapon-intake.yml
YAGAPON_KNOWLEDGE_GITHUB_APP_ID=234567
YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID=89012345
YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_SECRET=yagapon-knowledge-github-app-private-key
YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_HOST_FILE=/etc/yagapon/secrets/knowledge-github-app-private-key.pem
YAGAPON_SOURCE_GITHUB_APP_ID=123456
YAGAPON_SOURCE_GITHUB_APP_INSTALLATION_ID=78901234
YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_SECRET=yagapon-github-app-private-key
YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_HOST_FILE=/etc/yagapon/secrets/github-app-private-key.pem
YAGAPON_SOURCE_REQUIRE_FULL_MANIFEST=true
YAGAPON_SOURCE_REPOSITORIES=
YAGAPON_SOURCE_HISTORY_SINCE=2024-10-01T00:00:00+09:00
YAGAPON_SOURCE_INDEX_ENABLED=true
# 通常はCIで検証済みのimmutable imageを使う。mutableな本番データと
# remote credentialまで再検証する保守デプロイ時だけtrueにする。
YAGAPON_DEPLOY_RUNTIME_PREFLIGHT=false
EOF
sudo chown -R 10001:10001 /var/lib/yagapon
sudo systemctl daemon-reload
sudo systemctl enable --now yagapon
sudo systemctl status yagapon
```

## main pushからの自動更新

通常リリースでは、CIで完了済みのimport・Compose・ソース索引検証を本番VMで
重複実行しない。`/etc/yagapon/deploy.env`の
`YAGAPON_DEPLOY_RUNTIME_PREFLIGHT=true`は、SQLiteや設定ファイル、remote source
credentialを再検査する保守デプロイ時だけ使用する。

`CI` workflowがmainで成功すると`Deploy to GCE`が次を実行する。

1. CIと同一commitからDockerイメージを作成し、Artifact Registryへcommit SHAタグでpush
2. GitHub OIDCからWorkload Identity Federationで短期認証
3. IAP + OS LoginでVMへcomposeと更新スクリプトを転送
4. Docker Compose用systemd unitを配置して、新イメージをpull・再起動
5. コンテナID、実行イメージ、systemd、`/health`を最大60秒確認し、失敗時は旧unitを含む直前の構成へ戻す

Artifact Registryではcommit SHAをイメージタグに使い、同じcommitの再実行時は既存イメージを
再利用する。リポジトリには`deploy/artifact-cleanup-policy.json`を適用し、直近10世代を残しつつ
30日を超えた古いイメージを削除する。cleanup対象にするため、タグのimmutabilityは無効にする。

GitHubのRepository Variablesへ次を設定する。`production` Environmentはデプロイ履歴と
必要に応じた保護ルールに使うが、完全自動化する場合は承認待ちルールを付けない。

| Variable | 例 |
|---|---|
| `YAGAPON_DEPLOY_ENABLED` | 準備完了後に`true` |
| `GCP_PROJECT_ID` | `yagapon-prod` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/123.../locations/global/workloadIdentityPools/github/providers/yagapon` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `github-deploy@PROJECT.iam.gserviceaccount.com` |
| `GCE_INSTANCE` | VM名 |
| `GCE_ZONE` | `asia-northeast1-b` |
| `GCP_ARTIFACT_IMAGE` | `asia-northeast1-docker.pkg.dev/PROJECT/REPOSITORY/yagapon-chatbot` |

長期サービスアカウントJSONはGitHub Secretsへ保存しない。GitHubのOIDC subjectはこの
リポジトリに限定する。デプロイ用サービスアカウントにはArtifact Registry書き込み、
Compute参照、OS Admin Login、IAP tunnelの必要最小権限を付与する。VM自身のサービス
アカウントにはArtifact Registry Readerを付与し、OS LoginとIAPを有効化する。

初期設定が完了するまで`YAGAPON_DEPLOY_ENABLED`を未設定または`false`にする。この状態では
mainへpushしてもdeploy jobはskipされ、本番VMを変更しない。接続と手動ロールバックを確認後に
`true`へ変更すると、以後はmainのCI成功ごとに自動デプロイされる。

## 外部公開

Composeの既定値はAPIを`127.0.0.1:8000`へだけ公開する。外部からAPIを使う場合は、認証付きHTTPS reverse proxyを経由する。

- `/health`のみ認証不要
- `/status`、`/ask`、`/backfill`は`YAGAPON_API_TOKEN`必須

Secretが未設定の場合、保護対象APIはfail-closedで拒否する。

## Google Drive OAuth

個人のGoogle Driveへ保存する場合は、サービスアカウントではなく保存先を所有する
Googleアカウントのauthorized-user OAuth認証情報を使う。OAuthクライアントはDesktop appとして
作成し、次のスコープでoffline accessを許可する。

- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/drive.file`
- `https://www.googleapis.com/auth/documents`

authorized-user JSONはリポジトリやGitHub Secretsへ置かず、Secret Managerの
`yagapon-google-drive-oauth`へ保存する。VMのサービスアカウントにはこのSecretだけの
`roles/secretmanager.secretAccessor`を付与する。デプロイ時に
`/var/lib/yagapon/google-drive-oauth.json`へ`0600`で展開され、コンテナから
`/data/google-drive-oauth.json`として参照される。

OAuth同意画面がTestingの間はrefresh tokenが7日で失効するため、常設運用前にProductionへ
変更する。認証情報の失効時は新しいSecret versionを追加して再デプロイする。

## GitHubソース・履歴の読み取り

ソースコードの正本は複製snapshotではなくGitHubとする。組織所有の専用GitHub Appを作成し、
webhookを無効、Repository permissionsは`Contents: Read-only`（`Metadata: Read-only`は自動）だけ、
Repository accessは`All repositories`としてインストールする。Appは組織の全repositoryを読めるが、
source workerは`source_index/repositories.yaml`のレビュー済みentryだけをinstallation tokenへ指定して
取り込む。GitHub Appからrepositoryを列挙して未知のrepositoryを自動索引する処理はない。本番では
`YAGAPON_SOURCE_REQUIRE_FULL_MANIFEST=true`のまま`YAGAPON_SOURCE_REPOSITORIES`を空欄にする。これにより
manifestへYagamyなどを追加した後も、古い固定リストが新規entryを黙って除外せず、preflightで誤設定を
拒否できる。環境変数が空でも対象はmanifest内の全entryだけであり、組織全体を意味しない。
個人PATや書き込み権限を本番Botへ渡さない。

GitHub Appのprivate key PEMはSecret Managerの`yagapon-github-app-private-key`へ保存する。
VMサービスアカウントにはこのSecretだけの`roles/secretmanager.secretAccessor`を付与する。
デプロイ時に共有data path外の`/etc/yagapon/secrets/github-app-private-key.pem`へUID 10001、
mode `0600`で展開し、`source-index-watch`だけへ
`/run/secrets/github-app-private-key.pem:ro`としてbind mountする。他のBot/同期workerからはmountしない。
App IDとinstallation IDは秘密ではないため`deploy.env`に置く。source workerはPEMから短命な
installation tokenを発行し、有効期限前に更新する。発行されたtokenはログ、状態ファイル、
File Searchへ保存しない。鍵の更新中は旧鍵をhost上の一時0600ファイルへ退避し、デプロイ失敗時は
復元する。旧`/var/lib/yagapon/github-app-private-key.pem`は新generationのhealth check成功後だけ削除する。

`source-index-watch`は各repositoryの現在のmainに加え、commit日時、SHA、message、変更path、diffを
`source_code_history/repository_history/historical`として登録する。patchが省略・切断された場合だけ、
allowlist内の小さいファイルをそのSHAで補助取得する。初回履歴は1周期あたり最大100 commitずつ、
新着を優先して永続cursorから遡る。GitHub rate limit、認証、アップロードのどれかに失敗した場合、
直前の完全な索引世代を保持する。大きいWeb repositoryの現在値はrepository全体のtarballではなく、
Git treeからallowlistに一致する小さいblobだけを取得する。

## Knowledge登録キュー

BotはGitHub Contents/Pull requests権限を持たず、専用GCS bucketへ機密検査済み候補を
create-onlyで登録する。整理直後の起動要求だけは、Knowledgeリポジトリ1件・`Actions: Write`
だけの別GitHub Appから短命tokenを発行して`yagapon-intake.yml`へ送る。204応答は要求受付であり、
PR作成済みとは扱わない。KnowledgeリポジトリのGitHub Actionsだけが候補を読み、専用branchと
Draft PRを作成する。
mainへマージされた承認済み文書は、read-only GitHub App経由で`source-index-watch`が差分同期する。
VMサービスアカウントへはintake bucketのObject Creatorと、用途別2つのGitHub App private-key
SecretへのAccessorだけを付与する。Knowledge AppのPEMはSecret Managerから
`/etc/yagapon/secrets/knowledge-github-app-private-key.pem`へatomicに更新し、`yagapon`と
`knowledge-curation-watch`だけへread-only mountする。source AppとはApp ID、installation ID、
Secret、host pathを共有できず、他サービスではKnowledge Appのdirect/file環境変数を明示的に空にする。
どちらの鍵も更新前の0600 backupを保持し、デプロイ失敗時はatomicに復元する。
