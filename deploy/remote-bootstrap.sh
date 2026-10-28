#!/usr/bin/env bash
set -Eeuo pipefail

image="${1:-}"
staging_dir="${2:-}"

if [[ ! "$image" =~ ^[a-z0-9.-]+-docker\.pkg\.dev/[a-z0-9._/-]+:[0-9a-f]{40}$ ]]; then
  echo "Refusing invalid Artifact Registry image reference" >&2
  exit 2
fi
if [[ ! "$staging_dir" =~ ^/tmp/yagapon-deploy-[0-9]+-[0-9]+$ ]]; then
  echo "Refusing invalid deployment staging directory" >&2
  exit 2
fi
if [[ -e "$staging_dir" ]]; then
  echo "Refusing existing deployment staging directory" >&2
  exit 2
fi

registry=${image%%/*}
gcloud auth configure-docker "$registry" --quiet
pull_started=$SECONDS
for attempt in 1 2 3; do
  if docker pull "$image"; then
    break
  fi
  if [[ "$attempt" == 3 ]]; then
    echo "Image pull failed after $attempt attempts" >&2
    exit 1
  fi
  # Docker/containerd can occasionally lose a temporary extraction lease
  # while unpacking a freshly pushed image.  The registry object is immutable
  # by commit SHA, so retrying the exact reference is safe and resumes already
  # downloaded layers without redeploying a different artifact.
  delay=$((attempt * 5))
  echo "Transient image pull failure; retrying in ${delay}s ($attempt/3)" >&2
  sleep "$delay"
done
echo "Deploy timing: image-pull=$((SECONDS - pull_started))s"

container_id=
cleanup() {
  if [[ -n "$container_id" ]]; then
    docker rm --force "$container_id" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

container_id=$(docker create "$image" true)
extract_started=$SECONDS
install -d -o root -g root -m 0700 "$staging_dir"
docker cp "$container_id:/opt/yagapon-deploy/." "$staging_dir/"
docker rm "$container_id" >/dev/null
container_id=
echo "Deploy timing: asset-extract=$((SECONDS - extract_started))s"

required=(
  compose.yaml
  remote-bootstrap.sh
  remote-deploy.sh
  rag-eval-control.sh
  rag-eval-cases.yaml
  yagapon.service
)
for name in "${required[@]}"; do
  path="$staging_dir/$name"
  if [[ ! -f "$path" || -L "$path" ]]; then
    echo "Image is missing a regular deployment asset: $name" >&2
    exit 2
  fi
done

exec /bin/bash "$staging_dir/remote-deploy.sh" "$image" "$staging_dir"
