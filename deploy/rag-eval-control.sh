#!/usr/bin/env bash
set -Eeuo pipefail

guild_id=${1:-}
min_pass_rate=${2:-0.8}
app_dir=/opt/yagapon
deploy_env=/etc/yagapon/deploy.env
cases_source=/opt/yagapon/deploy/rag-eval-cases.yaml
report_host=/tmp/rag-eval-report.json

[[ "$guild_id" =~ ^[0-9]+$ ]] || { echo "Invalid guild id" >&2; exit 2; }
[[ "$min_pass_rate" =~ ^0(\.[0-9]+)?$|^1(\.0+)?$ ]] \
  || { echo "Invalid minimum pass rate" >&2; exit 2; }
[[ -f "$app_dir/compose.yaml" && -f "$deploy_env" && -f "$cases_source" ]] || {
  echo "YagaPon deployment or evaluation cases are not configured" >&2
  exit 2
}

compose=(
  docker compose
  --project-directory "$app_dir"
  --env-file "$deploy_env"
)
container_id=$("${compose[@]}" ps --status running --quiet yagapon)
[[ -n "$container_id" ]] || { echo "YagaPon container is not running" >&2; exit 3; }

# Keep the API token inside the production container. The Actions runner only
# receives the sanitized JSON report, never the bearer token or the env file.
"${compose[@]}" exec -T yagapon sh -c \
  'cat > /tmp/rag-eval-cases.yaml' < "$cases_source"

rm -f "$report_host"
set +e
"${compose[@]}" exec -T \
  --env EVAL_GUILD_ID="$guild_id" \
  --env EVAL_MIN_PASS_RATE="$min_pass_rate" \
  yagapon sh -c '
    rm -f /tmp/rag-eval-report.json
    python -m rag_eval \
      --base-url http://127.0.0.1:8000 \
      --guild-id "$EVAL_GUILD_ID" \
      --cases /tmp/rag-eval-cases.yaml \
      --min-pass-rate "$EVAL_MIN_PASS_RATE" \
      --output /tmp/rag-eval-report.json
  '
eval_status=$?
set -e

if docker cp "$container_id:/tmp/rag-eval-report.json" "$report_host"; then
  chmod 0644 "$report_host"
else
  echo "RAG evaluation did not produce a report" >&2
fi

exit "$eval_status"
