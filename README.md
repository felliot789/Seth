# Seth

Seth runs on the VirtualBox host. An explicit host request captures the Windows
VM's framebuffer, sends it to a vision model, and optionally selects the answers
for one question through VirtualBox's emulated mouse. It supports single-choice,
table classification, and multiple-answer checkbox questions. Nothing is installed
or executed by Seth inside Windows.

Completed milestones and verification results are recorded in [CHANGELOG.md](CHANGELOG.md).

On this configured host, Seth starts automatically when you log into KDE. To
start the whole application manually, run this from any directory:

```sh
seth up
```

`seth` with no arguments does the same thing. It starts the listener, keyboard
shortcuts, host indicator, and persistent browser with Gemini and Qwen tabs.
Repeating it reuses healthy services and browser sessions. Seth never starts,
stops, or changes the Windows VM; start it separately and open a question.
On a fresh checkout, install the command once with `./seth install` after the
Python dependencies and keyboard permissions below are configured.

| Shortcut | Action |
| --- | --- |
| **Ctrl+D+F** | Capture and answer the current question once |
| **Ctrl+Shift+G** | Interrupt the current run and return to idle |
| **Ctrl+E+R** | Cycle API → Gemini → Qwen → API |
| **Ctrl+Shift+E+R** | Toggle Single ↔ Scroll capture |
| **Ctrl+Shift+D+F** | Cycle Hidden → Dot-only → Full → Hidden |
| **Ctrl+Shift+Q+W** | Toggle indicator text White ↔ Black |

Hold the modifiers, press both letters together in either order, then release
all keys. Startup defaults are **API · Single · Full**, with white text. Provider and capture
changes only apply while idle; busy presses are discarded. Switching sends no
inference request, and uncertainty does not automatically change providers.
Check startup with `seth status` and follow progress with `seth logs`. Use
`seth restart listener` after listener code or `.env` changes; browser sessions
remain open. Use `seth down` to stop all Seth services. `seth doctor` checks
services, device access, and API connectivity without capturing or submitting.
The examples below show individual modes and foreground alternatives; do not
start a second listener while the service is running.

VM capture and input use the official host VirtualBox Python SDK (`vboxapi`). Host
window borders, scaling, position, focus, and desktop shortcuts are outside the
coordinate path. X11, xdotool, xwd, and ImageMagick are no longer required.
Windows still receives mouse events; running on the host does not guarantee
undetectability by software inside the VM.

Install Python dependencies using the Python version supported by the host's
VirtualBox SDK:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

Install VirtualBox's Python bindings from the same host VirtualBox distribution.
On this Debian host they are already available to `/usr/bin/python3`. The venv
uses `--system-site-packages` to access them. Alternatively, set `vm.sdk_python`
to an interpreter with the SDK installed, for example `/usr/bin/python3`; the
helper needs only the standard library and the host SDK. Do not install anything
inside Windows. Run as the host user who owns the VM, without sudo.

In `config.json`, set `vm.name` to the exact registered VM name and `vm.screen`
to the zero-based guest monitor number (normally `0`). A `vm.uuid` setting takes
precedence over the name. List registered and running VMs with:

```sh
VBoxManage list vms
VBoxManage list runningvms
```

Check the connection and capture locally without calling inference or clicking:

```sh
.venv/bin/python main.py --mode status
.venv/bin/python main.py --mode capture
```

The VM must be running with an awake, enabled guest display. Capture does not
wake Windows. Clicking requires absolute mouse support, normally provided by a
VirtualBox USB Tablet. If it is unavailable, configure that pointing device in
VirtualBox while the VM is powered off. Seth never silently falls back to host
mouse control or changes the VM's device configuration.

Copy `.env.example` to `.env` if needed, then configure your vision provider:

```dotenv
INFERENCE_BASE_URL=https://your-provider.example/v1
INFERENCE_API_KEY=your-key
INFERENCE_MODEL=your-provider-vision-model
```

The base URL may also be the full `/chat/completions` URL. Select a model that
accepts images. Environment variables override `.env`; legacy `SETH_API_*` and
`SETTH_API_*` names remain supported. Restart the listener after editing `.env`.
Keys are never written into `config.json`. `.env` and captures are ignored by Git.
A normal cycle sends the screenshot to your configured inference provider.

If the overlay reports **API offline** and the log shows HTTP 530, the inference
gateway cannot resolve its upstream origin. Restart the provider's tunnel on the
machine hosting inference, or replace `INFERENCE_BASE_URL` with its current URL
and restart `seth-listener`. A screenshot, model, or API-key change does not fix
that origin DNS failure. Seth makes no automatic retry and sends no clicks for
this error. See [Cloudflare's HTTP 530 documentation](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-530/).

Requests use the image format in the official
[Chat Completions API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Set `api.response_format` to `"json_object"` if the provider supports JSON mode,
or `null` to omit it. A single complete Markdown JSON code fence is accepted for
gateways that add one. Local JSON, action, coordinate, and confidence validation
still applies; surrounding prose and multiple JSON fragments are rejected.

Inference input accepts PNG and JPEG. API requests inspect the image bytes and
set `image/png` or `image/jpeg` accordingly, including when a file's extension
differs from its contents. Scroll contact sheets are generated as PNG.

## Browser web inference (experimental)

Seth also has a web backend for sending the captured PNG to a model's web chat.
Its Python controller drives either Lightpanda or an installed Chromium/Chrome
directly over CDP. It needs no standby AI agent, Playwright, or Node. This host now
supports Gemini and Qwen with separate authenticated sessions. The hotkey
listener starts with API and selects one provider at a time. Outside hotkey mode,
`inference.backend` selects the backend for foreground or socket-only runs.

Install the controller:

```sh
.venv/bin/pip install -r requirements-browser.txt
```

Gemini is configured with `web.sites.gemini.engine: "chromium"` because the
current Lightpanda build fails to initialize its signed-in chat on this host.
Gemini also sets `keep_browser_open: true`: its dedicated browser stays open
between requests, and the Python controller reconnects to the same chat tab.
You can minimize that window; no standby agent is needed. This avoids transferring
or repeatedly restarting the authenticated session. Set
`web.chromium_executable_path` if the executable is outside the normal PATH.
Exported cookies are not re-imported over the native profile.

To select Lightpanda for a provider, set its `engine` to `"lightpanda"` and run
`.venv/bin/python web_inference.py --install`. This is also the default engine for
providers without an override. Remove any shared `browser_profile` override
when switching away from Chromium. The verified official binary lives under
`~/.local/share/seth/runtime/lightpanda/`, separate from
system packages. Each downloaded version has its SHA-256 recorded in
`release.json`; `current` selects the installed version. Startup never downloads
or updates the browser. Set `web.executable_path` to pin a specific binary.

The manual web choices are Gemini (Argon / Medium) and Qwen
(Qwen3.8-Omni-Flash / Thinking). This host keeps them in separate tabs of the
same persistent Chromium window. Gemini sets `browser_profile: "qwen"` to use
that native profile, with a separate private endpoint record selecting its tab.
The default profile is the provider's own directory. Closing a shared window
closes both providers' tabs; leave it open or minimized between requests.
`web.providers` controls only nonmanual runs; this checkout currently configures
Qwen there for the practice-question observation run. The hotkey listener uses
exactly the provider selected with Ctrl+E+R.
Each site needs its own sign-in session and the visible label of the selected
vision model in `web.sites.<provider>.model_label`. Qwen also verifies its separate
thinking control. Its screenshot uploads, single-choice and MCMA answers, repeated
same-file questions, and a controlled fallback from Gemini have passed live tests;
see [Qwen validation](docs/qwen-validation.md) for scope and remaining checks.
ChatGPT web integration has been withdrawn from the configuration because
[OpenAI's consumer terms](https://openai.com/policies/terms-of-use/) prohibit
automated extraction of outputs. The existing API backend remains available.

The login helper uses an installed Chromium/Chrome browser for manual sign-in.
It saves the native profile for Chromium and exports only the selected site's
cookies for Lightpanda:

With the application services running, sign in directly in the existing Gemini
and Qwen tabs and select their configured models and thinking levels. The browser
manager detects readiness and saves the site's cookies automatically. Check
`seth status`; login, verification, and model problems are reported separately.
Startup and status checks preserve conversations and unsent drafts.

For standalone use without the browser service, the manual login helper is:

```sh
.venv/bin/python web_inference.py --provider gemini --login
.venv/bin/python web_inference.py --provider qwen --login
```

Sign in on the KDE desktop and select the configured model and thinking level.
For Gemini and Qwen, keep the window open after the helper reports that it is
ready; it hands that exact session to Seth. Minimizing the window is supported.
Other providers close normally unless their `keep_browser_open` setting is
enabled. The cookie export is saved immediately.
For another installed executable, add
`--login-browser /path/to/chromium` and set the same executable in configuration.
Lightpanda itself needs no desktop browser during inference.

The managed browser endpoint is bound to loopback and recorded privately alongside
the native profile under `~/.local/share/seth/web/<provider>/`. `seth down` closes
the managed browser normally and flushes its profile. The browser service repairs
closed tabs and reopens a closed window while running. For a standalone browser
without the service, close it with:

```sh
.venv/bin/python web_inference.py --provider gemini --close-browser
```

Closing this browser or restarting the computer may require signing in again.
The API backend is available while web authentication is being repaired.

For Lightpanda, alternatively use an existing browser to export the site's cookies
to a local file. Import CDP/Puppeteer JSON, Playwright storage-state JSON, or
Netscape cookies without pasting session secrets into chat:

```sh
.venv/bin/python web_inference.py --provider gemini --import-cookies /path/to/cookies.json
.venv/bin/python web_inference.py --provider gemini --check
.venv/bin/python web_inference.py --provider gemini --image captures/question.png
```

The image command accepts PNG or JPEG, uploads to exactly the named provider,
and prints a validated decision; it never sends VM input. Cookies are filtered to that provider's domains
and stored in private files under `~/.local/share/seth/web/<provider>/`. Updated cookies are
saved atomically after successful operations when time and cancellation allow.
Failed operations preserve the imported session. Cookie import does not
guarantee that a website will accept the session; reauthentication,
device-bound sessions, and additional browser storage may require further work.

`--check` opens an empty chat and checks login, model, and any configured thinking
label without uploading a screenshot. `web.sites.gemini.thinking_label` is
`"Medium"` on this host. Run a Seth dry cycle with
`.venv/bin/python main.py --backend web`. `INFERENCE_BACKEND=web`
or `inference.backend: "web"` selects the same backend for nonmanual runs. The
hotkey listener starts at API regardless of that saved setting. An explicit
`--backend` argument can override its initial choice. Restart after configuration
changes; manual selections last until the listener restarts.

Each attempt starts an empty chat using the site's new-chat control when reusing
a live session, and requires an attachment matching the image's filename,
upload completion, the configured model, the current request's user message,
and a completed answer. A one-second stable answer and absence of a generation
control supplement the completion signal. The shared JSON, confidence, geometry,
capture-age, and target checks still apply. A provider is tried at most once per
cycle. Nonmanual web runs can fall through to the next configured provider on
errors, and only the first valid answer reaches the clicker. The hotkey listener
never falls through: change providers yourself and trigger again. An explicit
`action: "none"` is accepted as an abstention.

Gemini can convert a large PNG into a JPEG preview during upload. Gemini and
Qwen accept PNG/JPG/JPEG preview extensions and an optional six-character hex
suffix while requiring the full original filename stem. Upload completion and
Send readiness are still checked. A pending unsent image is removed before a
new cycle; Gemini's attachment close control is revealed when necessary.

`web.timeout_seconds` is the total fallback budget (240 by default).
`web.attempt_timeout_seconds` caps an individual attempt (150 by default;
240 in this checkout).
Fallback never resets the total budget or the screenshot's original age. The
overlay reports the current provider, upload, wait, and web errors. Results record
the successful provider and preceding attempt error codes without session data.

Lightpanda's support for file inputs does not prove compatibility with every
site's upload preprocessing or JavaScript. Tests with both real engines verify a local
PNG upload byte for byte, cookie restoration across browser restarts, complete
answer extraction, cancellation, and failure handling. Gemini screenshot inference
has passed live checks using the persistent Chromium session. Qwen has also
completed a VM answer cycle with the selected control confirmed in a fresh capture;
see [Qwen validation](docs/qwen-validation.md) for scope and limitations.
ChatGPT web integration has been withdrawn.

Run the offline integration suites with installed browser engines:

```sh
SETH_LIGHTPANDA_TESTS=1 SETH_CHROMIUM_TESTS=1 SETH_LOGIN_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

These tests serve local fixtures and never contact a model provider or live VM.
Each environment flag enables that engine's local integration tests; without
them, browser integration tests are skipped. Configuration, fallback, API, SDK,
hotkey, and overlay tests still run. The login helper test uses a local fixture
with an installed Chromium browser.

The latest complete run on this host (2026-10-18) passed **290 tests** with all
three integration flags enabled. Local browser fixtures verified combined-image
uploads and control-ID resolution; API fixtures covered streamed and nonstreamed
context requests. Separate live Gemini Argon/Medium and Qwen Thinking probes
read earlier question sections and returned the correct control IDs without VM
answer input. The table-hover correction is covered by anonymous control glyphs
from the actual stopped cycle. Detailed results and remaining live checks are
recorded in [the validation notes](docs/qwen-validation.md). The latest run also
covers PNG/JPEG preview conversion, API MIME detection, JPEG scroll references,
and recovery from unsent attachment drafts. Replaying the actual large Gemini
scroll image completed successfully after its preview was converted to JPEG.
Scroll-back coverage includes graphical MCMA options, tall classification tables,
overlap deduplication, independent panels, verified tiny panel nudges, restored-control
checks, and stopping after an unconfirmed click. Live Qwen cycles selected all three categorization
rows across two views and two graph-MCMA options across two other views; every
selection was visually confirmed. Both plans reported 90% confidence.
The latest regressions also cover persistent native radio/checkbox focus
rectangles, all ten controls on alternating table rows, and full table/MCMA
execution with real glyphs and fake VM input. Scrollbar-free whole-page fixtures
cover wheel capture and first/last answer return trips. A live wheel step and
its inverse restored the exact question pixels with no answer clicks.

## Running Seth

Run one dry cycle, then exit:

```sh
.venv/bin/python main.py
```

A dry cycle captures and calls inference without clicking. Add `--execute` to
allow the current question's answer selections, with a maximum of ten clicks.
Single-choice questions use one click; classification tables use one per
unanswered row; multiple-choice multiple-answer (MCMA) checkbox questions select
all correct unmarked options. Existing selections are preserved. If the model
abstains, its explanation appears in the cycle result and listener log.
`--mode manual` and `--mode test` are aliases
for one cycle. They do not start a listener. There is no repeat/auto-next mode.

For host-triggered operation, start a listener:

```sh
.venv/bin/python main.py --mode listen
```

To capture and select answers using **Ctrl+D+F**, start:

```sh
.venv/bin/python main.py --mode listen --execute --hotkey
```

Hold Ctrl, press D and F together (either order), then release all three keys.
Seth captures the VM, waits for inference, and selects the proposed answers for
that question. Watch the terminal for progress and completion, then use the
shortcut again for another question. Holding the chord does not repeat it;
requests received while busy are discarded. Answer correctness is not guaranteed.
Omit `--execute` to test inference without selecting answers.

Provider and capture selection are independent and persist between questions.
Ctrl+Shift+E+R enables scroll capture only when needed; toggle back for a question
that fits in one screenshot. Ctrl+E+R changes the provider without capturing,
uploading, or selecting anything. All six shortcuts fire on release.

Ctrl+Shift+G cancels the current capture, scrolling, inference, or remaining
selections. The indicator briefly shows Cancelling while the current operation
finishes cleanup, then Idle. The listener stays running with the same provider
and capture mode; use Ctrl+D+F for a fresh run. Any selections already applied
remain on the page. Late responses from an interrupted run cannot select answers.

The Linux shortcut uses [python-evdev](https://python-evdev.readthedocs.io/en/stable/usage.html)
and works independently of desktop focus, including on Wayland. It consumes the
D/F, E/R, shifted Q/W, and Ctrl+Shift+G so they do not activate the guest's individual shortcuts.
Incomplete chords pass through when released. Ctrl+P and Ctrl+O pass through normally.
Release all keys before starting.
Stop with Ctrl+C; the keyboard grab and virtual keyboard are released on exit.
If a keyboard disconnects or input events are lost, restart the listener.

The host user needs read/write access to the physical keyboard event device and
`/dev/uinput`. On this host the built-in keyboard is `/dev/input/event0`:

```sh
sudo setfacl -m "u:$(id -un):rw" /dev/input/event0 /dev/uinput
```

This permission lasts until the devices are recreated (usually reboot). Grant it
only to your own account; run Seth without sudo. The listener processes key state
locally and does not log or send keystrokes. Automatic discovery ignores mouse
devices; to select specific keyboards, add `"hotkey": {"devices":
["/dev/input/event0"]}` to `config.json`. Press the entire chord on one keyboard.
The socket trigger remains available alongside the keyboard shortcut.

Add `--overlay` for a small status indicator at the top-right of the host desktop:

```sh
.venv/bin/pip install -r requirements-overlay.txt
.venv/bin/python main.py --mode listen --execute --hotkey --overlay
```

**Ctrl+Shift+D+F** cycles **Hidden → Dot-only → Full → Hidden**; it never requests
a cycle. The listener starts in Full mode, so the first press hides it. Dot-only
shows a single 3-pixel colored circle with no text or panel. The dot stays in the
same screen position in Full and Dot-only modes, and status changes never expand
it. Full mode also shows the selected provider, Single/Scroll, and current status
as bare text, with a transparent background and no border or highlight. The
window also disables desktop tooltip shadows so no outline surrounds the text.
**Ctrl+Shift+Q+W** toggles that text between white and black. It works while busy
and in every display mode; changing visibility or status preserves the color
choice. Text starts white each time the listener starts. The dot retains its
status color independently of the text.

| Dot color | Status |
| --- | --- |
| Gray | Idle or cancelled |
| Blue | Capturing or scrolling; dry-run ready |
| Yellow | Waiting for inference |
| Purple | Selecting |
| Green | Done |
| Orange | Uncertain or no selection |
| Red | Error |

The last result color remains until the next answer cycle, including when you
change provider or capture mode. Hold the modifiers before the letters, then
release all keys. A connection/response timeout is labeled `API timeout`.
Logs record when the inference POST starts and when its response arrives.
`api.stream` is enabled in the supplied configuration so response events and
heartbeats can keep the tunnel connection active during inference. Seth waits
for the entire answer and a successful finish before validating or clicking;
partial, interrupted, or truncated streams never produce clicks. A clean stream
close after `finish_reason: "stop"` is supported even without a `[DONE]` marker.
The supplied configuration allows 240 seconds for a streamed answer and caps the
original screenshot age at 300 seconds, leaving time to validate and apply a plan.
If you hide it, it stays hidden through status changes and continues tracking
progress. A stopped partial plan shows how many selections completed; see the
listener log for details. Stop an existing listener before starting another.

The indicator is a separate Linux Qt window, with click-through input and no
keyboard focus. It uses XWayland (`xcb`) for corner placement on KDE/Wayland.
It is visible above host windows, including VirtualBox, and is not tied to the
VM window's position or visibility. No overlay code runs in Windows, and the
indicator is outside the guest framebuffer used by Seth's SDK capture.
These properties do not establish anti-cheat compatibility or undetectability.

`seth.target` groups two user services: `seth-listener` owns the shortcuts and
indicator, and `seth-browser` owns persistent Chromium processes and provider
readiness. Gemini and Qwen share one profile/window in this configuration, with
one endpoint record per tab. The browser remains open when the listener restarts.
An unavailable web provider does not prevent the listener from starting or the
API from being selected. Both services report startup completion to systemd;
provider readiness is shown separately from service readiness.

The application starts when you log into the graphical desktop and stops on
logout. It waits for your shortcut; startup does not request a cycle. It can
start with the VM off. The VM must be running before you request an answer cycle.

The templates in `deploy/` are rendered by `seth install` using this checkout's
absolute paths and Python environment. Reinstall after moving the checkout or
changing service definitions. `--config /absolute/path/config.json` before
`install` selects a configuration that subsequent commands remember. Installation
replaces the older listener-only autostart and restarts that listener once on the
next `seth up` if its definition changed. It does not install or update dependencies.

The supplied udev rules match this host's built-in keyboard. They grant access to it and
`/dev/uinput` to the active local desktop user; the module configuration loads
`uinput` at boot. With these installed, manual `setfacl` is unnecessary after
reboot. To install or restore the setup, adjust the keyboard rule when moving to
another machine, install the dependencies, then run:

```sh
.venv/bin/pip install -r requirements-browser.txt -r requirements-overlay.txt
sudo install -m 644 deploy/70-seth-hotkey.rules /etc/udev/rules.d/
sudo install -m 644 deploy/seth-uinput.conf /etc/modules-load.d/
sudo modprobe uinput
sudo udevadm control --reload-rules
sudo udevadm trigger --action=change --subsystem-match=input
sudo udevadm trigger --action=change --subsystem-match=misc --sysname-match=uinput
sudo udevadm settle
./seth install
seth up
```

The installed `~/.local/bin/seth` symlink works from any directory when that
directory is on PATH. The checkout's `./seth` command also works directly.

| Command | Action |
| --- | --- |
| `seth up` or `seth` | Ensure both services are running; report providers and API connectivity |
| `seth down` | Cancel listener work, then stop Seth and close its browsers |
| `seth restart listener` | Restart the listener while preserving the browser sessions |
| `seth restart browser` | Restart browser management and Chromium |
| `seth restart all` | Restart the application, including its browsers |
| `seth status` / `seth status --json` | Read service and cached provider readiness |
| `seth logs [listener\|browser\|all]` | Follow logs; add `--no-follow` to print recent entries |
| `seth doctor` | Check services, keyboard/uinput access, and API connectivity |

`up` is safe to repeat, including when the target is already active but a child
service has stopped. Successful startup means the services started; individual
providers may still need login, verification, or model selection. `doctor`
returns a nonzero status for issues needing attention. API checks use an empty
HEAD request, without credentials, image upload, or inference; reachable does
not establish valid credentials or a working model. The API endpoint is external
in this setup; there is no local inference server or tunnel for Seth to start.
Lightpanda remains an optional runtime started on demand for configured providers.

To disable automatic startup, run `systemctl --user disable seth.target`; use
`seth down` to stop the current session too. Start no second listener while the
service is running. A newly started listener resets to API, Single capture, and
the Full indicator with white text.
If KDE's display is still starting, the service retries until it can open the
overlay. A process lock prevents duplicate listeners; an abandoned socket from a
crash is recovered automatically after verifying that it has no live listener.

Add `--execute` to the **listener** command to enable clicks. Request a cycle
from another host terminal or over SSH:

```sh
.venv/bin/python main.py --mode trigger
```

The VM window does not need host focus. `--delay 3` optionally delays sending
the request by three seconds. Without `--hotkey`, no keyboard combination is observed or injected.
The trigger is a Unix socket in a directory accessible only to your host user.
`accepted` means the request was accepted; check the listener's log for its
result. Requests while busy are discarded. Each request returns to idle after
one validated decision plan. Ctrl+C or SIGTERM stops the listener and
cancels clicking that has not been dispatched. After an unclean shutdown, the
next listener automatically recovers its abandoned socket. It refuses to replace
a socket with a live listener or any unrelated file.

For Gemini and API Single capture with the default prompt, the model returns a point on a 0..1000
grid over the entire screenshot, explicitly
declaring `coordinate_space: "normalized_1000"`. Seth converts it to actual guest
pixels locally. Explicit `"pixels"` decisions are also accepted; a missing or
unknown coordinate space is rejected. Model IDs are passed through unchanged.
The SDK adapter then accounts for the monitor's origin and VirtualBox's one-based
mouse coordinates. Each frame records the VM UUID, session PID, state-change
timestamp, monitor, size, and origin. The SDK rechecks those values and capture age before pressing the
button. A stopped/restarted VM, display change, stale image, invalid decision,
low confidence, or unsupported pointing device prevents clicking. Host window
movement and focus do not affect this check. The supplied `capture.max_age_seconds`
is 300, allowing for the 240-second inference response budget;
`clicker.delay_ms` defaults to `[200, 800]`. Clicks use the exact selected
pixel without jitter or simulated host cursor movement.

Qwen receives a temporary copy of the screenshot with numbered badges over
supported unchecked controls. It returns badge IDs; Seth calculates click pixels
from the original control outlines. The original VM capture stays unchanged.
Unknown IDs, wrong control types, and duplicate controls reject the whole plan
before input. The current local detector supports radios 16–34 pixels across
and checkboxes 12–34 pixels across on a sufficiently uniform background.
After each Qwen click, a fresh capture must show the control's interior selected;
otherwise the cycle stops without retrying the click. Confirmed clicks include
`selection_verified: true` in the result. This verifies selection, not correctness.
Before confirming a grounded selection, Seth moves the guest pointer without
pressing any button to the left screen margin. This clears row hover shading
before the visual check. A native control's persistent focus rectangle may remain;
Seth allows a bounded six-pixel focus margin only while the filled interior,
control outline, and surrounding context still confirm selection. Control detection
uses each candidate's local background so alternating table rows retain their IDs.
Up to 64 controls can receive badges; an answer plan can select at most ten.
Scroll capture uses the same pointer position
for its final baseline. A failed confirmation still stops the plan without a
second click. Partial-plan errors retain the inferred decision and confidence.
Explicit custom Qwen coordinate prompts instead use a conservative local check
within 24 pixels of each predicted point; missing or ambiguous controls stop input.

For long questions, **Ctrl+Shift+E+R** selects Scroll
capture for API, Gemini, or Qwen. In nonmanual runs,
`capture.scroll_context.enabled` enables the same collection. The supplied
configuration uses `capture.scroll_context.layout: "auto"`. On each trigger,
Seth detects native scrollbars throughout the guest screenshot. In wheel mode,
the pointer is parked before taking the layout baseline. An unambiguous
whole-page column with a native outer rail, full-width header
divider and matching card borders is recognized locally, avoiding a layout
request. Other layouts use a separate inspection by the selected provider. That request
identifies the question and answer viewports, associates them with numbered
scrollbars, and locates stationary identity content and changing timer digits.
It supports one whole-page scroller, independent question/answer panels in
either order, and fixed content beside a scrolling panel. No region coordinates
are needed in automatic mode.

The host validates the layout and confidence and checks that the captured content
is still current. With `capture.scroll_context.scroll_method: "wheel"`, a single
long question page uses button-free guest mouse-wheel events. This is the scroll
effect of a two-finger gesture; no touchpad device or draggable scrollbar is
required. The model marks a single `both` viewport as `scrollable: true`, even
when no native scrollbar can be identified. Two stable wheel probes establish
each endpoint, and screenshot alignment measures actual movement between
overlapping sections. Return trips replay wheel ticks from the verified top
and must match the recorded content before a target is clicked.

Independent panels keep their native scrollbar ownership probes and drag/return
path. Set `scroll_method: "drag"` to retain that path for whole pages too. The
supplied configuration uses wheel for whole pages. Collection finishes with each
scrolling area at its bottom. Stationary URL/question anchors and the captured content
protect identity even when the question heading itself scrolls. Only bounded,
explicit timer/caret exclusions may change; answer controls cannot be excluded.
Timer widgets are matched visually in each view, including when they move with
the page and then stick to its top. Fixed overlays are excluded from scroll
alignment while the question and answer content remains checked.
Before the first answer selection, Seth revisits a recorded section of each
scrolling question area to recheck content that may now be off-screen.
The captured question uses one private PNG upload. For whole-page wheel capture
without fixed overlays, verified overlaps are removed to reconstruct one
continuous page at its original resolution. Diagrams remain intact and each
answer receives one badge. Other layouts retain the overlapping sections and
final screen. Supported answer controls receive
stable numbered IDs across the sections: overlapping copies of one control
have the same ID. One inference request can select graph-based MCMA options or
categorize table rows spread across different scroll positions. The current
configuration bounds collection to 32
views total across the areas, preserving at least 35% overlap. A question that
needs more stops before answer inference, with **Scroll limit** in Full status
and the required/allowed counts in the log. Native thumbs allow a limit check
before scrolling; the wheel path discovers the required count while collecting.
The combined image keeps its 8192-pixel height and 16-megapixel limits; duplicate
wheel overlap no longer consumes that image budget. Original captures and their
control positions remain available for verified return trips before clicking.

Automatic mode adds a layout request when local whole-page recognition cannot
establish the viewport. Recognized pages start wheel capture directly; both
paths make the existing answer request after complete capture.
See [automatic layout validation](docs/scroll-layout-validation.md) for test
coverage and the scope of the live checks.
Layout discovery uses the selected provider and never switches providers on
uncertainty. Whole-page wheel capture does not require a recognized scrollbar.
Independent panels still require native scrollbars and recognizable arrow
buttons. Uncertain ownership, insufficient visual overlap, and nested scrolling
containers that move one another produce errors instead of an incomplete capture.
Keep the VM page still while discovery, inference and selection run.
Keep the answer controls large enough for the local detector; reducing browser
zoom to squeeze the whole page into one frame can make controls undetectable.

For an explicitly configured layout, set `layout: "manual"` and provide normalized
`regions` rectangles and an `identity_box` covering a stationary question header,
excluding the timer. Existing configurations with `regions` and no `layout` keep
that manual behavior. The supplied configuration removes those fixed regions.

All three providers use numbered control IDs when receiving scroll context.
After validating the complete answer plan and confidence, Seth groups targets
by recorded view and returns to each needed scroll position. It checks that the
restored content matches its capture, finds the original control in the fresh
frame, and confirms each selection visually. Small vertical offsets on return
are measured locally before any click; contact-sheet coordinates are never used
as VM click coordinates. Full status shows **Returning to section …** and selection
counts. The existing Scroll toggle enables this behavior; no additional hotkey
or model request is needed for the return trips. API/Gemini Single capture retains
its coordinate behavior.

Between clicks, each panel can also account for a vertical nudge of at
most three pixels. This requires unchanged VM/question identity and an exact
foreground match after alignment. Native panels also require matching scrollbar
geometry and position.
Previously selected controls remain verified. The same check runs during click
confirmation, so a tiny movement in either the question or answer panel does not
cause a false stop. Larger movements, changed content, and ambiguous matches stop
the cycle without retrying a click.

The native drag collector requires a recognizable gray scrollbar thumb and matching
arrow buttons within the configured columns. Both collectors check the question header and
VM identity throughout capture, and the visible question/answer pixels before
each selection, allowing changes around earlier confirmed controls. A changed
view, uncertain scrollbar, failed endpoint, or excess capture count stops input.
Overlapping answer views must align uniquely; ambiguous or repeated content stops
the cycle rather than assigning guessed control identities. Two categories for
the same physical table row and repeated selections of one control are rejected.
Navigation failures show **Scroll stopped**, or **Stopped (n)** after input, with
the reason in the listener log. A previously confirmed visible answer is checked
again after returning to a view. Failed input is never retried automatically,
and Seth never presses Next or Submit.

A table or multiple-answer plan must identify its type and provide unique row
numbers or option labels. Every point is validated before the first click, and
the plan's confidence must meet the threshold. Before each planned selection,
Seth captures again and checks that the target's surrounding pixels are unchanged.
An input failure, changed target, cancellation, or stale frame stops the remaining
plan. A partial failure reports how many clicks completed and is never retried
automatically. The prompt asks the model to preserve existing selections and to
abstain when it cannot reliably complete the question. Keep the page stable:
local visual checks do not prove that the question's meaning is unchanged.

Seth uses short-lived host SDK helpers so native SDK objects never cross listener
threads. Each helper holds a shared VM session and releases it on completion.
Input consists of a move, one left press held for 80 ms, and a release. The hold
lets the guest's virtual input device observe the pressed state. Release is
attempted even when pressing reports an error. Input errors are not retried,
because completion may be uncertain. Outside Qwen and grounded scroll plans, `clicked` confirms
SDK event delivery, not application acceptance. Screen content can change during
inference even if display geometry stays constant. Start with a dry run.
The prompt excludes login, navigation, next, and submit controls and allows
`action: "none"`; model behavior is not guaranteed.

Implementation follows the official
[VirtualBox SDK reference](https://download.virtualbox.org/virtualbox/7.2.2/SDKRef.pdf)
for shared sessions, framebuffer screenshots, and absolute mouse input.

Run verification with:

```sh
python3 -m unittest discover -s tests -v
```

Tests use a fake SDK and a local mock HTTP service. The integration test runs the
real host helper subprocesses, captures and validates a PNG, checks the actual
vision request, and verifies one press followed by release at the expected guest
coordinates. Tests also cover session/display changes, cancellation, stale images,
input errors, and socket cleanup. They never connect to the live VM or provider.
Use `--mode status`, `--mode capture`, and a dry cycle for live verification.
