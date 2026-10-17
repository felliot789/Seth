# Qwen web validation — updated 2026-10-17

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

Qwen initially requested original pixel coordinates. Initial normalized-grid
table probes produced incorrect positions despite correct categories; omitting
dimensions still missed some controls. Original-pixel prompts passed at both
1000 × 800 and 1600 × 900, but later VM tests exposed large horizontal errors too.
The current default uses locally numbered controls, described below.
Gemini and API prompts retain their existing format.

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

## VM targeting regression — 2026-10-17

The first live VM request returned a point inside the correct single-choice
control. The next request identified the correct fourth option, but its point
was 13 pixels left and 3 pixels above the actual radio center. The SDK delivered
that input and reported `clicked`; a follow-up capture and the user confirmed
that the radio remained unchecked. A later response also returned an incorrectly
scaled horizontal coordinate. The earlier controlled images therefore did not
establish reliable targeting of small controls in real VM screenshots.

An initial local check centered nearby model points on the actual control and
correctly blocked another response whose horizontal coordinate was near half the
screenshot width. Because Qwen's position estimates were inconsistent, the default
path now detects supported unchecked controls locally and overlays numbered badges
on a temporary upload copy. Qwen returns exact badge IDs; Seth resolves their
pixel centers from the original image, without model coordinate estimates.

The Pillow detector supports radios 16–34 pixels across and checkboxes 12–34
pixels across on sufficiently uniform backgrounds, with at most 64 candidates.
Unknown IDs, mismatched control types, and duplicate controls reject the whole
plan. Already-selected controls are not labeled. Explicit custom coordinate
prompts retain the conservative nearby-point check with a 24-pixel search limit.
Gemini and API coordinate handling is unchanged. Temporary annotated images are
private and deleted after the attempt; full VM screenshots are not test fixtures.

After each Qwen click, a fresh VM capture must show a newly selected interior and
the same control shape. Hover/focus changes and broad screen changes do not count.
Failure stops the remaining plan without retrying input. Successful clicks carry
`selection_verified: true`; selection confirmation does not prove answer correctness.

A live retry also stopped before upload with `browser_layout`. A local Chromium
fixture reproduced the failure when the editor appeared before the upload control.
The Qwen adapter now waits up to three seconds for each pre-upload stage. It
also reopens an attachment menu that closes before its item was clicked, waiting
between attempts. Only an explicit missing/ambiguous/disabled-control result that
dispatched no click is recoverable; JavaScript, connection, protocol, and
submission failures still stop immediately. File selection occurs at most once.
Provider attempt logs now include the sanitized failure message as well as its code.

The configured Gemini → Qwen path then completed a fresh full VM cycle in
29.613 seconds. Qwen returned badge `04`; Seth resolved it to `(986, 470)` and
confirmed the selected control in a fresh framebuffer capture. The control's
appearance changed to a blue circle with a white check. This was the same graph
question at a different zoom/scroll position, without hard-coded answer coordinates.
Gemini still reported `browser_login`, so the successful provider was Qwen.

The subsequent MCMA probe exposed Qwen's Monaco-based code renderer. Its DOM
includes line numbers, nonbreaking spaces, and only the visible subset of lines;
reading its text is not a complete answer feed. When a completed final response
uses that renderer, Seth uses its scoped Download button to read the complete
UTF-8 code file into a private temporary directory. It limits the file to 64 KiB,
checks that the conversation stayed unchanged, deletes the file/directory, and
attempts to restore Chromium's normal download behavior even after failure/cancellation.
It does not copy to or read the host clipboard. Ordinary plain JSON and simple
code blocks retain their existing extraction path.

Uninstrumented live checks through the normal client, using 1600 × 900 screenshots
and the final numbered-control prompt:

| Screenshot | Result | Elapsed |
| --- | --- | --- |
| MCMA: select odd numbers from 27, 38, 49 | Correct A/C, badge IDs `01`/`03`, both local checkbox centers | 13.042 s |
| Three-row classification | Correct true/false/true, IDs `01`/`04`/`05`, all local radio centers | 18.457 s |
| Repeat capture of the answered VM question | Correctly recognized the blue selected answer and returned `no_action` | 29.199 s |

These checks sent no VM input. A prior live MCMA response rendered in the code
editor was also retrieved intact through its Download button and parsed as the
correct A/C plan; DOM-only extraction had rejected that same response.

## Local regression coverage

`tests/test_qwen_web.py` drives a real Chromium against a local HTTP fixture.
It covers byte-for-byte PNG transfer, controlled textarea input, per-upload menu
arming, upload progress while Send is enabled, exact attachment identity,
same-file upload after persistent reconnect, new-chat reset, separate thinking
and final content, incomplete-answer rejection, transient anonymous startup,
signed-out failure before upload, delayed input activation, and a native chooser
whose input differs from the stale page selector. It also verifies that the
chooser event actually fires. Coverage includes a late-mounted upload control,
an attachment menu dismissed before its item could be clicked,
the complete client path for numbered uploads, and a virtualized/truncated code
view whose full answer must be read through its Download control after generation.
Cleanup unit tests check expired deadlines, cancellation, answer size/encoding,
private file cleanup, and restoration without masking the original error.

`tests/test_control_targets.py` includes anonymous native-radio glyphs from the
actual missed click and successful retry, supported light/dark controls, selected
controls, ambiguous and dense layouts, duplicate resolutions, numbered cataloging,
strict ID resolution for all three question types, and post-click confirmation.
Cycle tests verify that an unchecked control produces an error and no click retry,
and that a failed confirmation stops a multi-answer plan before further input.

The complete suite passed all 150 tests in 133.453 seconds with Lightpanda,
Chromium, and native-login integrations enabled after these changes.

Run the Qwen regressions with:

```sh
SETH_CHROMIUM_TESTS=1 .venv/bin/python -m unittest tests.test_qwen_web -v
```

## Remaining checks

- Real VM MCMA and table execution through Qwen. Controlled screenshots cover
  those plans, but only single-choice selection has been confirmed in the VM.
- Longer sessions, quota exhaustion, and provider UI changes. Such failures stop
  the affected provider attempt; they do not relax answer validation or retry an
  ambiguous submission to the same provider.
- Google login persistence remains a separate unresolved issue documented in
  [the browser validation notes](lightpanda-validation.md).
