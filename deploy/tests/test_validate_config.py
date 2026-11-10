import unittest
from pathlib import Path

import yaml

from deploy.validate_config import validate


ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_production_contract(self):
        keys = validate(ROOT / "compose.production.yml", ROOT / "production.env.example")
        self.assertIn("API_IMAGE", keys)

    def test_database_password_has_no_default(self):
        compose = yaml.safe_load((ROOT / "compose.production.yml").read_text(encoding="utf-8"))
        self.assertEqual(
            compose["services"]["db"]["environment"]["POSTGRES_PASSWORD"],
            "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}",
        )

    def test_release_images_use_swr_instead_of_legacy_registry(self):
        template = (ROOT / "production.env.example").read_text(encoding="utf-8")
        self.assertNotIn("aliyuncs.com", template)
        self.assertNotIn("crpi-", template)
        self.assertIn("myhuaweicloud.com", template)

    def test_repository_compose_is_not_production_contract(self):
        with self.assertRaises(ValueError):
            validate(Path(__file__).resolve().parents[2] / "docker-compose.yml", ROOT / "production.env.example")


if __name__ == "__main__":
    unittest.main()
