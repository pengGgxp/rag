"""Production health checks with finite retries and fail-closed behavior."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REDIS_HEARTBEAT = "deploy:worker-heartbeat"
HEALTH_USER_AGENT = "zhiku-deploy-health/1.0"


def probe(
    url: str,
    *,
    attempts: int = 12,
    delay: float = 5.0,
    expect_json: bool = False,
    require_status: bool = False,
    headers: dict[str, str] | None = None,
) -> dict:
    last = None
    for _ in range(attempts):
        try:
            request_headers = {"User-Agent": HEALTH_USER_AGENT, **(headers or {})}
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=8) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                body = response.read()
                if not body:
                    raise RuntimeError("empty response")
                if expect_json:
                    data = json.loads(body)
                    if not isinstance(data, dict):
                        raise RuntimeError("JSON response is not an object")
                    if require_status and data.get("status") != "ok":
                        raise RuntimeError("health endpoint is degraded")
                    return data
                return {"status": "ok", "http_status": response.status}
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            last = exc
            time.sleep(delay)
    raise RuntimeError(f"health probe failed: {url}: {last}")


def compose_ps(service: str) -> str:
    """Return a running container id, matching the release's project."""
    cwd = Path("/opt/zhiku/current")
    env_file = cwd / ".env.release"
    try:
        output = subprocess.check_output(
            [
                "docker", "compose", "--project-directory", str(cwd),
                "--env-file", str(env_file), "-f", str(cwd / "compose.yml"),
                "ps", "-q", service,
            ],
            text=True,
            stderr=subprocess.PIPE,
        ).strip().splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"{service} container is not available") from exc
    if not output:
        raise RuntimeError(f"{service} container is not running")
    return output[0]


def compose_exec(service: str, *args: str) -> str:
    cwd = Path("/opt/zhiku/current")
    env_args = [
        "--env-file", str(cwd / ".env.release"),
        "--env-file", str(cwd / "app.env"),
    ]
    if service == "db":
        env_args = ["--env-file", str(cwd / ".env.release")]
    return subprocess.check_output(
        [
            "docker", "compose", "--project-directory", str(cwd),
            *env_args,
            "-f", str(cwd / "compose.yml"),
            "exec", "-T", service, *args,
        ],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def verify_maintenance_control() -> dict:
    """Read control state through the API container's loopback interface."""
    script = (
        "import json,os,urllib.request; "
        "t=os.environ.get('DEPLOY_CONTROL_TOKEN',''); "
        "h={'Authorization':'Bearer '+t,'X-RAG-Request':'1'}; "
        "q=urllib.request.Request("
        "'http://127.0.0.1:8000/internal/deploy/maintenance',headers=h); "
        "print(urllib.request.urlopen(q,timeout=10).read().decode())"
    )
    try:
        output = compose_exec("api", "python", "-c", script)
        status = json.loads(output.splitlines()[-1])
    except (
        OSError,
        ValueError,
        IndexError,
        subprocess.CalledProcessError,
    ) as exc:
        raise RuntimeError("deployment maintenance status is unavailable") from exc
    if not isinstance(status, dict) or status.get("active") is not False:
        raise RuntimeError("deployment maintenance is still active")
    return status


def verify_worker_heartbeat() -> str:
    """Require a fresh broker heartbeat, not just a running container."""
    try:
        ttl = int(compose_exec("redis", "redis-cli", "TTL", REDIS_HEARTBEAT))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("worker heartbeat cannot be read") from exc
    if ttl <= 0:
        raise RuntimeError("worker heartbeat is missing or expired")
    if not compose_ps("worker"):
        raise RuntimeError("worker containers are not running")
    return "fresh"


def verify_redis() -> str:
    try:
        pong = compose_exec("redis", "redis-cli", "PING").strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Redis is unavailable") from exc
    if pong != "PONG":
        raise RuntimeError("Redis did not return PONG")
    return "ok"


def verify_storage() -> str:
    """Verify the configured object store without exposing credentials."""
    try:
        output = compose_exec(
            "api",
            "python",
            "-c",
            (
                "from app.db import SessionLocal; "
                "from app.storage_backend import active_settings, configured_backend; "
                "db=SessionLocal(); row=active_settings(db); "
                "result='skipped' if row is None else ("
                "'unexpected' if configured_backend(row).head('_deploy/health-missing') "
                "is not None else 'ok'); "
                "db.close(); print(result)"
            ),
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("configured object storage is unavailable") from exc
    result = output.splitlines()[-1] if output else ""
    if result == "unexpected":
        raise RuntimeError("object storage health probe unexpectedly found a missing object")
    if result not in {"skipped", "ok"}:
        raise RuntimeError("configured object storage is unavailable")
    return result


def verify_question_workbench() -> str:
    """Check the authenticated workbench bridge on its private network."""
    cwd = Path("/opt/zhiku/current")
    env_file = cwd / ".env.release"
    values = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
    app_env = cwd / "app.env"
    if app_env.is_file():
        for line in app_env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
    workbench_env = cwd / ".env.question-workbench"
    if workbench_env.is_file():
        for line in workbench_env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
    if not values.get("QUESTION_WORKBENCH_API_URL"):
        return "skipped"
    configuration = None
    for service in ("api", "worker"):
        try:
            identifier = compose_ps(service)
            output = subprocess.check_output(
                ["docker", "inspect", identifier, "--format", "{{json .Config.Env}}"],
                text=True,
                stderr=subprocess.PIPE,
            )
            environment = dict(item.split("=", 1) for item in json.loads(output))
        except (OSError, ValueError, TypeError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"{service} question workbench configuration is unavailable") from exc
        current = tuple(environment.get(key, "") for key in (
            "QUESTION_WORKBENCH_URL", "QUESTION_WORKBENCH_API_URL", "QUESTION_WORKBENCH_SECRET",
        ))
        if not all(current) or len(current[2]) < 32:
            raise RuntimeError(f"{service} question workbench configuration is incomplete")
        if configuration is not None and current != configuration:
            raise RuntimeError("API and worker question workbench configurations differ")
        configuration = current
    command = [
        "python", "-c",
        "import os,urllib.request; "
        "u=os.environ['QUESTION_WORKBENCH_API_URL'].rstrip('/'); "
        "q=urllib.request.Request("
        "u+'/api/health',headers={'User-Agent':'zhiku-deploy-health/1.0'}); "
        "print(urllib.request.urlopen(q,timeout=5).status)"
    ]
    for service in ("api", "worker"):
        try:
            output = compose_exec(service, *command).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"question workbench is unavailable from {service}") from exc
        if not output or output.splitlines()[-1] != "200":
            raise RuntimeError(f"question workbench is unavailable from {service}")
    return "ok"


def check_all(
    base_url: str = "http://127.0.0.1",
    *,
    expected_version: str = "",
    expected_commit: str = "",
    expected_alembic: str = "",
    public_base_url: str = "https://zk.str1ct.top",
) -> dict:
    current = Path("/opt/zhiku/current")
    api = probe(f"{base_url}:8000/ready", expect_json=True, require_status=True)
    version = probe(f"{base_url}:8000/version", expect_json=True)
    maintenance = verify_maintenance_control()
    if expected_version and version.get("version") != expected_version:
        raise RuntimeError("API version does not match the release")
    if expected_commit and version.get("commit") != expected_commit:
        raise RuntimeError("API commit does not match the release")
    result = {
        "api": api,
        "api_version": version,
        "maintenance": maintenance,
        "resources": probe(f"{base_url}:3000/resources", attempts=24),
        "checkout": probe(f"{base_url}:3000/payments/checkout", attempts=24),
        "web": probe(f"{base_url}:3000/", attempts=24),
        "preview": probe(
            f"{base_url}:8001/health", expect_json=True, require_status=True
        ),
    }
    public_health = probe(
        f"{public_base_url.rstrip('/')}/api/health",
        expect_json=True,
        require_status=True,
    )
    if public_health.get("database") not in {None, "ok"}:
        raise RuntimeError("public API database dependency is unavailable")
    result["public_api"] = public_health
    if expected_alembic:
        cwd = Path("/opt/zhiku/current")
        output = subprocess.check_output(
            [
                "docker", "compose", "--project-directory", str(cwd),
                "--env-file", str(cwd / ".env.release"),
                "-f", str(cwd / "compose.yml"),
                "exec", "-T", "db", "psql", "-U", "rag", "-d", "rag", "-Atc",
                "SELECT version_num FROM alembic_version",
            ],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
        if output != expected_alembic:
            raise RuntimeError("database Alembic version does not match the release")
        result["alembic"] = output
    result["question_workbench"] = verify_question_workbench()
    result["redis_ping"] = verify_redis()
    result["storage"] = verify_storage()
    result["worker"] = verify_worker_heartbeat()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1")
    parser.add_argument("--public-base-url", default="https://zk.str1ct.top")
    parser.add_argument("--expected-version", default="")
    parser.add_argument("--expected-commit", default="")
    parser.add_argument("--expected-alembic", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = check_all(
            args.base_url,
            expected_version=args.expected_version,
            expected_commit=args.expected_commit,
            expected_alembic=args.expected_alembic,
            public_base_url=args.public_base_url,
        )
    except Exception as exc:
        print(f"health check failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False) if args.json else "health checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
