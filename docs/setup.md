# Setup and configuration

## Host requirements

Use a Linux desktop with user systemd services, a running Windows VirtualBox VM,
and a Python 3 installation that can import the host's VirtualBox SDK (`vboxapi`).
The VM and Seth must run as the same host user. Install Chromium or Google Chrome
for Qwen/Gemini, and use XWayland on a Wayland desktop for status-bar placement.

Create the environment from the repository root:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt -r requirements-browser.txt -r requirements-overlay.txt
.venv/bin/python -c 'import vboxapi, evdev, PyQt6, websocket'
```

If `vboxapi` is missing, install the SDK matching your host VirtualBox installation
and make it available to this Python environment.

## VM and API

Edit [config.json](../config.json). Set `vm.name` to the registered VM name, or set
`vm.uuid` to its UUID. `vm.screen` is the zero-based guest monitor number. Start
the VM yourself before capturing.

Create your private API configuration:

```sh
cp .env.example .env
chmod 600 .env
```

Fill in `INFERENCE_BASE_URL`, `INFERENCE_API_KEY`, and `INFERENCE_MODEL`. Use a
vision-capable model. Both a base URL ending in `/v1` and a full
`/chat/completions` URL are supported. Remote endpoints require HTTPS; local HTTP
endpoints are supported. Process environment values take precedence over `.env`,
which takes precedence over corresponding `api` settings in the JSON file.

Seth reads `.env` beside the selected configuration file. Keep credentials there
or in the service environment; do not commit them.

## Shortcut permissions

Seth reads host keyboard events and consumes its shortcuts through a virtual
keyboard. Ordinary typing is forwarded. This is independent of the read-only VM
capture connection.

Inspect `/proc/bus/input/devices` for your keyboard name. Adjust the keyboard
`ATTRS{name}` in [the udev rule](../deploy/70-seth-hotkey.rules) before installing
it; the supplied rule matches `AT Translated Set 2 keyboard`.

```sh
sudo install -m 0644 deploy/70-seth-hotkey.rules /etc/udev/rules.d/70-seth-hotkey.rules
sudo install -m 0644 deploy/seth-uinput.conf /etc/modules-load.d/seth-uinput.conf
sudo modprobe uinput
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input
sudo udevadm trigger --subsystem-match=misc
```

The `uaccess` rules grant access to the active local desktop user. If access is
still missing, sign out and back in. For an explicit keyboard selection, add
`"hotkey": {"devices": ["/dev/input/by-id/your-keyboard-event-kbd"]}` to your
configuration, using a real event-device path from your host. Leave this setting
absent for automatic keyboard discovery.

## Install the services

Run these commands from your logged-in desktop terminal:

```sh
./seth install
./seth up
./seth status
./seth doctor
```

`install` writes the user units, creates `~/.local/bin/seth`, and enables
desktop-session autostart. `up` starts the services. Add `~/.local/bin` to your
PATH to use `seth` from any directory. For a separate configuration, use
`./seth --config /absolute/path/config.json install`; later commands remember it.

Sign in to Qwen and Gemini in the managed Chromium window. Select each model and
thinking mode manually to match its configuration. Seth verifies those choices;
it does not select models on your behalf. Each provider reports readiness
independently, so a sign-in issue in one provider need not block the others.

The checked-in configuration starts with API/Table and shares the Qwen native
profile with Gemini. Preserve that sharing to retain the existing sign-ins.
Provider and format shortcuts change the running listener only; a restart reads
the configured defaults again.

After `.env`, provider, format, confidence, or capture-setting changes, use
`seth restart listener`. Browser configuration changes may need a browser restart,
which closes its windows and can require signing in again.

## Configuration reference

| Field | Meaning / default |
| --- | --- |
| `provider` | Startup `api`, `qwen`, or `gemini`; default `api` |
| `question_type` | Startup `mc`, `mcma`, or `table`; default `mc` |
| `vm.name` / `vm.uuid` | Registered VM identity |
| `vm.screen` | Zero-based monitor, default `0` |
| `vm.sdk_python` | Optional SDK-compatible Python executable; default the running Python |
| `capture.output_dir` | Capture/reply directory relative to the configuration directory; default `captures` |
| `min_confidence` | Finite value from 0 to 1; default `0.85` |
| `api.base_url`, `api.api_key`, `api.model` | API settings overridden by the three `INFERENCE_*` variables |
| `api.timeout_seconds` | Positive response timeout, at most 300 seconds; default `120`, configured `240` |
| `api.stream` | Enable streaming transport; default `false`, configured `true` |
| `api.response_format` | `json_object` or omitted for endpoints without that option |
| `web.timeout_seconds` | Positive request timeout, at most 240 seconds; default `120`, configured `240` |
| `web.chromium_executable_path` | Optional executable path; otherwise find `chromium` or `google-chrome` |
| `web.state_dir` | Native browser state root; default `~/.local/share/seth/web` |
| `web.sites.<provider>.model_label` | Model label that must match the selected website model |
| `web.sites.<provider>.thinking_label` | Optional selected thinking-mode label to verify |
| `web.sites.<provider>.browser_profile` | Profile owner, `qwen` or `gemini`; default the provider itself |
| `hotkey.devices` | Optional list of host keyboard event-device paths |

Table mode supports two category columns. L and R refer to the visible left and
right columns, rather than particular category names. Fit all rows into the
captured screen. MC/MCMA numbers identify option positions, not the text of an
option that happens to contain a number.

## Data and diagnostics

- Captures: `capture_*.png`; raw replies: matching `capture_*.reply.txt`. Files are
  private to the host user and remain until you remove them.
- Native profiles: `<web.state_dir>/<profile owner>/native-profile`.
- Managed-tab records: `<web.state_dir>/<provider>/native-browser.json`.
- Install metadata and user units: under `~/.config/seth` and
  `~/.config/systemd/user` (`XDG_CONFIG_HOME` is respected).
- Listener logs include `capture_s`, `inference_s`, and `total_s` timings.

To inspect VM metadata without asking a provider:

```sh
.venv/bin/python main.py --mode status
.venv/bin/python main.py --mode capture
```

`capture` saves one screenshot and prints its path/display metadata. An explicit
terminal-only inference run is:

```sh
.venv/bin/python main.py --mode once --provider api --question-type mc
```

It prints JSON to the terminal; it does not update the managed status bar.
`--mode trigger` instead requests the existing listener with that listener's
current provider and format. See [troubleshooting](troubleshooting.md) for errors.
