"""OpenAI-compatible vision requests and strict, screenshot-relative decisions."""

import base64
import io
import json
import logging
import math
import time
from urllib.parse import urlsplit

import requests
from inference import AIError, DecisionClient, build_control_prompt, build_prompt, screenshot_info, screenshot_size

from settings import load_config


LOG = logging.getLogger("seth")


class SethAIClient(DecisionClient):
    supports_scroll_context = True

    def __init__(self, config=None, config_file=None):
        self.config = config if config is not None else load_config(config_file)
        self.api = self.config.get("api", {})
        self.last_grounded = False

    @property
    def endpoint(self):
        base = self.api.get("base_url", "").strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise AIError("Set INFERENCE_BASE_URL to your provider's API base URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AIError("API base URL must not contain credentials, a query, or a fragment.")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise AIError("Use HTTPS for remote API endpoints.")
        return base if parsed.path.endswith("/chat/completions") else base + "/chat/completions"

    def validate_config(self):
        self.endpoint
        if not self.api.get("api_key"):
            raise AIError("Set INFERENCE_API_KEY in .env or the environment.")
        if not self.api.get("model"):
            raise AIError("Set INFERENCE_MODEL to a vision-capable model.")
        timeout = self.api.get("timeout_seconds", 120)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise AIError("api.timeout_seconds must be greater than 0 and at most 300.")
        if type(self.api.get("stream", False)) is not bool:
            raise AIError("api.stream must be true or false.")

    def analyze_screenshot(self, image_path, prompt=None, response_format=None, *, context_images=None, control_regions=None, scroll_targets=None):
        self.last_grounded = False
        if not context_images:
            return self._analyze_screenshot(image_path, prompt, response_format)
        if prompt:
            raise AIError("Scroll context requires the default control-ID prompt.", "browser_grounding")
        self.validate_config()
        width, height = screenshot_size(image_path)
        from control_targets import annotated_controls
        with annotated_controls(image_path, context_images=context_images, regions=control_regions, scroll_targets=scroll_targets) as (upload, controls):
            raw = self._analyze_screenshot(upload, build_control_prompt(width, height, controls, scroll_context=True,
                                                                      scroll_navigation=scroll_targets is not None), response_format)
            decision = self.parse_coordinates(raw, width, height, control_targets=controls)
        self.last_grounded = decision is not None
        return json.dumps(decision) if decision is not None else raw

    def _analyze_screenshot(self, image_path, prompt=None, response_format=None):
        self.validate_config()
        with open(image_path, "rb") as stream:
            data = stream.read()
        (width, height), mime = screenshot_info(io.BytesIO(data))
        if prompt is None:
            prompt = build_prompt(width, height)
        encoded = base64.b64encode(data).decode("ascii")
        payload = {
            "model": self.api["model"],
            "stream": self.api.get("stream", False),
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + encoded}},
            ]}],
        }
        fmt = response_format if response_format is not None else self.api.get("response_format")
        if fmt:
            payload["response_format"] = {"type": fmt} if isinstance(fmt, str) else fmt
        # No automatic retries: a failed request must never lead to repeated actions.
        endpoint = self.endpoint
        read_timeout = self.api.get("timeout_seconds", 120)
        started = time.monotonic()
        LOG.info("Sending inference POST to %s (model %s; response timeout %ss)",
                 urlsplit(endpoint).hostname, self.api["model"], read_timeout)
        try:
            response = requests.post(
                endpoint, json=payload,
                headers={"Authorization": "Bearer " + self.api["api_key"]},
                timeout=(10, read_timeout),
                allow_redirects=False,
                stream=payload["stream"],
            )
        except requests.exceptions.ConnectTimeout:
            raise AIError("Inference connection timed out; no click was attempted.", "connect_timeout") from None
        except requests.Timeout:
            raise AIError(f"Inference response timed out after {read_timeout}s; no click was attempted.",
                          "response_timeout") from None
        except requests.exceptions.SSLError:
            raise AIError("Inference TLS verification failed; check the provider URL and host certificates.") from None
        except requests.ConnectionError:
            raise AIError("Cannot connect to inference; check the provider URL and host network.") from None
        except requests.RequestException:
            raise AIError("Inference request failed or timed out; no click was attempted.") from None
        try:
            LOG.info("Inference returned HTTP %s after %.1fs", response.status_code, time.monotonic() - started)
            if response.status_code == 524:
                raise AIError("Inference tunnel timed out waiting for the upstream answer (HTTP 524); no click was attempted.",
                              "response_timeout")
            if response.status_code == 530:
                raise AIError("Inference gateway cannot resolve its origin (HTTP 530). Restart the provider tunnel or update INFERENCE_BASE_URL; no click was attempted.",
                              "gateway_unavailable")
            if response.status_code != 200:
                raise AIError(f"Inference returned HTTP {response.status_code}; no click was attempted.")
            if payload["stream"]:
                if "text/event-stream" not in response.headers.get("content-type", "").lower():
                    raise AIError("Inference did not return the requested event stream; no click was attempted.")
                content = self._read_stream(response, started, read_timeout)
                LOG.info("Inference stream completed after %.1fs", time.monotonic() - started)
                return content
            try:
                completion = response.json()
                if isinstance(completion, dict) and completion.get("error") is not None:
                    raise AIError("Inference returned an API error inside HTTP 200; check provider, model, vision, and JSON-mode support.")
                message = completion["choices"][0]["message"]
                content = message["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise AIError("Inference returned an invalid chat completion.") from None
            if not isinstance(content, str) or not content.strip():
                raise AIError("Inference returned no text decision.")
            return content
        except requests.RequestException:
            raise AIError("Inference stream was interrupted; no click was attempted.", "stream_error") from None
        finally:
            response.close()

    @staticmethod
    def _read_stream(response, started, timeout):
        """Assemble a completed answer; never expose partial JSON to the clicker."""
        response.encoding = "utf-8"
        parts, event_data = [], []
        finish = None
        size = 0

        def consume(data):
            nonlocal finish, size
            if data.strip() == "[DONE]":
                return True
            try:
                chunk = json.loads(data)
                if not isinstance(chunk, dict) or chunk.get("error") is not None:
                    raise AIError("Inference stream returned an API error; no click was attempted.")
                choices = chunk.get("choices", [])
                if not isinstance(choices, list):
                    raise ValueError
                for choice in choices:
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta", {})
                    if delta.get("tool_calls") or delta.get("function_call") or delta.get("refusal"):
                        raise AIError("Inference did not return a plain answer decision.")
                    text = delta.get("content")
                    if text is not None:
                        if not isinstance(text, str) or (finish is not None and text):
                            raise ValueError
                        size += len(text)
                        if size > 65536:
                            raise AIError("Inference decision exceeded the response size limit.")
                        parts.append(text)
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if finish is not None and reason != finish:
                            raise ValueError
                        finish = reason
            except (ValueError, TypeError, AttributeError):
                raise AIError("Inference returned an invalid event stream; no click was attempted.") from None
            return False

        for line in response.iter_lines(chunk_size=1, decode_unicode=True):
            if time.monotonic() - started > timeout:
                raise AIError(f"Inference response timed out after {timeout}s; no click was attempted.", "response_timeout")
            if not line:
                if event_data:
                    done = consume("\n".join(event_data))
                    event_data.clear()
                    if done:
                        break
            elif line.startswith("data:"):
                event_data.append(line[5:].lstrip(" "))
            # SSE comments/heartbeats and event metadata carry no answer text.
        if event_data:
            consume("\n".join(event_data))
        content = "".join(parts)
        # Some gateways close after finish_reason=stop without a [DONE] sentinel.
        # A clean EOF is acceptable only when an explicit successful finish arrived.
        if finish != "stop" or not content.strip():
            raise AIError("Inference stream ended without a complete answer; no click was attempted.")
        return content
