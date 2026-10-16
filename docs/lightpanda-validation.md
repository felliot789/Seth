# Lightpanda validation — 2026-10-15

Status: Gemini screenshot inference verified with a persistent Chromium session;
configured inference backend is now `web`. Local browser mechanics are verified
with both Lightpanda and Chromium. Authenticated Gemini initialization fails in
the checked Lightpanda build; ChatGPT and Qwen are not operational fallbacks yet.

## Runtime

- Linux x86_64, official release asset `lightpanda-x86_64-linux`.
- Version: `1.1.0-nightly.10619+ed5a25e5d`.
- SHA-256: `30cdea8677b91fb0372774633a0438fa80e05d7e6bdac49917277899d3f1dcc3`.
- Download checksum matched the digest returned by GitHub's release API.
- Installed outside the repository under `~/.local/share/seth/runtime/lightpanda/`.
- Controller uses Python and websocket-client directly over CDP. Lightpanda
  tests need no Chromium, Playwright, Node, or second inference model. The optional
  Chromium runtime uses the same controller and an installed executable.

## Verified with real Lightpanda processes

- A page received a 320 × 200 PNG through `DOM.setFileInputFiles`. JavaScript
  read its filename, `image/png` MIME type, size, and PNG signature.
- The integration fixture sent that PNG through `fetch` to a local HTTP server.
  The received bytes' SHA-256 matched the original file.
- A complete synthetic answer was extracted and passed through Seth's shared
  coordinate validation, producing the expected screenshot-relative point.
- Valid JSON displayed during generation was withheld until the completion
  control appeared, generation stopped, and the answer remained stable.
- Cookies were saved privately, restored into a new Lightpanda process, and
  observed by the page after navigation.
- Signed-out pages, the wrong model, and restored conversations stopped before
  upload; failed, stalled, or mismatched attachments stopped before submission.
- Interrupted generation, quota errors, changed conversations, and changed model
  selection produced errors without resubmission to that provider.
- Cancellation stopped waiting and terminated the browser process.
- Unit tests verify ordered fallback, a shared total deadline, one attempt per
  provider, invalid-answer rejection, session locks, and private cookie import.
- The same seven integration tests pass against real headless Chromium processes,
  including byte-for-byte image upload and cookie persistence after restart.
- A native login-helper integration test saves a session privately and closes
  Chromium normally after model selection. It uses a local fixture, not Google.
- Failed page operations preserve the previous imported cookie file; failure to
  refresh cookies cannot mask a complete inference result.

The checked binary reported `isContentEditable` as false for an element carrying
`contenteditable="true"`; `Input.insertText` did not populate that element in a
probe. The adapter sets composer content and dispatches input events explicitly,
then checks the submitted message's request identifier. This passed the local
fixture; acceptance by each provider's editor framework still needs a live test.

## Live public-page checks

Lightpanda loaded Gemini, ChatGPT, and Qwen public pages on this host. Gemini
presented sign-in; ChatGPT and Qwen exposed their signed-out pages and upload
controls. Those initial unauthenticated probes submitted no screenshots and
established reachability only. The later authenticated acceptance checks are
recorded below.

## Authenticated Gemini initialization

The native helper captured a Google session with the selected model `Argon`.
Repeated Lightpanda attempts then loaded a partial Gemini application without a
usable composer/model picker. A document-start error listener observed Angular
router `NoMatch` errors for the `app` segment; waiting did not recover the UI.
The failure occurs before any screenshot upload. The precise engine/API cause
has not been established.

An isolated Chromium comparison loaded the full chat shell, but longer checks
showed the exported session no longer authenticated. A loaded composer alone is
therefore insufficient evidence of a working signed-in session. Chromium now
uses its native dedicated login profile instead of re-importing an older cookie
export over that profile. The helper uses `Browser.close` to flush profile state.
Restart tests still returned signed-out Gemini pages despite restored cookies.
This has not been established as a cookie-encryption, website, or profile issue.
Consequently Gemini now keeps a dedicated native browser alive and reconnects to
its exact authenticated tab. A clean native profile replaces the mixed prototype
profile, which remains preserved separately.

The local Chromium fixture verifies two separate image questions over two
controller connections to one live browser/tab. A JavaScript session marker
survives, a new chat clears the prior turn, and submitting the same PNG twice
produces two distinct completed answers. File inputs are cleared before attachment
so selecting the same path again still triggers an upload. Live Gemini preview
tooltips expose the uploaded filename through `aria-describedby`; image
preprocessing adds a six-character hexadecimal suffix. The adapter requires the
complete original filename or that specific transformation. It selects the image
input rather than the separate document inputs and waits for asynchronous menus.

## Passed live acceptance checks

The model was `Argon` with `Medium` thinking. Both labels are now checked before
upload/submission and after completion.

- Three controlled 960 × 720 PNG questions contained unique codes only in the
  images. The text prompt contained no question data or answer key. Gemini read
  each code correctly and selected the expected option (C, D, then B).
- Successful cycles took approximately 11.5, 25.0, and 15.8 seconds. The latter
  two used fresh chats and separate controller connections to the same live browser.
- A real Seth dry cycle captured the VM at 1920 × 1080, uploaded it, obtained a
  complete valid decision in about 17 seconds, and converted normalized coordinates
  to the expected answer's radio control at (392, 743). No VM input was sent.
- The controller required a completed reply and passed the shared JSON,
  coordinate, and confidence checks. Failed setup probes stopped before prompt
  submission. No standby inference agent participated.
- Synthetic validation images, expected bounds, response records, browser state,
  and cookies remain local and outside Git.

ChatGPT and Qwen model labels and signed-in selectors still need configuration.

## Remaining acceptance checks

1. Browser restart and long-term session renewal remain unverified. Keep the
   dedicated native browser open (minimizing is fine); use `--login` if it closes
   or Gemini requests authentication again. The live session is the validated path.
2. Check the site's upload preprocessing in Lightpanda if its initialization
   compatibility is fixed. Lightpanda can pass file bytes but does
   not decode images, so a site requiring browser image dimensions or canvas
   resizing may fail before submission.
3. Verify ChatGPT and Qwen independently, including their signed-in DOM selectors
   and completion signals, before adding them as operational fallbacks.
4. A live VM dry cycle has passed. Actual guest selection remains user-triggered
   through the existing shortcut; no automatic execution test was performed here.

Source references: [Lightpanda file-input implementation](https://github.com/lightpanda-io/browser/pull/2635)
and [cookie/image-loading options](https://lightpanda.io/docs/reference/cli/common-options).
