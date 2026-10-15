# Development milestones

## 2026-10-15 — Experimental Lightpanda web inference

- Added a checksum-verified, private Lightpanda installation and a direct Python
  CDP controller. Inference has no Chromium, Playwright, Node, or standby-agent
  dependency.
- Separated shared prompting and strict answer validation from the API transport.
  Added `--backend web` and kept API inference as the configured default.
- Added Gemini screenshot flow and configurable ChatGPT/Qwen adapter scaffolding,
  ordered fallback, private per-provider cookie import/persistence, one submission
  per provider, cancellation, and a shared total inference deadline.
- Added local session/model checks and an image-only dry-run command, with provider
  progress and sanitized attempt results in the host listener.
- Verified real PNG transfer byte for byte, cookie restoration after browser
  restart, completed-answer extraction, and failure/cancellation behavior against
  local fixtures. Existing API and VM behavior remains covered by its tests.

Authenticated screenshot inference on Gemini, ChatGPT, and Qwen is still pending.
Their public pages load in Lightpanda on this host, but no live model screenshot
was submitted. ChatGPT/Qwen selectors and model labels require account-specific
verification. The detailed findings and remaining acceptance checks are recorded
in [docs/lightpanda-validation.md](docs/lightpanda-validation.md).

## 2026-10-15 — Desktop controls, streamed inference, and startup recovery

- Added Ctrl+P+O to request one answer cycle and Ctrl+Shift+P+O to toggle the host
  status indicator. Chords are consumed locally and activate on release; holding
  them does not repeat a request, and busy cycles do not queue additional work.
- Added a click-through Linux status indicator with elapsed inference time,
  timeout reporting, selection progress, and persistent visibility within a run.
- Added classification-table and multiple-answer plans with up to ten targets,
  explicit coordinate spaces, normalized-to-pixel conversion, target checks,
  cancellation, and partial-completion reporting.
- Held VirtualBox mouse presses for 80 ms so guest input polling can observe them.
- Added streamed inference to keep tunnel connections active while answers are
  generated. Only complete, validated answers reach the clicker; malformed,
  interrupted, oversized, or truncated responses are rejected.
- Configured a 240-second inference budget and a 300-second screenshot-age limit.
  Added request timing logs and distinct timeout errors.
- Added a persistent desktop-session service, targeted keyboard/uinput permission
  rules, and boot-time uinput loading. Documented the single-command launch path.
- Fixed early-login overlay startup by checking display readiness before Qt
  initialization. Added process locking and abandoned-socket recovery after a
  crash, while preserving live listeners and unrelated files.

Validation: 65 automated tests, including fake-SDK subprocess integration, local
HTTP/SSE transport tests, keyboard routing, overlay behavior, and crash recovery.
Live host checks verified keyboard access, click-through input, unchanged focus,
foreground/service startup, and clean shutdown. A streamed screenshot request
produced a validated three-target plan in about 112 seconds without sending VM
input. Early-login failure and recovery were tested; another full reboot after
the final startup fix has not yet been verified.

The model proposes selections; local Python validates and executes them through
the host VirtualBox SDK. Model accuracy and anti-cheat compatibility are not
guaranteed. The supplied service path and keyboard rule are specific to this host
and must be adjusted when installing elsewhere.
