# Development milestones

## 2026-10-17 — Smaller dot and bare indicator text

- Halved the status dot's diameter from ten to five pixels in Full and Dot-only
  modes, retaining its screen position and status colors.
- Removed the text panel's black background and border. Full mode now renders
  bare white text on a transparent window.
- Ctrl+Shift+Q+W toggles the text between white and black, including while busy,
  hidden, or in Dot-only mode. Preserve that choice across status/mode changes;
  a newly started listener defaults to white.
- Consume the new chord on full release, including late Shift presses and either
  letter order. Incomplete/unshifted chords preserve their original event order.
- All 64 focused overlay, keyboard, runtime-selection, and cycle tests passed.
  Render checks confirm transparent Full-mode backgrounds, white/black glyphs,
  and the five-pixel dot; keyboard checks cover all five independent actions.

## 2026-10-17 — Track independent panel nudges after selections

- Diagnosed a two-column categorization stop after its first confirmed selection:
  the left question panel moved one pixel, while the right answer stayed stable.
  The preceding MCMA confirmation failure likewise followed a one-pixel movement
  in the answer panel; its checkbox had actually become selected.
- Track each panel's tiny vertical offsets before input and during confirmation.
  Require an exact foreground alignment within three pixels, unchanged question
  identity, compatible native scrollbar geometry/position, and visibly preserved
  selections. Larger movements and changed content still stop immediately.
- Replayed both recorded failures without VM clicks. Added regressions for
  independent question-panel nudges, checkbox confirmation after answer-panel
  nudges, rejection of larger movement, and rejection of even a one-pixel content
  edit. All 48 targeted tests passed in 62.128 seconds.
- A live API retry preserved the first table answer and visually confirmed both
  remaining rows at 95% confidence, from one inference request.
- All 229 tests passed in 245.027 seconds with Lightpanda, Chromium, and native
  login integrations enabled. Updated the README and validation notes.

## 2026-10-17 — Return to answers across scroll views

- Scroll mode now indexes answer controls across the captured sections for API,
  Gemini, and Qwen. Overlapping copies share stable IDs; graphical MCMA answers
  and table rows no longer need to fit in the final view.
- Validate one complete inference plan, group its targets by recorded view, and
  return using bounded SDK drags. Recheck the VM, question, current content,
  scrollbar geometry, restored content, and exact control before each click.
- Measure small vertical offsets after a return and use fresh VM coordinates.
  Confirm each selected glyph, preserve earlier selections, and stop immediately
  on ambiguity, changed content, cancellation, or unconfirmed input. No retries,
  automatic provider handoff, or Next/Submit clicks were added.
- Added generated graph-MCMA and nine-row table exercises, independent two-panel
  navigation, global-ID uploads for all three providers, no-input dry/uncertain
  plans, and partial-failure coverage. A live five-view capture indexed all five
  radio options and verified three return trips without answer input, including
  two one-pixel restoration offsets.
- A live Qwen categorization cycle then selected Benar / Salah / Benar at 90%
  confidence, returning to the offscreen first row and back down for the others.
  All three selections were visually confirmed from one inference request.
- The original graph MCMA collected nine views and five distinct options. Qwen
  chose two controls at 90% confidence; Seth returned to two earlier sections and
  visually confirmed both selections, correcting a three-pixel return offset.
- All 225 tests passed in 226.830 seconds with Lightpanda, Chromium, and native
  login integrations enabled. Updated the README and validation notes.

## 2026-10-17 — Expand long-question capture

- Diagnosed the shared API/Gemini/Qwen errors on the graph-based MCMA question:
  its native scrollbar required nine overlapping views, exceeding the eight-view
  capture limit before any scrolling or inference request.
- Raised the configured and default limit to twelve views, retaining overlap,
  endpoint checks, and the existing image-size bounds. Excess views now report
  **Scroll limit** with required/allowed counts in the cycle result.
- Kept selections across different answer viewports manual, as requested. Earlier
  graph options remain reference context; only final-view controls are clickable.
- All 49 targeted scroll, runtime-selection, and control-target tests passed,
  including nine-view collection and rejection before inference for each provider.

## 2026-10-17 — PNG/JPEG uploads and draft recovery

- Fixed Gemini/Scroll waiting at upload after Gemini converted a large PNG to a
  JPEG preview. The timed-out request never reached Send and produced no VM input.
- Gemini and Qwen accept supported PNG/JPG/JPEG preview names, requiring the full
  original stem and allowing only the bounded six-character hex suffix.
- API requests detect image MIME from the actual bytes, including misnamed files.
  PNG/JPEG input and JPEG scroll references are supported; contact sheets use PNG.
- Recover unsent image drafts before another cycle. Gemini's pending attachment
  is explicitly removed after revealing its close control, and the old composer
  is cleared. Submissions and answer clicks are never retried automatically.
- Replayed the real 1920 × 1986, three-section image through Gemini. Its JPEG
  preview was accepted and Argon/Medium returned a valid four-option MCMA plan
  at confidence 0.99 in 133.039 seconds, with no VM answer input.
- The user's API/Scroll retry also completed after the hover fix: it preserved
  the first answer and visually confirmed the two remaining selections at 0.95.
- All 209 tests passed in 179.937 seconds with Lightpanda, Chromium, and native
  login integrations enabled. Both live provider sessions remain authenticated.

## 2026-10-17 — Clear table hover before selection confirmation

- Fixed API/Scroll stopping after one successful selection because the hovered
  table row changed its background. Park the guest pointer with a button-free SDK
  move before capturing the final scroll baseline and after each grounded click.
  The strict visual checks and no-click-retry behavior remain in place.
- Retain the inferred decision and confidence when a selection plan stops after
  input. Added anonymous 40 × 40 control fixtures from the actual failed cycle.
- Connected Gemini to the user's existing Argon/Medium tab beside Qwen in the
  same Chromium window. `browser_profile` selects that shared native profile;
  private endpoint records still identify the individual provider tabs.
- Both real web sessions passed sign-in and selected-model checks. All 202 local
  tests passed in 143.684 seconds with all browser/login integrations enabled.
- A live Gemini context-only probe uploaded one combined PNG and returned the
  correct badge `02` at confidence 1.0 in 14.709 seconds. Coordinates resolved to
  the original answer frame, and the probe sent no VM answer input.

## 2026-10-17 — Manual provider, capture, and indicator modes

- Ctrl+E+R cycles API → Gemini → Qwen; Ctrl+Shift+E+R toggles Single/Scroll
  capture independently. Switching sends no request. Busy switches are discarded,
  and uncertainty never triggers another provider in the hotkey listener.
- Listener startup resets to API, Single capture, and Full status. Full status
  shows provider/capture selection while preserving the previous result color.
- Ctrl+Shift+D+F cycles Hidden → Dot-only → Full → Hidden. Dot-only renders one
  small colored circle with no text or panel, at the same position as Full mode's
  dot. Orange means uncertain/no-selection; red means error.
- Extended scroll-context uploads and local control-ID resolution to API and
  Gemini, with the same fresh-frame selection confirmation used by Qwen.
- Kept Ctrl+D+F as one explicit answer cycle. Provider/capture changes hold the
  cycle lock through result logging, so the recorded source cannot change midway.
- Updated the private 25-question ledger to retain provider-specific confidence
  and distinguish completed selections from visually confirmed selections.
- All 199 local tests passed in 140.828 seconds with Lightpanda, Chromium, and
  native-login integration suites enabled. Both real browser engines uploaded a
  combined context image and resolved fixture badge IDs to original VM pixels.
  Gemini's subsequent live context check passed using the shared signed-in tab.

## 2026-10-17 — Scroll context for Qwen questions

- Added bounded native-panel scroll capture for the current Windows quiz layout.
  Overlapping question sections and the final numbered answer view are uploaded
  in one private PNG; local click coordinates stay tied to the live VM frame.
- Check question/VM identity, scrollbar endpoints, and visible answer context.
  Changed views, cancellation, ambiguous scrollbars, or excess views stop input.
  All required answer controls must still fit in the final live view.
- A live scroll pass collected the hidden diagram/instructions and returned to
  the three-row table. Qwen read the combined image and returned all three
  correct categories at 90% confidence in 39.851 seconds, without answer input.
- Archived the first mock after its timer expired at ten recorded selections;
  reset confidence monitoring for the user's new 25-question mock.
- All 174 tests passed with Lightpanda, Chromium, and native-login integrations
  enabled, including scroll cancellation, endpoint checks, context packing, and
  rejection of changed answer views between selections.

## 2026-10-17 — Qwen monitoring and abstention confidence

- Preserve valid model-reported confidence for no-action results in cycle logs,
  alongside the abstention explanation. Missing/invalid confidence remains
  absent, and each parse clears previous abstention telemetry.
- Started the requested Qwen-only 25-question observation run. The first ten
  questions have recorded selections. Question 11 exposed an incomplete source
  screenshot and a subsequent 150-second thinking timeout; the late response
  reported 60% confidence and no input was sent. The run is still in progress.
- Increased this run's attempt limit to its existing 240-second total budget;
  kept the 85% minimum confidence and VM selection/freshness checks.
- All 152 tests passed with Lightpanda, Chromium, and native-login integrations
  enabled, including abstention telemetry validation and stale-value rejection.

## 2026-10-17 — Qwen control IDs and confirmed selection

- Fixed missed VM answers caused by Qwen's inconsistent pixel estimates. Default
  Qwen requests now upload a private numbered copy of the screenshot and return
  control IDs; local detection supplies actual click coordinates. Strict plan
  validation rejects unknown IDs, wrong control types, and duplicate controls.
- Confirm each Qwen selection in a fresh capture. An unchecked control stops the
  cycle without retrying input. A live Gemini → Qwen cycle selected the correct
  radio and confirmed its blue checked state in 29.613 seconds.
- Wait for late-mounted Qwen upload controls without retrying uncertain events.
  Read virtualized multiline code through its final-response Download control,
  with private bounded files and cleanup; line-number/viewport text is not parsed
  as a complete answer. The host clipboard is not used.
- Preserve Gemini/API coordinate behavior and explicit custom Qwen coordinate
  prompts. See `docs/qwen-validation.md` for detector limits and remaining checks.
- All 150 tests passed with Lightpanda, Chromium, and native-login integrations
  enabled. Live numbered-control MCMA/table probes returned every correct target;
  repeating the answered VM question correctly returned no action.

## 2026-10-16 — Qwen screenshot fallback

- Enabled Qwen3.8-Omni-Flash with Thinking after Gemini in the web fallback order.
  The native Qwen profile restored its sign-in after reopening.
- Added Qwen's per-upload menu preparation, filename and upload-progress checks,
  separate model/thinking verification, and final-answer extraction that excludes
  thinking. Temporary anonymous UI during account loading no longer immediately
  reports a lost session. Native chooser targets handle changing page inputs;
  file-chooser cleanup survives deadlines/cancellation.
- Qwen now requests original pixel coordinates after normalized-grid table probes
  exposed a scaling error. Four consecutive live requests passed for single-choice,
  MCMA, and tables at two aspect ratios, including points within all target controls.
  Repeated uploads of the same file are covered. A controlled Gemini login
  failure fell back to a real Qwen response. The VM was off, so no Qwen VM click
  cycle was run. See `docs/qwen-validation.md` for measurements and limitations.
- Removed ChatGPT from configured providers and retained the existing API backend.
- All 121 tests passed with Lightpanda, Chromium, and native-login integration
  enabled. The configured Gemini-to-Qwen path also passed against the actual
  Gemini sign-in failure and a live Qwen screenshot response in 24.59 seconds.
- Investigated Google login persistence: synthetic native cookies survive
  restarts, but Google's authentication cookies selectively disappeared. The
  cause remains unresolved; keeping the live Gemini session is still a workaround.

## 2026-10-16 — Shortcut remapping

- Changed the answer-cycle shortcut to Ctrl+D+F and the overlay toggle to
  Ctrl+Shift+D+F. Both still activate after release and consume their letter keys;
  incomplete shortcuts and the former P/O chords pass through normally.
- Verified both key orders, left/right modifiers, repeated keys, separate cycle
  and overlay callbacks, release/rearming, and passthrough of the former chords.

## 2026-10-16 — MCMA confirmation and answer diagnostics

- The user confirmed working live Gemini inference for single-choice, table
  classification, and multiple-choice multiple-answer (MCMA) questions.
  The listener log confirms a completed MCMA plan with two separate checkbox clicks.
- Expanded the shared MCMA instructions with a complete response object,
  English/Indonesian question cues, and handling for partially answered questions
  and unlabeled options. Existing multi-selection execution remains in use.
- Preserved the model's explanation when it returns no action, exposing the reason
  in cycle results and logs. Missing or invalid explanations are omitted; later
  responses cannot reuse an earlier reason.
- All 108 tests pass with Lightpanda, Chromium, and native-login integration
  enabled, including no-action diagnostics and existing multi-selection execution.

## 2026-10-15 — Persistent browser interaction fixes

- Disambiguated Gemini's New chat control from the similarly labeled logo link
  shown in the expanded sidebar.
- Kept persistent Chromium tabs rendering through CDP focus emulation, allowing
  animation-dependent upload controls to work while the window is minimized
  without restoring it or taking desktop focus.
- Verified an explicitly requested full listener cycle with Gemini minimized:
  screenshot capture, upload, completed answer, and the correct VM radio selection
  in 38.5 seconds; a follow-up capture confirmed the selected control.
- All 105 tests pass, including background rendering, expanded-sidebar chat reset,
  and upload/response timeout regressions with browser startup timed separately.

## 2026-10-15 — Native browser fallback and session persistence

- Added an installed Chromium/Chrome engine using the existing direct CDP
  controller, headless operation, dedicated profiles, deadlines, and cancellation.
- Added a native sign-in helper that saves cookies immediately and closes through
  the browser's normal shutdown path after model selection. Chromium inference
  reuses the full profile instead of overwriting it with exported cookies.
- Failed web operations preserve imported sessions. Cookie-refresh failures do
  not mask a complete answer.
- Added a persistent managed-browser mode that hands off the exact signed-in tab,
  reconnects between questions, starts a fresh chat, and closes through an explicit
  `--close-browser` command. This avoids repeated session transfer/restart during
  normal use; a standby AI agent is unnecessary.
- The same image-integrity, completion, failure, cookie, and cancellation fixtures
  pass in both real browser engines. Three controlled Gemini screenshot questions
  and a 1920 × 1080 VM dry cycle passed with Argon Medium. The web backend is now
  selected for normal use. Its current Lightpanda initialization fails before upload.

## 2026-10-15 — Experimental Lightpanda web inference

- Added a checksum-verified, private Lightpanda installation and a direct Python
  CDP controller. Inference has no Chromium, Playwright, Node, or standby-agent
  dependency.
- Separated shared prompting and strict answer validation from the API transport.
  Added `--backend web` and kept API inference as the configured default.
- Added Gemini screenshot flow and configurable ChatGPT/Qwen adapter scaffolding,
  ordered fallback, private per-provider cookie import/persistence, one submission
  per provider, cancellation, and a shared total inference deadline.
- Added local session/model checks and an image-only dry-run command, with provider
  progress and sanitized attempt results in the host listener.
- Verified real PNG transfer byte for byte, cookie restoration after browser
  restart, completed-answer extraction, and failure/cancellation behavior against
  local fixtures. Existing API and VM behavior remains covered by its tests.

Authenticated screenshot inference on Gemini, ChatGPT, and Qwen is still pending.
Their public pages load in Lightpanda on this host, but no live model screenshot
was submitted. ChatGPT/Qwen selectors and model labels require account-specific
verification. The detailed findings and remaining acceptance checks are recorded
in [docs/lightpanda-validation.md](docs/lightpanda-validation.md).

## 2026-10-15 — Desktop controls, streamed inference, and startup recovery

- Added Ctrl+P+O to request one answer cycle and Ctrl+Shift+P+O to toggle the host
  status indicator. Chords are consumed locally and activate on release; holding
  them does not repeat a request, and busy cycles do not queue additional work.
- Added a click-through Linux status indicator with elapsed inference time,
  timeout reporting, selection progress, and persistent visibility within a run.
- Added classification-table and multiple-answer plans with up to ten targets,
  explicit coordinate spaces, normalized-to-pixel conversion, target checks,
  cancellation, and partial-completion reporting.
- Held VirtualBox mouse presses for 80 ms so guest input polling can observe them.
- Added streamed inference to keep tunnel connections active while answers are
  generated. Only complete, validated answers reach the clicker; malformed,
  interrupted, oversized, or truncated responses are rejected.
- Configured a 240-second inference budget and a 300-second screenshot-age limit.
  Added request timing logs and distinct timeout errors.
- Added a persistent desktop-session service, targeted keyboard/uinput permission
  rules, and boot-time uinput loading. Documented the single-command launch path.
- Fixed early-login overlay startup by checking display readiness before Qt
  initialization. Added process locking and abandoned-socket recovery after a
  crash, while preserving live listeners and unrelated files.

Validation: 65 automated tests, including fake-SDK subprocess integration, local
HTTP/SSE transport tests, keyboard routing, overlay behavior, and crash recovery.
Live host checks verified keyboard access, click-through input, unchanged focus,
foreground/service startup, and clean shutdown. A streamed screenshot request
produced a validated three-target plan in about 112 seconds without sending VM
input. Early-login failure and recovery were tested; another full reboot after
the final startup fix has not yet been verified.

The model proposes selections; local Python validates and executes them through
the host VirtualBox SDK. Model accuracy and anti-cheat compatibility are not
guaranteed. The supplied service path and keyboard rule are specific to this host
and must be adjusted when installing elsewhere.
