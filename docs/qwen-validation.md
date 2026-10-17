# Qwen web validation — 2026-10-16

The configured fallback is Qwen3.8-Omni-Flash with Thinking, using the installed
Chromium and a private native profile. Gemini remains first in the provider list.
ChatGPT is excluded from the configured path. The user selected the Qwen model
and thinking mode and authorized continued development after reviewing Qwen's
web-automation restriction. This validation documents technical behavior, not
provider permission or guaranteed future compatibility.

## Live observations and tests

Qwen restored the signed-in account and selected settings after the dedicated
browser reopened. Its anonymous shell briefly displayed Log in before the saved
account loaded; the adapter now allows a bounded initialization grace period.
This single restoration does not establish long-term session renewal behavior.

The upload input is permanently present, but selecting Upload attachment arms
its handler for one upload. Each cycle therefore opens that menu first. While
uploading, the preview container has `vision-item-container-uploading` and a
`vision-item-loading-icon`; Send can already be enabled. Seth waits for those
markers to disappear and for a preview matching the exact PNG filename.
The native adapter waits for Chromium's intercepted file chooser and assigns its
backend node directly. The earlier selector-based path intermittently lost the
marked input before assignment. The chooser path also clears that exact input
before re-selecting the same file, without dispatching an empty change event.

Qwen's default prompt requests original pixel coordinates. Initial normalized-grid
table probes produced incorrect positions despite correct categories; omitting
dimensions still missed some controls. Original-pixel prompts passed at both
1000 × 800 and 1600 × 900. Gemini and API prompts retain their existing format.

Qwen's final answer is read only from `phase-answer`, excluding its thinking
content. A matching request identifier, exactly one user/assistant turn, a
response-footer Copy control, no Stop control, a stable final answer, and the
existing strict decision parser are all required. Repeated requests start a new
chat through Qwen's UI without reopening the browser.

Controlled 1000 × 700 image-only question tests:

| Test | Result | Elapsed |
| --- | --- | --- |
| Initial upload/DOM probe: identify an exact random-looking code | Correct option B and a point inside its radio button | Not timed |
| Full adapter: select all odd numbers from 27, 38, 49 | Correct A and C; both points inside their checkboxes | 11.12 s |
| Same MCMA PNG again, new chat, persistent reconnect | Correct A and C; both points inside their checkboxes | 15.76 s |
| Injected Gemini login failure, real Qwen single-choice response | Fell back once to Qwen; correct B and radio-button point | 15.68 s |

After the native chooser and pixel-coordinate changes, four consecutive requests
through the normal controller passed without browser instrumentation or upload retries:

| Screenshot | Result | Elapsed |
| --- | --- | --- |
| Single-choice, 1000 × 700 | Correct B; point inside its radio control | 8.78 s |
| MCMA, 1000 × 700 | Correct A and C; both points inside their checkboxes | 15.49 s |
| Classification, 1600 × 900 | All three categories and radio positions correct | 15.42 s |
| Classification, 1000 × 800 | All three categories and radio positions correct | 14.38 s |
| Configured Gemini → Qwen fallback, 1000 × 700 | Actual Gemini sign-in failure; Qwen returned correct B and radio position | 24.59 s |

The first fallback test intentionally injected Gemini's failure. The later
configured-path test exercised the actual sign-in failure and a genuine Qwen
response; Gemini stopped before upload. Gemini login remains unresolved. Qwen's
responses were genuine live responses to uploaded test images. No VM input was
sent. The VM was powered off when its status was checked.

## Local regression coverage

`tests/test_qwen_web.py` drives a real Chromium against a local HTTP fixture.
It covers byte-for-byte PNG transfer, controlled textarea input, per-upload menu
arming, upload progress while Send is enabled, exact attachment identity,
same-file upload after persistent reconnect, new-chat reset, separate thinking
and final content, incomplete-answer rejection, transient anonymous startup,
signed-out failure before upload, delayed input activation, and a native chooser
whose input differs from the stale page selector. It also verifies that the
chooser event actually fires. Cleanup unit tests check expired deadlines,
cancellation, and socket failures without masking the original error.

The complete suite passed all 121 tests in 109.092 seconds with Lightpanda,
Chromium, and native-login integrations enabled.

Run the Qwen regressions with:

```sh
SETH_CHROMIUM_TESTS=1 .venv/bin/python -m unittest tests.test_qwen_web -v
```

## Remaining checks

- A Qwen VM dry cycle and explicitly authorized click cycle when the VM is running.
- Real VM table categorization through Qwen; controlled table screenshots passed,
  but a VM screenshot and execution have not yet been exercised with this provider.
- Longer sessions, quota exhaustion, and provider UI changes. Such failures stop
  the affected provider attempt; they do not relax answer validation or retry an
  ambiguous submission to the same provider.
- Google login persistence remains a separate unresolved issue documented in
  [the browser validation notes](lightpanda-validation.md).
