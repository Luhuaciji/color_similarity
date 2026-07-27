from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lipcolor_pipeline.config import (
    MissingEnvironmentVariable,
    load_env_file,
    require_env,
)
from lipcolor_pipeline.security import (
    build_self_test_secret,
    scan_bytes,
    self_test,
)


class SecurityTests(unittest.TestCase):
    def test_detector_positive_fixture_is_effective_and_redacted(self) -> None:
        self.assertTrue(self_test())
        secret = build_self_test_secret()
        findings = scan_bytes(
            f'api_key = "{secret}"'.encode(),
            scope="test",
            location="fixture.py",
        )
        self.assertEqual(1, len(findings))
        serialized = json.dumps(findings[0].__dict__)
        self.assertNotIn(secret, serialized)

    def test_placeholder_is_not_reported(self) -> None:
        findings = scan_bytes(
            b'api_key = "YOUR_DASHSCOPE_API_KEY_PLACEHOLDER"',
            scope="test",
            location=".env.example",
        )
        self.assertEqual([], findings)

    def test_missing_required_env_names_variable_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingEnvironmentVariable) as context:
                require_env("DASHSCOPE_API_KEY")
        self.assertEqual(
            "missing required environment variable: DASHSCOPE_API_KEY",
            str(context.exception),
        )

    def test_env_file_loads_without_returning_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = Path(temporary) / ".env"
            env_path.write_text("PIPELINE_TEST_SECRET=local-value\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                loaded = load_env_file(env_path)
                self.assertEqual(("PIPELINE_TEST_SECRET",), loaded)
                self.assertEqual("local-value", require_env("PIPELINE_TEST_SECRET"))
                self.assertNotIn("local-value", repr(loaded))


if __name__ == "__main__":
    unittest.main()
