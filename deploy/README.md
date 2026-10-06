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
EOF
sudo chown -R 10001:10001 /var/lib/yagapon
sudo systemctl daemon-reload
sudo systemctl enable --now yagapon
sudo systemctl status yagapon
```

## main pushからの自動更新

`CI` workflowがmainで成功すると`Deploy to GCE`が次を実行する。

1. CIと同一commitからDockerイメージを作成し、Artifact Registryへcommit SHAタグでpush
2. GitHub OIDCからWorkload Identity Federationで短期認証
3. IAP + OS LoginでVMへcomposeと更新スクリプトを転送
4. 新イメージをpullし、systemdサービスを再起動
5. `/health`を最大60秒確認し、失敗時は直前のcompose・イメージ設定へ戻す

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
