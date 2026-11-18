"""Fail-closed production release orchestration.

The production host runs only digest-pinned images. This module owns the
ordering that matters: maintenance, worker drain, backup, rehearsal,
migration, atomic release switch, health checks and rollback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
REQUIRED_SERVICES = ("api", "worker", "web")
ROLLBACK_POLICIES = {"automatic-app-rollback", "restore-backup-required"}
ALEMBIC_HEAD_SUFFIX = re.compile(r"\s+\(head\)$")
NGINX_FILES = ("zk.str1ct.top.conf", "http-preview.conf")
APPLICATION_SERVICES = ("api", "worker", "preview", "web")
WORKER_SERVICES = ("worker",)
DRAIN_TIMEOUT_SECONDS = 120
DRAIN_POLL_SECONDS = 5
REQUIRED_PACKAGE_FILES = {
    "release.json",
    "compose.yml",
    "check_health.py",
    "deploy_release.py",
    "maintenance.py",
    "prune_registry.py",
    "alembic_head.py",
    "run_release.sh",
    "bootstrap.sh",
    "alembic.ini",
    "release-policy.json",
}
REQUIRED_PACKAGE_FILE_NAMES = {
    "nginx/zk.str1ct.top.conf",
    "nginx/http-preview.conf",
    "bin/deploy-release",
    "bin/health",
    "bin/maintenance",
    "bin/prune-images",
    "bin/rollback",
    "bin/retain-releases",
}
REQUIRED_PACKAGE_PREFIXES = ("alembic/", "nginx/", "bin/")


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    root: Path = Path("/opt/zhiku")
    nginx_root: Path = Path("/www/server/panel/vhost/nginx")

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def incoming(self) -> Path:
        return self.root / "incoming"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def previous(self) -> Path:
        return self.root / "previous"

    @property
    def lock(self) -> Path:
        return self.root / "deploy.lock"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def app_env(self) -> Path:
        return self.config / "app.env"

    @property
    def workbench_env(self) -> Path:
        return self.config / ".env.question-workbench"

    @property
    def nginx(self) -> Path:
        return self.nginx_root


@dataclass(frozen=True)
class MigrationState:
    current: str | None
    head: str
    has_migration: bool


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_manifest(manifest: dict) -> dict:
    if manifest.get("schema_version") != 1:
        raise ReleaseError("unsupported release manifest schema")
    if not VERSION_RE.fullmatch(str(manifest.get("version", ""))):
        raise ReleaseError("invalid release version")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("commit", ""))):
        raise ReleaseError("invalid release commit")
    try:
        datetime.fromisoformat(str(manifest.get("built_at", "")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseError("invalid release build time") from exc
    images = manifest.get("images")
    if not isinstance(images, dict):
        raise ReleaseError("release images are missing")
    for service in REQUIRED_SERVICES:
        if not DIGEST_RE.fullmatch(str(images.get(service, ""))):
            raise ReleaseError(f"{service} image is not pinned by digest")
    if manifest.get("database_rollback") not in ROLLBACK_POLICIES:
        raise ReleaseError("invalid database rollback policy")
    if manifest.get("database_backup_strategy") not in {"full", "none"}:
        raise ReleaseError("invalid database backup strategy")
    if manifest.get("config_contract") != 1:
        raise ReleaseError("unsupported configuration contract")
    if not isinstance(manifest.get("alembic_head"), str) or not manifest["alembic_head"].strip():
        raise ReleaseError("release alembic head is missing")
    return manifest


def load_manifest(release_dir: Path) -> dict:
    manifest_path = release_dir / "release.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid release manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ReleaseError("invalid release manifest")
    return parse_manifest(manifest)


def verify_checksums(release_dir: Path) -> None:
    sums = release_dir / "SHA256SUMS"
    try:
        entries = {}
        for line in sums.read_text(encoding="utf-8").splitlines():
            digest, name = line.split("  ", 1)
            normalized = Path(name[2:] if name.startswith("./") else name)
            if normalized.is_absolute() or ".." in normalized.parts:
                raise ValueError("unsafe checksum path")
            entries[normalized.as_posix()] = digest
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"invalid checksum file: {exc}") from exc
    if not entries:
        raise ReleaseError("release checksum file is empty")
    missing = (REQUIRED_PACKAGE_FILES | REQUIRED_PACKAGE_FILE_NAMES) - set(entries)
    for prefix in REQUIRED_PACKAGE_PREFIXES:
        if not any(Path(name).parts[: len(Path(prefix).parts)] == Path(prefix).parts for name in entries):
            missing.add(prefix.rstrip("/"))
    if missing:
        raise ReleaseError(f"release artifact is missing: {', '.join(sorted(missing))}")
    for name, digest in entries.items():
        path = release_dir / name
        if not path.is_file() or path.is_symlink():
            raise ReleaseError(f"release file missing: {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise ReleaseError(f"release checksum mismatch: {name}")


class CommandRunner:
    def _execute(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict | None = None,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            cwd=cwd,
            env=env,
            input=input_bytes,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict | None = None,
        input_bytes: bytes | None = None,
    ) -> str:
        completed = self._execute(args, cwd=cwd, env=env, input_bytes=input_bytes)
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            suffix = f": {detail[-1][:300]}" if detail else ""
            raise ReleaseError(f"command failed: {args[0]} (exit {completed.returncode}){suffix}")
        return completed.stdout.decode("utf-8", "replace")

    def run_bytes(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes:
        completed = self._execute(args, cwd=cwd, env=env, input_bytes=input_bytes)
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            suffix = f": {detail[-1][:300]}" if detail else ""
            raise ReleaseError(f"command failed: {args[0]} (exit {completed.returncode}){suffix}")
        return completed.stdout


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            values[key] = value
    return values


def _replace_env_value(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=.*$", re.MULTILINE)
    line = f"{key}={value}"
    return pattern.sub(line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"


def _database_parts(database_url: str) -> urllib.parse.SplitResult:
    return urllib.parse.urlsplit(database_url.replace("postgresql+psycopg", "postgresql", 1))


def render_environment(release_dir: Path, paths: Paths | None = None) -> dict[str, str]:
    """Copy protected server configuration into a release directory."""
    paths = paths or Paths()
    if not paths.app_env.is_file():
        raise ReleaseError(f"protected environment is missing: {paths.app_env}")
    values = parse_env_file(paths.app_env)
    manifest = load_manifest(release_dir)
    required = (
        "DATABASE_URL",
        "REDIS_URL",
        "AUTH_SECRET_KEY",
        "SITE_CONFIG_ENCRYPTION_KEY",
        "MCP_CREDENTIAL_KEY",
        "NEXT_PUBLIC_API_URL",
        "FRONTEND_URL",
        "API_PUBLIC_URL",
        "AUTH_COOKIE_SECURE",
        "PREVIEW_ENABLED",
        "PREVIEW_DOMAIN",
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ReleaseError(f"protected environment missing: {', '.join(missing)}")
    if values["AUTH_COOKIE_SECURE"].lower() not in {"true", "1", "yes"}:
        raise ReleaseError("AUTH_COOKIE_SECURE must be true in production")
    if not values["NEXT_PUBLIC_API_URL"].startswith("https://"):
        raise ReleaseError("NEXT_PUBLIC_API_URL must use https in production")
    if "localhost" in values["NEXT_PUBLIC_API_URL"] or "127.0.0.1" in values["NEXT_PUBLIC_API_URL"]:
        raise ReleaseError("NEXT_PUBLIC_API_URL must not point to localhost")
    database = _database_parts(values["DATABASE_URL"])
    if not database.username or not database.password or not database.hostname:
        raise ReleaseError("DATABASE_URL must contain a user, password and host")

    app_text = paths.app_env.read_text(encoding="utf-8")
    if not values.get("DEPLOY_CONTROL_TOKEN"):
        # The token must survive across releases because maintenance is turned
        # on through the currently running API before the new release starts.
        values["DEPLOY_CONTROL_TOKEN"] = secrets.token_urlsafe(48)
        app_text = _replace_env_value(
            app_text, "DEPLOY_CONTROL_TOKEN", values["DEPLOY_CONTROL_TOKEN"]
        )
        protected_temporary = paths.app_env.with_name(paths.app_env.name + ".tmp")
        protected_temporary.write_text(app_text, encoding="utf-8")
        protected_temporary.chmod(0o600)
        os.replace(protected_temporary, paths.app_env)
    metadata = {
        "APP_VERSION": manifest["version"],
        "GIT_COMMIT": manifest["commit"],
        "CONFIG_CONTRACT": str(manifest["config_contract"]),
        "DEPLOYMENT_ROLLBACK": manifest["database_rollback"],
    }
    for key, value in metadata.items():
        app_text = _replace_env_value(app_text, key, value)
    release_app_env = release_dir / "app.env"
    release_app_env.write_text(app_text, encoding="utf-8")
    release_app_env.chmod(0o600)
    values.update(metadata)

    workbench_values = {}
    if paths.workbench_env.is_file():
        workbench_values = parse_env_file(paths.workbench_env)
        workbench = release_dir / ".env.question-workbench"
        workbench.write_bytes(paths.workbench_env.read_bytes())
        workbench.chmod(0o600)

    workbench_url = values.get("QUESTION_WORKBENCH_URL") or "https://q.str1ct.top"
    workbench_api_url = (
        values.get("QUESTION_WORKBENCH_API_URL")
        or "http://question-workbench-api:8100"
    )
    # Keep these in app.env as well for the worker and any one-off jobs. They
    # are non-secret selectors; the bridge secret remains only in its own
    # protected env_file.
    if workbench_values.get("QUESTION_WORKBENCH_SECRET"):
        app_text = _replace_env_value(app_text, "QUESTION_WORKBENCH_URL", workbench_url)
        app_text = _replace_env_value(
            app_text, "QUESTION_WORKBENCH_API_URL", workbench_api_url
        )
        release_app_env.write_text(app_text, encoding="utf-8")
        release_app_env.chmod(0o600)
        values.update(
            {
                "QUESTION_WORKBENCH_URL": workbench_url,
                "QUESTION_WORKBENCH_API_URL": workbench_api_url,
            }
        )

    # Compose interpolation must not become a second copy of the protected
    # application environment. Only values that Compose itself needs are
    # written here; application secrets stay in app.env and are mounted only
    # as a service env_file.
    compose_values = {
        "API_IMAGE": manifest["images"]["api"],
        "WORKER_IMAGE": manifest["images"]["worker"],
        "WEB_IMAGE": manifest["images"]["web"],
        "APP_VERSION": manifest["version"],
        "GIT_COMMIT": manifest["commit"],
        "CONFIG_CONTRACT": str(manifest["config_contract"]),
        "DEPLOYMENT_ROLLBACK": manifest["database_rollback"],
        "POSTGRES_PASSWORD": urllib.parse.unquote(database.password),
        "RUSTFS_ACCESS_KEY": values.get("S3_ACCESS_KEY", "minioadmin"),
        "RUSTFS_SECRET_KEY": values.get("S3_SECRET_KEY", "minioadmin"),
        "ZHIKU_APP_ENV_FILE": "app.env",
    }
    release_env = release_dir / ".env.release"
    release_env.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(compose_values.items())),
        encoding="utf-8",
    )
    release_env.chmod(0o600)
    values.update(compose_values)
    return values


def compose_args(release_dir: Path, *, extra_env: Path | None = None) -> list[str]:
    args = [
        "docker",
        "compose",
        "--project-directory",
        str(release_dir),
        "--env-file",
        str(release_dir / ".env.release"),
    ]
    if extra_env:
        args.extend(["--env-file", str(extra_env)])
    args.extend(["-f", str(release_dir / "compose.yml")])
    return args


def current_compose_args(paths: Paths) -> list[str]:
    if not paths.current.exists():
        raise ReleaseError("current release does not exist")
    return compose_args(paths.current.resolve())


def current_state(current: Path) -> dict:
    if not current.is_dir():
        return {}
    try:
        return load_manifest(current.resolve())
    except ReleaseError:
        return {}


def query_alembic(release_dir: Path, runner: CommandRunner, *, compose: list[str] | None = None) -> str | None:
    release_app_env = release_dir / "app.env"
    if not release_app_env.is_file():
        raise ReleaseError("release environment is not rendered")
    command = compose or compose_args(release_dir)
    value = runner.run(
        [
            *command,
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "rag",
            "-d",
            "rag",
            "-Atc",
            "SELECT version_num FROM alembic_version",
        ]
    ).strip()
    return value or None


def migration_state(release_dir: Path, runner: CommandRunner) -> MigrationState:
    manifest = load_manifest(release_dir)
    current = query_alembic(release_dir, runner)
    return MigrationState(current=current, head=manifest["alembic_head"], has_migration=current != manifest["alembic_head"])


def create_backup(release_dir: Path, runner: CommandRunner) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups = release_dir / "backups"
    target = backups / f"database-{stamp}.dump"
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = runner.run_bytes(
        [*compose_args(release_dir), "exec", "-T", "db", "pg_dump", "-U", "rag", "-d", "rag", "-Fc"]
    )
    if not data:
        raise ReleaseError("database backup is empty")
    target.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    checksum = backups / f"{target.name}.sha256"
    checksum.write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    checksum.chmod(0o600)
    target.chmod(0o600)
    return target


def verify_backup_copy(backup: Path) -> None:
    """Detect truncation or storage corruption before any migration runs."""
    if not backup.is_file():
        raise ReleaseError("database backup is missing")
    checksum_path = backup.with_name(backup.name + ".sha256")
    if not checksum_path.is_file():
        raise ReleaseError("database backup checksum is missing")
    try:
        expected = checksum_path.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError) as exc:
        raise ReleaseError("database backup checksum is invalid") from exc
    actual = hashlib.sha256(backup.read_bytes()).hexdigest()
    if actual != expected:
        raise ReleaseError("database backup checksum does not match")
    copy_path = backup.with_name(backup.name + ".verify")
    try:
        shutil.copy2(backup, copy_path)
        if hashlib.sha256(copy_path.read_bytes()).hexdigest() != expected:
            raise ReleaseError("database backup verification copy is corrupt")
    finally:
        copy_path.unlink(missing_ok=True)


def rehearsal_database_url(values: dict[str, str], name: str) -> str:
    parsed = _database_parts(values["DATABASE_URL"])
    user = urllib.parse.quote(urllib.parse.unquote(parsed.username or "rag"), safe="")
    password = urllib.parse.quote(urllib.parse.unquote(parsed.password or ""), safe="")
    host = parsed.hostname or "db"
    port = parsed.port or 5432
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


def rehearse_migration(release_dir: Path, runner: CommandRunner, values: dict[str, str], backup: Path) -> None:
    name = "rag_migration_rehearsal_" + secrets.token_hex(6)
    compose = compose_args(release_dir, extra_env=release_dir / "app.env")
    try:
        runner.run([*compose, "exec", "-T", "db", "createdb", "-U", "rag", name])
        runner.run(
            [
                *compose,
                "exec", "-T", "db", "psql", "-v", "ON_ERROR_STOP=1", "-U", "rag", "-d", name,
                "-c", "CREATE EXTENSION IF NOT EXISTS vector",
            ]
        )
        runner.run(
            [*compose, "exec", "-T", "db", "pg_restore", "--no-owner", "--no-privileges", "-U", "rag", "-d", name],
            input_bytes=backup.read_bytes(),
        )
        rehearsal_env = release_dir / ".env.rehearsal"
        rehearsal_env.write_text(
            f"MIGRATION_DATABASE_URL={rehearsal_database_url(values, name)}\n",
            encoding="utf-8",
        )
        rehearsal_env.chmod(0o600)
        runner.run(
            [
                *compose_args(release_dir, extra_env=rehearsal_env),
                "--profile",
                "migration",
                "run",
                "--rm",
                "--no-deps",
                "migrate-rehearsal",
            ]
        )
        result = runner.run(
            [
                *compose,
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "rag",
                "-d",
                name,
                "-Atc",
                "SELECT version_num FROM alembic_version",
            ]
        ).strip()
        if result != load_manifest(release_dir)["alembic_head"]:
            raise ReleaseError("migration rehearsal did not reach the release head")
    finally:
        try:
            runner.run([*compose, "exec", "-T", "db", "dropdb", "-U", "rag", "--if-exists", "--force", name])
        except ReleaseError:
            pass
        rehearsal_env = release_dir / ".env.rehearsal"
        if rehearsal_env.exists():
            rehearsal_env.unlink()


def migrate_production(release_dir: Path, runner: CommandRunner) -> None:
    migration_env = release_dir / ".env.migration"
    # The migration container receives its credential through the service
    # env_file; only the non-secret target URL selector belongs in Compose
    # interpolation.
    migration_env.write_text("MIGRATION_DATABASE_URL=\n", encoding="utf-8")
    migration_env.chmod(0o600)
    try:
        runner.run(
            [
                *compose_args(release_dir, extra_env=migration_env),
                "--profile",
                "migration",
                "run", "--rm", "--no-deps", "migrate",
            ]
        )
    finally:
        migration_env.unlink(missing_ok=True)
    current = query_alembic(release_dir, runner)
    if current != load_manifest(release_dir)["alembic_head"]:
        raise ReleaseError("production database did not reach the release Alembic head")


def verify_migration_heads(release_dir: Path, runner: CommandRunner) -> None:
    manifest = load_manifest(release_dir)
    actual_heads = {
        ALEMBIC_HEAD_SUFFIX.sub("", line.strip())
        for line in runner.run(
            [
                *compose_args(release_dir),
                "run", "--rm", "--no-deps", "migrate-heads",
            ]
        ).splitlines()
        if line.strip()
    }
    if actual_heads != {manifest["alembic_head"]}:
        raise ReleaseError("release Alembic head does not match its manifest")


def verify_running_digests(release_dir: Path, runner: CommandRunner) -> None:
    """Fail closed if a running application service differs from its manifest."""
    manifest = load_manifest(release_dir)
    expected = {
        "api": manifest["images"]["api"],
        "preview": manifest["images"]["api"],
        "worker": manifest["images"]["worker"],
        "web": manifest["images"]["web"],
    }
    for service, image in expected.items():
        identifier = runner.run(
            [
                *compose_args(release_dir),
                "ps",
                "-q",
                service,
            ]
        ).strip()
        if not identifier:
            continue
        actual = runner.run(
            [
                "docker",
                "inspect",
                identifier,
                "--format",
                "{{.Config.Image}}",
            ]
        ).strip()
        if actual != image:
            raise ReleaseError(
                f"{service} image digest does not match the release manifest"
            )


def expected_alembic_head(release_dir: Path) -> str:
    """Read the packaged Alembic head without starting an application container."""
    manifest = load_manifest(release_dir)
    script = release_dir / "alembic_head.py"
    alembic_ini = release_dir / "alembic.ini"
    alembic_dir = release_dir / "alembic"
    if script.exists() and alembic_ini.exists() and alembic_dir.is_dir():
        completed = subprocess.run(
            [sys.executable, str(script), str(release_dir)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.decode().strip()
    return manifest["alembic_head"]


def maintenance_request(release_dir: Path, active: bool, manifest: dict) -> dict:
    values = parse_env_file(release_dir / "app.env")
    token = values.get("DEPLOY_CONTROL_TOKEN", "")
    if len(token) < 32:
        raise ReleaseError("DEPLOY_CONTROL_TOKEN is missing or too short")
    payload = json.dumps(
        {
            "active": active,
            "release": manifest["version"],
            "message": "系统正在完成版本切换，请稍后刷新" if active else None,
        }
    ).encode()
    try:
        # Execute inside the API container so request.client is true loopback.
        # The control token is only passed via compose exec's stdin epilogue,
        # not argv, logs, or a second env file.
        script = (
            "import json,os,urllib.request;"
            "u='http://127.0.0.1:8000/internal/deploy/maintenance';"
            "d=os.environ['ZHIKU_CONTROL_PAYLOAD'].encode();"
            "h={'Authorization':'Bearer '+os.environ['ZHIKU_CONTROL_TOKEN'],"
            "'Content-Type':'application/json','X-RAG-Request':'1'};"
            "r=urllib.request.urlopen(urllib.request.Request(u,data=d,method='PUT',headers=h),timeout=15);"
            "print(r.read().decode())"
        )
        completed = subprocess.run(
            [
                *compose_args(release_dir), "exec", "-T",
                "-e", "ZHIKU_CONTROL_TOKEN",
                "-e", "ZHIKU_CONTROL_PAYLOAD",
                "api", "python", "-c", script,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "ZHIKU_CONTROL_TOKEN": token,
                "ZHIKU_CONTROL_PAYLOAD": payload.decode(),
            },
            text=True,
        )
        if completed.returncode:
            raise ReleaseError("maintenance control container failed")
        return json.loads(completed.stdout)
    except Exception as exc:
        raise ReleaseError("maintenance control failed") from exc


def nginx_maintenance_enabled(paths: Paths) -> bool:
    conf = paths.nginx / NGINX_FILES[0]
    if not conf.is_file():
        return False
    text = conf.read_text(encoding="utf-8", errors="replace")
    return "deploy-maintenance" in text


def set_nginx_maintenance(paths: Paths, runner: CommandRunner, active: bool) -> None:
    """Fail closed for every public API request without duplicate locations."""
    conf = paths.nginx / NGINX_FILES[0]
    if not conf.is_file():
        raise ReleaseError("system Nginx configuration is missing")
    backup = paths.root / "config" / "nginx-first-takeover.conf"
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if active:
        if not backup.exists():
            shutil.copy2(conf, backup)
            backup.chmod(0o600)
        text = conf.read_text(encoding="utf-8")
        marker = "        # deploy-maintenance\n"
        if marker not in text:
            location = re.search(
                r"^[ \t]*location(?:\s+\^~)?\s+/api/\s*\{", text, re.MULTILINE
            )
            if location is None:
                raise ReleaseError("Nginx API location is missing")
            insert_at = text.find("{", location.start()) + 1
            if insert_at < 0:
                raise ReleaseError("Nginx API location is missing")
            gate = (
                "\n        # deploy-maintenance\n"
                "        default_type application/json;\n"
                "        add_header Retry-After 60 always;\n"
                "        add_header Cache-Control \"no-store\" always;\n"
                "        return 503 '{\"detail\":\"系统正在完成版本切换，请稍后刷新\"}';\n"
            )
            text = text[:insert_at] + gate + text[insert_at:]
            health_exception = (
                "    location = /api/health {\n"
                "        proxy_cache off;\n"
                "        proxy_pass http://127.0.0.1:8000/health;\n"
                "        proxy_http_version 1.1;\n"
                "        proxy_set_header Host $host;\n"
                "        proxy_set_header X-Real-IP $remote_addr;\n"
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "        proxy_set_header X-Forwarded-Proto $scheme;\n"
                "    }\n"
            )
            text = text[:location.start()] + health_exception + text[location.start():]
            conf.write_text(text, encoding="utf-8")
    elif backup.is_file():
        shutil.copy2(backup, conf)
        backup.unlink()
    runner.run(["nginx", "-t"])
    runner.run(["nginx", "-s", "reload"])


def finish_first_takeover_maintenance(paths: Paths) -> None:
    """Drop the temporary Nginx gate without restoring the legacy config."""
    backup = paths.root / "config" / "nginx-first-takeover.conf"
    backup.unlink(missing_ok=True)


def recover_first_takeover(paths: Paths, release_dir: Path, runner: CommandRunner) -> None:
    """Accept a healthy first takeover after an evidence-only failure."""
    if not paths.current.exists() or not paths.previous.exists():
        raise ReleaseError("managed current and previous links are required")
    current = paths.current.resolve()
    if current != release_dir.resolve():
        raise ReleaseError("recovery target is not the current release")
    if paths.previous.resolve() != (current / "legacy-running").resolve():
        raise ReleaseError("recovery target is not a first takeover")
    verify_running_digests(release_dir, runner)
    verify_migration_heads(release_dir, runner)
    manifest = load_manifest(release_dir)
    health = health_check(paths)
    install_nginx(paths, release_dir, runner)
    write_evidence(
        release_dir,
        status="healthy",
        manifest=manifest,
        migration=MigrationState(
            current=expected_alembic_head(release_dir),
            head=manifest["alembic_head"],
            has_migration=False,
        ),
        backup=next(iter(sorted((release_dir / "backups").glob("*.dump"))), None),
        nginx={},
        health=health,
    )
    backup = paths.root / "config" / "nginx-first-takeover.conf"
    backup.unlink(missing_ok=True)
    for name in ("manual-first-takeover-recovery-required",):
        (release_dir / name).unlink(missing_ok=True)


def celery_active_tasks(paths: Paths, runner: CommandRunner, service: str) -> int:
    output = runner.run(
        [
            *current_compose_args(paths),
            "exec",
            "-T",
            service,
            "celery",
            "-A",
            "app.tasks.celery_app",
            "inspect",
            "active",
            "--timeout",
            "10",
            "--json",
        ]
    ).strip()
    if not output:
        return 0
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"could not read active {service} tasks") from exc
    total = 0
    for worker in payload.values():
        active = worker.get("active", []) if isinstance(worker, dict) else []
        total += len(active or [])
    return total


def drain_workers(paths: Paths, runner: CommandRunner) -> None:
    """Stop accepting new work and wait for current work within a fixed bound."""
    if not paths.current.exists():
        return
    deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
    runner.run(
        [
            *current_compose_args(paths),
            "exec",
            "-T",
            "worker",
            "celery",
            "-A",
            "app.tasks.celery_app",
            "control",
            "cancel_consumer",
        ]
    )
    while time.monotonic() < deadline:
        active = sum(celery_active_tasks(paths, runner, service) for service in WORKER_SERVICES)
        if active == 0:
            stop_current_workers(paths, runner)
            return
        time.sleep(DRAIN_POLL_SECONDS)
    raise ReleaseError(
        f"worker drain exceeded {DRAIN_TIMEOUT_SECONDS} seconds; maintenance remains active"
    )


def switch_links(paths: Paths, release_dir: Path) -> Path | None:
    paths.previous.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous_target = paths.current.resolve() if paths.current.exists() else None
    if not previous_target:
        raise ReleaseError(
            "no current release; first takeover must be performed by the controlled cutover runbook"
        )
    if previous_target:
        previous_temporary = paths.root / f".previous-{time.time_ns()}"
        previous_temporary.symlink_to(previous_target, target_is_directory=True)
        os.replace(previous_temporary, paths.previous)
    temporary = paths.root / f".current-{time.time_ns()}"
    temporary.symlink_to(release_dir.resolve(), target_is_directory=True)
    os.replace(temporary, paths.current)
    return previous_target


def restore_current(paths: Paths, target: Path) -> None:
    temporary = paths.root / f".rollback-{time.time_ns()}"
    temporary.symlink_to(target.resolve(), target_is_directory=True)
    os.replace(temporary, paths.current)


def restore_previous_link(paths: Paths, previous: Path | None) -> None:
    """Put the release symlink back after a failed switch."""
    if previous:
        restore_current(paths, previous)
    elif paths.current.exists() or paths.current.is_symlink():
        paths.current.unlink()


def restore_legacy_nginx(paths: Paths, runner: CommandRunner) -> None:
    backup = paths.root / "config" / "nginx-first-takeover.conf"
    if not backup.is_file():
        return
    shutil.copy2(backup, paths.nginx / NGINX_FILES[0])
    backup.unlink()
    runner.run(["nginx", "-t"])
    runner.run(["nginx", "-s", "reload"])


def ensure_release_images(release_dir: Path, runner: CommandRunner) -> None:
    """Resolve immutable application images without refreshing cached digests."""
    manifest = load_manifest(release_dir)
    for image in dict.fromkeys(manifest["images"].values()):
        try:
            runner.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
        except ReleaseError:
            runner.run(["docker", "pull", image])


def compose_up(
    paths: Paths,
    runner: CommandRunner,
    services: tuple[str, ...] | None = None,
    *,
    force_workers: bool = False,
) -> None:
    compose = current_compose_args(paths)
    ensure_release_images(paths.current.resolve(), runner)
    targets = list(services or APPLICATION_SERVICES)
    if force_workers:
        targets = list(dict.fromkeys((*targets, "worker")))
    runner.run(
        [
            *compose,
            "up",
            "-d",
            "--pull",
            "never",
            "--remove-orphans",
            *targets,
        ]
    )
    confirm_running_digests(paths, runner)


def confirm_running_digests(paths: Paths, runner: CommandRunner) -> None:
    """Check actual container image IDs against the current digest manifest."""
    release_dir = paths.current.resolve()
    manifest = load_manifest(release_dir)
    expected = {
        "api": manifest["images"]["api"],
        "preview": manifest["images"]["api"],
        "worker": manifest["images"]["worker"],
        "web": manifest["images"]["web"],
    }
    compose = current_compose_args(paths)
    for service, image in expected.items():
        identifier = runner.run([*compose, "ps", "-q", service]).strip()
        if not identifier:
            if service == "preview":
                continue
            raise ReleaseError(f"{service} container is not running")
        inspected = runner.run(
            [
                "docker",
                "inspect",
                identifier,
                "--format", "{{.Config.Image}}",
            ]
        ).strip()
        if inspected != image:
            raise ReleaseError(f"{service} container is not running its pinned image")


def changed_services(previous: Path | None, release_dir: Path) -> tuple[str, ...]:
    """Recreate only services whose immutable image changed."""
    if previous is None:
        return APPLICATION_SERVICES
    previous_manifest = load_manifest(previous)
    manifest = load_manifest(release_dir)
    before = previous_manifest["images"]
    after = manifest["images"]
    changed: list[str] = []
    if before["api"] != after["api"]:
        changed.extend(("api", "preview"))
    if before["worker"] != after["worker"]:
        changed.append("worker")
    if before["web"] != after["web"]:
        changed.append("web")
    return tuple(changed)


def stop_current_workers(paths: Paths, runner: CommandRunner) -> None:
    """Stop producers before migration without touching the managed stack."""
    if not paths.current.exists():
        return
    runner.run([*current_compose_args(paths), "stop", "-t", "120", *WORKER_SERVICES])


def stop_legacy_producers(runner: CommandRunner) -> None:
    """Stop legacy producers before both stacks share the production database."""
    compose_files = [
        Path("/data1/rag-platform/docker-compose.yml"),
        Path("/data1/rag-platform/docker-compose.deploy.yml"),
        Path("/data1/rag-platform/docker-compose.override.yml"),
        Path("/tmp/web-image-override.yml"),
        Path("/data1/rag-platform/scripts/question-workbench.compose.yml"),
    ]
    args = [
        "docker", "compose",
        "--project-directory", "/data1/rag-platform",
    ]
    for path in compose_files:
        if path.is_file():
            args.extend(["-f", str(path)])
    # The legacy stack did not define a separate preview worker.
    args.extend(["stop", "worker", "preview"])
    runner.run(args)


def start_legacy_producers(runner: CommandRunner) -> None:
    """Best-effort recovery before a first takeover has replaced containers."""
    compose_files = [
        Path("/data1/rag-platform/docker-compose.yml"),
        Path("/data1/rag-platform/docker-compose.deploy.yml"),
        Path("/data1/rag-platform/docker-compose.override.yml"),
        Path("/tmp/web-image-override.yml"),
        Path("/data1/rag-platform/scripts/question-workbench.compose.yml"),
    ]
    args = [
        "docker", "compose",
        "--project-directory", "/data1/rag-platform",
    ]
    for path in compose_files:
        if path.is_file():
            args.extend(["-f", str(path)])
    args.extend(["start", "worker"])
    runner.run(args)
    # The preview gateway was stopped above but is not a task producer for the
    # new stack. Restart it independently so a failed pre-switch attempt leaves
    # the legacy environment as it was.
    preview_args = list(args[:-1]) + ["start", "preview"]
    runner.run(preview_args)


def rollback(paths: Paths, previous: Path, runner: CommandRunner) -> None:
    if not previous.exists():
        raise ReleaseError("no previous release is available")
    restore_current(paths, previous)
    compose_up(paths, runner)


def backup_nginx(paths: Paths, release_dir: Path, runner: CommandRunner) -> dict[str, str]:
    backup = release_dir / "nginx-backup"
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    copied: dict[str, str] = {}
    for name in NGINX_FILES:
        source = paths.nginx / name
        if not source.is_file():
            raise ReleaseError(f"system Nginx file is missing: {name}")
        target = backup / name
        shutil.copy2(source, target)
        copied[name] = str(source)
    runner.run(["nginx", "-t"])
    return copied


def legacy_container_ids(runner: CommandRunner) -> dict[str, str]:
    output = runner.run(
        [
            "docker", "ps", "-a",
            "--filter", "label=com.docker.compose.project=rag-platform",
            "--format", "{{.Label \"com.docker.compose.service\"}}\t{{.ID}}\t{{.Names}}",
        ]
    )
    return {
        service: identifier
        for line in output.splitlines()
        if len(parts := line.split("\t")) == 3
        for service, identifier, _name in [parts]
    }


def install_nginx(paths: Paths, release_dir: Path, runner: CommandRunner) -> None:
    canonical = release_dir / "nginx"
    changed = False
    try:
        for name in NGINX_FILES:
            source = canonical / name
            if not source.is_file():
                raise ReleaseError(f"canonical Nginx file is missing: {name}")
            target = paths.nginx / name
            if not target.is_file() or target.read_bytes() != source.read_bytes():
                shutil.copy2(source, target)
                changed = True
        runner.run(["nginx", "-t"])
        if changed:
            runner.run(["nginx", "-s", "reload"])
    except Exception:
        restore_nginx(paths, release_dir, runner)
        raise


def backup_host_config(paths: Paths, release_dir: Path) -> None:
    """Archive the legacy Compose chain and temporary override for evidence only."""
    target = release_dir / "legacy-config"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidates = [
        Path("/data1/rag-platform/docker-compose.yml"),
        Path("/data1/rag-platform/docker-compose.deploy.yml"),
        Path("/data1/rag-platform/docker-compose.override.yml"),
        Path("/data1/rag-platform/.env"),
        Path("/data1/rag-platform/.env.question-workbench"),
        Path("/data1/rag-platform/scripts/question-workbench.compose.yml"),
        Path("/tmp/web-image-override.yml"),
    ]
    for source in candidates:
        if source.is_file():
            destination = target / source.name
            if destination.exists():
                destination = target / (source.parent.name + "-" + source.name)
            shutil.copy2(source, destination)
            destination.chmod(0o600)


def write_legacy_release(paths: Paths, release_dir: Path, runner: CommandRunner) -> None:
    """Create a read-only compose snapshot of the running legacy stack.

    This is the previous release for the first controlled cutover. It is never
    treated as a managed release, and its missing digest contract means it can
    only be restored through the documented manual rollback command.
    """
    target = release_dir / "legacy-running"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = [
        Path("/data1/rag-platform/docker-compose.yml"),
        Path("/data1/rag-platform/docker-compose.deploy.yml"),
        Path("/data1/rag-platform/docker-compose.override.yml"),
        Path("/tmp/web-image-override.yml"),
        Path("/data1/rag-platform/scripts/question-workbench.compose.yml"),
    ]
    compose_files = [path for path in files if path.is_file()]
    with (target / "compose-files.txt").open("w", encoding="utf-8") as handle:
        for path in compose_files:
            handle.write(str(path) + "\n")
    (target / "containers.txt").write_text(
        runner.run(
            [
                "docker", "ps", "--filter", "label=com.docker.compose.project=rag-platform",
                "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}",
            ]
        ),
        encoding="utf-8",
    )
    (target / "images.txt").write_text(
        runner.run(
            [
                "docker", "image", "ls",
                "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Digest}}",
            ]
        ),
        encoding="utf-8",
    )
    target.chmod(0o700)


def restore_nginx(paths: Paths, release_dir: Path, runner: CommandRunner) -> None:
    backup = release_dir / "nginx-backup"
    restored = False
    for name in NGINX_FILES:
        source = backup / name
        if source.is_file():
            shutil.copy2(source, paths.nginx / name)
            restored = True
    if restored:
        runner.run(["nginx", "-t"])
        runner.run(["nginx", "-s", "reload"])


def health_check(paths: Paths) -> dict:
    current = paths.current.resolve()
    manifest = load_manifest(current)
    script = current / "check_health.py"
    completed = subprocess.run(
        [
            sys.executable, str(script), "--json",
            "--expected-version", manifest["version"],
            "--expected-commit", manifest["commit"],
            "--expected-alembic", manifest["alembic_head"],
        ],
        cwd=current,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ReleaseError(f"release health check failed: {detail[:300]}")
    return json.loads(completed.stdout)


def write_evidence(
    release_dir: Path,
    *,
    status: str,
    manifest: dict,
    migration: MigrationState | None,
    backup: Path | None,
    nginx: dict[str, str] | None,
    health: dict | None,
    error: str | None = None,
) -> None:
    evidence = {
        "status": status,
        "recorded_at": now_iso(),
        "version": manifest["version"],
        "commit": manifest["commit"],
        "images": manifest["images"],
        "database_rollback": manifest["database_rollback"],
        "alembic_before": migration.current if migration else None,
        "alembic_expected": migration.head if migration else manifest["alembic_head"],
        "backup": str(backup) if backup else None,
        "backup_sha256": (
            hashlib.sha256(backup.read_bytes()).hexdigest() if backup and backup.is_file() else None
        ),
        "nginx_backup": nginx or {},
        "health": health,
        "error": error,
        "rollback_command": (
            f"/opt/zhiku/bin/rollback {manifest['version']}"
            if manifest["database_rollback"] == "automatic-app-rollback"
            else "Restore the verified database backup before running any application rollback."
        ),
    }
    evidence["release_package"] = {
        name: hashlib.sha256((release_dir / name).read_bytes()).hexdigest()
        for name in sorted(REQUIRED_PACKAGE_FILES)
        if (release_dir / name).is_file()
    }
    evidence["rollback_commands"] = {
        "application": (
            f"/opt/zhiku/bin/rollback {manifest['version']}"
            if manifest["database_rollback"] == "automatic-app-rollback"
            else None
        ),
        "database": (
            f"Restore {backup} only after stopping writers; verify its .sha256 file first."
            if backup
            else "No database backup was necessary for this release."
        ),
        "legacy": (
            "See legacy-running/compose-files.txt and containers.txt; restore only with an "
            "explicit administrator-approved legacy compose command."
        ),
    }
    runtime_evidence = runtime_evidence_for(release_dir)
    evidence["runtime"] = runtime_evidence
    path = release_dir / "evidence.json"
    path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)


def runtime_evidence_for(release_dir: Path) -> dict:
    """Capture the effective deploy inputs without exposing secret values."""
    compose_path = release_dir / "compose.yml"
    env_path = release_dir / ".env.release"
    manifest = load_manifest(release_dir)
    containers = []
    completed = subprocess.run(
        [
            *compose_args(release_dir),
            "ps",
            "--format",
            "json",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        return {
            "compose_path": str(compose_path),
            "env_path": str(env_path),
            "images": manifest["images"],
            "containers": [],
            "diagnostic": "compose ps was unavailable after the release attempt",
        }
    raw = completed.stdout.strip()
    rows = []
    if raw:
        try:
            parsed = json.loads(raw)
            rows = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    for row in rows:
        containers.append(
            {
                key: row.get(key)
                for key in ("Service", "Name", "Image", "State", "Health")
                if key in row
            }
        )
    result = {
        "compose_path": str(compose_path),
        "env_path": str(env_path),
        "images": manifest["images"],
        "containers": containers,
    }
    try:
        result["compose_sha256"] = sha256_file(compose_path)
        result["release_env_contract"] = sorted(
            key for key in parse_env_file(env_path) if key.isupper()
        )
    except OSError:
        result["evidence_error"] = "compose or release environment disappeared"
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def transactional_deploy(
    paths: Paths,
    release_dir: Path,
    runner: CommandRunner,
    *,
    health_check,
    maintenance_on,
    maintenance_off,
    migrate,
    has_migration: bool,
    rehearse=None,
    takeover: bool = False,
) -> None:
    manifest = load_manifest(release_dir)
    verify_checksums(release_dir)
    values = render_environment(release_dir, paths)
    previous = paths.current.resolve() if paths.current.exists() else None
    migration = None
    backup = None
    nginx = None
    maintenance_active = False
    nginx_maintenance = False
    switched = False
    legacy_stopped = False
    stack_replacement_started = False
    try:
        backup_host_config(paths, release_dir)
        if takeover and not previous:
            write_legacy_release(paths, release_dir, runner)
            backup_nginx(paths, release_dir, runner)
            set_nginx_maintenance(paths, runner, True)
            nginx_maintenance = True
            stop_legacy_producers(runner)
            legacy_stopped = True
        elif previous:
            maintenance_on(manifest)
            maintenance_active = True
            drain_workers(paths, runner)
        else:
            raise ReleaseError("shadow verified; use the explicit takeover action for first cutover")
        migration = migration_state(release_dir, runner)
        if migration.has_migration:
            if manifest.get("database_backup_strategy", "full") == "none":
                raise ReleaseError("a migration release must declare full database backup")
            backup = create_backup(release_dir, runner)
            verify_backup_copy(backup)
            (rehearse or (lambda release, runner, values, backup: rehearse_migration(
                release, runner, values, backup
            )))(release_dir, runner, values, backup)
            migrate(release_dir)
        nginx = {
            name: str(paths.nginx / name)
            for name in NGINX_FILES
            if (paths.nginx / name).is_file()
        }
        release_services = changed_services(previous, release_dir)
        if previous:
            previous = switch_links(paths, release_dir)
        else:
            # First takeover has no managed previous symlink. Point
            # /opt/zhiku/previous at the archived legacy evidence only.
            paths.previous.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = paths.root / f".previous-{time.time_ns()}"
            temporary.symlink_to((release_dir / "legacy-running").resolve(), target_is_directory=True)
            os.replace(temporary, paths.previous)
            previous = release_dir / "legacy-running"
            temporary_current = paths.root / f".current-{time.time_ns()}"
            temporary_current.symlink_to(release_dir.resolve(), target_is_directory=True)
            os.replace(temporary_current, paths.current)
        switched = True
        stack_replacement_started = True
        compose_up(
            paths,
            runner,
            None if takeover else release_services,
            force_workers=maintenance_active,
        )
        verify_migration_heads(release_dir, runner)
        install_nginx(paths, release_dir, runner)
        if maintenance_active:
            maintenance_off(manifest, failed=False)
            maintenance_active = False
        health = health_check(paths)
        evidence_error = None
        try:
            write_evidence(
                release_dir,
                status="healthy",
                manifest=manifest,
                migration=migration,
                backup=backup,
                nginx=nginx,
                health=health,
            )
        except (OSError, ReleaseError, ValueError) as exc:
            # Release acceptance is based on runtime health. Evidence failure
            # is retained as a marker and escalated after maintenance is
            # deliberately cleared, so it cannot leave a healthy stack gated.
            evidence_error = exc
            try:
                (release_dir / "evidence-write-failed.txt").write_text(
                    f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
                )
            except OSError:
                pass
        if nginx_maintenance:
            finish_first_takeover_maintenance(paths)
            nginx_maintenance = False
        if evidence_error:
            raise ReleaseError(
                f"release is healthy but evidence could not be written: {evidence_error}"
            ) from evidence_error
    except Exception as failure:
        if takeover and nginx_maintenance and legacy_stopped and not stack_replacement_started:
            try:
                start_legacy_producers(runner)
            except Exception:
                pass
        maintenance_reenable_failed = False
        if switched and not takeover:
            try:
                # A failed control request may have applied server-side even
                # when its response was lost. Reassert the gate before any
                # rollback or further recovery work.
                maintenance_on(manifest)
                maintenance_active = True
            except Exception as maintenance_failure:
                maintenance_reenable_failed = True
                try:
                    (release_dir / "maintenance-reenable-failure.txt").write_text(
                        f"{type(maintenance_failure).__name__}: {maintenance_failure}\n",
                        encoding="utf-8",
                    )
                except OSError:
                    pass
        if nginx and not takeover:
            try:
                restore_nginx(paths, release_dir, runner)
            except Exception:
                pass
        can_rollback = (
            switched
            and previous
            and not takeover
            and (
                not (migration and migration.has_migration)
                or manifest["database_rollback"] == "automatic-app-rollback"
            )
        )
        rollback_failed = False
        if maintenance_reenable_failed:
            rollback_failed = True
        elif can_rollback:
            try:
                rollback(paths, previous, runner)
                previous_manifest = load_manifest(previous)
                maintenance_off(previous_manifest, failed=True)
                maintenance_active = False
                health_check(paths)
            except Exception as rollback_failure:
                rollback_failed = True
                try:
                    maintenance_on(load_manifest(previous))
                    maintenance_active = True
                except Exception:
                    pass
                (release_dir / "rollback-failure.json").write_text(
                    json.dumps(
                        {
                            "deploy_error": str(failure),
                            "rollback_error": str(rollback_failure),
                            "created_at": now_iso(),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        # restore-backup-required forbids application rollback before the
        # verified database backup is restored. Keep the new release symlink
        # and maintenance state for the explicit recovery procedure.
        elif switched and not takeover and manifest["database_rollback"] == "automatic-app-rollback":
            try:
                restore_previous_link(paths, previous)
            except Exception as rollback_failure:
                rollback_failed = True
                (release_dir / "rollback-failure.json").write_text(
                    json.dumps(
                        {
                            "deploy_error": str(failure),
                            "rollback_error": str(rollback_failure),
                            "created_at": now_iso(),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        try:
            write_evidence(
                release_dir,
                status="failed" if not rollback_failed else "rollback-failed",
                manifest=manifest,
                migration=migration,
                backup=backup,
                nginx=nginx,
                health=None,
                error=str(failure),
            )
        except (OSError, ReleaseError, ValueError):
            # Preserve the original deployment failure. Evidence write errors
            # are surfaced by the failed release directory and never turn a
            # recoverable deployment failure into an unclear rollback result.
            pass
        if takeover and switched:
            # The first takeover never hands traffic back to the overlapping
            # legacy stack. A failed switch leaves evidence and an explicit
            # recovery marker for the administrator.
            try:
                (release_dir / "manual-first-takeover-recovery-required").write_text(
                    "First takeover failed. Inspect evidence.json and containers, "
                    "restore the verified database backup when required, then resume "
                    "traffic explicitly.\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
        if nginx_maintenance and takeover:
            # Even if canonical Nginx was installed before the final evidence
            # write, put the public write gate back and keep it there.
            try:
                set_nginx_maintenance(paths, runner, True)
            except Exception:
                pass
        elif nginx_maintenance:
            try:
                restore_legacy_nginx(paths, runner)
            except Exception:
                pass
        if backup:
            try:
                (release_dir / "restore-command.txt").write_text(
                    "Restore only after stopping writers and verifying "
                    f"{backup}.sha256 with sha256sum -c.\\n"
                    f"pg_restore --clean --if-exists --no-owner --no-privileges "
                    f"-U rag -d rag < {backup}\\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
        if rollback_failed:
            raise ReleaseError(
                "deployment or rollback verification failed; maintenance remains active"
            ) from failure
        if can_rollback:
            (release_dir / "manual-maintenance-release-required").unlink(missing_ok=True)
        elif maintenance_active:
            (release_dir / "manual-recovery-required").write_text(
                "The database rollback policy requires restoring the verified backup "
                "before application rollback. Maintenance remains active.\n",
                encoding="utf-8",
            )
        raise


def shadow_release(
    paths: Paths,
    incoming: Path,
    runner: CommandRunner,
    *,
    inspect_runtime: bool = False,
) -> Path:
    """Validate a release and render protected configuration without switching."""
    if not incoming.is_dir() or incoming.is_symlink():
        raise ReleaseError("incoming release directory is invalid")
    if not incoming.is_relative_to(paths.incoming):
        raise ReleaseError("release must be under /opt/zhiku/incoming")
    verify_checksums(incoming)
    manifest = load_manifest(incoming)
    if incoming.name != manifest["version"]:
        raise ReleaseError("incoming directory must match the release version")
    target = paths.root / "shadow" / incoming.name
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(incoming, target)
    render_environment(target, paths)
    compose = compose_args(target)
    runner.run([*compose, "config", "--quiet"])
    if inspect_runtime:
        ensure_release_images(target, runner)
        verify_migration_heads(target, runner)
    else:
        # The shadow package must be inspectable without starting the release
        # image or reading production credentials. Runtime checks are done by
        # the approved takeover/preflight command.
        verify_checksums(target)
    runner.run(["nginx", "-t"])
    return target


def bootstrap_server(paths: Paths, release_dir: Path) -> None:
    """Install trusted entrypoints and seed protected environment files.

    This action intentionally does not start containers, change Nginx or touch
    the database. It must run from a checksum-verified archive.
    """
    if not release_dir.is_dir() or release_dir.is_symlink():
        raise ReleaseError("bootstrap release directory is invalid")
    verify_checksums(release_dir)
    paths.config.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not paths.app_env.is_file():
        source_env = Path("/data1/rag-platform/.env")
        if not source_env.is_file():
            raise ReleaseError("legacy protected environment is missing")
        temporary = paths.app_env.with_name(paths.app_env.name + ".tmp")
        temporary.write_bytes(source_env.read_bytes())
        temporary.chmod(0o600)
        os.replace(temporary, paths.app_env)
    paths.app_env.chmod(0o600)
    legacy_workbench = Path("/data1/rag-platform/.env.question-workbench")
    if not paths.workbench_env.is_file() and legacy_workbench.is_file():
        temporary = paths.workbench_env.with_name(paths.workbench_env.name + ".tmp")
        temporary.write_bytes(legacy_workbench.read_bytes())
        temporary.chmod(0o600)
        os.replace(temporary, paths.workbench_env)
    if paths.workbench_env.is_file():
        paths.workbench_env.chmod(0o600)
    binaries = paths.root / "bin"
    binaries.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in (
        "deploy-release",
        "health",
        "maintenance",
        "prune-images",
        "rollback",
        "retain-releases",
    ):
        source = release_dir / "bin" / name
        if not source.is_file() or source.is_symlink():
            raise ReleaseError(f"bootstrap entrypoint is missing: {name}")
        temporary = binaries / f".{name}.tmp"
        temporary.write_bytes(source.read_bytes())
        temporary.chmod(0o700)
        os.replace(temporary, binaries / name)
    runner_source = release_dir / "run_release.sh"
    runner_target = binaries / "run-release"
    temporary = binaries / ".run-release.tmp"
    temporary.write_bytes(runner_source.read_bytes())
    temporary.chmod(0o700)
    os.replace(temporary, runner_target)
    bootstrap_target = paths.root / "bootstrap.sh"
    temporary = paths.root / ".bootstrap.sh.tmp"
    temporary.write_bytes((release_dir / "bootstrap.sh").read_bytes())
    temporary.chmod(0o700)
    os.replace(temporary, bootstrap_target)
    runner = CommandRunner()
    runner.run(["nginx", "-t"])
    runner.run(["docker", "compose", "version"])
    paths.incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.releases.mkdir(parents=True, exist_ok=True, mode=0o700)


def deploy_release(paths: Paths, incoming: Path, runner: CommandRunner) -> Path:
    if not incoming.is_dir() or incoming.is_symlink():
        raise ReleaseError("incoming release directory is invalid")
    if not incoming.is_relative_to(paths.incoming):
        raise ReleaseError("release must be under /opt/zhiku/incoming")
    release_dir = paths.releases / incoming.name
    if release_dir.exists():
        raise ReleaseError(f"release already exists: {incoming.name}")
    verify_checksums(incoming)
    manifest = load_manifest(incoming)
    if incoming.name != manifest["version"]:
        raise ReleaseError("incoming directory must match the release version")
    render_environment(incoming, paths)
    ensure_release_images(incoming, runner)
    verify_migration_heads(incoming, runner)
    paths.releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(incoming, release_dir)
    transactional_deploy(
        paths,
        release_dir,
        runner,
        health_check=health_check,
        maintenance_on=lambda manifest: maintenance_request(
            paths.releases / manifest["version"], True, manifest
        ),
        maintenance_off=lambda manifest, failed=False: maintenance_request(
            paths.releases / manifest["version"], False, manifest
        ),
        migrate=lambda release: (verify_migration_heads(release, runner), migrate_production(release, runner)),
        has_migration=True,
    )
    return release_dir


def retain_releases(paths: Paths, current: Path) -> list[Path]:
    """Keep only the current and previous immutable release directories."""
    if not paths.current.exists() or not paths.previous.exists():
        raise ReleaseError("current and previous releases must both exist")
    current_dir = paths.current.resolve()
    previous_dir = paths.previous.resolve()
    if current_dir != current.resolve():
        raise ReleaseError("retention target is not the current release")
    releases_root = paths.releases.resolve()
    if not current_dir.is_relative_to(releases_root):
        raise ReleaseError("release links point outside the managed release directory")
    candidates = {path.resolve() for path in paths.releases.iterdir() if path.is_dir()}
    first_takeover_previous = previous_dir == (current_dir / "legacy-running").resolve()
    if not first_takeover_previous and not previous_dir.is_relative_to(releases_root):
        raise ReleaseError("release links point outside the managed release directory")
    missing = {current_dir} - candidates
    if not first_takeover_previous and previous_dir not in candidates:
        missing.add(previous_dir)
    if missing:
        raise ReleaseError("current or previous release directory is missing")
    obsolete = candidates - {current_dir}
    if not first_takeover_previous:
        obsolete.discard(previous_dir)
    staged = paths.releases / f".retention-{time.time_ns()}"
    staged.mkdir(mode=0o700)
    moved = []
    try:
        for candidate in obsolete:
            destination = staged / candidate.name
            os.replace(candidate, destination)
            moved.append((candidate, destination))
    except Exception:
        for candidate, destination in reversed(moved):
            os.replace(destination, candidate)
        shutil.rmtree(staged, ignore_errors=True)
        raise
    shutil.rmtree(staged)
    retained = {current_dir, previous_dir}
    return sorted(retained)


def takeover_release(paths: Paths, incoming: Path, runner: CommandRunner) -> Path:
    """Perform the one-time, explicitly approved cutover from the legacy stack."""
    shadow_release(paths, incoming, runner, inspect_runtime=True)
    if paths.current.exists():
        raise ReleaseError("first takeover is only valid when no managed current release exists")
    if not incoming.is_dir() or incoming.is_symlink():
        raise ReleaseError("incoming release directory is invalid")
    if not incoming.is_relative_to(paths.incoming):
        raise ReleaseError("release must be under /opt/zhiku/incoming")
    verify_checksums(incoming)
    manifest = load_manifest(incoming)
    if incoming.name != manifest["version"]:
        raise ReleaseError("incoming directory must match the release version")
    release_dir = paths.releases / incoming.name
    if release_dir.exists():
        raise ReleaseError(f"release already exists: {incoming.name}")
    expected_alembic_head(incoming)
    paths.releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(incoming, release_dir)
    transactional_deploy(
        paths,
        release_dir,
        runner,
        health_check=health_check,
        maintenance_on=lambda manifest: (_ for _ in ()).throw(
            ReleaseError("legacy API has no deployment maintenance endpoint")
        ),
        maintenance_off=lambda manifest, failed=False: None,
        migrate=lambda release: (verify_migration_heads(release, runner), migrate_production(release, runner)),
        has_migration=True,
        takeover=True,
    )
    return release_dir


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "deploy", "shadow", "stage", "takeover", "bootstrap", "rollback",
            "retain", "recover-takeover",
        ),
        nargs="?",
        default="deploy",
    )
    parser.add_argument("incoming", type=Path)
    args = parser.parse_args(argv[1:])
    incoming = args.incoming.resolve()
    paths = Paths()
    runner = CommandRunner()
    try:
        if args.action == "bootstrap":
            bootstrap_server(paths, incoming)
            print(f"deployment bootstrap installed: {incoming.name}")
        elif args.action == "shadow":
            target = shadow_release(paths, incoming, runner, inspect_runtime=True)
            print(f"shadow release verified: {target.name}")
        elif args.action == "takeover":
            target = takeover_release(paths, incoming, runner)
            print(f"release taken over: {target.name}")
        elif args.action == "rollback":
            failed = incoming.resolve()
            previous = paths.previous.resolve() if paths.previous.exists() else None
            if not previous:
                raise ReleaseError("previous release is missing")
            evidence = json.loads((failed / "evidence.json").read_text(encoding="utf-8"))
            if evidence.get("database_rollback") == "restore-backup-required":
                raise ReleaseError("this release requires database restore before application rollback")
            if not paths.current.resolve().is_relative_to(paths.releases.resolve()):
                raise ReleaseError("current release is not managed by this deployment contract")
            current = paths.current.resolve()
            current_manifest = load_manifest(current)
            previous_manifest = load_manifest(previous)
            maintenance_request(current, True, current_manifest)
            restore_current(paths, previous)
            compose_up(paths, runner)
            try:
                maintenance_request(previous, False, previous_manifest)
                health_check(paths)
            except Exception:
                try:
                    maintenance_request(previous, True, previous_manifest)
                except Exception:
                    pass
                raise
            print(f"application rolled back to: {previous.name}")
        elif args.action == "retain":
            retained = retain_releases(paths, incoming)
            print("retained releases: " + ", ".join(path.name for path in retained))
        elif args.action == "recover-takeover":
            recover_first_takeover(paths, incoming, runner)
            print(f"first takeover recovered: {incoming.name}")
        elif args.action in {"deploy", "stage"}:
            if args.action == "stage":
                target = shadow_release(paths, incoming, runner)
                print(f"release staged without switching: {target.name}")
            else:
                target = deploy_release(paths, incoming, runner)
                print(f"release deployed: {target.name}")
        return 0
    except (OSError, ReleaseError, ValueError, json.JSONDecodeError) as exc:
        print(f"release failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
