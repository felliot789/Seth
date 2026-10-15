"""Project configuration; environment variables override the local .env file."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def load_config(path=None):
    path = Path(path).resolve() if path else ROOT / "config.json"
    load_dotenv(path.parent / ".env", override=False)
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    api = config.setdefault("api", {})
    for field, names in {
        "base_url": ("INFERENCE_BASE_URL", "SETH_API_BASE_URL", "SETTH_API_BASE_URL"),
        "api_key": ("INFERENCE_API_KEY", "SETH_API_KEY", "SETTH_API_KEY", "OPENAI_API_KEY"),
        "model": ("INFERENCE_MODEL", "SETH_API_MODEL"),
    }.items():
        for name in names:
            if os.environ.get(name):
                api[field] = os.environ[name]
                break
    config["project_dir"] = str(path.parent)
    return config
