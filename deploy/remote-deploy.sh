#!/usr/bin/env bash
set -Eeuo pipefail

image="${1:-}"
app_dir=/opt/yagapon
deploy_env=/etc/yagapon/deploy.env
compose_source=/tmp/compose.yaml
compose_target="$app_dir/compose.yaml"
backup_dir=/var/backups/yagapon/"$(date -u +%Y%m%dT%H%M%SZ)"

if [[ ! "$image" =~ ^[a-z0-9.-]+-docker\.pkg\.dev/[a-z0-9._/-]+:[0-9a-f]{40}$ ]]; then
  echo "Refusing invalid Artifact Registry image reference" >&2
  exit 2
fi
if [[ ! -f "$compose_source" || ! -f /tmp/remote-deploy.sh ]]; then
  echo "Deployment files were not copied to /tmp" >&2
  exit 2
fi
if [[ ! -f /etc/yagapon/yagapon.env ]]; then
  echo "Missing /etc/yagapon/yagapon.env" >&2
  exit 2
fi

install -d -m 0750 "$app_dir" /etc/yagapon "$backup_dir"
exec 9>/run/lock/yagapon-deploy.lock
flock -n 9 || { echo "Another deployment is running" >&2; exit 3; }

[[ -f "$compose_target" ]] && cp -a "$compose_target" "$backup_dir/compose.yaml"
[[ -f "$deploy_env" ]] && cp -a "$deploy_env" "$backup_dir/deploy.env"

rollback() {
  echo "Deployment failed; restoring the previous Compose configuration" >&2
  [[ -f "$backup_dir/compose.yaml" ]] && cp -a "$backup_dir/compose.yaml" "$compose_target"
  [[ -f "$backup_dir/deploy.env" ]] && cp -a "$backup_dir/deploy.env" "$deploy_env"
  systemctl restart yagapon || true
}
trap rollback ERR

deploy_tmp=$(mktemp /etc/yagapon/deploy.env.XXXXXX)
if [[ -f "$deploy_env" ]]; then
  grep -v '^YAGAPON_IMAGE=' "$deploy_env" > "$deploy_tmp" || true
else
  {
    echo 'YAGAPON_ENV_FILE=/etc/yagapon/yagapon.env'
    echo 'YAGAPON_DATA_PATH=/var/lib/yagapon'
    echo 'YAGAPON_BIND_ADDRESS=127.0.0.1'
    echo 'YAGAPON_HOST_PORT=8000'
  } > "$deploy_tmp"
fi
printf 'YAGAPON_IMAGE=%s\n' "$image" >> "$deploy_tmp"
chmod 0640 "$deploy_tmp"
mv "$deploy_tmp" "$deploy_env"
install -m 0644 "$compose_source" "$compose_target"

registry=${image%%/*}
gcloud auth configure-docker "$registry" --quiet
docker compose --project-directory "$app_dir" --env-file "$deploy_env" config --quiet
docker compose --project-directory "$app_dir" --env-file "$deploy_env" pull yagapon
systemctl restart yagapon

host_port=$(sed -n 's/^YAGAPON_HOST_PORT=//p' "$deploy_env" | tail -1)
host_port=${host_port:-8000}
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error "http://127.0.0.1:${host_port}/health" >/dev/null; then
    trap - ERR
    docker image prune --force --filter 'until=168h' >/dev/null
    echo "Deployment healthy: $image"
    exit 0
  fi
  sleep 2
done

echo "Health check timed out" >&2
false
