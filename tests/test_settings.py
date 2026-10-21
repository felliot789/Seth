"""Configuration precedence uses isolated files and environment values."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from settings import load_config


class SettingsTests(unittest.TestCase):
    def test_environment_overrides_adjacent_dotenv_then_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            path.write_text(json.dumps({"api": {"base_url": "https://json.example/v1",
                                                "api_key": "json-key", "model": "json-model"}}))
            (root / ".env").write_text("INFERENCE_API_KEY=dotenv-key\nINFERENCE_MODEL=dotenv-model\n")
            with patch.dict(os.environ, {"INFERENCE_MODEL": "process-model"}, clear=True):
                config = load_config(path)
            self.assertEqual(config["api"], {"base_url": "https://json.example/v1",
                                            "api_key": "dotenv-key", "model": "process-model"})
            self.assertEqual(config["project_dir"], str(root))

    def test_unrelated_or_retired_api_variables_do_not_select_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text('{"api":{"api_key":"configured-key"}}')
            legacy = {name: "unrelated-value" for name in (
                "OPENAI_API_KEY", "SETH_API_BASE_URL", "SETTH_API_BASE_URL", "SETH_API_KEY",
                "SETTH_API_KEY", "SETH_API_MODEL")}
            with patch.dict(os.environ, legacy, clear=True):
                self.assertEqual(load_config(path)["api"], {"api_key": "configured-key"})
