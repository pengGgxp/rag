"""Build and verify an immutable production release archive."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
IMAGE_PACKAGES = ("backend", "web")
COPIES = {
    "compose.production.yml": "compose.yml",
    "check_health.py": "check_health.py",
    "deploy_release.py": "deploy_release.py",
    "payment_proxy.py": "payment_proxy.py",
    "alembic_head.py": "alembic_head.py",
    "run_release.sh": "run_release.sh",
    "bootstrap.sh": "bootstrap.sh",
    "maintenance.py": "maintenance.py",
    "prune_registry.py": "prune_registry.py",
    "validate_config.py": "validate_config.py",
    "app.env.example": "app.env.example",
    "deployment.env.example": "deployment.env.example",
    "production.env.reference.md": "production.env.reference.md",
    "nginx/syfc.moonlitstarway.top": "nginx/syfc.moonlitstarway.top",
    "nginx/preview-syfc.moonlitstarway.top": "nginx/preview-syfc.moonlitstarway.top",
    "nginx/zk.str1ct.top.conf": "nginx/zk.str1ct.top.conf",
    "nginx/http-preview.conf": "nginx/http-preview.conf",
    "bin/deploy-release": "bin/deploy-release",
    "bin/health": "bin/health",
    "bin/maintenance": "bin/maintenance",
    "bin/prune-images": "bin/prune-images",
    "bin/rollback": "bin/rollback",
    "bin/retain-releases": "bin/retain-releases",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_text_lf(path: Path, text: str) -> None:
    """Write release metadata identically on Windows and Linux hosts."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def build(
    deploy_root: Path,
    backend_root: Path,
    digests: Path,
    output: Path,
    *,
    version: str,
    commit: str,
    image_prefix: str,
    alembic_head: str,
) -> Path:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("invalid release version")
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("invalid release commit")
    references = {}
    for package in IMAGE_PACKAGES:
        digest = (digests / f"{package}.digest").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"invalid {package} digest")
        references[package] = f"{image_prefix}{package}@{digest}"
    images = {
        "api": references["backend"],
        "worker": references["backend"],
        "web": references["web"],
    }
    staging = output / "release"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for source_name, target_name in COPIES.items():
        source = deploy_root / source_name
        if not source.is_file():
            legacy_names = {
                "nginx/syfc.moonlitstarway.top": "nginx/zk.str1ct.top.conf",
                "nginx/preview-syfc.moonlitstarway.top": "nginx/http-preview.conf",
            }
            source = deploy_root / legacy_names.get(source_name, source_name)
        if not source.is_file():
            raise FileNotFoundError(source)
        target = staging / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(target, source.read_text(encoding="utf-8"))
    shutil.copy2(backend_root / "alembic.ini", staging / "alembic.ini")
    shutil.copytree(backend_root / "alembic", staging / "alembic")
    manifest = {
        "schema_version": 1,
        "version": version,
        "commit": commit,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "images": images,
        "config_contract": 1,
        "alembic_head": alembic_head,
        "database_rollback": os.environ.get(
            "RELEASE_DATABASE_ROLLBACK", "automatic-app-rollback"
        ),
        "database_backup_strategy": "full",
    }
    if manifest["database_rollback"] not in {
        "automatic-app-rollback", "restore-backup-required"
    }:
        raise ValueError("invalid database rollback policy")
    write_text_lf(staging / "release.json", json.dumps(manifest, indent=2) + "\n")
    write_text_lf(
        staging / "release-policy.json",
        json.dumps(
            {
                "schema_version": 1,
                "database_rollback": manifest["database_rollback"],
                "allow_automatic_downgrade": False,
            },
            indent=2,
        )
        + "\n",
    )
    executable = {name for name in COPIES.values() if name.endswith(".sh")} | {
        "bin/deploy-release",
        "bin/health",
        "bin/maintenance",
        "bin/prune-images",
        "bin/rollback",
        "bin/retain-releases",
    }
    for path in staging.rglob("*"):
        if path.is_file():
            relative = path.relative_to(staging).as_posix()
            path.chmod(0o700 if relative in executable else 0o600)
    sums = []
    for path in sorted(p for p in staging.rglob("*") if p.is_file()):
        relative = path.relative_to(staging).as_posix()
        sums.append(f"{sha256(path)}  {relative}")
    write_text_lf(staging / "SHA256SUMS", "\n".join(sums) + "\n")
    archive = output / f"zhiku-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        for path in sorted(staging.rglob("*")):
            handle.add(path, arcname=path.relative_to(staging).as_posix())
    write_text_lf(
        output / f"{archive.name}.sha256",
        f"{sha256(archive)}  {archive.name}\n",
    )
    return archive


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy-root", type=Path, default=Path(__file__).parent)
    parser.add_argument("--backend-root", type=Path, required=True)
    parser.add_argument("--digests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--image-prefix", required=True)
    parser.add_argument("--alembic-head", required=True)
    args = parser.parse_args()
    try:
        build(
            args.deploy_root.resolve(),
            args.backend_root.resolve(),
            args.digests.resolve(),
            args.output.resolve(),
            version=args.version,
            commit=args.commit,
            image_prefix=args.image_prefix,
            alembic_head=args.alembic_head,
        )
    except (OSError, ValueError) as exc:
        print(f"release package failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
