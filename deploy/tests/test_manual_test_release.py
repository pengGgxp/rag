"""Tests for the manually invoked test release path; no registry credentials."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "testing"
SPEC = importlib.util.spec_from_file_location("manual_test_release", SOURCE / "release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def manifest():
    return {
        "schema_version": 1,
        "version": "test-" + "a" * 12 + "-20261117T040000000000Z",
        "commit": "a" * 40,
        "alembic_head": "0076_explicit_cloud_computers",
        "images": {
            service: f"registry.example.test/team/{service}@sha256:" + "b" * 64
            for service in release.IMAGES
        },
    }


@unittest.skipUnless(shutil.which("git"), "Git is unavailable")
class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.repo, self.origin = root / "checkout", root / "origin.git"
        self.git("init", "--bare", str(self.origin), cwd=root)
        self.git("init", "--initial-branch=test", str(self.repo), cwd=root)
        self.git("config", "user.email", "release-fixture@example.test")
        self.git("config", "user.name", "release fixture")
        (self.repo / "source.txt").write_text("source\n", encoding="utf-8")
        self.git("add", "source.txt")
        self.commit()
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "origin", "test")

    def git(self, *args, cwd=None):
        return release.run(["git", *args], cwd=cwd or self.repo)

    def commit(self):
        self.git("commit", "-m", "test: create release fixture", "-m", "Record fixture source for release identity validation.")

    def test_accepts_exact_pushed_test_commit(self):
        self.assertEqual(release.source_commit(self.repo), self.git("rev-parse", "HEAD"))

    def test_rejects_feature_branch(self):
        self.git("switch", "-c", "feature")
        with self.assertRaisesRegex(release.ReleaseError, "test branch"):
            release.source_commit(self.repo)

    def test_rejects_uncommitted_source(self):
        (self.repo / "source.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(release.ReleaseError, "clean"):
            release.source_commit(self.repo)

    def test_rejects_untracked_source(self):
        (self.repo / "untracked.txt").write_text("new\n", encoding="utf-8")
        with self.assertRaisesRegex(release.ReleaseError, "clean"):
            release.source_commit(self.repo)

    def test_rejects_unpushed_commit(self):
        (self.repo / "source.txt").write_text("changed\n", encoding="utf-8")
        self.git("add", "source.txt")
        self.commit()
        with self.assertRaisesRegex(release.ReleaseError, "origin/test"):
            release.source_commit(self.repo)

    def prepare_publisher(self):
        (self.repo / "deploy" / "testing").mkdir(parents=True)
        for name in ("compose.yml", "release.py"):
            shutil.copy2(SOURCE / name, self.repo / "deploy" / "testing" / name)
        (self.repo / "deploy" / "alembic_head.py").write_text(
            "print('0076_explicit_cloud_computers')\n", encoding="utf-8",
        )
        (self.repo / ".gitignore").write_text(".env\n", encoding="utf-8")
        self.git("add", ".")
        self.commit()
        self.git("push", "origin", "test")
        (self.repo / ".env").write_text("SECRET=ignored-private-fixture\n", encoding="utf-8")

    def test_publish_builds_frozen_commit_and_packages_all_three_digests(self):
        self.prepare_publisher()
        original_run = release.run
        builds = []

        def fake_build(args, **kwargs):
            if args[0] != "docker":
                return original_run(args, **kwargs)
            builds.append(args)
            snapshot = Path(kwargs["cwd"])
            self.assertNotEqual(snapshot, self.repo)
            self.assertFalse((snapshot / ".env").exists())
            self.assertEqual((snapshot / "source.txt").read_text(encoding="utf-8"), "source\n")
            metadata = Path(args[args.index("--metadata-file") + 1])
            release.write_json(metadata, {"containerimage.digest": "sha256:" + "b" * 64})
            return ""

        output = Path(self.temporary.name) / "published"
        with patch.object(release, "run", side_effect=fake_build):
            archive = release.publish(self.repo, output, "registry.example.test", "team")
        self.assertEqual(len(builds), 3)
        document = release.verify_package(archive.parent / archive.name.removesuffix(".tar.gz"))
        self.assertEqual(document["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(set(document["images"]), {"api", "preview-worker", "web"})
        self.assertTrue(all("linux/amd64" in command for command in builds))
        self.assertTrue(archive.with_name(archive.name + ".sha256").is_file())

    def test_failed_publish_does_not_create_deployable_archive(self):
        self.prepare_publisher()
        original_run = release.run

        def failed_build(args, **kwargs):
            if args[0] == "docker":
                raise release.ReleaseError("registry unavailable")
            return original_run(args, **kwargs)

        output = Path(self.temporary.name) / "published"
        with patch.object(release, "run", side_effect=failed_build):
            with self.assertRaisesRegex(release.ReleaseError, "registry"):
                release.publish(self.repo, output, "registry.example.test", "team")
        self.assertEqual(list(output.glob("*.tar.gz")), [])


class PackageTests(unittest.TestCase):
    def test_rejects_mutable_image_tag(self):
        document = manifest()
        document["images"]["api"] = "registry.example.test/team/api:latest"
        with self.assertRaisesRegex(release.ReleaseError, "digest"):
            release.validate_manifest(document)

    def test_rejects_mismatched_commit_and_version(self):
        document = manifest()
        document["commit"] = "c" * 40
        with self.assertRaisesRegex(release.ReleaseError, "version"):
            release.validate_manifest(document)

    def test_release_package_contains_no_server_secrets_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / manifest()["version"]
            root.mkdir()
            for name in ("release.py", "compose.yml"):
                shutil.copy2(SOURCE / name, root / name)
            release.write_json(root / "release.json", manifest())
            (root / ".env").write_text("AUTH_SECRET_KEY=not-for-upload\n", encoding="utf-8")
            archive = release.package(root)
            with tarfile.open(archive) as stream:
                names = {Path(member.name).name for member in stream.getmembers()}
            self.assertEqual(names, {"release.py", "compose.yml", "release.json", "SHA256SUMS"})
            self.assertEqual(release.verify_package(root), manifest())
            (root / "compose.yml").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(release.ReleaseError, "checksum mismatch"):
                release.verify_package(root)

    @unittest.skipIf(__import__("os").name == "nt", "Unix permission bits enforced on the server")
    def test_preserves_server_configuration_and_replaces_only_release_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / ".env"
            source.write_text(
                'POSTGRES_PASSWORD="keep-this-verbatim"\nAPI_IMAGE=old\nWEB_IMAGE=old\n',
                encoding="utf-8",
            )
            source.chmod(0o600)
            result = release.release_environment(source, manifest())
            self.assertIn('POSTGRES_PASSWORD="keep-this-verbatim"\n', result)
            self.assertNotIn("=old", result)
            self.assertIn("GIT_COMMIT=" + "a" * 40, result)
            self.assertEqual(result.count("API_IMAGE="), 1)
            source.chmod(0o644)
            with self.assertRaisesRegex(release.ReleaseError, "mode 600"):
                release.release_environment(source, manifest())


class DeploymentTests(unittest.TestCase):
    def test_wrong_database_volume_is_rejected_before_pull_or_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            deployment = release.Deployment(Path(tmp), Path(tmp), manifest())
            deployment.compose = Mock(side_effect=["", "existing-db"])
            with patch.object(release, "run", return_value=json.dumps([
                {"Name": "production_postgres", "Destination": "/var/lib/postgresql/data"},
            ])):
                with self.assertRaisesRegex(release.ReleaseError, "volume"):
                    deployment.preflight()
            self.assertEqual(len(deployment.compose.call_args_list), 2)
            self.assertFalse((Path(tmp) / "evidence.json").exists())

    def make_deployment(self, root, fail_at=None):
        destination = root / "releases" / manifest()["version"]
        destination.mkdir(parents=True)
        deployment = release.Deployment(root, destination, manifest())
        deployment.commands = []

        def compose(*args, **kwargs):
            deployment.commands.append(args)
            if args[0] == "ps":
                return ""
            if "pg_dump" in args:
                kwargs["stdout"].write(b"verified-database-fixture")
            if fail_at and fail_at(args):
                raise release.ReleaseError("simulated failure")
            if args[0] == "exec" and "psql" in args:
                return manifest()["alembic_head"]
            return ""

        deployment.compose = compose
        deployment.preflight = Mock()
        deployment.health = Mock()
        return deployment

    def test_migration_failure_keeps_verified_backup_and_does_not_start_new_apps(self):
        with tempfile.TemporaryDirectory() as tmp:
            deployment = self.make_deployment(Path(tmp), lambda args: args[0] == "run")
            with self.assertRaisesRegex(release.ReleaseError, "simulated"):
                deployment.execute()
            self.assertFalse(any(args[0] == "up" for args in deployment.commands))
            backup = Path(deployment.evidence["backup"])
            self.assertEqual(backup.read_bytes(), b"verified-database-fixture")
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), deployment.evidence["backup_sha256"])
            self.assertEqual(deployment.evidence["failed_step"], "migrating")
            deployment.health.assert_not_called()

    def test_invalid_backup_never_runs_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            deployment = self.make_deployment(Path(tmp), lambda args: "pg_restore" in args)
            with self.assertRaises(release.ReleaseError):
                deployment.execute()
            self.assertFalse(any(args[0] in {"run", "up"} for args in deployment.commands))

    def test_running_writer_blocks_backup_and_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            deployment = self.make_deployment(Path(tmp))
            deployment.compose = Mock(side_effect=["", "still-running-container"])
            with self.assertRaisesRegex(release.ReleaseError, "writers"):
                deployment.execute()
            self.assertFalse((Path(tmp) / "backups").exists())

    def test_pull_failure_does_not_stop_running_apps(self):
        with tempfile.TemporaryDirectory() as tmp:
            deployment = self.make_deployment(Path(tmp))
            deployment.preflight.side_effect = release.ReleaseError("pull failed")
            with self.assertRaises(release.ReleaseError):
                deployment.execute()
            self.assertEqual(deployment.commands, [])

    def test_failed_health_never_reports_healthy_or_downgrades_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            deployment = self.make_deployment(Path(tmp))
            deployment.health.side_effect = release.ReleaseError("wrong running commit")
            with self.assertRaises(release.ReleaseError):
                deployment.execute()
            self.assertEqual(deployment.evidence["status"], "failed")
            self.assertEqual(deployment.evidence["failed_step"], "starting")
            self.assertFalse(any("downgrade" in args for args in deployment.commands))
            self.assertTrue(Path(deployment.evidence["backup"]).exists())

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose is unavailable")
    def test_compose_reuses_existing_volumes_and_sets_running_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "releases" / "fixture"
            directory.mkdir(parents=True)
            (root / "config").mkdir()
            (root / "config" / "app.env").write_text("FRONTEND_URL=https://example.test\n", encoding="utf-8")
            shutil.copy2(SOURCE / "compose.yml", directory / "compose.yml")
            values = {
                "POSTGRES_PASSWORD": "test-fixture-only",
                "APP_VERSION": manifest()["version"], "GIT_COMMIT": manifest()["commit"],
                **{release.IMAGES[key]: value for key, value in manifest()["images"].items()},
            }
            env = directory / ".env"
            env.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
            output = subprocess.check_output([
                "docker", "compose", "--project-directory", str(directory),
                "--env-file", str(env), "-f", str(directory / "compose.yml"),
                "config", "--format", "json",
            ], stderr=subprocess.PIPE, text=True)
            config = json.loads(output)
            for key in ("postgres", "redis", "cache"):
                self.assertTrue(config["volumes"][key]["external"])
                self.assertEqual(config["volumes"][key]["name"], release.PROJECT + "_" + key)
            self.assertEqual(config["services"]["api"]["environment"]["GIT_COMMIT"], "a" * 40)
            self.assertEqual(config["services"]["web"]["image"], manifest()["images"]["web"])
            self.assertFalse(any("build" in service for service in config["services"].values()))


if __name__ == "__main__":
    unittest.main()
