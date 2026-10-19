# Automatic scroll layout validation — 2026-10-18

Scroll mode uses automatic discovery in the supplied configuration. Clear
whole-page question columns are recognized locally and captured with guest wheel
input. Other layouts use one provider layout request; native independent panels
verify scrollbar ownership with reversible drags. Both paths reuse overlapping
capture and verified answer-control navigation, then make a separate answer
request. Single mode keeps its existing capture behavior.

The full regression command passed 272 tests in 293.813 seconds with Lightpanda,
Chromium and native-login integrations enabled:

```sh
SETH_LIGHTPANDA_TESTS=1 SETH_CHROMIUM_TESTS=1 SETH_LOGIN_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

All 25 final focused layout checks passed in 36.583 seconds. They cover the final question-reference guard,
visible-scrollbar/no-association rejection, and object-wrapped rectangle schema.
They include whole-page questions with a moving heading, independent panels with
reversed semantic roles, fixed/scrolling combinations, static content, global-ID
deduplication, return trips, changing/sticky countdowns, stale layouts, unknown
IDs, low confidence, cancellation, stuck thumbs, and unchanged-URL question
replacement. Anonymous native arrow and timer fixtures reproduce the live pixel
issues. Logs are private under `captures/auto-layout-final-*.log`.

## Live host checks

Gemini identified the current quiz's outer page scrollbar and main content
column. Four overlapping views recovered the earlier diagram and the answers.
Native-only follow-up capture with that recorded layout verified return targets
in sections `1:0`, `1:2` and `1:3`. These checks sent scrollbar drags and neutral
pointer moves, with no answer selections or navigation/submission clicks.

The final question-reference check also passed against the recorded native
before/returned pair. That replay finds the countdown after it moves from the
sticky position back into document flow and checks the complete question
foreground, including the edge rows previously omitted by alignment tolerance.
Its private result is `captures/auto-layout-native-return-replay-result.json`.

Qwen's first multiline layout response lost its nested rectangle-array fields
in the extracted web answer. The schema now wraps rectangles in objects and asks
for one-line JSON, while accepting the earlier array form for compatibility.
Malformed responses are rejected before guest input. The final Qwen response
used the object schema successfully on the VM's later static dashboard. This
was a format/static-layout check; Qwen scrolling on a live question was not
retested after the VM left the question page. API layout requests and both web
providers are covered by offline response/control-ID integration checks.

## Limits

Independent-panel discovery requires recognizable native vertical thumbs and
arrow buttons. Interacting nested scroll containers are rejected when the layout
cannot be verified. Collection retains the twelve-view and
existing contact-sheet limits. Model-proposed bounds and confidence are verified
locally, and uncertain ownership or changed content stops the cycle. Explicit
manual layouts and legacy configurations containing region coordinates remain
supported.

## Whole-page wheel capture — 2026-10-18

The latest single-page failure occurred before scroll input: the layout response
contained a blank identity rectangle. Valid anchors now survive extra blank
rectangles. If all proposed identity rectangles are blank, a locally detected
outer native viewport can ground the complete stationary browser chrome above
it. Without that local evidence, blank identity is still rejected.

The supplied configuration now sets `capture.scroll_context.scroll_method` to
`wheel`. A single `both` viewport marked `scrollable: true` uses guest wheel ticks
with no mouse-button press. It does not need a native scrollbar. Independent
panels retain the existing native path; drag remains an explicit configuration
choice. The wheel collector finds the top and bottom with two stable probes,
measures unique content translations, preserves at least 35% overlap, and keeps
the twelve-view limit. It calibrates a small initial step before increasing the
bounded wheel batch. A declared long page that never moves is rejected.

Each recorded section retains its measured pixel offset and wheel count. Return
navigation starts from a visually established top so extra ticks clamped at the
bottom cannot shift the target. Restored foreground, question identity, original
control outlines, earlier selections and post-click selection are still checked.
Answer inputs are never retried. A wheel SDK error has unknown completion and
stops the operation.

Generated scrollbar-free pages exercise first/last MCMA selections, overlap
deduplication, one layout plus one answer request, changed question content on
the same URL, cancellation, stuck scrolling, excessive steps and the view limit.
SDK tests cover both wheel directions, monitor origin conversion, stale frames,
invalid tick counts and cancellation between ticks without a button press.

A live single-tick VM probe moved the native thumb by 75 pixels and its inverse
restored the exact question pixels and original thumb position, with zero answer
clicks. Its private captures and result are recorded in
`captures/wheel-live-probe.json`. This checks actual guest wheel delivery and
reversal; it is not a completed live long-page answer cycle.

The wheel full regression passed all 290 tests in 331.542 seconds with all three
browser integration flags enabled. Two final stricter checks of changed-question
return rejection and stuck/jumping/over-limit pages passed in 21.516 seconds.
The full log is saved privately as `captures/wheel-full-regression.log`.

## Local whole-page discovery

The next live failure also occurred before wheel input: the sidebar changed
width during the provider's layout request, invalidating the initial screenshot.
Wheel mode now first recognizes conservative whole-page geometry locally. It
requires one native outer rail with a grounded browser viewport, a continuous
neutral divider below the browser chrome, and exactly two matching long neutral
card borders. The card interior below the divider becomes the moving area and
the browser chrome supplies stationary identity. No site coordinates or URL
are configured. Pages without this evidence retain provider inspection.

The three recorded failure screenshots, including expanded and collapsed
sidebars, produce the expected columns locally in less than one second after
scrollbar detection. Synthetic regressions exercise both widths, bypass of the
provider request, ambiguous geometry, missing viewport/divider, inner rails and
reflow before input. The usual wheel alignment and endpoint checks still apply;
local recognition does not infer timer masks or bypass content verification.

Automatic wheel discovery parks the pointer before capturing its baseline. A
generated hovered row at the top exercises two clamped upward probes followed
by complete collection; clearing hover after the first probe previously could
look like unexplained motion. Changes after the neutral baseline still fail
the existing discovery, identity and foreground checks.

A live diagnostic on the current long passage captured five overlapping
sections and indexed six controls. Returning to the answer section passed its
target check, and restoring every one of the five recorded sections passed the
question-content check. It sent zero answer clicks. The private result and frame
references are in `captures/full-wheel-native-probe.json`. This verifies full
capture and return navigation; it does not claim a submitted answer cycle.

An integrated `SethEngine` dry run then captured the same five sections and six
controls with zero layout requests and sent the combined image to Qwen. Qwen
returned a valid three-row selection at confidence 0.6, so the engine correctly
reported `low_confidence` against the unchanged 0.85 threshold and sent no
answer clicks. The diagnostic wrapper had expected only `dry_run` and labels
its result failed; the stored engine result distinguishes that assertion from
the successful scrolling and inference stages. Details are private in
`captures/astra-scroll-live-dry-result.json`.

The final live verification used the complete engine capture path with the
pointer deliberately starting over an answer row. The neutral baseline cleared
that hover, and collection again found five sections and six controls without a
layout-model request. All five recorded sections were restored and verified.
The three IDs from Qwen's actual response were grounded against this fresh
capture, and every target position passed its return check. This check sent no
new answer-model request and no answer clicks. Its successful result is private
in `captures/astra-scroll-final-live-verification.json`.

The final focused coverage comprises 29 layout tests and eight wheel tests.
The combined 36-test run passed before the last pre-probe identity guard; all
eight wheel tests then passed with that guard and its added same-URL replacement
regression. Logs are private in `captures/astra-layout-wheel-tests.log` and
`captures/astra-final-wheel-tests.log`.

## Long image-choice questions — 2026-10-19

The image-choice question exceeded the previous 12-view limit. The shared limit
is now 32 views, with enough bounded upward probes to return from the longest
supported capture. The 8192-pixel height and 16-megapixel upload bounds remain.

For whole-page wheel captures without fixed overlays, measured overlap is
removed to assemble one continuous image at the original resolution. Diagrams
crossing capture boundaries stay intact. Each global badge is drawn once in the
assembled page; its original frame and control coordinates still determine the
verified return position. Other layouts retain overlapping context sections.

The live question produced 16 views and five answer controls. Its assembled
image is 1383 by 5143 pixels. A round letter inside a diagram initially resembled
a sixth radio; unchecked controls now require a clear surrounding gap, with
bounded tolerance for native antialiasing. An anonymous glyph fixture guards
against that false badge.

All 25 document and control checks passed. A generated long page starts at the
bottom, requires more than 32 unit ticks to reach the top and more than 12 views
to capture, preserves diagram pixels exactly through assembly, and returns to
one single-choice target for a verified selection. Malformed, gapped and
oversized images are rejected. Existing native focus, MCMA, ten-control table
and selection-confirmation regressions also pass.

The final native run verified all five target returns in sections `1:3`, `1:4`,
`1:7`, `1:9` and `1:12`, including the off-screen question-reference check. It
sent zero answer clicks and zero model requests. The result is saved privately
as `captures/image-choice-live-verification.json`; the complete capture and all
five sequential diagnostic returns took 263.412 seconds. A normal single-choice
run only returns to its chosen target.

The final regression run passed 103 scroll, document, control and layout tests.
Its additional interrupt module initially failed to import because the command
omitted the test fixture path. Rerunning that module with `PYTHONPATH=.:tests`
passed all nine interrupt checks in 2.302 seconds. Logs are private under
`captures/scroll-final-validation.log` and
`captures/scroll-final-interrupt-validation.log`. The listener was restarted
after the native diagnostic completed.
