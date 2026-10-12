#!/usr/bin/env bash
set -Eeuo pipefail

action=${1:-replay-longest}
target_guild_name=${2:-}
target_channel_id=${3:-}
retranscribe=${4:-false}
job_id=${5:-${2:-}}
target_guild_id=${6:-}

case "$action" in
  replay-longest|status) ;;
  *) echo "Unsupported action" >&2; exit 2 ;;
esac
if [[ "$action" == replay-longest ]]; then
  [[ -n "$target_guild_name" || -n "$target_guild_id" ]] \
    || { echo "Target guild name or id is required" >&2; exit 2; }
  [[ -z "$target_guild_id" || "$target_guild_id" =~ ^[0-9]+$ ]] \
    || { echo "Invalid guild id" >&2; exit 2; }
  [[ -z "$target_channel_id" || "$target_channel_id" =~ ^[0-9]+$ ]] \
    || { echo "Invalid channel id" >&2; exit 2; }
  [[ "$retranscribe" == true || "$retranscribe" == false ]] \
    || { echo "Invalid retranscribe flag" >&2; exit 2; }
fi
if [[ "$action" == status ]]; then
  [[ "$job_id" =~ ^replay-[A-Za-z0-9_.-]{1,120}$ ]] || { echo "Invalid job id" >&2; exit 2; }
fi

app_dir=/opt/yagapon
deploy_env=/etc/yagapon/deploy.env
[[ -f "$app_dir/compose.yaml" && -f "$deploy_env" ]] || {
  echo "YagaPon deployment is not configured" >&2
  exit 2
}

docker compose --project-directory "$app_dir" --env-file "$deploy_env" exec -T \
  --env CONTROL_ACTION="$action" \
  --env CONTROL_TARGET_GUILD_NAME="$target_guild_name" \
  --env CONTROL_TARGET_GUILD_ID="$target_guild_id" \
  --env CONTROL_TARGET_CHANNEL_ID="$target_channel_id" \
  --env CONTROL_RETRANSCRIBE="$retranscribe" \
  --env CONTROL_JOB_ID="$job_id" \
  yagapon python - <<'PY'
import json
import os
import urllib.error
import urllib.request

token = os.environ.get("YAGAPON_API_TOKEN", "")
if not token:
    raise SystemExit("YAGAPON_API_TOKEN is not configured")
action = os.environ["CONTROL_ACTION"]
base = "http://127.0.0.1:8000"

def request(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method="POST" if data is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise SystemExit(f"API error {exc.code}: {body}") from exc

if action == "replay-longest":
    channel = os.environ.get("CONTROL_TARGET_CHANNEL_ID", "")
    target_guild = os.environ.get("CONTROL_TARGET_GUILD_NAME", "")
    target_guild_id = os.environ.get("CONTROL_TARGET_GUILD_ID", "")
    if not target_guild and not target_guild_id:
        raise SystemExit("A target Discord guild is not configured")
    result = request("/admin/recordings/replay-longest", {
        "target_guild_name": target_guild,
        "target_guild_id": int(target_guild_id) if target_guild_id else None,
        "target_channel_id": int(channel) if channel else None,
        "retranscribe": os.environ.get("CONTROL_RETRANSCRIBE") == "true",
    })
else:
    result = request(
        "/admin/recordings/replay/" + os.environ["CONTROL_JOB_ID"]
    )
safe = {key: result.get(key) for key in ("id", "status", "stage", "error_type")}
print(json.dumps(safe, ensure_ascii=False, indent=2))
PY
