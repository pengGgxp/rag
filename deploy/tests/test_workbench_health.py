import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy import check_health


class WorkbenchHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / "app.env").write_text(
            "QUESTION_WORKBENCH_API_URL=http://question-workbench-api:8100\n",
            encoding="utf-8",
        )
        self.root = root
        self.environment = {
            "QUESTION_WORKBENCH_URL": "https://questions.example.test",
            "QUESTION_WORKBENCH_API_URL": "http://question-workbench-api:8100",
            "QUESTION_WORKBENCH_SECRET": "private-test-secret-" * 3,
        }

    def verify(self, worker=None, health=None):
        worker = self.environment if worker is None else worker
        outputs = [json.dumps([f"{key}={value}" for key, value in env.items()])
                   for env in (self.environment, worker)]
        with patch.object(check_health, "Path", return_value=self.root), \
             patch.object(check_health, "compose_ps", side_effect=lambda service: service), \
             patch.object(check_health.subprocess, "check_output", side_effect=outputs), \
             patch.object(check_health, "compose_exec", side_effect=health or ["200", "200"]) as probe:
            result = check_health.verify_question_workbench()
        return result, probe

    def test_probes_both_api_and_worker_with_matching_configuration(self):
        result, probe = self.verify()
        self.assertEqual(result, "ok")
        self.assertEqual([call.args[0] for call in probe.call_args_list], ["api", "worker"])

    def test_configuration_only_in_protected_file_is_not_skipped(self):
        (self.root / "app.env").write_text("", encoding="utf-8")
        (self.root / ".env.question-workbench").write_text(
            "QUESTION_WORKBENCH_API_URL=http://question-workbench-api:8100\n",
            encoding="utf-8",
        )
        result, probe = self.verify()
        self.assertEqual(result, "ok")
        self.assertEqual(probe.call_count, 2)

    def test_api_success_does_not_hide_missing_worker_secret(self):
        with self.assertRaisesRegex(RuntimeError, "worker.*incomplete") as error:
            self.verify({key: value for key, value in self.environment.items()
                         if key != "QUESTION_WORKBENCH_SECRET"})
        self.assertNotIn(self.environment["QUESTION_WORKBENCH_SECRET"], str(error.exception))

    def test_rejects_short_worker_secret(self):
        with self.assertRaisesRegex(RuntimeError, "worker.*incomplete"):
            self.verify({**self.environment, "QUESTION_WORKBENCH_SECRET": "short"})

    def test_rejects_different_worker_credentials_without_logging_them(self):
        changed = "different-private-test-secret-" * 3
        with self.assertRaisesRegex(RuntimeError, "configurations differ") as error:
            self.verify({**self.environment, "QUESTION_WORKBENCH_SECRET": changed})
        self.assertNotIn(changed, str(error.exception))
        self.assertNotIn(self.environment["QUESTION_WORKBENCH_SECRET"], str(error.exception))

    def test_rejects_different_worker_endpoint(self):
        with self.assertRaisesRegex(RuntimeError, "configurations differ"):
            self.verify({**self.environment, "QUESTION_WORKBENCH_API_URL": "http://other:8100"})

    def test_api_health_success_does_not_hide_worker_network_failure(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable from worker"):
            self.verify(health=["200", subprocess.CalledProcessError(1, ["python"])])

    def test_empty_health_response_is_a_failure(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable from worker"):
            self.verify(health=["200", ""])

    def test_unconfigured_workbench_is_explicitly_skipped(self):
        (self.root / "app.env").write_text("", encoding="utf-8")
        with patch.object(check_health, "Path", return_value=self.root), \
             patch.object(check_health, "compose_ps") as inspect:
            self.assertEqual(check_health.verify_question_workbench(), "skipped")
        inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
