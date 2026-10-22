from pathlib import Path

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
