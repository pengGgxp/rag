"""Validate the production deployment contract without loading secrets."""
from __future__ import annotations

import argparse
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
REQUIRED_APP_KEYS = {
    "NEXT_PUBLIC_API_URL",
    "FRONTEND_URL",
    "API_PUBLIC_URL",
    "AUTH_COOKIE_SECURE",
    "AUTH_SECRET_KEY",
    "SITE_CONFIG_ENCRYPTION_KEY",
    "MCP_CREDENTIAL_KEY",
    "PREVIEW_ENABLED",
    "PREVIEW_DOMAIN",
    "APP_VERSION",
    "GIT_COMMIT",
    "CONFIG_CONTRACT",
    "DEPLOYMENT_ROLLBACK",
}
DIGEST_IMAGE = re.compile(
    r"^(?:[a-z0-9.-]+(?::[0-9]+)?/)?[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$"
)


def fail(message: str) -> None:
    raise ValueError(message)


def validate(compose_path: Path, env_template: Path) -> list[str]:
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
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
        if name in {"api", "worker", "preview-worker", "preview", "web", "migrate", "migrate-heads", "migrate-rehearsal"}:
            if "${" not in image and not DIGEST_IMAGE.match(image):
                fail(f"{name}: application image must use a digest")
        elif "${" not in image and not DIGEST_IMAGE.match(image):
            fail(f"{name}: infrastructure image must use a digest")
        if ":latest" in image or (
            ":" in image.rsplit("/", 1)[-1]
            and "@" not in image
            and "${" not in image
        ):
            fail(f"{name}: floating image tags are forbidden")
        if name in {"api", "worker", "preview-worker", "preview", "web", "migrate", "migrate-heads", "migrate-rehearsal"}:
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

    values = {}
    for line in env_template.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            fail(f"invalid env key: {key}")
        values[key] = value
    missing_keys = REQUIRED_APP_KEYS - set(values)
    if missing_keys:
        fail(f"env contract missing keys: {sorted(missing_keys)}")
    return sorted(values)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", type=Path, default=ROOT / "compose.production.yml")
    parser.add_argument("--env-template", type=Path, default=ROOT / "production.env.example")
    args = parser.parse_args()
    try:
        keys = validate(args.compose, args.env_template)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"deployment contract invalid: {exc}", file=sys.stderr)
        return 1
    print(f"deployment contract valid ({len(keys)} environment keys)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
