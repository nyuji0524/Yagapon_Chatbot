#!/usr/bin/env bash
set -Eeuo pipefail

image="${1:-}"
staging_dir="${2:-/tmp}"
staging_dir="${staging_dir%/}"
app_dir=/opt/yagapon
deploy_env=/etc/yagapon/deploy.env
runtime_env=/etc/yagapon/yagapon.env
compose_source="$staging_dir/compose.yaml"
compose_target="$app_dir/compose.yaml"
unit_source="$staging_dir/yagapon.service"
unit_target=/etc/systemd/system/yagapon.service
rag_eval_source="$staging_dir/rag-eval-control.sh"
rag_eval_target="$app_dir/deploy/rag-eval-control.sh"
rag_eval_cases_source="$staging_dir/rag-eval-cases.yaml"
rag_eval_cases_target="$app_dir/deploy/rag-eval-cases.yaml"
bootstrap_source="$staging_dir/remote-bootstrap.sh"
bootstrap_target="$app_dir/deploy/remote-bootstrap.sh"
backup_dir=/var/backups/yagapon/"$(date -u +%Y%m%dT%H%M%SZ)"
github_app_key_path=/etc/yagapon/secrets/github-app-private-key.pem
github_app_key_existed=false
github_app_key_installed=false
github_app_key_backup=
github_app_key_tmp=
knowledge_github_app_key_path=/etc/yagapon/secrets/knowledge-github-app-private-key.pem
knowledge_github_app_key_existed=false
knowledge_github_app_key_installed=false
knowledge_github_app_key_backup=
knowledge_github_app_key_tmp=
oauth_tmp=
data_path=
recording_snapshot_marker=
recording_snapshot_root=
recording_snapshot_dir=

if [[ ! "$image" =~ ^[a-z0-9.-]+-docker\.pkg\.dev/[a-z0-9._/-]+:[0-9a-f]{40}$ ]]; then
  echo "Refusing invalid Artifact Registry image reference" >&2
  exit 2
fi
if [[ "$staging_dir" != /tmp \
  && ! "$staging_dir" =~ ^/tmp/yagapon-deploy-[0-9]+-[0-9]+$ ]]; then
  echo "Refusing invalid deployment staging directory" >&2
  exit 2
fi
if [[ ! -f "$compose_source" || ! -f "$staging_dir/remote-deploy.sh" || ! -f "$unit_source" \
  || ! -f "$rag_eval_source" || ! -f "$bootstrap_source" ]]; then
  echo "Deployment files were not copied to the staging directory" >&2
  exit 2
fi
[[ -f "$rag_eval_cases_source" ]] || {
  echo "RAG evaluation cases were not copied to the staging directory" >&2
  exit 2
}
if [[ ! -f "$runtime_env" ]]; then
  echo "Missing $runtime_env" >&2
  exit 2
fi

# Production uses separate Secret Manager PEMs mounted only into their exact
# source-reader or Knowledge-dispatch services. Reject legacy direct values
# before changing deployment state: env_file would otherwise inject them into
# every service and direct values take precedence over *_FILE readers. The
# scanner deliberately emits no values.
if ! python3 - "$runtime_env" <<'PY'
import re
import sys
from pathlib import Path

forbidden = {
    "YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY",
    "YAGAPON_SOURCE_GITHUB_TOKEN",
    "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY",
}


def has_forbidden_value(raw_line: str) -> bool:
    line = raw_line.lstrip("\ufeff").strip()
    if not line or line.startswith("#"):
        return False
    export_prefix = re.match(r"export[ \t]+", line)
    if export_prefix:
        line = line[export_prefix.end():]
    key, separator, raw_value = line.partition("=")
    if not separator or key.strip() not in forbidden:
        return False
    value = raw_value.strip()
    if not value:
        return False
    # Docker Compose treats quoted whitespace and an unquoted value followed
    # by a whitespace-separated comment as empty after read_secret().strip().
    if re.fullmatch(r'''(?:"[ \t]*"|'[ \t]*')(?:[ \t]+#.*)?''', value):
        return False
    effective = re.split(r"[ \t]+#", raw_value, maxsplit=1)[0].strip()
    return bool(effective)


lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
raise SystemExit(1 if any(has_forbidden_value(line) for line in lines) else 0)
PY
then
  echo "Direct GitHub App credentials are forbidden in $runtime_env" >&2
  echo "Remove direct private-key/token entries; configure the dedicated GitHub App IDs and Secret Manager PEM mounts in deploy.env" >&2
  exit 2
fi

# This first retention rollout must still be able to run the preceding image
# after rollback.  That image interprets zero as an immediate recording cap,
# while the new image interprets it as unlimited.  Keep the shared env-file at
# its old >=4 GiB value (or omit the key for the old 4 GiB default); the new
# Compose service-level literal below overrides it only for the new generation.
if ! python3 - "$runtime_env" <<'RECORDING_CAP_PY'
import re
import shlex
import sys
from pathlib import Path

key = "YAGAPON_MAX_RECORDING_BYTES"
minimum_rollback_cap = 4 * 1024 * 1024 * 1024
found = False
effective = ""

for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw_line.lstrip("\ufeff").strip()
    if not line or line.startswith("#"):
        continue
    export_prefix = re.match(r"export[ \t]+", line)
    if export_prefix:
        line = line[export_prefix.end():]
    name, separator, raw_value = line.partition("=")
    if name.strip() != key:
        continue
    found = True
    if not separator:
        effective = ""
        continue
    try:
        values = shlex.split(raw_value, comments=True, posix=True)
    except ValueError:
        raise SystemExit(1)
    effective = values[0] if len(values) == 1 else ""

if not found:
    raise SystemExit(0)
try:
    cap = int(effective)
except ValueError:
    raise SystemExit(1)
raise SystemExit(0 if cap >= minimum_rollback_cap else 1)
RECORDING_CAP_PY
then
  echo "Unsafe YAGAPON_MAX_RECORDING_BYTES in $runtime_env for first-retention rollback" >&2
  echo "Keep it unset or at least 4294967296; the new Compose generation overrides it to 0" >&2
  exit 2
fi

install -d -m 0750 "$app_dir" /etc/yagapon "$backup_dir"
exec 9>/run/lock/yagapon-deploy.lock
flock -n 9 || { echo "Another deployment is running" >&2; exit 3; }

[[ -f "$compose_target" ]] && cp -a "$compose_target" "$backup_dir/compose.yaml"
[[ -f "$deploy_env" ]] && cp -a "$deploy_env" "$backup_dir/deploy.env"
[[ -f "$unit_target" ]] && cp -a "$unit_target" "$backup_dir/yagapon.service"
[[ -f "$rag_eval_target" ]] && cp -a "$rag_eval_target" "$backup_dir/rag-eval-control.sh"
[[ -f "$rag_eval_cases_target" ]] && cp -a "$rag_eval_cases_target" "$backup_dir/rag-eval-cases.yaml"

rollback() {
  echo "Deployment failed; restoring the previous Compose configuration" >&2
  set +e
  systemctl stop yagapon
  docker compose --project-directory "$app_dir" --env-file "$deploy_env" down
  # The failed generation may have accepted a recording after the pre-restart
  # retention snapshot.  Freeze that generation first, then preserve its final
  # bind-mounted state before an old generation that still unlinks terminal PCM
  # is allowed to start.  A second hard-link tree costs directory entries, not
  # another copy of the audio bytes.
  if [[ -n "$recording_snapshot_dir" \
    && -n "$recording_snapshot_marker" \
    && ! -f "$recording_snapshot_marker" ]]; then
    recording_failed_generation_snapshot_dir="${recording_snapshot_dir}-failed-generation"
    if ! install -d -o root -g root -m 0700 \
      "$recording_failed_generation_snapshot_dir"; then
      echo "CRITICAL: could not create failed-generation recording snapshot; old generation remains stopped" >&2
      return 1
    fi
    if [[ -d "$data_path/recordings" ]] \
      && ! cp -al -- "$data_path/recordings/." \
        "$recording_failed_generation_snapshot_dir/"; then
      echo "CRITICAL: could not hard-link failed-generation recordings; old generation remains stopped" >&2
      return 1
    fi
    if ! sync -f "$recording_failed_generation_snapshot_dir"; then
      echo "CRITICAL: could not fsync failed-generation recording snapshot; old generation remains stopped" >&2
      return 1
    fi
  fi
  [[ -f "$backup_dir/compose.yaml" ]] && cp -a "$backup_dir/compose.yaml" "$compose_target"
  [[ -f "$backup_dir/deploy.env" ]] && cp -a "$backup_dir/deploy.env" "$deploy_env"
  [[ -f "$backup_dir/yagapon.service" ]] && cp -a "$backup_dir/yagapon.service" "$unit_target"
  [[ -f "$backup_dir/rag-eval-control.sh" ]] \
    && install -o root -g root -m 0755 "$backup_dir/rag-eval-control.sh" "$rag_eval_target"
  [[ -f "$backup_dir/rag-eval-cases.yaml" ]] \
    && install -o root -g root -m 0644 "$backup_dir/rag-eval-cases.yaml" "$rag_eval_cases_target"
  if [[ -n "$github_app_key_backup" && -f "$github_app_key_backup" ]]; then
    install -d -o root -g root -m 0700 "$(dirname "$github_app_key_path")"
    source_rollback_tmp=$(mktemp /etc/yagapon/secrets/github-app-private-key.pem.rollback-install.XXXXXX)
    install -o 10001 -g 10001 -m 0600 \
      "$github_app_key_backup" "$source_rollback_tmp"
    mv -f -- "$source_rollback_tmp" "$github_app_key_path"
  elif [[ "$github_app_key_installed" == true && "$github_app_key_existed" == false ]]; then
    rm -f -- "$github_app_key_path"
  fi
  if [[ -n "$knowledge_github_app_key_backup" && -f "$knowledge_github_app_key_backup" ]]; then
    install -d -o root -g root -m 0700 "$(dirname "$knowledge_github_app_key_path")"
    knowledge_rollback_tmp=$(mktemp /etc/yagapon/secrets/knowledge-github-app-private-key.pem.rollback-install.XXXXXX)
    install -o 10001 -g 10001 -m 0600 \
      "$knowledge_github_app_key_backup" "$knowledge_rollback_tmp"
    mv -f -- "$knowledge_rollback_tmp" "$knowledge_github_app_key_path"
  elif [[ "$knowledge_github_app_key_installed" == true \
    && "$knowledge_github_app_key_existed" == false ]]; then
    rm -f -- "$knowledge_github_app_key_path"
  fi
  systemctl daemon-reload
  systemctl restart yagapon || true
}
cleanup_staging() {
  if [[ "$staging_dir" != /tmp ]]; then
    if ! rm -rf -- "$staging_dir"; then
      echo "Warning: could not remove deployment staging directory: $staging_dir" >&2
    fi
  fi
}
cleanup_secret_temps() {
  [[ -z "$oauth_tmp" ]] || rm -f -- "$oauth_tmp"
  [[ -z "$github_app_key_tmp" ]] || rm -f -- "$github_app_key_tmp"
  [[ -z "$github_app_key_backup" ]] || rm -f -- "$github_app_key_backup"
  [[ -z "$knowledge_github_app_key_tmp" ]] || rm -f -- "$knowledge_github_app_key_tmp"
  [[ -z "$knowledge_github_app_key_backup" ]] || rm -f -- "$knowledge_github_app_key_backup"
}
handle_error() {
  status=$?
  trap - ERR
  rollback
  cleanup_staging
  exit "$status"
}
trap handle_error ERR
trap cleanup_secret_temps EXIT

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
install -m 0644 "$unit_source" "$unit_target"
install -d -o root -g root -m 0755 "$app_dir/deploy"
install -o root -g root -m 0755 "$rag_eval_source" "$rag_eval_target"
install -o root -g root -m 0644 "$rag_eval_cases_source" "$rag_eval_cases_target"
systemctl daemon-reload

oauth_secret=$(sed -n 's/^YAGAPON_GOOGLE_OAUTH_SECRET=//p' "$deploy_env" | tail -1)
oauth_project=$(sed -n 's/^YAGAPON_GCP_PROJECT_ID=//p' "$deploy_env" | tail -1)
if [[ -n "$oauth_secret" ]]; then
  if [[ -z "$oauth_project" || ! "$oauth_secret" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "Invalid Google OAuth Secret Manager configuration" >&2
    false
  fi
  oauth_tmp=$(mktemp /var/lib/yagapon/google-drive-oauth.json.XXXXXX)
  gcloud secrets versions access latest \
    --project "$oauth_project" \
    --secret "$oauth_secret" > "$oauth_tmp"
  python3 -c '
import json
import sys

with open(sys.argv[1]) as source:
    value = json.load(source)
required = {"client_id", "client_secret", "refresh_token"}
if value.get("type") not in (None, "authorized_user") or not required.issubset(value):
    raise SystemExit("Secret must contain authorized-user OAuth credentials")
required_scopes = {
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
}
raw_scopes = value.get("scopes") or value.get("scope") or ()
scopes = set(raw_scopes.split() if isinstance(raw_scopes, str) else raw_scopes)
if not required_scopes.issubset(scopes):
    raise SystemExit("OAuth credential must include drive.readonly, drive.file, and documents scopes")
' "$oauth_tmp"
  install -o 10001 -g 10001 -m 0600 \
    "$oauth_tmp" /var/lib/yagapon/google-drive-oauth.json
  rm -f "$oauth_tmp"
  oauth_tmp=
fi

# The GitHub App private key stays in Secret Manager and is materialized only
# as a root-managed, container-readable file.  App/installation IDs are not
# secrets and remain ordinary deploy configuration; the PEM must never be put
# in an environment variable or copied through GitHub Actions.
github_app_key_secret=$(sed -n 's/^YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_SECRET=//p' "$deploy_env" | tail -1)
github_app_id=$(sed -n 's/^YAGAPON_SOURCE_GITHUB_APP_ID=//p' "$deploy_env" | tail -1)
github_app_installation_id=$(sed -n 's/^YAGAPON_SOURCE_GITHUB_APP_INSTALLATION_ID=//p' "$deploy_env" | tail -1)
github_app_key_host_file=$(sed -n 's/^YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_HOST_FILE=//p' "$deploy_env" | tail -1)
if [[ -n "$github_app_key_secret$github_app_id$github_app_installation_id$github_app_key_host_file" ]]; then
  if [[ -z "$oauth_project" \
    || ! "$github_app_key_secret" =~ ^[a-zA-Z0-9_-]+$ \
    || ! "$github_app_id" =~ ^[a-zA-Z0-9]+$ \
    || ! "$github_app_installation_id" =~ ^[0-9]+$ \
    || "$github_app_installation_id" == 0 \
    || "$github_app_key_host_file" != "$github_app_key_path" ]]; then
    echo "Invalid GitHub App Secret Manager configuration" >&2
    false
  fi
  install -d -o root -g root -m 0700 /etc/yagapon/secrets
  if [[ -f "$github_app_key_path" ]]; then
    github_app_key_backup=$(mktemp /etc/yagapon/secrets/github-app-private-key.pem.rollback.XXXXXX)
    install -o root -g root -m 0600 \
      "$github_app_key_path" "$github_app_key_backup"
    github_app_key_existed=true
  fi
  github_app_key_tmp=$(mktemp /etc/yagapon/secrets/github-app-private-key.pem.XXXXXX)
  gcloud secrets versions access latest \
    --project "$oauth_project" \
    --secret "$github_app_key_secret" > "$github_app_key_tmp"
  if ! openssl rsa -in "$github_app_key_tmp" -check -noout >/dev/null 2>&1; then
    echo "GitHub App secret must contain a valid RSA private key" >&2
    false
  fi
  chown 10001:10001 "$github_app_key_tmp"
  chmod 0600 "$github_app_key_tmp"
  mv -f -- "$github_app_key_tmp" "$github_app_key_host_file"
  github_app_key_installed=true
  github_app_key_tmp=
fi

# A second GitHub App is deliberately isolated from the source reader.  It can
# only dispatch the Knowledge repository workflow (actions:write); its key is
# mounted into the two curation entrypoints and nowhere else.
knowledge_github_app_key_secret=$(sed -n 's/^YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_SECRET=//p' "$deploy_env" | tail -1)
knowledge_github_app_id=$(sed -n 's/^YAGAPON_KNOWLEDGE_GITHUB_APP_ID=//p' "$deploy_env" | tail -1)
knowledge_github_app_installation_id=$(sed -n 's/^YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID=//p' "$deploy_env" | tail -1)
knowledge_github_app_key_host_file=$(sed -n 's/^YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_HOST_FILE=//p' "$deploy_env" | tail -1)
if [[ -n "$knowledge_github_app_key_secret$knowledge_github_app_id$knowledge_github_app_installation_id$knowledge_github_app_key_host_file" ]]; then
  if [[ -z "$oauth_project" \
    || ! "$knowledge_github_app_key_secret" =~ ^[a-zA-Z0-9_-]+$ \
    || ! "$knowledge_github_app_id" =~ ^[a-zA-Z0-9]+$ \
    || ! "$knowledge_github_app_installation_id" =~ ^[0-9]+$ \
    || "$knowledge_github_app_installation_id" == 0 \
    || "$knowledge_github_app_key_host_file" != "$knowledge_github_app_key_path" ]]; then
    echo "Invalid Knowledge GitHub App Secret Manager configuration" >&2
    false
  fi
  if [[ -n "$github_app_key_secret" \
    && ( "$knowledge_github_app_key_secret" == "$github_app_key_secret" \
      || "$knowledge_github_app_id" == "$github_app_id" \
      || "$knowledge_github_app_installation_id" == "$github_app_installation_id" \
      || "$knowledge_github_app_key_host_file" == "$github_app_key_host_file" ) ]]; then
    echo "Knowledge dispatch and source indexing must use separate GitHub Apps, installations, secrets, and key files" >&2
    false
  fi
  install -d -o root -g root -m 0700 /etc/yagapon/secrets
  if [[ -f "$knowledge_github_app_key_path" ]]; then
    knowledge_github_app_key_backup=$(mktemp /etc/yagapon/secrets/knowledge-github-app-private-key.pem.rollback.XXXXXX)
    install -o root -g root -m 0600 \
      "$knowledge_github_app_key_path" "$knowledge_github_app_key_backup"
    knowledge_github_app_key_existed=true
  fi
  knowledge_github_app_key_tmp=$(mktemp /etc/yagapon/secrets/knowledge-github-app-private-key.pem.XXXXXX)
  gcloud secrets versions access latest \
    --project "$oauth_project" \
    --secret "$knowledge_github_app_key_secret" > "$knowledge_github_app_key_tmp"
  if ! openssl rsa -in "$knowledge_github_app_key_tmp" -check -noout >/dev/null 2>&1; then
    echo "Knowledge GitHub App secret must contain a valid RSA private key" >&2
    false
  fi
  chown 10001:10001 "$knowledge_github_app_key_tmp"
  chmod 0600 "$knowledge_github_app_key_tmp"
  mv -f -- "$knowledge_github_app_key_tmp" "$knowledge_github_app_key_host_file"
  knowledge_github_app_key_installed=true
  knowledge_github_app_key_tmp=
fi

docker compose --project-directory "$app_dir" --env-file "$deploy_env" config --quiet
# remote-bootstrap.sh authenticated and pulled this exact immutable SHA before
# extracting these scripts. Repeating Compose pull here added another registry
# round trip and layer verification without changing the selected image.
docker image inspect "$image" >/dev/null
data_path=$(sed -n 's/^YAGAPON_DATA_PATH=//p' "$deploy_env" | tail -1)
data_path=${data_path:-/var/lib/yagapon}
install -d -o 10001 -g 10001 -m 0750 "$data_path/releases"
recording_snapshot_marker="$data_path/releases/recording-retention-v1.json"
preflight_started=$SECONDS
timeout --signal=TERM --kill-after=30s 420s docker run --rm \
  --env-file /etc/yagapon/yagapon.env \
  --volume "$data_path:/data" \
  --tmpfs /data/recordings:rw,noexec,nosuid,nodev,mode=000,size=4096 \
  --tmpfs /data/transcripts:rw,noexec,nosuid,nodev,mode=000,size=4096 \
  --tmpfs /data/replays:rw,noexec,nosuid,nodev,mode=000,size=4096 \
  --tmpfs /data/voiceprints:rw,noexec,nosuid,nodev,mode=000,size=4096 \
  "$image" python -m bot.preflight
timeout --signal=TERM --kill-after=30s 420s docker compose \
  --project-directory "$app_dir" \
  --env-file "$deploy_env" \
  run --rm --no-deps source-index-watch \
  python -m source_index --preflight
echo "Deploy timing: preflight=$((SECONDS - preflight_started))s"

# The image deployed before permanent retention removed terminal PCM during
# cleanup. A failed first migration can therefore roll back to code that would
# unlink the same bind-mounted recording files. Preserve a root-only hard-link
# snapshot outside /data/recordings before any restart. Hard links need almost
# no extra audio capacity and keep the bytes recoverable if the old path is
# unlinked; the marker is written only after the new generation is healthy.
if [[ ! -f "$recording_snapshot_marker" ]]; then
  recording_snapshot_root="$data_path/recording-snapshots"
  install -d -o root -g root -m 0700 "$recording_snapshot_root"
  recording_snapshot_dir="$recording_snapshot_root/retention-v1-$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -o root -g root -m 0700 "$recording_snapshot_dir"
  if [[ -d "$data_path/recordings" ]]; then
    cp -al -- "$data_path/recordings/." "$recording_snapshot_dir/"
  fi
  sync -f "$recording_snapshot_dir"
fi
restart_started=$SECONDS
systemctl restart yagapon
echo "Deploy timing: systemd-restart=$((SECONDS - restart_started))s"

host_port=$(sed -n 's/^YAGAPON_HOST_PORT=//p' "$deploy_env" | tail -1)
host_port=${host_port:-8000}
# A cold start on the production VM has reached roughly 80 seconds. Keep this
# longer than the image health-check start period so a slow but progressing boot
# is not rolled back as a false negative.
for _ in $(seq 1 48); do
  container_id=$(docker compose \
    --project-directory "$app_dir" \
    --env-file "$deploy_env" \
    ps --status running --quiet yagapon)
  running_image=
  if [[ -n "$container_id" ]]; then
    running_image=$(docker inspect --format '{{.Config.Image}}' "$container_id")
  fi
  source_index_id=$(docker compose \
    --project-directory "$app_dir" \
    --env-file "$deploy_env" \
    ps --status running --quiet source-index-watch)
  if [[ "$running_image" == "$image" ]] \
    && [[ -n "$source_index_id" ]] \
    && systemctl is-active --quiet yagapon \
    && curl --fail --silent --show-error --max-time 5 \
      "http://127.0.0.1:${host_port}/ready" >/dev/null; then
    trap - ERR
    printf '{"image":"%s","deployed_at":"%s","container":"%s"}\n' \
      "$image" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$container_id" \
      > "$data_path/releases/current.json"
    chown 10001:10001 "$data_path/releases/current.json"
    chmod 0640 "$data_path/releases/current.json"
    if [[ -n "$recording_snapshot_dir" ]]; then
      printf '{"migration":"permanent-recording-retention-v1","snapshot":"%s","completed_at":"%s"}\n' \
        "$recording_snapshot_dir" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        > "$recording_snapshot_marker"
      chown root:root "$recording_snapshot_marker"
      chmod 0600 "$recording_snapshot_marker"
      sync -f "$data_path/releases"
    fi
    find /var/backups/yagapon -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf -- {} +
    # Release images are content-addressed by commit SHA, so unused images and
    # build objects accumulate on the small VM disk. Prune only after the new
    # generation is healthy: the rollback trap must retain the former image
    # until every preflight, restart, and health check has succeeded. Deliberately
    # omit --volumes because /var/lib/yagapon contains durable user data.
    if ! docker system prune --all --force >/dev/null; then
      echo "Warning: deployment is healthy but Docker cleanup failed" >&2
    fi
    if [[ "$github_app_key_installed" == true ]]; then
      # The former location was part of the shared /data bind and therefore
      # visible to every Compose service running as UID 10001. Keep it until
      # the new generation is healthy so a failed migration can still roll back.
      rm -f -- /var/lib/yagapon/github-app-private-key.pem
    fi
    # Update the stable entrypoint only after the release is proven healthy.
    # A failed release therefore cannot strand the next deployment.
    install -o root -g root -m 0755 "$bootstrap_source" "$bootstrap_target"
    cleanup_staging
    echo "Deploy timing: restart-to-ready=$((SECONDS - restart_started))s"
    echo "Deployment healthy: $image (container: $container_id)"
    exit 0
  fi
  sleep 5
done

echo "Health check timed out" >&2
false
