"""Manually publish a clean test branch, then deploy its digest-pinned package."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request

PROJECT = "zhiku-private-test"
PUBLIC_URL = "https://test.zk.moonlitstarway.top"
SERVICES = ("api", "worker", "preview", "web")
IMAGES = {"api": "API_IMAGE", "web": "WEB_IMAGE"}
BUILDS = {
    "api": ("backend", "backend/Dockerfile", "zhiku-api"),
    "web": ("frontend", "deploy/testing/Dockerfile.web", "zhiku-web"),
}
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST_IMAGE = re.compile(r"[a-z0-9][a-z0-9.:-]*/[a-z0-9_./-]+@sha256:[0-9a-f]{64}")
VERSION = re.compile(r"test-[0-9a-f]{12}-[0-9]{8}T[0-9]{12}Z")


class ReleaseError(RuntimeError):
    pass


def run(args, *, cwd=None, stdin=None, stdout=None, timeout=1800, log=None):
    """Keep arbitrary subprocess output (including configuration) out of logs."""
    result = subprocess.run(
        [str(arg) for arg in args], cwd=cwd, stdin=stdin,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout,
    )
    if log is not None:
        with Path(log).open("ab") as target:
            os.chmod(log, 0o600)
            target.write((json.dumps([str(arg) for arg in args]) + "\n").encode())
            if result.stdout is not None:
                target.write(result.stdout)
            target.write(result.stderr)
            target.write(b"\n")
    if result.returncode:
        suffix = f"; see protected log {log}" if log else ""
        raise ReleaseError(f"{Path(str(args[0])).name} failed (exit {result.returncode}){suffix}")
    return result.stdout.decode("utf-8").strip() if result.stdout is not None else ""


def validate_manifest(document):
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ReleaseError("invalid test release manifest")
    if not isinstance(document.get("commit"), str) or not SHA.fullmatch(document["commit"]):
        raise ReleaseError("invalid release commit")
    version = document.get("version", "")
    if not isinstance(version, str) or not VERSION.fullmatch(version) or not version.startswith("test-" + document["commit"][:12] + "-"):
        raise ReleaseError("invalid test release version")
    images = document.get("images")
    if not isinstance(images, dict) or set(images) != set(IMAGES):
        raise ReleaseError("test release requires API and Web images")
    if any(not isinstance(image, str) or not DIGEST_IMAGE.fullmatch(image) for image in images.values()):
        raise ReleaseError("all test images must be pinned by digest")
    if not isinstance(document.get("alembic_head"), str) or not re.fullmatch(r"[a-zA-Z0-9_]+", document["alembic_head"]):
        raise ReleaseError("invalid Alembic head")
    return document


def source_commit(repo):
    # A normal PR merge is the only entry to the source branch. Never fetch PR
    # head refs or silently publish the operator's unrelated current checkout.
    if run(["git", "branch", "--show-current"], cwd=repo) != "test":
        raise ReleaseError("check out the test branch before publishing")
    if run(["git", "status", "--porcelain"], cwd=repo):
        raise ReleaseError("the test checkout must be clean")
    run(["git", "fetch", "--no-tags", "origin", "refs/heads/test"], cwd=repo)
    commit = run(["git", "rev-parse", "HEAD"], cwd=repo)
    if not SHA.fullmatch(commit) or commit != run(["git", "rev-parse", "FETCH_HEAD"], cwd=repo):
        raise ReleaseError("local test must match the pushed origin/test commit")
    return commit


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def package(release):
    names = ("compose.yml", "release.py", "release.json")
    lines = [
        f"{checksum(release / name)}  {name}"
        for name in names
    ]
    (release / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    archive = release.parent / (release.name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as stream:
        for name in (*names, "SHA256SUMS"):
            stream.add(release / name, arcname=release.name + "/" + name)
    archive_checksum = checksum(archive)
    archive.with_name(archive.name + ".sha256").write_text(
        f"{archive_checksum}  {archive.name}\n", encoding="utf-8",
    )
    return archive


def publish(repo, output, registry, organization):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.:-]*", registry):
        raise ReleaseError("invalid registry hostname")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", organization):
        raise ReleaseError("invalid registry organization")
    commit = source_commit(repo)
    version = f"test-{commit[:12]}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"
    release = output / version
    release.mkdir(parents=True, exist_ok=False)
    # Freeze tracked source before a long build. Later edits and ignored local
    # env files must never enter an image attributed to this commit.
    source_archive = release / "source.tar"
    run(["git", "archive", "--format=tar", "--output", source_archive, commit], cwd=repo)
    source = release / "source"
    source.mkdir()
    with tarfile.open(source_archive) as stream:
        stream.extractall(source, filter="data")
    document = {
        "schema_version": 1, "version": version, "commit": commit, "images": {},
        "alembic_head": run([sys.executable, "deploy/alembic_head.py", "backend"], cwd=source),
    }
    for service, (context, dockerfile, repository) in BUILDS.items():
        print(f"Building and pushing {service} for {commit[:12]}", flush=True)
        image = f"{registry}/{organization}/{repository}"
        metadata = release / f"{service}.build.json"
        run([
            "docker", "buildx", "build", "--platform", "linux/amd64",
            "--file", dockerfile, "--build-arg", f"GIT_COMMIT={commit}",
            "--provenance=false", "--sbom=false",
            "--output", "type=image,push=true,oci-mediatypes=false",
            "--metadata-file", metadata, "--tag", f"{image}:{version}", context,
        ], cwd=source, timeout=7200, log=release / f"{service}.build.log")
        digest = json.loads(metadata.read_text(encoding="utf-8")).get("containerimage.digest", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ReleaseError(f"{service} did not publish a digest")
        document["images"][service] = f"{image}@{digest}"
    validate_manifest(document)
    for name in ("compose.yml", "release.py"):
        shutil.copy2(source / "deploy" / "testing" / name, release / name)
    write_json(release / "release.json", document)
    archive = package(release)
    print(f"Published {version}\nPackage: {archive}\nChecksum: {archive}.sha256")
    return archive


def verify_package(release):
    expected = {"compose.yml", "release.py", "release.json"}
    rows = (release / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    found = set()
    for row in rows:
        expected_checksum, name = row.split("  ", 1)
        if name not in expected or name in found:
            raise ReleaseError("invalid package checksum list")
        if checksum(release / name) != expected_checksum:
            raise ReleaseError("test package checksum mismatch")
        found.add(name)
    if found != expected:
        raise ReleaseError("incomplete test package")
    return validate_manifest(json.loads((release / "release.json").read_text(encoding="utf-8")))


def release_environment(previous, manifest):
    if not previous.is_file() or stat.S_IMODE(previous.stat().st_mode) != 0o600:
        raise ReleaseError("previous release .env must exist with mode 600")
    # Preserve server-owned values verbatim, including quoted Compose secrets.
    replacements = {
        **{IMAGES[service]: image for service, image in manifest["images"].items()},
        "APP_VERSION": manifest["version"], "GIT_COMMIT": manifest["commit"],
    }
    lines = [
        line for line in previous.read_text(encoding="utf-8").splitlines()
        if line.split("=", 1)[0].strip() not in replacements
    ]
    return "\n".join(lines + [f"{key}={value}" for key, value in replacements.items()]) + "\n"


def probe(url, *, as_json=True):
    request = urllib.request.Request(url, headers={"User-Agent": "zhiku-test-deploy/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        if response.status != 200:
            raise ReleaseError("test endpoint is not healthy")
        body = response.read()
    if not body:
        raise ReleaseError("test endpoint returned an empty response")
    return json.loads(body) if as_json else {}


class Deployment:
    def __init__(self, root, release, manifest):
        self.root, self.release, self.manifest = root, release, manifest
        self.evidence = {"version": manifest["version"], "commit": manifest["commit"], "status": "pending"}

    def compose(self, *args, **kwargs):
        kwargs.setdefault("log", self.release / "deploy.log")
        kwargs.setdefault("timeout", 120)
        return run([
            "docker", "compose", "--project-name", PROJECT,
            "--project-directory", self.release, "--env-file", self.release / ".env",
            "--file", self.release / "compose.yml", *args,
        ], **kwargs)

    def record(self, **values):
        self.evidence.update(values)
        write_json(self.release / "evidence.json", self.evidence)

    def revision(self):
        return self.compose(
            "exec", "-T", "db", "psql", "-U", "rag_test", "-d", "rag_test",
            "-Atqc", "SELECT version_num FROM alembic_version",
        )

    def preflight(self):
        self.compose("config", "--quiet")
        # Adopt only the existing test deployment. Never start a new DB, Redis,
        # network or volume just because a path/name was mistyped.
        for service, volume, destination in (
            ("db", PROJECT + "_postgres", "/var/lib/postgresql/data"),
            ("redis", PROJECT + "_redis", "/data"),
        ):
            container = self.compose("ps", "--status", "running", "-q", service)
            if not container or "\n" in container:
                raise ReleaseError(f"existing test {service} must be running")
            mounts = json.loads(run(["docker", "inspect", "--format", "{{json .Mounts}}", container]))
            if not any(m.get("Name") == volume and m.get("Destination") == destination for m in mounts):
                raise ReleaseError(f"existing test {service} volume does not match")
        run(["docker", "volume", "inspect", PROJECT + "_cache"])
        self.record(previous_alembic=self.revision(), status="pulling")
        self.compose("pull", "api", "web", timeout=1800)
        heads = self.compose("run", "--rm", "--no-deps", "migrate", "alembic", "heads")
        if heads.split() != [self.manifest["alembic_head"], "(head)"]:
            raise ReleaseError("API image Alembic head does not match the release")

    def backup(self):
        self.record(status="stopping")
        self.compose("stop", "--timeout", "60", *SERVICES)
        if self.compose("ps", "--status", "running", "-q", *SERVICES):
            raise ReleaseError("application writers are still running")
        directory = self.root / "backups" / self.manifest["version"]
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        backup = directory / "database.dump"
        with backup.open("xb") as output:
            os.chmod(backup, 0o600)
            self.compose("exec", "-T", "db", "pg_dump", "-U", "rag_test", "-d", "rag_test", "-Fc", stdout=output, timeout=1800)
        if not backup.stat().st_size:
            raise ReleaseError("database backup is empty")
        with backup.open("rb") as source:
            self.compose("exec", "-T", "db", "pg_restore", "--list", stdin=source, stdout=subprocess.DEVNULL)
        backup_checksum = checksum(backup)
        backup.with_suffix(".dump.sha256").write_text(f"{backup_checksum}  database.dump\n", encoding="utf-8")
        self.record(backup=str(backup), backup_sha256=backup_checksum, status="backed-up")

    def health(self):
        last = None
        for attempt in range(24):
            try:
                ready = probe("http://127.0.0.1:18180/ready")
                if any(ready.get(key) != "ok" for key in ("status", "database", "redis", "worker")):
                    raise ReleaseError("API, database, Redis or Worker is degraded")
                version = probe("http://127.0.0.1:18180/version")
                if version.get("commit") != self.manifest["commit"] or version.get("version") != self.manifest["version"]:
                    raise ReleaseError("running API version does not match")
                if probe("http://127.0.0.1:18181/health").get("status") != "ok":
                    raise ReleaseError("preview gateway is degraded")
                probe("http://127.0.0.1:18130/", as_json=False)
                for service in SERVICES:
                    container = self.compose("ps", "--status", "running", "-q", service)
                    key = "api" if service in ("api", "worker", "preview") else service
                    image = run(["docker", "inspect", "--format", "{{.Config.Image}}", container])
                    if image != self.manifest["images"][key]:
                        raise ReleaseError(f"{service} image does not match")
                    if service == "worker":
                        hostname = run(["docker", "inspect", "--format", "{{.Config.Hostname}}", container])
                        node = ("tasks" if service == "worker" else "previews") + "@" + hostname
                        replies = json.loads(self.compose(
                            "exec", "-T", service, "celery", "-A", "app.tasks.celery_app",
                            "inspect", "ping", "--destination", node, "--timeout", "5", "--json",
                        ))
                        if replies.get(node, {}).get("ok") != "pong":
                            raise ReleaseError(f"{service} did not respond")
                if self.revision() != self.manifest["alembic_head"]:
                    raise ReleaseError("deployed Alembic revision does not match")
                if probe(PUBLIC_URL + "/api/ready").get("status") != "ok":
                    raise ReleaseError("public test API is degraded")
                if probe(PUBLIC_URL + "/api/version").get("commit") != self.manifest["commit"]:
                    raise ReleaseError("public test domain points to a different commit")
                probe(PUBLIC_URL + "/", as_json=False)
                return
            except (OSError, ValueError, ReleaseError, subprocess.TimeoutExpired) as exc:
                last = exc
                if attempt < 23:
                    time.sleep(5)
        raise ReleaseError("test release health checks failed") from last

    def execute(self):
        try:
            self.preflight()
            self.backup()
            self.record(status="migrating")
            self.compose("run", "--rm", "--no-deps", "migrate", timeout=1800)
            if self.revision() != self.manifest["alembic_head"]:
                raise ReleaseError("migration did not reach the release head")
            self.record(status="starting")
            self.compose("up", "-d", "--no-deps", "--pull", "never", *SERVICES)
            self.health()
            self.record(status="healthy")
        except BaseException:
            # No automatic DB downgrade or data restoration. The backup and
            # previous release remain available for an explicit recovery.
            self.record(failed_step=self.evidence["status"], status="failed")
            raise


def switch_link(path, target):
    temporary = path.with_name(path.name + ".next")
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target, target_is_directory=True)
    temporary.replace(path)


def deploy(root, release, adopt=None):
    import fcntl  # Server-only; the workstation may be Windows.

    root, release = root.resolve(), release.resolve()
    manifest = verify_package(release)
    if release.parent != root / "releases" or release.name != manifest["version"]:
        raise ReleaseError("unpack the package under the test root's releases directory")
    config = root / "config" / "app.env"
    if not config.is_file() or stat.S_IMODE(config.stat().st_mode) != 0o600:
        raise ReleaseError("server-owned config/app.env must exist with mode 600")
    with (root / "deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError("another test deployment is running") from exc
        current = root / "current"
        if current.exists() and not current.is_symlink():
            raise ReleaseError("current must be a managed release symlink")
        prior = root / "previous"
        if prior.exists() and not prior.is_symlink():
            raise ReleaseError("previous must be a managed release symlink")
        previous = current.resolve() if current.is_symlink() else (adopt.resolve() if adopt else None)
        if previous is None or previous.parent != root / "releases" or previous == release:
            raise ReleaseError("supply --adopt-release for the first existing test release")
        if (release / "evidence.json").exists() or (release / ".env").exists():
            raise ReleaseError("release directory was already used; publish a new release")
        values = release_environment(previous / ".env", manifest)
        with (release / ".env").open("x", encoding="utf-8") as target:
            os.chmod(release / ".env", 0o600)
            target.write(values)
        deployment = Deployment(root, release, manifest)
        deployment.record(previous_release=str(previous))
        deployment.execute()
        switch_link(root / "previous", previous)
        switch_link(current, release)
        print(f"Healthy test deployment: {manifest['version']} ({manifest['commit']})")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    publishing = commands.add_parser("publish", help="manually build, push and package origin/test")
    publishing.add_argument("--registry", default="swr.cn-south-1.myhuaweicloud.com")
    publishing.add_argument("--organization", default="str1ct")
    publishing.add_argument("--output", type=Path, default=Path("output/test-releases"))
    deploying = commands.add_parser("deploy", help="run on fsl1 after uploading and verifying the archive")
    deploying.add_argument("--root", type=Path, default=Path("/opt/zhiku-test"))
    deploying.add_argument("--release", type=Path, required=True)
    deploying.add_argument("--adopt-release", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "publish":
            repo = Path(__file__).resolve().parents[2]
            publish(repo, args.output.resolve(), args.registry, args.organization)
        else:
            deploy(args.root, args.release, args.adopt_release)
    except (ReleaseError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # Exception messages from arbitrary tools/HTTP bodies can contain data.
        message = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        print(f"Release stopped: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
