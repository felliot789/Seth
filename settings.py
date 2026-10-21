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
    for field, name in {
        "base_url": "INFERENCE_BASE_URL",
        "api_key": "INFERENCE_API_KEY",
        "model": "INFERENCE_MODEL",
    }.items():
        if os.environ.get(name):
            api[field] = os.environ[name]
    config["project_dir"] = str(path.parent)
    return config
