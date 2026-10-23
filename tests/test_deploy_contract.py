import subprocess
import sys
from pathlib import Path

import pytest

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
    health_check = deploy.index('"http://127.0.0.1:${host_port}/health"')
    prune = deploy.index("docker system prune --all --force")

    assert pull < preflight < restart < health_check < prune
    assert "Warning: deployment is healthy but Docker cleanup failed" in deploy


def _direct_source_credential_scanner() -> str:
    deploy = (ROOT / "deploy/remote-deploy.sh").read_text(encoding="utf-8")
    marker = 'if ! python3 - "$runtime_env" <<\'PY\'\n'
    return deploy.split(marker, 1)[1].split("\nPY\nthen", 1)[0]


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
