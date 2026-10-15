"""Gemini-only compatibility entry point for the Lightpanda web backend."""

import copy

from settings import load_config
from web_inference import WebInferenceClient, main


class GeminiWebClient(WebInferenceClient):
    def __init__(self, config=None, **kwargs):
        config = copy.deepcopy(config if config is not None else load_config())
        config.setdefault("web", {})["providers"] = ["gemini"]
        super().__init__(config, **kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
