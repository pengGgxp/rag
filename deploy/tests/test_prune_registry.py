import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import prune_registry


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
DIGEST_E = "sha256:" + "e" * 64


class PruneRegistryTests(unittest.TestCase):
    def test_keeps_newest_three_and_protected_rollback_tags(self):
        tags = [
            prune_registry.Tag("v0.9.0", DIGEST_E),
            prune_registry.Tag("v1.0.0", DIGEST_A),
            prune_registry.Tag("v1.1.0", DIGEST_B),
            prune_registry.Tag("v1.2.0", DIGEST_C),
            prune_registry.Tag("v1.3.0", DIGEST_D),
            prune_registry.Tag("sha-" + "0" * 40, DIGEST_E),
            prune_registry.Tag("sha-" + "1" * 40, DIGEST_A),
            prune_registry.Tag("sha-" + "2" * 40, DIGEST_B),
            prune_registry.Tag("sha-" + "3" * 40, DIGEST_C),
            prune_registry.Tag("sha-" + "4" * 40, DIGEST_D),
            prune_registry.Tag("legacy-ui", DIGEST_E),
        ]
        delete, unknown = prune_registry.select_tags_to_delete(
            tags, keep=3, protected={DIGEST_A, DIGEST_C}
        )
        self.assertEqual(
            delete,
            ["sha-" + "0" * 40, "v0.9.0"],
        )
        self.assertEqual(unknown, ["legacy-ui"])

    def test_refuses_empty_protected_set(self):
        with self.assertRaises(ValueError):
            prune_registry.select_tags_to_delete(
                [prune_registry.Tag("v1.0.0", DIGEST_A)], keep=3, protected=set()
            )

    def test_duplicate_tag_inventory_aborts(self):
        with self.assertRaises(ValueError):
            prune_registry.select_tags_to_delete(
                [
                    prune_registry.Tag("v1.0.0", DIGEST_A),
                    prune_registry.Tag("v1.0.0", DIGEST_B),
                ],
                keep=3,
                protected={DIGEST_A},
            )

    def test_protected_digest_file_rejects_malformed_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "digests.txt"
            path.write_text("not-a-digest\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                prune_registry.protected_digests(str(path))


if __name__ == "__main__":
    unittest.main()
