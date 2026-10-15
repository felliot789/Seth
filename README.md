# Seth

Host-side, explicitly triggered screen capture, vision inference, and at most one
answer-selection click. This continuation lives in `/home/liot/work1/seth`; the
handoff copy under `Documents/deepseek-harness/default-workspace/Seth` is preserved.

Install Python dependencies in a virtual environment:

```sh
cd /home/liot/work1/seth
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The host must have an X11 session, `xdotool`, `xwd` (usually in `x11-apps`), and
ImageMagick (`magick` or `convert`). These are available on the current host.
Run as the logged-in desktop user with that session's `DISPLAY` and `XAUTHORITY`.
The application never invokes sudo. An X11 authorization error needs the correct
session environment; running the entire program as root is not a configuration fix.

Edit `.env` (already created with permissions 0600):

```dotenv
INFERENCE_BASE_URL=https://your-provider.example/v1
INFERENCE_API_KEY=your-key
INFERENCE_MODEL=your-provider-vision-model
```

The base URL may also be the full `/chat/completions` URL. Model names are provider
specific: select a model that accepts image inputs. Existing environment variables
override `.env`. Legacy `SETH_API_*` and `SETTH_API_*` names remain supported.
Restart the listener after editing `.env`; configuration is read on startup.
Keys are never written into `config.json`. `.env` and captured images are ignored
by Git. The screenshot is sent to the configured inference provider.

Requests use the image content format documented in the official
[OpenAI Chat Completions API reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Set `api.response_format` to `"json_object"` if your provider supports JSON mode,
or `null` to omit that parameter; strict local JSON validation still applies.

Run one dry cycle, then exit:

```sh
.venv/bin/python main.py
```

A dry run still captures and calls the configured API. Add `--execute` to allow
one click. `--mode manual` and `--mode test` are aliases for one cycle; they do not
start a listener afterward. There is no repeat/auto-next mode.

For the host-only trigger, start a listener in one host terminal:

```sh
.venv/bin/python main.py --mode listen
```

Add `--execute` to the **listener** command when clicks should be enabled. From
another host terminal, request one cycle and switch focus back to the VM during
the countdown:

```sh
.venv/bin/python main.py --mode trigger --delay 3
```

When triggering over SSH or from another device, `--delay` can be omitted if the VM
already has focus. No keyboard combination is observed or injected, including
Ctrl+Alt+B, Ctrl+C+B, or Ctrl+P+O. A key combination delivered to the guest could
have application-specific side effects, so no unverified replacement shortcut is
enabled. The host trigger is a Unix socket in a directory accessible only to your
host user. Nothing is installed inside Windows; there is no process-hiding code.

`accepted` means the listener accepted the request, not that a click succeeded;
read the listener's result log. Requests while busy return `busy` and are discarded.
The listener returns to idle after one decision and at most one click. Ctrl+C or
SIGTERM stops the listener, cancels pending clicking, and removes its socket.
SIGKILL cannot run cleanup: if a socket remains, a new listener prints its path;
remove that file only after confirming the previous listener is stopped.

`config.json` controls window name, optional `vm.window_id`, confidence threshold,
capture age limit, movement, bounded jitter, and the delay range (default 200–800 ms).
The window ID is rediscovered each cycle unless explicitly configured. Coordinates
are relative to the actual screenshot dimensions; the captured window's position
is added once. Multiple matching windows, changed geometry, lost focus, stale
captures, invalid coordinates, API errors, and low confidence prevent clicking.
The model can return `action: "none"`, including for login or loading screens.
Navigation and submission controls are excluded by the prompt, which is not a
guarantee of model behavior.

A successful `clicked` result confirms delivery of one input event, not correctness
of an answer or acceptance by the application. Screen content can change while
inference runs even if the window geometry stays constant. Keep the target stable
and start with a dry run. Timing and jitter do not guarantee undetectability.

Verification:

```sh
python3 -m unittest discover -s tests -v
```

The desktop integration test uses a separate Xvfb display, a simple local test
window, and a mock HTTP API. It captures a real PNG, verifies the actual request,
and observes exactly one mouse press at the expected window-relative coordinates.
It requires `Xvfb` and `python-xlib` in addition to runtime dependencies; missing
desktop test tools cause that test to skip. It never clicks the live VM.
Live provider/model compatibility and guest input still require a configured API
key and a subsequent live run.
