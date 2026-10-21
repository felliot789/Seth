# Troubleshooting

Start with service status and recent listener logs:

```sh
seth status
seth logs listener -n 30 --no-follow
seth doctor
```

`doctor` checks API connectivity without sending a question. Its HTTP status is a
reachability check; it does not validate credentials or perform model inference.

| Symptom | What to check |
| --- | --- |
| No response to the shortcut | Release every chord key. Check the listener, keyboard device permissions, and `/dev/uinput` access. Follow [shortcut setup](setup.md#shortcut-permissions). |
| Indicator is invisible or only a dot | Cycle Ctrl+Shift+D+F to Full mode. Ctrl+Shift+Q+W changes text color. On Wayland, check XWayland and the desktop environment used to start Seth. |
| Wrong provider or question format | Select it while idle with Ctrl+E+R or Ctrl+Shift+Z. Changes during a running request are ignored. Startup choices come from the configuration. |
| `Uncertain` | Make the complete question and options legible, confirm the format, and capture again. You can manually try another provider. |
| `No answer` | The capture may show a loading screen, sign-in, or no complete question. Prepare the screen before triggering. |
| VM not found or capture error | Check `vm.name`/`vm.uuid`, the running VM, monitor index, and host SDK access under the same user. `main.py --mode status` checks metadata. |
| API authentication/model error | Check the three `INFERENCE_*` values, endpoint, model access, and vision support. Restart the listener after editing `.env`. |
| Invalid answer/schema error | Read the matching `.reply.txt` next to the capture. The selected format must receive a numeric key/list or an L/R list in the shared JSON reply format. |
| Qwen/Gemini needs login | Sign in in the managed provider tab; wait for `seth status` to report readiness. |
| Model or thinking-mode mismatch | Select the configured model/mode manually in that provider's tab, or update its labels and restart the listener. |
| Managed chat tab closed | Use `seth up` and wait for the manager to reopen it. Inspect `seth logs browser` if it stays unavailable. |
| Request appears stuck | Cancel with Ctrl+Shift+G, inspect the error/logs, then capture again. Provider timeouts bound the request. |
| `busy` or a repeat shortcut does nothing | A request is already active. Wait for it or cancel before switching choices or capturing again. |
| Conflicting listener/socket | Stop the duplicate manual listener. Run one managed listener per checkout; avoid deleting the socket of a live listener. |

Full-mode answers contain only keys and can wrap across several lines. Table
output has one L/R entry per visible row in top-to-bottom order. Compare the
keys with the screenshot; confidence is a model estimate and cannot guarantee
that an answer is correct.

For slow cycles, compare `capture_s` and `inference_s` in the logs. A slow model,
upload, or provider response appears in inference time. Switching providers is
manual and starts a fresh capture/request.

For a listener problem, `seth restart listener` retains the browser sessions.
`seth restart browser`, `seth restart all`, and `seth down` close the managed
browser. Preserve its profiles and your `.env` while troubleshooting.
