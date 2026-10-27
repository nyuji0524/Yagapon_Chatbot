import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_container_healthcheck_allows_cold_vm_startup():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "--start-period=150s" in dockerfile
    assert "--timeout=15s" in dockerfile
    assert "urlopen('http://127.0.0.1:8000/health', timeout=10)" in dockerfile


def test_remote_deploy_wait_exceeds_container_start_period():
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")

    assert "for _ in $(seq 1 48)" in deploy
    assert "sleep 5" in deploy
    assert "timeout --signal=TERM --kill-after=30s 420s docker run --rm" in deploy


def test_deploy_uses_image_assets_instead_of_iap_scp():
    workflow = (ROOT / ".github/workflows/deploy-gce.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    bootstrap = (ROOT / "deploy/remote-bootstrap.sh").read_text(encoding="utf-8")

    # SCP is retained only as a one-time bootstrap migration on a fresh VM.
    assert workflow.count("gcloud compute scp") == 1
    assert 'if [[ "$deploy_status" == 42 ]]' in workflow
    assert "/opt/yagapon/deploy/remote-bootstrap.sh" in workflow
    assert "docker/build-push-action@" in workflow
    assert "cache-from: type=gha,scope=yagapon-production-image" in workflow
    assert "COPY compose.yaml /opt/yagapon-deploy/compose.yaml" in dockerfile
    assert 'docker cp "$container_id:/opt/yagapon-deploy/." "$staging_dir/"' in bootstrap


def test_remote_preflights_remain_fail_fast_and_report_timing():
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")

    bot_start = deploy.index('"$image" python -m bot.preflight')
    source_start = deploy.index("python -m source_index --preflight")
    timing = deploy.index('echo "Deploy timing: preflight=')

    assert bot_start < source_start < timing
    assert 'echo "Deploy timing: restart-to-ready=' in deploy


def test_remote_deploy_preflights_source_index_before_restart():
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")

    preflight = "python -m source_index --preflight"
    restart = deploy.rindex("systemctl restart yagapon")
    assert "run --rm --no-deps source-index-watch" in deploy
    assert preflight in deploy
    assert deploy.index("github_app_key_installed=true") < deploy.index(preflight)
    assert deploy.index(preflight) < restart


def test_remote_deploy_keeps_rollback_image_until_health_succeeds():
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")

    pull = deploy.index('docker compose --project-directory "$app_dir" --env-file "$deploy_env" pull yagapon')
    preflight = deploy.index("python -m bot.preflight")
    restart = deploy.rindex("systemctl restart yagapon")
    health_check = deploy.index('"http://127.0.0.1:${host_port}/ready"')
    prune = deploy.index("docker system prune --all --force")

    assert pull < preflight < restart < health_check < prune
    assert "Warning: deployment is healthy but Docker cleanup failed" in deploy


def test_first_permanent_retention_deploy_snapshots_recordings_before_restart():
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")

    snapshot = 'cp -al -- "$data_path/recordings/." "$recording_snapshot_dir/"'
    restart = deploy.rindex("systemctl restart yagapon")
    marker_write = deploy.index('> "$recording_snapshot_marker"')
    health_check = deploy.index('"http://127.0.0.1:${host_port}/ready"')

    assert snapshot in deploy
    assert deploy.index(snapshot) < restart < health_check < marker_write
    assert 'recording_snapshot_root="$data_path/recording-snapshots"' in deploy
    assert 'install -d -o root -g root -m 0700 "$recording_snapshot_root"' in deploy


def test_first_retention_rollback_snapshots_failed_generation_before_old_restart():
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")
    rollback = deploy[
        deploy.index("rollback() {") : deploy.index("cleanup_staging() {")
    ]

    stopped = rollback.index("systemctl stop yagapon")
    down = rollback.index(
        'docker compose --project-directory "$app_dir" --env-file "$deploy_env" down'
    )
    snapshot = rollback.index('cp -al -- "$data_path/recordings/."')
    snapshot_destination = rollback.index(
        '"$recording_failed_generation_snapshot_dir/"',
        snapshot,
    )
    snapshot_fsync = rollback.index(
        'sync -f "$recording_failed_generation_snapshot_dir"'
    )
    restore = rollback.index(
        'cp -a "$backup_dir/compose.yaml" "$compose_target"'
    )
    old_restart = rollback.index("systemctl restart yagapon")

    assert (
        stopped
        < down
        < snapshot
        < snapshot_destination
        < snapshot_fsync
        < restore
        < old_restart
    )
    assert (
        'install -d -o root -g root -m 0700 \\\n'
        '      "$recording_failed_generation_snapshot_dir"'
    ) in rollback
    assert '&& ! -f "$recording_snapshot_marker"' in rollback
    assert "old generation remains stopped" in rollback


def test_yagapon_overrides_legacy_runtime_recording_cap_without_mutating_env():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    service_environment = compose["services"]["yagapon"]["environment"]
    legacy_runtime_environment = {"YAGAPON_MAX_RECORDING_BYTES": "4294967296"}

    # Compose service.environment has higher precedence than env_file.
    effective_environment = {
        **legacy_runtime_environment,
        **service_environment,
    }

    assert service_environment["YAGAPON_MAX_RECORDING_BYTES"] == "0"
    assert effective_environment["YAGAPON_MAX_RECORDING_BYTES"] == "0"
    # Keeping the shared file unchanged is what makes rollback safe: restoring
    # the old Compose file restores its former 4 GiB behavior instead of making
    # old code interpret zero as an immediate recording limit.
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")
    assert '> "$runtime_env"' not in deploy
    assert 'mv -f -- "$runtime_env"' not in deploy


def test_remote_deploy_requires_discord_readiness_not_only_process_liveness():
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")

    assert '"http://127.0.0.1:${host_port}/ready"' in deploy
    assert '"http://127.0.0.1:${host_port}/health"' not in deploy


def test_non_voice_workers_cannot_see_retained_recording_mounts():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    private_paths = {
        "/data/recordings",
        "/data/transcripts",
        "/data/replays",
        "/data/voiceprints",
    }

    assert not private_paths.intersection(services["yagapon"].get("tmpfs", ()))
    for service_name in (
        "knowledge-sync",
        "source-index-watch",
        "knowledge-curation-watch",
        "knowledge-sync-watch",
    ):
        masked = {
            entry.split(":", 1)[0]
            for entry in services[service_name].get("tmpfs", ())
        }
        assert masked == private_paths
        assert services[service_name]["environment"]["DISCORD_TOKEN"] == ""
        assert services[service_name]["environment"]["YAGAPON_API_TOKEN"] == ""


def test_remote_preflight_cannot_read_retained_voice_artifacts():
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")
    preflight = deploy[deploy.index("docker run --rm") : deploy.index("python -m bot.preflight")]

    for path in (
        "/data/recordings",
        "/data/transcripts",
        "/data/replays",
        "/data/voiceprints",
    ):
        assert f"--tmpfs {path}:rw,noexec,nosuid,nodev,mode=000,size=4096" in preflight


def _direct_source_credential_scanner() -> str:
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")
    marker = 'if ! python3 - "$runtime_env" <<\'PY\'\n'
    return deploy.split(marker, 1)[1].split("\nPY\nthen", 1)[0]


def _legacy_recording_cap_scanner() -> str:
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")
    marker = 'if ! python3 - "$runtime_env" <<\'RECORDING_CAP_PY\'\n'
    return deploy.split(marker, 1)[1].split("\nRECORDING_CAP_PY\nthen", 1)[0]


@pytest.mark.parametrize(
    "contents",
    (
        "DISCORD_TOKEN=unrelated\n",
        "YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY=\n",
        'YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY="" # migrated\n',
        "YAGAPON_SOURCE_GITHUB_TOKEN=   # migrated\n",
    ),
)
def test_remote_deploy_allows_absent_or_empty_direct_source_credentials(
    tmp_path,
    contents,
):
    runtime_env = tmp_path / "yagapon.env"
    runtime_env.write_text(contents, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", _direct_source_credential_scanner(), str(runtime_env)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0


@pytest.mark.parametrize(
    "name",
    (
        "YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY",
        "YAGAPON_SOURCE_GITHUB_TOKEN",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY",
    ),
)
def test_remote_deploy_rejects_direct_github_credentials_without_printing_values(
    tmp_path,
    name,
):
    secret = "never-print-this-production-secret"
    runtime_env = tmp_path / "yagapon.env"
    runtime_env.write_text(f"{name}={secret}\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", _direct_source_credential_scanner(), str(runtime_env)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert secret not in result.stdout
    assert secret not in result.stderr


@pytest.mark.parametrize(
    "contents",
    (
        "DISCORD_TOKEN=secret\n",
        "YAGAPON_MAX_RECORDING_BYTES=4294967296\n",
        'export YAGAPON_MAX_RECORDING_BYTES="8589934592" # rollback-safe\n',
    ),
)
def test_first_retention_deploy_keeps_legacy_runtime_cap_rollback_safe(
    tmp_path,
    contents,
):
    runtime_env = tmp_path / "yagapon.env"
    runtime_env.write_text(contents, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", _legacy_recording_cap_scanner(), str(runtime_env)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0


@pytest.mark.parametrize(
    "value",
    ("0", "1", "not-an-integer", '""'),
)
def test_first_retention_deploy_rejects_runtime_cap_unsafe_for_old_image(
    tmp_path,
    value,
):
    runtime_env = tmp_path / "yagapon.env"
    runtime_env.write_text(
        f"YAGAPON_MAX_RECORDING_BYTES={value}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-c", _legacy_recording_cap_scanner(), str(runtime_env)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


def test_remote_deploy_atomically_installs_and_rolls_back_dedicated_knowledge_key():
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")

    assert (
        "knowledge_github_app_key_path=/etc/yagapon/secrets/"
        "knowledge-github-app-private-key.pem"
    ) in deploy
    assert (
        "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_SECRET=//p"
    ) in deploy
    assert (
        'mv -f -- "$knowledge_github_app_key_tmp" '
        '"$knowledge_github_app_key_host_file"'
    ) in deploy
    assert (
        'mv -f -- "$knowledge_rollback_tmp" '
        '"$knowledge_github_app_key_path"'
    ) in deploy
    assert "Knowledge GitHub App secret must contain a valid RSA private key" in deploy
    assert (
        "Knowledge dispatch and source indexing must use separate GitHub Apps, "
        "installations, secrets, and key files"
    ) in deploy


def test_deploy_example_wires_both_dedicated_github_apps():
    documentation = (ROOT / "deploy/README.md").read_text(encoding="utf-8")

    assert (
        "YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY_HOST_FILE="
        "/etc/yagapon/secrets/github-app-private-key.pem"
    ) in documentation
    assert (
        "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_HOST_FILE="
        "/etc/yagapon/secrets/knowledge-github-app-private-key.pem"
    ) in documentation
    assert "source AppとはApp ID、installation ID、" in documentation
