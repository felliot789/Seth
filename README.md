# Seth

Seth runs on the VirtualBox host. An explicit host request captures the Windows
VM's framebuffer, sends it to a vision model, and optionally selects one answer
through VirtualBox's emulated mouse. Nothing is installed or executed by Seth
inside Windows.

Capture and input use the official host VirtualBox Python SDK (`vboxapi`). Host
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

Requests use the image format in the official
[Chat Completions API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Set `api.response_format` to `"json_object"` if the provider supports JSON mode,
or `null` to omit it. A single complete Markdown JSON code fence is accepted for
gateways that add one. Local JSON, action, coordinate, and confidence validation
still applies; surrounding prose and multiple JSON fragments are rejected.

Run one dry cycle, then exit:

```sh
.venv/bin/python main.py
```

A dry cycle captures and calls inference without clicking. Add `--execute` to
allow one answer-selection click. `--mode manual` and `--mode test` are aliases
for one cycle. They do not start a listener. There is no repeat/auto-next mode.

For host-triggered operation, start a listener:

```sh
.venv/bin/python main.py --mode listen
```

Add `--execute` to the **listener** command to enable clicks. Request a cycle
from another host terminal or over SSH:

```sh
.venv/bin/python main.py --mode trigger
```

The VM window does not need host focus. `--delay 3` optionally delays sending
the request by three seconds. No keyboard combination is observed or injected.
The trigger is a Unix socket in a directory accessible only to your host user.
`accepted` means the request was accepted; check the listener's log for its
result. Requests while busy are discarded. Each request returns to idle after
one decision and at most one click. Ctrl+C or SIGTERM stops the listener and
cancels clicking that has not been dispatched. SIGKILL cannot clean up the socket;
remove a stale socket only after confirming the previous listener has stopped.

Coordinates are pixels in the captured guest monitor. The SDK adapter accounts
for that monitor's origin and VirtualBox's one-based mouse coordinates. Each
frame records the VM UUID, session PID, state-change timestamp, monitor, size,
and origin. The SDK rechecks those values and capture age before pressing the
button. A stopped/restarted VM, display change, stale image, invalid decision,
low confidence, or unsupported pointing device prevents clicking. Host window
movement and focus do not affect this check. `capture.max_age_seconds` defaults
to 90; `clicker.delay_ms` defaults to `[200, 800]`. Clicks use the exact selected
pixel without jitter or simulated host cursor movement.

Seth uses short-lived host SDK helpers so native SDK objects never cross listener
threads. Each helper holds a shared VM session and releases it on completion.
Input consists of a move, one left press, and a release; release is attempted even
when pressing reports an error. Input errors are not retried, because completion
may be uncertain. `clicked` confirms SDK event delivery, not answer correctness
or application acceptance. Screen content can change during inference even if
display geometry stays constant. Keep the question stable and start with a dry run.
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
