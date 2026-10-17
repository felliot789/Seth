# Qwen web validation — updated 2026-10-17

The configured Qwen model is Qwen3.8-Omni-Flash with Thinking, using the installed
Chromium and a private native profile. The hotkey listener now starts with API
and cycles API → Gemini → Qwen using Ctrl+E+R; it tries only the selected provider.
The saved provider list for nonmanual runs is currently Qwen only.
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

## First monitored 25-question run

The user requested a Qwen-only run and a confidence record for every question.
The first ten questions have a recorded selection; questions 2–10 have fresh-frame
selection confirmation. Question 1 predates that confirmation telemetry.

Question 11 first returned no action because relevant question text was below
the visible area. Its reported confidence was zero. After the user scrolled and
retried, Qwen was still generating when the 150-second attempt limit expired.
That attempt sent no VM input. Qwen subsequently finished with confidence 0.6,
below the configured 0.85 threshold. This late response is recorded separately
and does not turn the timed-out cycle into a successful selection.

For this run, the provider list is temporarily Qwen only and its attempt limit
uses the existing 240-second total budget. This allows more thinking time; it
does not guarantee completion or a sufficiently confident answer. The threshold
and screenshot freshness checks remain in place. The mock's timer expired
before the run resumed. It ended with ten recorded selections and question 11
unresolved; questions 12–25 were not observed. Its files are archived under
the ignored `captures/qwen-history/mock-1-20261017/` directory.

Abstention results now preserve a reported confidence in the cycle log when it
is a finite number from zero to one. Missing or invalid values remain absent;
they are not replaced with zero, and later cycles cannot reuse an earlier value.
The ignored `captures/qwen-confidence-25.csv` and
`captures/qwen-confidence-attempts.jsonl` retain per-question and per-attempt
records, including the late response and timeout as separate facts.
All 152 local tests passed with Lightpanda, Chromium, and native-login
integrations enabled in 130.368 seconds after the confidence logging change.

## Scrolling question context — second mock

The new mock's first question contains a diagram and instructions above a
three-row classification table inside the right column's independent scrollbar.
The diagram and table cannot fit in one visible viewport at the current zoom.

The Qwen path now captures overlapping top/middle sections of configured native
scroll panels, leaves each panel at its bottom, and uploads their reference crops
above a numbered copy of the final live answer frame in one PNG. Only controls
within the question columns are labeled; context copies never supply click
coordinates. Source VM coordinates remain unchanged despite the larger upload.
The native SDK uses one bounded scrollbar drag per position and always attempts
button release, including on cancellation or ambiguous input failure.

The live capture pass found the right column, reached both endpoints, collected
two reference sections, and returned to all six radios in the final table.
A genuine Qwen inference-only test read the combined image and returned the
correct false/true/false plan, IDs `02`/`03`/`06`, at confidence 0.9 in 39.851
seconds. It sent no answer input. A subsequent user-triggered cycle collected
two context sections and returned the same plan at confidence 0.8. That fell
below the unchanged 0.85 threshold, so Seth selected nothing. A sufficiently
confident Qwen selection remains unverified on this live table.

Capture is bounded by configured columns and a maximum of eight views. Native
gray thumbs with up/down arrows are required; unsupported/custom scrollbar
themes may not be recognized. The question header and VM identity are checked
throughout collection. Before each answer click, the visible question columns
must still match, except around earlier verified selections. Question changes,
scroll changes, endpoint failures, and cancellation stop the plan. Every required
new control must be visible in the final answer view; cross-viewport answer
selection is not implemented, and the prompt requires abstention in that case.

The second mock has a fresh 25-question confidence ledger. The first mock's
results are retained separately. The standalone context probe is saved in the
ignored `captures/scroll-qwen-probe-result.json` file.
All 174 local tests passed with Lightpanda, Chromium, and native-login
integrations enabled in 138.134 seconds for the initial scroll implementation.

## Manual controls and shared scroll context

The hotkey listener starts at API / Single / Full. Ctrl+E+R selects API, Gemini,
or Qwen; Ctrl+Shift+E+R independently toggles Single/Scroll capture. Both changes
are idle-only, persist between questions, and reset on restart. Switching sends
no request and preserves the last result color. An uncertain answer never
automatically requests a different provider in this mode.

Ctrl+Shift+D+F cycles Hidden → Dot-only → Full → Hidden. Full includes the
selected provider and capture mode; Dot-only renders a small colored circle
without text or panel and never expands on a status change. Both modes place
the dot at the same screen position. Gray is idle, blue capturing, yellow waiting,
purple selecting, green done, orange uncertain/no-selection, and red error.

API and Gemini now support the same combined scroll-context image and strict
control-ID resolution as Qwen. The live-frame coordinates remain authoritative,
and every grounded click requires fresh-frame confirmation. Single capture
retains the existing API/Gemini coordinate behavior. Native scrollbar and final
answer-viewport limitations still apply to all providers.

The confidence ledger now records the actual provider and capture mode for each
attempt. Separate Qwen/Gemini/API confidence columns prevent another provider's
answer from being reported as Qwen confidence. Completed selections advance the
estimated question number; visual confirmation is recorded separately. Explicit
frame-to-question mappings can correct the estimates. Pending questions remain
blank, and the first mock's records remain in their archive.

All 199 local tests passed in 140.828 seconds with Lightpanda, Chromium, and
native-login integration suites enabled. Real Lightpanda and Chromium tests
verified one combined PNG upload, ID extraction, and original-frame coordinates;
API context uploads passed through both local streamed and nonstreamed HTTP
fixtures. Manual-switch tests cover no external requests, busy rejection, mode
persistence, no uncertainty fallback, and lock coverage through result logging.
The actual Qwen context probe above remains valid. Gemini's initial session
mismatch and subsequent successful context probe are recorded below.

## Table hover correction and current browser sessions

A user-triggered API/Scroll cycle submitted a valid classification plan, but
stopped after its first click because the gray hover background filled the
post-click comparison area. The original and post-click captures show that the
first control was selected successfully. Moving the guest pointer away clears
the hover state; strict confirmation then recognizes the selected glyph.

Seth now parks the guest pointer with no pressed buttons before the final scroll
baseline and after each grounded click. This preserves the existing strict
question and control checks, without retrying an answer click. Anonymous 40 × 40
control fixtures cover the hovered and cleared states, and SDK tests require
exactly one button-free move with the usual age/VM checks. Partial-plan errors
also retain the model decision and confidence.

The signed-in Gemini session was in tab 2 of the Qwen Chromium window; Seth's
previous Gemini endpoint selected a separate signed-out window. Gemini now uses
`browser_profile: "qwen"` and a private endpoint record for its actual Argon/Medium
tab. Qwen remains attached to its own tab. Both sessions passed login/model
checks without restarting the signed-in browser. Keep the shared window open or
minimized. Closing it affects both providers.

All 202 local tests passed in 143.684 seconds with the three integration flags.
The next user-triggered API/Scroll cycle completed at 07:14:38 on 2026-10-17.
It collected four context sections, preserved the first answered row, and
selected the two remaining rows at confidence 0.95. Both selections were
confirmed in fresh captures, validating the table-hover correction in the VM.

The live Gemini inference-only probe uploaded one combined PNG whose reference
section contained a code absent from the answer view. Argon/Medium selected the
correct badge `02` at confidence 1.0 in 14.709 seconds; local ID resolution
returned `(100, 380)` in the original answer frame. No VM answer input was sent.
The private result is saved in `captures/gemini-scroll-probe-result.json`.

## PNG/JPEG preview conversion and unsent drafts

The user-triggered Gemini/Scroll cycle at 07:15:00 on 2026-10-17 reached upload
completion, but Gemini converted the large PNG into a preview ending
`-controls_16fa84.jpg`. The adapter only accepted a `.png` preview, so it waited
for 240 seconds without pressing Send. DOM inspection confirmed one completed
attachment, enabled Send, no submitted user turn, and no model response. No VM
answer input was sent.

Gemini and Qwen now accept PNG/JPG/JPEG preview extensions with the exact source
stem and an optional six-character hexadecimal suffix. Wrong stems, malformed
suffixes, additional extensions, and unsupported formats remain rejected. The
API inspects the same bytes it sends and sets the PNG or JPEG MIME type from
their contents, including when the filename extension differs. JPEG reference
and answer images can also be packed into a PNG scroll-context contact sheet.

Gemini retained this unsent image even after New chat was clicked. Recovery now
reveals the attachment's close button when it is hidden until hover, removes
that one draft attachment, and clears the old composer. The live stuck draft
was cleared successfully without submitting it. Qwen's new-chat path also
resets a pending unsent image before another upload. This does not retry an
ambiguous submission to either provider.

A live replay rebuilt the failed three-section context image at 1920 × 1986
pixels. Gemini again converted the PNG preview to `-controls_176b3e.jpg`; Seth
recognized it, confirmed upload completion and enabled Send, and submitted once.
Argon/Medium returned a valid four-option MCMA plan at confidence 0.99 in
133.039 seconds. The plan resolved IDs `01`–`04` to the original live-frame
checkbox coordinates. This inference-only replay sent no VM answer input; its
result is saved privately in `captures/gemini-large-format-probe-result.json`.

All 209 local tests passed in 179.937 seconds with Lightpanda, Chromium, and
native-login integration suites enabled. Coverage includes converted preview
names, single submission after draft recovery, a close control hidden until
hover, original-byte upload identity, PNG/JPEG API MIME detection in both normal
and streamed requests, mismatched filename extensions, unsupported formats, and
JPEG scroll reference packing. Qwen's PNG/JPG/JPEG source and preview-conversion
paths passed against its real-Chromium local fixture; a converted JPEG preview
was observed on live Gemini. Both signed-in provider sessions passed a final
model check.

## Long graph question capture limit

The graph-based MCMA question at question 11 stopped identically with Gemini,
Qwen, and API before scrolling or inference. Its scrollbar thumb was 58 pixels
high inside a 351-pixel track. Preserving the existing overlap required nine
views; the configured maximum was eight. These failures were in the shared
collector, not provider requests.

The configured and default cap is now twelve views. A larger question produces
the `scroll_limit` result with `required_views` and `max_views`, and Full status
shows **Scroll limit**. The image-height and pixel-count bounds remain in place.
Packing eight reference rectangles at the failed question's actual geometry
plus its live frame yields 1920 × 2856 pixels (5,483,520 pixels), within both
bounds. This was a geometry-only check, not a reconstruction of missing views.
Selections across different answer viewports remain manual at the user's request;
only the final live view has clickable badge IDs.

All 49 targeted scroll, runtime-selection, and control-target tests passed in
9.077 seconds. A generated short-thumb viewport reproduced the nine-view case,
failed before input at eight, then collected all nine at twelve. Runtime checks
confirmed that a limit failure invokes neither inference nor answer clicks for
API, Gemini, or Qwen. The VM had advanced to question 12 before the fix, so the
original nine-view question has not been replayed live.

## Selecting across captured answer views

Scroll mode now assigns IDs to fully visible supported answer controls throughout
the capture. Foreground alignment and the native thumb's movement identify
overlapping copies, so one checkbox or table cell keeps one ID. The model sees
badges in every section and returns one complete plan. The host validates all IDs,
rejects repeated targets or two categories in one physical row, and groups the
remaining selections by their recorded view. This applies to API, Gemini, and
Qwen, using the existing Scroll toggle.

Before a return, the current VM, header, question columns, scrollbar position,
and previously confirmed visible selections must still match. Each return uses
one SDK drag, waits for settled content, parks the pointer, and checks the
restored view. A unique local vertical alignment accounts for native scrollbar
rounding; only the freshly verified control coordinates reach the clicker. A
changed target, ambiguous alignment, stuck scroll, cancellation, or failed
selection confirmation stops the remaining plan without retrying input.

Fourteen new generated-question tests passed in 36.709 seconds. They cover a
five-option graph MCMA, a nine-row table spanning views, two independent panels,
duplicate-ID rejection, same-row category conflicts, identical local coordinates
in different views, cancellation, external scrolling, changed answer content,
one-request execution, partial failure, and no return trips for dry/uncertain
plans. The API/Gemini/Qwen client checks confirm global-ID annotation, original
source preservation, private temporary-file cleanup, and retained view metadata.

A live capture of the Windows question at number 12 collected five views and
indexed five distinct radio options. Capture-only checks returned to sections
`1:2`, `1:3`, and `1:4` and verified their targets without answer clicks. The first
two returned one pixel from their original positions; local alignment measured
that shift and resolved fresh control coordinates. Private artifacts are
`captures/scroll-navigation-live-manifest.json`,
`captures/scroll-navigation-live-upload.png`, and
`captures/scroll-navigation-live-return-result.json`.

A full live categorization cycle on question 14 collected five views and indexed
six radio controls. Qwen returned **Benar / Salah / Benar** at confidence 0.90.
The first row's controls were outside the final view. Seth returned to section
`1:3` for that row, then to `1:4` for the remaining rows. All three clicks were
confirmed visually, from one inference request. The cycle took 55.353 seconds
from initial capture to completion; its decision, confidence, target IDs, view
IDs, and confirmation flags are saved privately in
`captures/scroll-navigation-live-q14-result.json`.

A full live replay of the original graph MCMA at question 11 collected nine
views and indexed five distinct checkboxes. Qwen chose the increasing
velocity-time graph and constant acceleration-time graph at confidence 0.90.
Seth returned to `1:1` and `1:3`, confirmed both selections visually, and accounted
for a three-pixel offset on the second return. It made one inference request;
the cycle took 88.362 seconds from initial capture to completion. The complete
result is in `captures/scroll-navigation-live-mcma-result.json`. Both live-cycle
confidences and their question numbers are also recorded in
`captures/scroll-navigation-live-confidence.json`.

All 225 tests passed in 226.830 seconds with Lightpanda, Chromium, and native
login integrations enabled. The full report is kept privately at
`captures/scroll-navigation-full-regression.log`.

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
