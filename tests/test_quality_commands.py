from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class QualityCommandTests(unittest.TestCase):
    def test_profile_tests_propagates_pytest_failure(self) -> None:
        environment = os.environ.copy()
        environment["PYTEST_ADDOPTS"] = "--definitely-invalid"

        result = subprocess.run(
            ["just", "profile-tests"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=30,
        )

        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
