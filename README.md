# Seth

Capture a question from a running Windows VM, ask your selected provider, and
read the answer keys in the host status bar.

**One screenshot → one provider → answer keys.**

| Question format | Status-bar answer |
| --- | --- |
| MC | `2` — 1=A/first option, 2=B/second option, etc. |
| MCMA | `1, 3, 5` — all selected option numbers |
| Table categorization | `L, R, L, R, R` — left/right column, one entry per row from top to bottom |

Seth captures the currently visible guest screen as an unmodified PNG. Make the
whole question and its choices visible first. Choose the question format and
provider yourself; Seth displays the result without operating the VM.

## Daily use

Start your VM, then run:

```sh
seth up
```

| Shortcut | Action |
| --- | --- |
| Ctrl+D+F | Capture, ask the selected provider, display keys |
| Ctrl+E+R | Switch API → Qwen → Gemini → API |
| Ctrl+Shift+Z | Switch MC → MCMA → Table → MC |
| Ctrl+Shift+G | Cancel the current request |
| Ctrl+Shift+D+F | Cycle indicator Full → Hidden → Dot → Full |
| Ctrl+Shift+Q+W | Switch white/black indicator text |

Actions fire after you release the shortcut. Provider and format changes apply
while idle, clear the previous answer, and send no request. On restart, Seth
reads the starting choices from [config.json](config.json).

Successful feedback contains **only the answer keys**. Long lists wrap and stay
visible until the next capture, selection change, cancellation, or restart.
Choose Full indicator mode to read the keys.

If the provider is uncertain or its confidence is below the configured threshold,
Seth shows **Uncertain**. A screen with no answerable question produces **No
answer**. Failures show **Error: …**. To get another view, switch providers and
capture again. Retries and fallback are manual.

## Providers

- **API:** a vision-capable model at a compatible chat-completions endpoint,
  configured through `.env` or environment variables.
- **Qwen and Gemini:** native Chromium sessions. Sign in and select your model
  and thinking mode in each provider tab. Seth checks the configured labels.

Browser sessions stay open independently of the listener. Restart just the
listener after changing its configuration:

```sh
seth restart listener
```

## Install and maintain

Seth runs on a Linux desktop with user systemd services, the host VirtualBox SDK,
Chromium/Chrome, and access to the host keyboard and `/dev/uinput`.

Follow [setup and configuration](docs/setup.md) for the Python environment,
credentials, VM selection, and shortcut permissions.

| Command | Purpose |
| --- | --- |
| `seth install` | Install the command and enable desktop-session autostart |
| `seth up` or `seth` | Start or reuse Seth's services |
| `seth status` | Inspect services and provider readiness |
| `seth doctor` | Check dependencies, keyboard access, and API connectivity |
| `seth logs listener -n 30 --no-follow` | Read recent requests and timings |
| `seth restart listener` | Reload the listener while retaining browser sessions |
| `seth down` | Stop Seth and its managed browser |

If `seth` is outside your PATH, use `./seth` from this checkout.

See [troubleshooting](docs/troubleshooting.md),
[contributing and architecture](CONTRIBUTING.md), and the
[current revamp notes](CHANGELOG.md).

Captures and raw provider replies are saved in `captures/` by default. Credentials,
captures, and browser profiles are private local data and are ignored by Git.
