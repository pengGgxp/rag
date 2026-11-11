import hashlib
import json
import shutil
import tempfile
import unittest
import dataclasses
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, call, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import deploy_release
import check_health


DIGEST = "sha256:" + "a" * 64
IMAGE = "registry.example.test/org/zhiku-api@" + DIGEST


def manifest(version="v1.2.3", policy="automatic-app-rollback"):
    return {
        "schema_version": 1,
        "version": version,
        "commit": "a" * 40,
        "built_at": "2026-11-10T00:00:00+00:00",
        "images": {name: IMAGE for name in ("api", "worker", "web")},
        "config_contract": 1,
        "alembic_head": "0063_deployment_maintenance",
        "database_rollback": policy,
        "database_backup_strategy": "full",
    }


def make_release(root: Path, payload: dict | None = None) -> Path:
    payload = payload or manifest()
    source = Path(__file__).resolve().parents[1]
    for name in ("check_health.py", "deploy_release.py", "maintenance.py", "alembic_head.py",
                 "run_release.sh", "bootstrap.sh", "validate_config.py", "app.env.example",
                 "deployment.env.example", "production.env.reference.md", "prune_registry.py"):
        shutil.copy2(source / name, root / name)
    (root / "release.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    for name in ("alembic.ini", "release-policy.json"):
        (root / name).write_text("# test\n", encoding="utf-8")
    (root / "alembic").mkdir(exist_ok=True)
    (root / "alembic" / "versions").mkdir(exist_ok=True)
    (root / "alembic" / "versions" / "head.py").write_text("# test\n", encoding="utf-8")
    (root / "nginx").mkdir(exist_ok=True)
    shutil.copy2(source / "nginx" / "zk.str1ct.top.conf", root / "nginx" / "zk.str1ct.top.conf")
    shutil.copy2(source / "nginx" / "http-preview.conf", root / "nginx" / "http-preview.conf")
    (root / "bin").mkdir(exist_ok=True)
    for name in (
        "deploy-release",
        "health",
        "maintenance",
        "prune-images",
        "rollback",
        "retain-releases",
    ):
        shutil.copy2(source / "bin" / name, root / "bin" / name)
    sums = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path == root / "SHA256SUMS":
            continue
        relative = path.relative_to(root).as_posix()
        sums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}")
    (root / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    return root


class ManifestTests(unittest.TestCase):
    def test_health_probe_sets_stable_user_agent(self):
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"status":"ok"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(check_health.urllib.request, "urlopen", return_value=response) as open_url:
            result = check_health.probe(
                "https://example.test/health",
                attempts=1,
                delay=0,
                expect_json=True,
                require_status=True,
            )
        self.assertEqual(result, {"status": "ok"})
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), check_health.HEALTH_USER_AGENT)

    def test_running_image_uses_container_config_image_not_repo_digests(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            images = {
                "api": IMAGE,
                "preview": IMAGE,
                "worker": IMAGE,
                "preview-worker": IMAGE,
                "web": IMAGE,
            }
            calls = iter(
                item
                for service in images
                for item in (f"{service}-container-id\n", images[service] + "\n")
            )
            runner = SimpleNamespace(
                run=Mock(side_effect=lambda _args: next(calls))
            )
            with patch.object(
                deploy_release,
                "compose_args",
                return_value=["docker", "compose"],
            ), patch.object(
                deploy_release,
                "load_manifest",
                return_value=manifest(),
            ):
                deploy_release.verify_running_digests(release, runner)
        inspect_command = runner.run.call_args_list[1].args[0]
        self.assertEqual(inspect_command[-1], "{{.Config.Image}}")

    def test_running_image_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            calls = iter(
                item
                for service in ("api", "preview", "worker", "preview-worker", "web")
                for item in (
                    f"{service}-container-id\n",
                    "registry.example.test/org/zhiku-api:latest\n",
                )
            )
            runner = SimpleNamespace(
                run=Mock(side_effect=lambda _args: next(calls))
            )
            with patch.object(
                deploy_release,
                "compose_args",
                return_value=["docker", "compose"],
            ), patch.object(
                deploy_release,
                "load_manifest",
                return_value=manifest(),
            ):
                with self.assertRaises(deploy_release.ReleaseError):
                    deploy_release.verify_running_digests(release, runner)

    def test_accepts_digest_pinned_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            deploy_release.verify_checksums(release)
            self.assertEqual(deploy_release.load_manifest(release)["version"], "v1.2.3")

    def test_rejects_tagged_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = manifest()
            payload["images"]["api"] = "registry.example.test/org/zhiku-api:v1.2.3"
            with self.assertRaises(deploy_release.ReleaseError):
                deploy_release.load_manifest(make_release(Path(tmp), payload))

    def test_rejects_release_without_full_database_backup_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = manifest()
            payload["database_backup_strategy"] = "unsafe"
            with self.assertRaises(deploy_release.ReleaseError):
                deploy_release.load_manifest(make_release(Path(tmp), payload))

    def test_rejects_modified_release_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            (release / "compose.yml").write_text("changed\n", encoding="utf-8")
            with self.assertRaises(deploy_release.ReleaseError):
                deploy_release.verify_checksums(release)

    def test_rejects_invalid_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = manifest(version="v1.2.3-rc1")
            with self.assertRaises(deploy_release.ReleaseError):
                deploy_release.load_manifest(make_release(Path(tmp), payload))

    def test_accepts_alembic_head_marker_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            runner = SimpleNamespace(
                run=lambda _args: "0063_deployment_maintenance (head)\n"
            )
            with patch.object(
                deploy_release,
                "compose_args",
                return_value=["docker", "compose"],
            ):
                deploy_release.verify_migration_heads(release, runner)

    def test_legacy_producer_commands_only_name_existing_services(self):
        runner = SimpleNamespace(run=Mock())
        deploy_release.stop_legacy_producers(runner)
        deploy_release.start_legacy_producers(runner)
        commands = [call.args[0] for call in runner.run.call_args_list]
        self.assertEqual(commands[0][-3:], ["stop", "worker", "preview"])
        self.assertEqual(commands[1][-2:], ["start", "worker"])
        self.assertEqual(commands[2][-2:], ["start", "preview"])


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.runner = deploy_release.CommandRunner()

    def test_deploy_renders_release_environment_before_migration_head_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = deploy_release.Paths(root=root)
            incoming = paths.incoming / "v1.2.3"
            incoming.mkdir(parents=True)
            make_release(incoming)
            current = paths.releases / "v1.2.2"
            current.mkdir(parents=True)
            paths.current.symlink_to(current.resolve(), target_is_directory=True)
            events = []

            def render(release, _paths):
                events.append("render")
                (release / ".env.release").write_text("API_IMAGE=test\n", encoding="utf-8")

            def verify(release, _runner):
                self.assertTrue((release / ".env.release").is_file())
                events.append("migration-head")

            with patch.object(deploy_release, "verify_checksums"), \
                 patch.object(deploy_release, "load_manifest", return_value=manifest()), \
                 patch.object(deploy_release, "render_environment", side_effect=render), \
                 patch.object(deploy_release, "verify_migration_heads", side_effect=verify), \
                 patch.object(deploy_release, "transactional_deploy") as transaction:
                deploy_release.deploy_release(paths, incoming, self.runner)

            self.assertEqual(events, ["render", "migration-head"])
            transaction.assert_called_once()

    def _transaction(self, root, *, policy="automatic-app-rollback", takeover=False, migration=True):
        release = root / "releases" / "v1.2.3"
        release.mkdir(parents=True)
        make_release(release, manifest(policy=policy))
        (release / "app.env").write_text("DEPLOY_CONTROL_TOKEN=" + "a" * 50 + "\n", encoding="utf-8")
        paths = deploy_release.Paths(root=root)
        events = []
        if not takeover:
            paths.current.symlink_to((root / "releases" / "v1.2.2").resolve(), target_is_directory=True)
            (root / "releases" / "v1.2.2").mkdir()
        state = deploy_release.MigrationState("0062_old", "0063_deployment_maintenance", migration)
        calls = SimpleNamespace(maintenance_on=0, maintenance_off=0, drain=0, rehearse=0,
                                migrate=0, health=0, nginx=0, compose=0, rollback=0)
        patches = {
            "load_manifest": patch.object(deploy_release, "load_manifest", return_value=manifest(policy=policy)),
            "verify_checksums": patch.object(deploy_release, "verify_checksums"),
            "render_environment": patch.object(deploy_release, "render_environment"),
            "backup_host_config": patch.object(deploy_release, "backup_host_config"),
            "backup_nginx": patch.object(deploy_release, "backup_nginx", return_value={"main": "old"}),
            "write_legacy_release": patch.object(deploy_release, "write_legacy_release"),
            "drain_workers": patch.object(deploy_release, "drain_workers", side_effect=lambda *_: events.append("drain")),
            "set_nginx_maintenance": patch.object(deploy_release, "set_nginx_maintenance", side_effect=lambda *_: events.append("nginx-on")),
            "finish_first_takeover_maintenance": patch.object(deploy_release, "finish_first_takeover_maintenance", side_effect=lambda *_: events.append("nginx-finish")),
            "stop_legacy_producers": patch.object(deploy_release, "stop_legacy_producers", side_effect=lambda *_: events.append("legacy-stop")),
            "start_legacy_producers": patch.object(deploy_release, "start_legacy_producers", side_effect=lambda *_: events.append("legacy-start")),
            "restore_legacy_nginx": patch.object(deploy_release, "restore_legacy_nginx"),
            "restore_nginx": patch.object(deploy_release, "restore_nginx"),
            "install_nginx": patch.object(deploy_release, "install_nginx", side_effect=lambda *_: events.append("nginx-install")),
            "migration_state": patch.object(deploy_release, "migration_state", return_value=state),
            "create_backup": patch.object(deploy_release, "create_backup", return_value=release / "backup.dump"),
            "verify_backup_copy": patch.object(deploy_release, "verify_backup_copy"),
            "verify_migration_heads": patch.object(deploy_release, "verify_migration_heads"),
            "switch_links": patch.object(deploy_release, "switch_links", return_value=root / "releases" / "v1.2.2"),
            "compose_up": patch.object(deploy_release, "compose_up"),
            "write_evidence": patch.object(deploy_release, "write_evidence"),
            "rollback": patch.object(deploy_release, "rollback", side_effect=lambda *_: events.append("rollback")),
        }
        for p in patches.values():
            p.start()
        try:
            def check(_):
                events.append("health")
                return {"status": "ok"}
            deploy_release.transactional_deploy(
                paths,
                release,
                self.runner,
                health_check=check,
                maintenance_on=lambda _: events.append("maintenance-on"),
                maintenance_off=lambda *_args, **_kwargs: events.append("maintenance-off"),
                migrate=lambda _: events.append("migrate"),
                rehearse=lambda *_args: events.append("rehearse"),
                has_migration=migration,
                takeover=takeover,
            )
        finally:
            for p in reversed(list(patches.values())):
                p.stop()
        return events

    def test_binary_backup_is_byte_exact_and_checksummed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "releases" / "v1.2.3"
            release.mkdir(parents=True)
            payload = bytes(range(256))
            runner = Mock()
            runner.run_bytes.return_value = payload
            with patch.object(deploy_release, "compose_args", return_value=["docker", "compose"]):
                backup = deploy_release.create_backup(release, runner)
            self.assertEqual(backup.read_bytes(), payload)
            checksum = backup.with_name(backup.name + ".sha256").read_text(encoding="utf-8")
            self.assertEqual(checksum, hashlib.sha256(payload).hexdigest() + "  " + backup.name + "\n")

    def test_migration_jobs_enable_the_migration_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp)
            release.mkdir(exist_ok=True)
            (release / "app.env").write_text("DATABASE_URL=test\n", encoding="utf-8")
            runner = Mock()
            runner.run.return_value = "0063_deployment_maintenance\n"
            with patch.object(
                deploy_release,
                "compose_args",
                return_value=["docker", "compose"],
            ), patch.object(
                deploy_release,
                "load_manifest",
                return_value=manifest(),
            ):
                deploy_release.migrate_production(release, runner)
            command = runner.run.call_args_list[0].args[0]
            self.assertIn("--profile", command)
            self.assertIn("migration", command)

    def test_rehearsal_runs_before_production_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = self._transaction(root)
            self.assertLess(events.index("rehearse"), events.index("migrate"))
            self.assertLess(events.index("nginx-install"), events.index("maintenance-off"))
            self.assertLess(events.index("maintenance-off"), events.index("health"))

    def test_compose_start_does_not_wait_for_health_checks(self):
        runner = Mock()
        paths = deploy_release.Paths(root=Path("/tmp/zhiku-test"))
        with patch.object(
            deploy_release,
            "current_compose_args",
            return_value=["docker", "compose"],
        ), patch.object(deploy_release, "confirm_running_digests"):
            deploy_release.compose_up(paths, runner)
        start_command = runner.run.call_args_list[1].args[0]
        self.assertIn("up", start_command)
        self.assertNotIn("--wait", start_command)
        self.assertNotIn("--wait-timeout", start_command)

    def test_successful_compatible_deployment_turns_maintenance_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = self._transaction(Path(tmp))
            self.assertEqual(events.count("maintenance-on"), 1)
            self.assertEqual(events.count("maintenance-off"), 1)

    def test_compatible_release_restarts_drained_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "releases" / "v1.2.3"
            release.mkdir(parents=True)
            make_release(release)
            paths = deploy_release.Paths(root=root)
            paths.current.symlink_to(
                (root / "releases" / "v1.2.2").resolve(),
                target_is_directory=True,
            )
            (root / "releases" / "v1.2.2").mkdir()
            with patch.object(deploy_release, "load_manifest", return_value=manifest()), \
                 patch.object(deploy_release, "verify_checksums"), \
                 patch.object(deploy_release, "render_environment"), \
                 patch.object(deploy_release, "backup_host_config"), \
                 patch.object(deploy_release, "maintenance_request"), \
                 patch.object(deploy_release, "drain_workers"), \
                 patch.object(deploy_release, "migration_state", return_value=deploy_release.MigrationState("0063_deployment_maintenance", "0063_deployment_maintenance", False)), \
                 patch.object(deploy_release, "changed_services", return_value=("web",)), \
                 patch.object(deploy_release, "switch_links", return_value=root / "releases" / "v1.2.2"), \
                 patch.object(deploy_release, "compose_up") as start, \
                 patch.object(deploy_release, "health_check", return_value={"status": "ok"}), \
                 patch.object(deploy_release, "verify_migration_heads"), \
                 patch.object(deploy_release, "install_nginx"), \
                 patch.object(deploy_release, "write_evidence"):
                deploy_release.transactional_deploy(
                    paths,
                    release,
                    deploy_release.CommandRunner(),
                    health_check=lambda _: {"status": "ok"},
                    maintenance_on=lambda _: None,
                    maintenance_off=lambda *args, **kwargs: None,
                    migrate=lambda _: None,
                    has_migration=False,
                )
            start.assert_called_once()
            self.assertTrue(start.call_args.kwargs["force_workers"])

    def test_first_takeover_stops_legacy_producers_before_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = self._transaction(Path(tmp), takeover=True, migration=True)
            self.assertLess(events.index("legacy-stop"), events.index("rehearse"))
            self.assertIn("nginx-install", events)
            self.assertIn("nginx-finish", events)

    def test_failed_first_takeover_does_not_restart_legacy_producers_after_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming" / "v1.2.3"
            incoming.mkdir(parents=True)
            make_release(incoming)
            paths = deploy_release.Paths(root=root)
            calls = []
            with patch.object(deploy_release, "shadow_release"), \
                 patch.object(deploy_release, "verify_checksums"), \
                 patch.object(deploy_release, "load_manifest", return_value=manifest()), \
                 patch.object(deploy_release, "expected_alembic_head"), \
                 patch.object(deploy_release, "transactional_deploy", side_effect=RuntimeError("failed")) as transaction:
                with self.assertRaises(RuntimeError):
                    deploy_release.takeover_release(
                        paths, incoming, deploy_release.CommandRunner()
                    )
            self.assertFalse(calls)
            self.assertEqual(transaction.call_count, 1)

    def test_incompatible_database_does_not_rollback(self):
        calls = []
        current = Path("/opt/zhiku/current")
        previous = Path("/opt/zhiku/releases/v1.2.2")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "releases" / "v1.2.3"
            release.mkdir(parents=True)
            make_release(release, manifest(policy="restore-backup-required"))
            (release / "app.env").write_text("DEPLOY_CONTROL_TOKEN=" + "a" * 50 + "\n", encoding="utf-8")
            paths = deploy_release.Paths(root=root)
            paths.current.symlink_to((root / "releases" / "v1.2.2").resolve(), target_is_directory=True)
            (root / "releases" / "v1.2.2").mkdir()
            with patch.object(deploy_release, "load_manifest", return_value=manifest(policy="restore-backup-required")), \
                 patch.object(deploy_release, "verify_checksums"), \
                 patch.object(deploy_release, "render_environment"), \
                 patch.object(deploy_release, "backup_host_config"), \
                 patch.object(deploy_release, "maintenance_request"), \
                 patch.object(deploy_release, "drain_workers"), \
                 patch.object(deploy_release, "migration_state", return_value=deploy_release.MigrationState("0062_old", "0063_deployment_maintenance", True)), \
                 patch.object(deploy_release, "create_backup", return_value=release / "backup.dump"), \
             patch.object(deploy_release, "verify_backup_copy"), \
                 patch.object(deploy_release, "rehearse_migration"), \
                 patch.object(deploy_release, "switch_links", side_effect=RuntimeError("switch failed")), \
                 patch.object(deploy_release, "rollback", side_effect=lambda *args: calls.append("rollback")), \
                 patch.object(deploy_release, "write_evidence"):
                with self.assertRaises(RuntimeError):
                    deploy_release.transactional_deploy(
                        paths,
                        release,
                        deploy_release.CommandRunner(),
                        health_check=lambda _: None,
                        maintenance_on=lambda _: None,
                        maintenance_off=lambda *args, **kwargs: None,
                        migrate=lambda _: None,
                        has_migration=True,
                    )
        self.assertFalse(calls)

    def test_compatible_manifest_uses_automatic_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            self.assertEqual(
                deploy_release.load_manifest(release)["database_rollback"],
                "automatic-app-rollback",
            )

    def test_checksum_tampering_does_not_stage_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "v1.2.3"
            release.mkdir()
            make_release(release)
            (release / "compose.yml").write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(deploy_release.ReleaseError):
                deploy_release.verify_checksums(release)

    def test_release_requires_complete_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            (release / "nginx" / "http-preview.conf").unlink()
            with self.assertRaises(deploy_release.ReleaseError):
                deploy_release.verify_checksums(release)

    def test_takeover_requires_shadow_and_no_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming" / "v1.2.3"
            incoming.mkdir(parents=True)
            make_release(incoming)
            paths = deploy_release.Paths(root=root)
            calls = []
            with patch.object(
                deploy_release, "shadow_release",
                side_effect=lambda *args, **kwargs: calls.append("shadow"),
            ):
                with self.assertRaises(deploy_release.ReleaseError):
                    deploy_release.takeover_release(
                        paths, incoming, deploy_release.CommandRunner()
                    )
            self.assertEqual(calls, ["shadow"])

    def test_environment_compose_file_does_not_copy_app_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "releases" / "v1.2.3"
            release.mkdir(parents=True)
            make_release(release)
            config = root / "config"
            config.mkdir()
            (config / "app.env").write_text(
                "\n".join(
                    [
                        "DATABASE_URL=postgresql+psycopg://rag:secret@db:5432/rag",
                        "REDIS_URL=redis://redis:6379/0",
                        "AUTH_SECRET_KEY=" + "a" * 40,
                        "SITE_CONFIG_ENCRYPTION_KEY=" + "b" * 40,
                        "MCP_CREDENTIAL_KEY=" + "c" * 40,
                        "NEXT_PUBLIC_API_URL=https://zk.str1ct.top/api",
                        "FRONTEND_URL=https://zk.str1ct.top",
                        "API_PUBLIC_URL=https://zk.str1ct.top/api",
                        "AUTH_COOKIE_SECURE=true",
                        "PREVIEW_ENABLED=true",
                        "PREVIEW_DOMAIN=preview.str1ct.top",
                        "PREVIEW_CACHE_MAX_BYTES=67108864",
                        "PREVIEW_CACHE_IDLE_SECONDS=3600",
                        "S3_ACCESS_KEY=rustfs",
                        "S3_SECRET_KEY=rustfs-secret",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            values = deploy_release.render_environment(
                release, deploy_release.Paths(root=root)
            )
            compose_env = (release / ".env.release").read_text(encoding="utf-8")
            self.assertIn("API_IMAGE=", compose_env)
            self.assertNotIn("AUTH_SECRET_KEY", compose_env)
            self.assertNotIn("postgresql+psycopg", compose_env)
            self.assertIn("DEPLOY_CONTROL_TOKEN", values)
            self.assertEqual(values["DATABASE_URL"], "postgresql+psycopg://rag:secret@db:5432/rag")
            self.assertIn("PREVIEW_CACHE_MAX_BYTES=67108864", compose_env)
            self.assertIn("PREVIEW_CACHE_IDLE_SECONDS=3600", compose_env)

    def test_nginx_gate_blocks_all_public_api_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nginx = root / "nginx"
            nginx.mkdir()
            original = """server {
    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
    }
}
"""
            (nginx / "zk.str1ct.top.conf").write_text(original, encoding="utf-8")
            runner = Mock()
            paths = dataclasses.replace(
                deploy_release.Paths(root=root),
                nginx_root=nginx,
            )
            with patch.object(deploy_release, "NGINX_FILES", ("zk.str1ct.top.conf",)):
                deploy_release.set_nginx_maintenance(paths, runner, True)
            text = (nginx / "zk.str1ct.top.conf").read_text(encoding="utf-8")
            self.assertEqual(text.count("location"), 2)
            self.assertIn("location = /api/health", text)
            self.assertIn("location /api/", text)
            self.assertIn("return 503", text)
            self.assertIn('add_header Retry-After 60 always;', text)

    def test_first_takeover_recovery_requires_legacy_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "releases" / "v1.2.3"
            current.mkdir(parents=True)
            paths = deploy_release.Paths(root=root)
            paths.current.symlink_to(current.resolve(), target_is_directory=True)
            paths.previous.symlink_to(current.resolve(), target_is_directory=True)
            with self.assertRaises(deploy_release.ReleaseError):
                deploy_release.recover_first_takeover(
                    paths, current, deploy_release.CommandRunner()
                )

    def test_first_takeover_recovery_rechecks_runtime_and_rewrites_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "releases" / "v1.2.3"
            legacy = current / "legacy-running"
            legacy.mkdir(parents=True)
            make_release(current)
            paths = deploy_release.Paths(root=root)
            paths.current.symlink_to(current.resolve(), target_is_directory=True)
            paths.previous.symlink_to(legacy.resolve(), target_is_directory=True)
            runner = deploy_release.CommandRunner()
            with patch.object(deploy_release, "verify_running_digests") as digests, \
                 patch.object(deploy_release, "verify_migration_heads") as migration, \
                 patch.object(
                     deploy_release,
                     "health_check",
                     return_value={"status": "ok"},
                 ) as health, \
                 patch.object(deploy_release, "install_nginx") as nginx, \
                 patch.object(deploy_release, "write_evidence") as evidence:
                deploy_release.recover_first_takeover(paths, current, runner)
            digests.assert_called_once_with(current, runner)
            migration.assert_called_once_with(current, runner)
            health.assert_called_once_with(paths)
            nginx.assert_called_once_with(paths, current, runner)
            self.assertEqual(evidence.call_args.kwargs["status"], "healthy")

    def test_retention_keeps_only_current_and_previous_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            releases = root / "releases"
            releases.mkdir()
            for version in ("v1.2.1", "v1.2.2", "v1.2.3"):
                (releases / version).mkdir()
            paths = deploy_release.Paths(root=root)
            paths.current.symlink_to((releases / "v1.2.3").resolve(), target_is_directory=True)
            paths.previous.symlink_to((releases / "v1.2.2").resolve(), target_is_directory=True)
            retained = deploy_release.retain_releases(paths, releases / "v1.2.3")
            self.assertEqual({path.name for path in retained}, {"v1.2.2", "v1.2.3"})
            self.assertFalse((releases / "v1.2.1").exists())
            self.assertTrue((releases / "v1.2.2").exists())
            self.assertTrue((releases / "v1.2.3").exists())

    def test_retention_accepts_first_takeover_legacy_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            releases = root / "releases"
            current = releases / "v1.2.3"
            legacy = current / "legacy-running"
            obsolete = releases / "v1.2.1"
            legacy.mkdir(parents=True)
            obsolete.mkdir()
            (legacy / "containers.txt").write_text("legacy evidence\n", encoding="utf-8")
            paths = deploy_release.Paths(root=root)
            paths.current.symlink_to(current.resolve(), target_is_directory=True)
            paths.previous.symlink_to(legacy.resolve(), target_is_directory=True)
            retained = deploy_release.retain_releases(paths, current)
            self.assertEqual({path.name for path in retained}, {"v1.2.3", "legacy-running"})
            self.assertFalse(obsolete.exists())
            self.assertTrue((legacy / "containers.txt").is_file())

    def test_drain_waits_for_active_tasks_before_stopping_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "releases" / "v1.2.2"
            current.mkdir(parents=True)
            paths = deploy_release.Paths(root=root)
            paths.current.symlink_to(current.resolve(), target_is_directory=True)
            runner = Mock()
            states = iter([1, 0])

            def active(*_args):
                try:
                    return next(states)
                except StopIteration:
                    return 0

            with patch.object(deploy_release, "celery_active_tasks", side_effect=active), patch.object(deploy_release.time, "sleep") as sleep, patch.object(
                deploy_release, "stop_current_workers"
            ) as stop:
                deploy_release.drain_workers(paths, runner)
            self.assertEqual(sleep.call_count, 1)
            stop.assert_called_once_with(paths, runner)

    def test_running_digest_check_rejects_mismatched_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "releases" / "v1.2.3"
            release.mkdir(parents=True)
            make_release(release)
            paths = deploy_release.Paths(root=root)
            paths.current.symlink_to(release.resolve(), target_is_directory=True)
            runner = Mock()
            runner.run.side_effect = ["container-id", '["registry.example.test/org/zhiku-api@sha256:' + "b" * 64 + '"]']
            with patch.object(deploy_release, "current_compose_args", return_value=["docker", "compose"]):
                with self.assertRaises(deploy_release.ReleaseError):
                    deploy_release.confirm_running_digests(paths, runner)

    def _deploy_harness(self, root: Path, *, policy="automatic-app-rollback"):
        release = root / "releases" / "v1.2.3"
        release.mkdir(parents=True)
        payload = manifest(policy=policy)
        make_release(release, payload)
        previous = root / "releases" / "v1.2.2"
        previous.mkdir()
        make_release(previous, manifest(version="v1.2.2"))
        paths = deploy_release.Paths(root=root)
        paths.current.symlink_to(previous.resolve(), target_is_directory=True)
        return release, previous, paths, payload

    def test_maintenance_enable_failure_stops_before_migration_and_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            release, previous, paths, payload = self._deploy_harness(Path(tmp))
            with patch.object(deploy_release, "load_manifest", return_value=payload), \
                 patch.object(deploy_release, "verify_checksums"), \
                 patch.object(deploy_release, "render_environment"), \
                 patch.object(deploy_release, "backup_host_config"), \
                 patch.object(deploy_release, "drain_workers") as drain, \
                 patch.object(deploy_release, "migration_state") as migration, \
                 patch.object(deploy_release, "switch_links") as switch, \
                 patch.object(deploy_release, "compose_up") as compose, \
                 patch.object(deploy_release, "write_evidence"):
                with self.assertRaises(deploy_release.ReleaseError):
                    deploy_release.transactional_deploy(
                        paths,
                        release,
                        deploy_release.CommandRunner(),
                        health_check=lambda _: {"status": "ok"},
                        maintenance_on=lambda _: (_ for _ in ()).throw(
                            deploy_release.ReleaseError("maintenance unavailable")
                        ),
                        maintenance_off=lambda *_args, **_kwargs: None,
                        migrate=lambda _: None,
                        has_migration=True,
                    )
            drain.assert_not_called()
            migration.assert_not_called()
            switch.assert_not_called()
            compose.assert_not_called()
            self.assertEqual(paths.current.resolve(), previous.resolve())

    def test_rehearsal_failure_does_not_migrate_switch_or_clear_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            release, _, paths, payload = self._deploy_harness(Path(tmp))
            events = []
            with patch.object(deploy_release, "load_manifest", return_value=payload), \
                 patch.object(deploy_release, "verify_checksums"), \
                 patch.object(deploy_release, "render_environment"), \
                 patch.object(deploy_release, "backup_host_config"), \
                 patch.object(deploy_release, "drain_workers"), \
                 patch.object(deploy_release, "migration_state", return_value=deploy_release.MigrationState("0062_old", payload["alembic_head"], True)), \
                 patch.object(deploy_release, "create_backup", return_value=release / "backup.dump"), \
                 patch.object(deploy_release, "verify_backup_copy"), \
                 patch.object(deploy_release, "switch_links") as switch, \
                 patch.object(deploy_release, "compose_up") as compose, \
                 patch.object(deploy_release, "write_evidence"):
                with self.assertRaises(deploy_release.ReleaseError):
                    deploy_release.transactional_deploy(
                        paths,
                        release,
                        deploy_release.CommandRunner(),
                        health_check=lambda _: {"status": "ok"},
                        maintenance_on=lambda _: events.append("maintenance-on"),
                        maintenance_off=lambda *_args, **_kwargs: events.append("maintenance-off"),
                        migrate=lambda _: events.append("migrate"),
                        rehearse=lambda *_args: (_ for _ in ()).throw(
                            deploy_release.ReleaseError("rehearsal failed")
                        ),
                        has_migration=True,
                    )
            self.assertEqual(events, ["maintenance-on"])
            switch.assert_not_called()
            compose.assert_not_called()

    def test_compatible_health_failure_reopens_maintenance_and_checks_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            release, previous, paths, payload = self._deploy_harness(Path(tmp))
            events = []
            health_calls = 0

            def switch(_paths, target):
                (root / "current").unlink()
                (root / "current").symlink_to(target.resolve(), target_is_directory=True)
                return previous

            def health(_paths):
                nonlocal health_calls
                health_calls += 1
                events.append(f"health-{health_calls}")
                if health_calls == 1:
                    raise deploy_release.ReleaseError("new release health failed")
                return {"status": "ok"}

            root = Path(tmp)
            with patch.object(deploy_release, "load_manifest", return_value=payload), \
                 patch.object(deploy_release, "verify_checksums"), \
                 patch.object(deploy_release, "render_environment"), \
                 patch.object(deploy_release, "backup_host_config"), \
                 patch.object(deploy_release, "drain_workers"), \
                 patch.object(deploy_release, "migration_state", return_value=deploy_release.MigrationState("0062_old", payload["alembic_head"], True)), \
                 patch.object(deploy_release, "create_backup", return_value=release / "backup.dump"), \
                 patch.object(deploy_release, "verify_backup_copy"), \
                 patch.object(deploy_release, "rehearse_migration"), \
                 patch.object(deploy_release, "switch_links", side_effect=switch), \
                 patch.object(deploy_release, "changed_services", return_value=("web",)), \
                 patch.object(deploy_release, "compose_up"), \
                 patch.object(deploy_release, "verify_migration_heads"), \
                 patch.object(deploy_release, "install_nginx"), \
                 patch.object(deploy_release, "rollback", side_effect=lambda *_args: events.append("rollback")), \
                 patch.object(deploy_release, "write_evidence") as evidence:
                with self.assertRaises(deploy_release.ReleaseError):
                    deploy_release.transactional_deploy(
                        paths,
                        release,
                        deploy_release.CommandRunner(),
                        health_check=health,
                        maintenance_on=lambda *_args: events.append("maintenance-on"),
                        maintenance_off=lambda *_args, **_kwargs: events.append("maintenance-off"),
                        migrate=lambda _: events.append("migrate"),
                        has_migration=True,
                    )
            self.assertIn("rollback", events)
            self.assertEqual(
                events,
                [
                    "maintenance-on",
                    "migrate",
                    "maintenance-off",
                    "health-1",
                    "maintenance-on",
                    "rollback",
                    "maintenance-off",
                    "health-2",
                ],
            )
            self.assertFalse((release / "manual-maintenance-release-required").exists())
            self.assertEqual(evidence.call_args_list[-1].kwargs["status"], "failed")

    def test_restore_backup_policy_health_failure_keeps_new_release_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            release, previous, paths, payload = self._deploy_harness(
                Path(tmp), policy="restore-backup-required"
            )

            def switch(_paths, target):
                paths.current.unlink()
                paths.current.symlink_to(target.resolve(), target_is_directory=True)
                return previous

            with patch.object(deploy_release, "load_manifest", return_value=payload), \
                 patch.object(deploy_release, "verify_checksums"), \
                 patch.object(deploy_release, "render_environment"), \
                 patch.object(deploy_release, "backup_host_config"), \
                 patch.object(deploy_release, "drain_workers"), \
                 patch.object(deploy_release, "migration_state", return_value=deploy_release.MigrationState("0062_old", payload["alembic_head"], True)), \
                 patch.object(deploy_release, "create_backup", return_value=release / "backup.dump"), \
                 patch.object(deploy_release, "verify_backup_copy"), \
                 patch.object(deploy_release, "rehearse_migration"), \
                 patch.object(deploy_release, "switch_links", side_effect=switch), \
                 patch.object(deploy_release, "changed_services", return_value=("web",)), \
                 patch.object(deploy_release, "compose_up"), \
                 patch.object(deploy_release, "restore_previous_link") as restore, \
                 patch.object(deploy_release, "rollback") as rollback, \
                 patch.object(deploy_release, "write_evidence"):
                with self.assertRaises(deploy_release.ReleaseError):
                    deploy_release.transactional_deploy(
                        paths,
                        release,
                        deploy_release.CommandRunner(),
                        health_check=lambda _: (_ for _ in ()).throw(
                            deploy_release.ReleaseError("health failed")
                        ),
                        maintenance_on=lambda _: None,
                        maintenance_off=lambda *_args, **_kwargs: None,
                        migrate=lambda _: None,
                        has_migration=True,
                    )
            restore.assert_not_called()
            rollback.assert_not_called()
            self.assertEqual(paths.current.resolve(), release.resolve())
            self.assertTrue((release / "manual-recovery-required").is_file())

    def test_nginx_install_failure_restores_prior_configuration_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            release, previous, paths, payload = self._deploy_harness(Path(tmp))
            nginx_root = Path(tmp) / "nginx"
            nginx_root.mkdir()
            for name in deploy_release.NGINX_FILES:
                (nginx_root / name).write_text("# previous\n", encoding="utf-8")
            paths = dataclasses.replace(paths, nginx_root=nginx_root)
            events = []
            with patch.object(deploy_release, "load_manifest", return_value=payload), \
                 patch.object(deploy_release, "verify_checksums"), \
                 patch.object(deploy_release, "render_environment"), \
                 patch.object(deploy_release, "backup_host_config"), \
                 patch.object(deploy_release, "drain_workers"), \
                 patch.object(deploy_release, "migration_state", return_value=deploy_release.MigrationState(payload["alembic_head"], payload["alembic_head"], False)), \
                 patch.object(deploy_release, "changed_services", return_value=("web",)), \
                 patch.object(deploy_release, "switch_links", return_value=previous), \
                 patch.object(deploy_release, "compose_up"), \
                 patch.object(deploy_release, "health_check", return_value={"status": "ok"}), \
                 patch.object(deploy_release, "verify_migration_heads"), \
                 patch.object(deploy_release, "install_nginx", side_effect=deploy_release.ReleaseError("nginx failed")), \
                 patch.object(deploy_release, "restore_nginx", side_effect=lambda *_: events.append("nginx-restore")), \
                 patch.object(deploy_release, "rollback", side_effect=lambda *_: events.append("rollback")), \
                 patch.object(deploy_release, "write_evidence"):
                with self.assertRaises(deploy_release.ReleaseError):
                    deploy_release.transactional_deploy(
                        paths,
                        release,
                        deploy_release.CommandRunner(),
                        health_check=lambda _: {"status": "ok"},
                        maintenance_on=lambda _: None,
                        maintenance_off=lambda *_args, **_kwargs: None,
                        migrate=lambda _: None,
                        has_migration=False,
                    )
            self.assertEqual(events, ["nginx-restore", "rollback"])

    def test_duplicate_release_is_rejected_before_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming" / "v1.2.3"
            incoming.mkdir(parents=True)
            make_release(incoming)
            (root / "releases" / "v1.2.3").mkdir(parents=True)
            paths = deploy_release.Paths(root=root)
            with patch.object(deploy_release, "verify_checksums") as verify:
                with self.assertRaises(deploy_release.ReleaseError):
                    deploy_release.deploy_release(
                        paths, incoming, deploy_release.CommandRunner()
                    )
            verify.assert_not_called()

    def test_protected_configuration_missing_fails_before_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = root / "releases" / "v1.2.3"
            release.mkdir(parents=True)
            make_release(release)
            paths = deploy_release.Paths(root=root)
            with patch.object(deploy_release, "switch_links") as switch:
                with self.assertRaises(deploy_release.ReleaseError):
                    deploy_release.render_environment(release, paths)
            switch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
