"""Validate the production configuration contract without loading secrets."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
REQUIRED_SERVICES = {
    "db",
    "redis",
    "minio",
    "api",
    "worker",
    "preview-worker",
    "preview",
    "web",
    "migrate",
    "migrate-heads",
    "migrate-rehearsal",
}
APPLICATION_SERVICES = {
    "api",
    "worker",
    "preview-worker",
    "preview",
    "web",
    "migrate",
    "migrate-heads",
    "migrate-rehearsal",
}

# These are read by backend/app/config.py. Listing every setting makes adding
# a runtime setting require an explicit documentation and contract update.
APP_KEYS = {
    "DATABASE_URL",
    "REDIS_URL",
    "S3_ENDPOINT",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_BUCKET",
    "S3_REGION",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_TIMEOUT_SECONDS",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "MCP_CREDENTIAL_KEY",
    "MCP_ALLOWED_PRIVATE_HOSTS",
    "MCP_TIMEOUT_SECONDS",
    "QUESTION_WORKBENCH_URL",
    "QUESTION_WORKBENCH_API_URL",
    "QUESTION_WORKBENCH_SECRET",
    "PDF_PARSER",
    "ALIYUN_ACCESS_KEY_ID",
    "ALIYUN_ACCESS_KEY_SECRET",
    "DOCMIND_TIMEOUT_SECONDS",
    "DOCMIND_POLL_SECONDS",
    "FRONTEND_URL",
    "API_PUBLIC_URL",
    "PREVIEW_DOMAIN",
    "PREVIEW_ENABLED",
    "AUTH_COOKIE_SECURE",
    "AUTH_SESSION_HOURS",
    "AUTH_SECRET_KEY",
    "OIDC_NAME",
    "OIDC_METADATA_URL",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_STARTTLS",
    "SITE_CONFIG_ENCRYPTION_KEY",
    "ZHIFUX_ENABLED",
    "ZHIFUX_API_BASE_URL",
    "ZHIFUX_MERCHANT_NUM",
    "ZHIFUX_SECRET",
    "ZHIFUX_PAY_TYPE",
    "ZHIFUX_PAYMENT_METHODS_JSON",
    "RECHARGE_PACKAGES_JSON",
    "ZHIFUX_CUSTOM_RECHARGE_ENABLED",
    "ZHIFUX_CUSTOM_CREDITS_PER_YUAN",
    "EZFP_ENABLED",
    "EZFP_API_BASE_URL",
    "EZFP_PID",
    "EZFP_PAY_TYPE",
    "EZFP_PAYMENT_METHODS_JSON",
    "EZFP_PRIVATE_KEY",
    "EZFP_PUBLIC_KEY",
    "EZFP_RECHARGE_PACKAGES_JSON",
    "EZFP_CUSTOM_RECHARGE_ENABLED",
    "EZFP_CUSTOM_CREDITS_PER_YUAN",
    "USAGE_AUTHORIZATION_HOLD_CREDITS",
    "WORKSPACE_EXECUTOR_URL",
    "RESOURCE_CACHE_DIR",
    "RESOURCE_CACHE_MAX_BYTES",
    "RESOURCE_CACHE_IDLE_SECONDS",
    "WORKSPACE_RUNNER_IMAGE",
    "WORKSPACE_FILE_RETENTION_DAYS",
    "WORKSPACE_FILE_QUOTA_MB",
    "PREVIEW_CACHE_MAX_BYTES",
    "PREVIEW_CACHE_IDLE_SECONDS",
    "DEPLOY_CONTROL_TOKEN",
    "APP_VERSION",
    "GIT_COMMIT",
    "CONFIG_CONTRACT",
    "DEPLOYMENT_ROLLBACK",
}

REQUIRED_APP_KEYS = {
    "DATABASE_URL",
    "REDIS_URL",
    "FRONTEND_URL",
    "API_PUBLIC_URL",
    "AUTH_COOKIE_SECURE",
    "AUTH_SECRET_KEY",
    "SITE_CONFIG_ENCRYPTION_KEY",
    "MCP_CREDENTIAL_KEY",
    "PREVIEW_ENABLED",
    "PREVIEW_DOMAIN",
    "DEPLOY_CONTROL_TOKEN",
    "APP_VERSION",
    "GIT_COMMIT",
    "CONFIG_CONTRACT",
    "DEPLOYMENT_ROLLBACK",
}

GENERATED_APP_KEYS = {
    "APP_VERSION",
    "GIT_COMMIT",
    "CONFIG_CONTRACT",
    "DEPLOYMENT_ROLLBACK",
}

DEPLOYMENT_KEYS = {
    "POSTGRES_PASSWORD",
    "ZHIKU_APP_ENV_FILE",
    "RUSTFS_ACCESS_KEY",
    "RUSTFS_SECRET_KEY",
}

GENERATED_COMPOSE_KEYS = {
    "API_IMAGE",
    "WORKER_IMAGE",
    "WEB_IMAGE",
    "APP_VERSION",
    "GIT_COMMIT",
    "CONFIG_CONTRACT",
    "DEPLOYMENT_ROLLBACK",
    "MIGRATION_DATABASE_URL",
}

COMPOSE_KEYS = DEPLOYMENT_KEYS | GENERATED_COMPOSE_KEYS | {
    "PREVIEW_CACHE_MAX_BYTES",
    "PREVIEW_CACHE_IDLE_SECONDS",
}

COMPOSE_VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
DIGEST_IMAGE = re.compile(
    r"^(?:[a-z0-9.-]+(?::[0-9]+)?/)?[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$"
)


def fail(message: str) -> None:
    raise ValueError(message)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            fail(f"{path.name}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            fail(f"{path.name}:{number}: invalid env key: {key}")
        if key in values:
            fail(f"{path.name}:{number}: duplicate env key: {key}")
        values[key] = value
    return values


def validate(
    compose_path: Path,
    app_template: Path,
    deployment_template: Path | None = None,
) -> list[str]:
    """Validate Compose plus the separate application/deployment tables."""
    compose_text = compose_path.read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(services, dict):
        fail("compose must contain a services mapping")
    missing = REQUIRED_SERVICES - set(services)
    unknown = set(services) - REQUIRED_SERVICES
    if missing or unknown:
        fail(f"service contract mismatch: missing={sorted(missing)} unknown={sorted(unknown)}")

    for name, service in services.items():
        if not isinstance(service, dict):
            fail(f"{name}: service must be a mapping")
        if "build" in service:
            fail(f"{name}: build is forbidden in production")
        image = service.get("image")
        if not isinstance(image, str) or not image:
            fail(f"{name}: image is required")
        if "${" not in image and not DIGEST_IMAGE.match(image):
            fail(f"{name}: image must use an immutable sha256 digest")
        if ":latest" in image or (
            ":" in image.rsplit("/", 1)[-1]
            and "@" not in image
            and "${" not in image
        ):
            fail(f"{name}: floating image tags are forbidden")
        if name in APPLICATION_SERVICES:
            volumes = service.get("volumes", [])
            if any(isinstance(item, dict) and item.get("type") == "bind" for item in volumes):
                fail(f"{name}: source bind mounts are forbidden")

    if services["api"].get("ports") != ["127.0.0.1:8000:8000"]:
        fail("api must only bind to loopback")
    if services["web"].get("ports") != ["127.0.0.1:3000:3000"]:
        fail("web must only bind to loopback")
    if services["preview"].get("ports") != ["127.0.0.1:8001:8001"]:
        fail("preview must only bind to loopback")
    if services["db"].get("ports") or services["redis"].get("ports") or services["minio"].get("ports"):
        fail("infrastructure services must not publish host ports")
    db_environment = services["db"].get("environment", {})
    if not isinstance(db_environment, dict):
        fail("db environment must be a mapping")
    if db_environment.get("POSTGRES_PASSWORD") != "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}":
        fail("db password must be required from the protected environment")

    compose_keys = set(COMPOSE_VARIABLE.findall(compose_text))
    undocumented = compose_keys - COMPOSE_KEYS
    if undocumented:
        fail(f"Compose uses undocumented variables: {sorted(undocumented)}")

    app_values = parse_env(app_template)
    unknown_app = set(app_values) - APP_KEYS
    if unknown_app:
        fail(f"application env table contains undocumented keys: {sorted(unknown_app)}")
    missing_app = REQUIRED_APP_KEYS - set(app_values)
    if missing_app:
        fail(f"application env table is missing required keys: {sorted(missing_app)}")

    if deployment_template is None:
        fail("a separate deployment env table is required")
    deployment_values = parse_env(deployment_template)
    application_keys = set(deployment_values) - DEPLOYMENT_KEYS
    if application_keys:
        fail(
            "deployment env table must not contain application keys: "
            f"{sorted(application_keys)}"
        )
    missing_deployment = DEPLOYMENT_KEYS - set(deployment_values)
    if missing_deployment:
        fail(f"deployment env table is missing keys: {sorted(missing_deployment)}")

    # deploy_release.py derives these from app.env while generating
    # .env.release. They must not be duplicated in the operator template.
    generated_compose_values = {
        key: app_values[key]
        for key in ("PREVIEW_CACHE_MAX_BYTES", "PREVIEW_CACHE_IDLE_SECONDS")
        if key in app_values
    }
    effective_compose_values = {**deployment_values, **generated_compose_values}
    missing_compose = COMPOSE_KEYS - set(effective_compose_values) - GENERATED_COMPOSE_KEYS
    if missing_compose:
        fail(f"deployment env table does not cover Compose: {sorted(missing_compose)}")
    return sorted(APP_KEYS | COMPOSE_KEYS)


def audit_server_env(app_path: Path, deployment_path: Path | None = None) -> dict:
    """Report missing/unknown keys from protected files without exposing values."""
    values = parse_env(app_path)
    result = {
        "app_keys": len(values),
        "unknown": sorted(set(values) - APP_KEYS),
        "missing_required": sorted(
            REQUIRED_APP_KEYS - GENERATED_APP_KEYS - set(values)
        ),
        "missing_optional": sorted(set(APP_KEYS) - REQUIRED_APP_KEYS - set(values)),
    }
    if deployment_path is not None and deployment_path.is_file():
        deployment_values = parse_env(deployment_path)
        result.update(
            {
                "deployment_keys": len(deployment_values),
                "deployment_unknown": sorted(
                    set(deployment_values) - (DEPLOYMENT_KEYS | GENERATED_COMPOSE_KEYS)
                ),
                "deployment_missing": sorted(
                    DEPLOYMENT_KEYS - set(deployment_values)
                ),
            }
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", type=Path, default=ROOT / "compose.production.yml")
    parser.add_argument("--app-template", type=Path)
    parser.add_argument(
        "--deployment-template",
        type=Path,
    )
    parser.add_argument(
        "--audit-app-env",
        type=Path,
        help="audit a protected app.env without printing values",
    )
    parser.add_argument(
        "--audit-deployment-env",
        type=Path,
        help="audit a generated .env.release without printing values",
    )
    args = parser.parse_args()
    try:
        if args.audit_app_env:
            report = audit_server_env(args.audit_app_env, args.audit_deployment_env)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 1 if report["unknown"] or report["missing_required"] else 0
        keys = validate(
            args.compose,
            args.app_template or ROOT / "app.env.example",
            args.deployment_template or ROOT / "deployment.env.example",
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"deployment contract invalid: {exc}", file=sys.stderr)
        return 1
    print(f"deployment contract valid ({len(keys)} documented variables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
