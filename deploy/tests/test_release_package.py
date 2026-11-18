import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import release_package


class ReleasePackageTests(unittest.TestCase):
    def test_fixed_release_entrypoint_uses_verified_runner_and_holds_lock(self):
        deploy_root = Path(__file__).resolve().parents[1]
        deploy_entrypoint = (deploy_root / "bin" / "deploy-release").read_text(
            encoding="utf-8"
        )
        package_runner = (deploy_root / "run_release.sh").read_text(encoding="utf-8")

        self.assertIn('/opt/zhiku/bin/run-release "$incoming" "$action"', deploy_entrypoint)
        self.assertIn('unpacked="$staging/$version"', package_runner)
        self.assertIn('/usr/bin/python3 "$incoming/deploy_release.py" "$action" "$incoming"', package_runner)
        self.assertNotIn("9>&-", package_runner)

    def test_builds_complete_checksummed_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deploy = root / "deploy"
            backend = root / "backend"
            digests = root / "digests"
            output = root / "output"
            deploy.mkdir()
            backend.mkdir()
            digests.mkdir()
            output.mkdir()
            source = Path(__file__).resolve().parents[1]
            for name in (
                "compose.production.yml", "check_health.py", "deploy_release.py", "payment_proxy.py",
                "alembic_head.py", "run_release.sh", "bootstrap.sh", "maintenance.py",
                "prune_registry.py", "validate_config.py", "app.env.example",
                "deployment.env.example", "production.env.reference.md",
            ):
                (deploy / name).write_text("# test\n", encoding="utf-8")
            for name in ("zk.str1ct.top.conf", "http-preview.conf"):
                (deploy / "nginx").mkdir(exist_ok=True)
                (deploy / "nginx" / name).write_text("# test\n", encoding="utf-8")
            (deploy / "bin").mkdir()
            for name in (
                "deploy-release", "health", "maintenance", "prune-images", "rollback",
                "retain-releases",
            ):
                (deploy / "bin" / name).write_text("# test\n", encoding="utf-8")
            (backend / "alembic.ini").write_text("# test\n", encoding="utf-8")
            (backend / "alembic" / "versions").mkdir(parents=True)
            (backend / "alembic" / "versions" / "head.py").write_text(
                "# test\n", encoding="utf-8"
            )
            for service in release_package.SERVICES:
                (digests / f"{service}.digest").write_text(
                    "sha256:" + "a" * 64 + "\n", encoding="utf-8"
                )
            archive = release_package.build(
                deploy,
                backend,
                digests,
                output,
                version="v1.2.3",
                commit="b" * 40,
                registry="registry.example.test",
                organization="org",
                alembic_head="0063_deployment_maintenance",
            )
            self.assertEqual(archive.name, "zhiku-v1.2.3.tar.gz")
            self.assertTrue((output / "zhiku-v1.2.3.tar.gz.sha256").is_file())
            with tarfile.open(archive, "r:gz") as handle:
                names = set(handle.getnames())
                for name in ("bin/deploy-release", "run_release.sh", "bootstrap.sh"):
                    self.assertNotIn(b"\r", handle.extractfile(name).read())
            self.assertTrue(
                {"release.json", "SHA256SUMS", "compose.yml", "nginx/http-preview.conf"}
                <= names
            )
            manifest = json.loads(
                (output / "release" / "release.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["alembic_head"], "0063_deployment_maintenance")
            self.assertIn(
                "registry.example.test/org/zhiku-api@sha256:",
                manifest["images"]["api"],
            )
            sums = (output / "release" / "SHA256SUMS").read_text(encoding="utf-8")
            self.assertIn("release.json", sums)
            self.assertNotIn("\r", sums)
            self.assertNotIn(
                "\r",
                (output / "zhiku-v1.2.3.tar.gz.sha256").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (output / "zhiku-v1.2.3.tar.gz.sha256").read_text(encoding="utf-8")
                .split()[0],
                hashlib.sha256(archive.read_bytes()).hexdigest(),
            )

    def test_rejects_invalid_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "digests").mkdir()
            for service in release_package.SERVICES:
                (root / "digests" / f"{service}.digest").write_text(
                    "not-a-digest\n", encoding="utf-8"
                )
            with self.assertRaises(ValueError):
                release_package.build(
                    root,
                    root,
                    root / "digests",
                    root,
                    version="v1.2.3",
                    commit="b" * 40,
                    registry="registry.example.test",
                    organization="org",
                    alembic_head="head",
                )


if __name__ == "__main__":
    unittest.main()
