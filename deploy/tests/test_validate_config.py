import unittest
import json
import tempfile
import ast
import subprocess
import sys
from pathlib import Path

import yaml

from deploy.validate_config import APP_KEYS, audit_server_env, validate


ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_api_start_commands_preserve_transport_peer(self):
        dockerfile = (ROOT.parent / "backend/Dockerfile").read_text()
        command = json.loads(next(line[4:] for line in dockerfile.splitlines() if line.startswith("CMD ")))
        self.assertIn("--no-proxy-headers", command)
        for path in ("compose.production.yml", "testing/compose.yml"):
            compose = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
            self.assertIn("--no-proxy-headers", compose["services"]["api"]["command"])

    def test_production_contract(self):
        keys = validate(
            ROOT / "compose.production.yml",
            ROOT / "app.env.example",
            ROOT / "deployment.env.example",
        )
        self.assertIn("API_IMAGE", keys)
        self.assertIn("AUTH_SECRET_KEY", keys)
        self.assertIn("NEXT_PUBLIC_API_URL", keys)

    def test_catalog_matches_backend_settings(self):
        tree = ast.parse(
            (ROOT.parent / "backend/app/config.py").read_text(encoding="utf-8")
        )
        settings = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Settings"
        )
        fields = {
            node.target.id.upper() for node in settings.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        self.assertEqual(APP_KEYS, fields)

    def test_template_covers_optional_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_env = Path(tmp) / "app.env"
            app_env.write_text(
                (ROOT / "app.env.example").read_text(encoding="utf-8")
                .replace("S3_REGION=us-east-1\n", ""),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "S3_REGION"):
                validate(
                    ROOT / "compose.production.yml", app_env,
                    ROOT / "deployment.env.example",
                )

    def test_database_password_has_no_default(self):
        compose = yaml.safe_load((ROOT / "compose.production.yml").read_text(encoding="utf-8"))
        self.assertEqual(
            compose["services"]["db"]["environment"]["POSTGRES_PASSWORD"],
            "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}",
        )

    def test_production_worker_is_limited_for_small_hosts(self):
        compose = yaml.safe_load((ROOT / "compose.production.yml").read_text(encoding="utf-8"))
        self.assertEqual(
            compose["services"]["worker"]["command"][-2:],
            ["--concurrency=5", "--max-tasks-per-child=20"],
        )

    def test_chat_services_require_the_protected_workbench_environment(self):
        for service in ("api", "worker"):
            with self.subTest(service=service), tempfile.TemporaryDirectory() as tmp:
                compose = yaml.safe_load((ROOT / "compose.production.yml").read_text(encoding="utf-8"))
                compose["services"][service]["env_file"] = [
                    entry for entry in compose["services"][service]["env_file"]
                    if entry["path"] != ".env.question-workbench"
                ]
                target = Path(tmp) / "compose.yml"
                target.write_text(yaml.safe_dump(compose), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, service + ": protected question workbench"):
                    validate(target, ROOT / "app.env.example", ROOT / "deployment.env.example")

    def test_release_images_use_swr_instead_of_legacy_registry(self):
        release_package = (
            Path(__file__).resolve().parents[1] / "release_package.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("aliyuncs.com", release_package)
        self.assertNotIn("crpi-", release_package)
        self.assertIn('f"{registry.rstrip', release_package)

    def test_repository_compose_is_not_production_contract(self):
        with self.assertRaises(ValueError):
            validate(
                Path(__file__).resolve().parents[2] / "docker-compose.yml",
                ROOT / "app.env.example",
                ROOT / "deployment.env.example",
            )

    def test_deployment_table_rejects_application_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            deployment = Path(tmp) / "deployment.env"
            source = ROOT / "deployment.env.example"
            deployment.write_text(
                source.read_text(encoding="utf-8") + "AUTH_SECRET_KEY=leaked\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "application keys"):
                validate(
                    ROOT / "compose.production.yml",
                    ROOT / "app.env.example",
                    deployment,
                )

    def test_application_table_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_env = Path(tmp) / "app.env"
            source = ROOT / "app.env.example"
            app_env.write_text(
                source.read_text(encoding="utf-8") + "UNKNOWN_SETTING=x\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "undocumented keys"):
                validate(
                    ROOT / "compose.production.yml",
                    app_env,
                    ROOT / "deployment.env.example",
                )

    def test_audit_reports_only_keys_not_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_env = Path(tmp) / "app.env"
            app_env.write_text(
                "DATABASE_URL=postgresql+psycopg://secret:secret@db/rag\n"
                "UNKNOWN_SECRET=do-not-print\n",
                encoding="utf-8",
            )
            report = audit_server_env(app_env)
            self.assertEqual(report["app_keys"], 2)
            self.assertIn("UNKNOWN_SECRET", report["unknown"])
            self.assertIn("AUTH_SECRET_KEY", report["missing_required"])
            self.assertNotIn("secret", str(report))
            self.assertNotIn("do-not-print", str(report))

    def test_audit_rejects_empty_required_and_incomplete_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_env = Path(tmp) / "app.env"
            deployment = Path(tmp) / ".env.release"
            app_env.write_text("AUTH_SECRET_KEY=\n", encoding="utf-8")
            deployment.write_text("POSTGRES_PASSWORD=private\n", encoding="utf-8")
            report = audit_server_env(app_env, deployment)
            self.assertEqual(report["empty_required"], ["AUTH_SECRET_KEY"])
            self.assertIn("API_IMAGE", report["deployment_missing"])
            result = subprocess.run(
                [sys.executable, str(ROOT / "validate_config.py"),
                 "--audit-app-env", str(app_env),
                 "--audit-deployment-env", str(deployment)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("private", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
