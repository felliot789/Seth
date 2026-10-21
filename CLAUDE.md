# Seth — Vision-guided exam answer selector

## Project overview

Seth is a host-side tool that captures a VirtualBox VM's framebuffer, sends it to a
vision model (API, Gemini web, or Qwen web), and optionally clicks the answer
controls in the Windows guest through the VirtualBox SDK. It supports
single-choice radio, table classification, and multiple-answer checkbox questions.

### Architecture

```
main.py              CLI entry, cycle orchestration, hotkeys, overlay
inference.py         DecisionClient (parse_coordinates, build_prompt, build_control_prompt)
ai_client.py         SethAIClient — OpenAI-compatible API inference
web_inference.py     WebInferenceClient — Lightpanda/Chromium CDP web inference
control_targets.py   Local control identification, grounding, coordinate mapping
single_capture.py    Single-frame capture preparation
scroll_capture.py    Multi-view scroll capture collection
scroll_navigation.py Return-to-answer navigation with verified scroll positions
capture.py           Frame capture
clicker.py           VM click execution
cancellation.py      Cycle cancellation event
hotkey.py            Keyboard shortcut registration
overlay.py           Host status indicator (optional)
trigger.py           Host request trigger
vbox_backend.py      VirtualBox backend
service_runtime.py   systemd readiness notifications
```

### Key data flow

1. **Capture** — `SethCapture.capture_frame()` returns a `Frame` with a path and `Display`.
2. **Upload preparation** — `coordinate_capture()` annotates controls with yellow badges,
   builds contact sheets for scroll context, and maps upload-space coordinates back to
   original VM control boxes. This is an upload-only view; the original VM frame is untouched.
3. **Inference** — The model receives pixel coordinates (or badge IDs for scroll) and returns
   a JSON decision with `action`, `coordinate_space`, `click_x`/`click_y`.
4. **Grounding** — `parse_coordinates()` validates the decision; `ground_capture_decision()`
   or `ground_decision()` maps model-reported points to verified original-frame control centers.
5. **Execution** — `SethClicker` sends VirtualBox mouse events; `verify_selection()` confirms
   visible state change.

### Coordinate handling

Two coordinate spaces are accepted:
- `normalized_1000` — 0..1000 grid; converted to pixel coordinates using screenshot dimensions.
- `pixels` — original image pixel coordinates (used by web backends, especially Qwen).

**Local grounding only**: `model_point` stores the model's raw reported point;
`click_x`/`click_y` are the grounded center of the resolved control. Only
`control_targets` (locally cataloged) may attach `target_id` and `target_control`.

### Configuration

- `config.json` — VM name/UUID, screen number, inference backend, capture settings.
- `.env` — API keys, base URLs, model names (never committed).
- `web.sites.<provider>` — per-provider engine (lightpanda/chromium), model_label, thinking_label.

### Development

```sh
.venv/bin/python -m pytest tests/ -x -q          # run tests
.venv/bin/python main.py --mode status           # check VM connection
.venv/bin/python main.py --mode capture          # test capture without inference
.venv/bin/python main.py --mode once             # dry-run one cycle
.venv/bin/python main.py --mode once --execute   # execute one cycle
```

Browser integration tests require `SETH_LIGHTPANDA_TESTS=1` or `SETH_CHROMIUM_TESTS=1`.

### Conventions

- Tests in `tests/` use `unittest` with `subTest` for parametrized cases.
- Sensitive paths use `tempfile.TemporaryDirectory` and `chmod 0o600`/`0o700`.
- Error codes: `browser_grounding`, `browser_login`, `browser_model`, `browser_timeout`,
  `response_timeout`, `connect_timeout`, `gateway_unavailable`, `cancelled`.
- `parse_coordinates` rejects untrusted `target_id`/`target_control` fields when
  `control_targets is None` (preventing model injection of fake control identities).
- Selection confirmation requires a **newly filled interior**, not just hover/focus.
