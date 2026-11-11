import unittest
import tempfile
from pathlib import Path

import yaml

from deploy.validate_config import audit_server_env, validate


ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_production_contract(self):
        keys = validate(
            ROOT / "compose.production.yml",
            ROOT / "app.env.example",
            ROOT / "deployment.env.example",
        )
        self.assertIn("API_IMAGE", keys)
        self.assertIn("AUTH_SECRET_KEY", keys)
        self.assertNotIn("NEXT_PUBLIC_API_URL", keys)

    def test_database_password_has_no_default(self):
        compose = yaml.safe_load((ROOT / "compose.production.yml").read_text(encoding="utf-8"))
        self.assertEqual(
            compose["services"]["db"]["environment"]["POSTGRES_PASSWORD"],
            "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}",
        )

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


if __name__ == "__main__":
    unittest.main()
