from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def _compose(path: str) -> dict:
    return yaml.safe_load(_read(path))


def _assert_restricted(service: dict) -> None:
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["tmpfs"] == ["/tmp:rw,nosuid,nodev,size=64m,mode=1777"]


def test_backend_runtime_declares_non_root_alpine_user_and_writable_paths():
    dockerfile = _read("Dockerfile")

    assert "addgroup -S -g 10001 app" in dockerfile
    assert "adduser -S -D -H -u 10001 -G app app" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "PYTHONUNBUFFERED=1" in dockerfile
    assert "HOME=/app/data/home" in dockerfile
    assert "XDG_CACHE_HOME=/app/data/cache" in dockerfile
    assert "TMPDIR=/tmp" in dockerfile
    assert "USER 10001:10001" in dockerfile


def test_backend_runtime_normalizes_private_snapshot_code_permissions_before_user():
    dockerfile = _read("Dockerfile")

    last_copy = dockerfile.index("COPY src /app/src")
    normalize = dockerfile.index(
        "RUN chmod -R a+rX /app/main.py /app/src /app/scripts"
    )
    user = dockerfile.index("USER 10001:10001")
    assert last_copy < normalize < user


def test_frontend_runtime_is_non_root_and_uses_only_tmp_for_runtime_writes():
    dockerfile = _read("frontend/Dockerfile")
    nginx = _read("frontend/nginx.conf")

    assert "pid /tmp/nginx.pid" in dockerfile
    assert "USER 101:101" in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert "listen 8080;" in nginx
    for directory in (
        "/tmp/client_temp",
        "/tmp/proxy_temp",
        "/tmp/fastcgi_temp",
        "/tmp/uwsgi_temp",
        "/tmp/scgi_temp",
    ):
        assert directory in nginx


def test_local_compose_enforces_minimum_runtime_privileges():
    services = _compose("docker-compose.yml")["services"]
    for name in ("xiaohongshu-mcp", "scheduler", "api", "frontend"):
        _assert_restricted(services[name])

    assert services["scheduler"]["user"] == "${RUNTIME_UID:-10001}:${RUNTIME_GID:-10001}"
    assert services["api"]["user"] == services["scheduler"]["user"]
    assert services["frontend"]["user"] == "101:101"
    assert services["frontend"]["ports"] == ["9000:8080"]
    assert "user" not in services["xiaohongshu-mcp"]

    scheduler_config = next(
        volume for volume in services["scheduler"]["volumes"] if "config.yaml" in volume
    )
    api_config = next(
        volume for volume in services["api"]["volumes"] if "config.yaml" in volume
    )
    assert scheduler_config.endswith(":ro")
    assert api_config.endswith(":ro")


def test_docker_context_recursively_excludes_python_and_frontend_caches():
    backend_ignore = _read(".dockerignore")
    frontend_ignore = _read("frontend/.dockerignore")

    assert "**/__pycache__/" in backend_ignore
    assert "**/*.py[cod]" in backend_ignore
    assert "**/node_modules/" in frontend_ignore
    assert "**/dist/" in frontend_ignore
    assert "**/coverage/" in frontend_ignore
