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
6. `docker compose config`と`docker compose build`を実行する。
7. 一時的に別ポートで起動し、`/health`とDiscord接続を確認する。
8. 現行nohupプロセスを停止し、systemdサービスを有効化する。
9. Discordコマンド、RAG質問、Drive保存、音声参加を確認する。

```bash
sudo install -d -m 0750 /etc/yagapon /var/lib/yagapon /opt/yagapon
sudo install -m 0644 deploy/systemd/yagapon.service /etc/systemd/system/yagapon.service
sudo chown -R 10001:10001 /var/lib/yagapon
sudo systemctl daemon-reload
sudo systemctl enable --now yagapon
sudo systemctl status yagapon
```

## 更新とロールバック

本番ではイメージタグだけでなくdigestを記録する。更新前に現在のdigestを保存し、新イメージをpullしてからサービスを再起動する。ヘルスチェックまたはDiscord接続に失敗した場合は、以前のdigestへ戻す。

```bash
docker compose pull
sudo systemctl restart yagapon
docker compose ps
```

## 外部公開

Composeの既定値はAPIを`127.0.0.1:8000`へだけ公開する。GitHub WebhookをVMへ直接送る場合のみ`YAGAPON_BIND_ADDRESS=0.0.0.0`を設定し、GCE Firewallの送信元制限を維持する。

- `/health`のみ認証不要
- `/status`、`/ask`、`/backfill`は`YAGAPON_API_TOKEN`必須
- `/webhook/github/*`は`GITHUB_WEBHOOK_SECRET`による署名必須

Secretが未設定の場合、保護対象APIはfail-closedで拒否する。
