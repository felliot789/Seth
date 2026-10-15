# Lightpanda validation — 2026-10-15

Status: local browser mechanics verified; authenticated provider inference is
pending. The configured inference backend remains `api`.

## Runtime

- Linux x86_64, official release asset `lightpanda-x86_64-linux`.
- Version: `1.1.0-nightly.10619+ed5a25e5d`.
- SHA-256: `30cdea8677b91fb0372774633a0438fa80e05d7e6bdac49917277899d3f1dcc3`.
- Download checksum matched the digest returned by GitHub's release API.
- Installed outside the repository under `~/.local/share/seth/runtime/lightpanda/`.
- Controller uses Python and websocket-client directly over CDP. No Chromium,
  Playwright, Node, or second inference model participates in the tests.

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

The checked binary reported `isContentEditable` as false for an element carrying
`contenteditable="true"`; `Input.insertText` did not populate that element in a
probe. The adapter sets composer content and dispatches input events explicitly,
then checks the submitted message's request identifier. This passed the local
fixture; acceptance by each provider's editor framework still needs a live test.

## Live public-page checks

Lightpanda loaded Gemini, ChatGPT, and Qwen public pages on this host. Gemini
presented sign-in; ChatGPT and Qwen exposed their signed-out pages and upload
controls. No screenshot was submitted to a live provider. These checks establish
reachability only, not authenticated application compatibility.

The separate Gemini login profile had no Google sign-in cookies at the last
check. A user sign-in and local session import are required for the next test.
ChatGPT and Qwen model labels and signed-in selectors also need configuration.

## Remaining acceptance checks

1. Import a valid Gemini session and confirm its selected vision model.
2. Upload a test screenshot containing unique information and obtain a complete
   answer that demonstrates the model actually received the image.
3. Repeat across multiple questions and browser restarts; test cookie renewal.
4. Check the site's upload preprocessing. Lightpanda can pass file bytes but does
   not decode images, so a site requiring browser image dimensions or canvas
   resizing may fail before submission.
5. Verify ChatGPT and Qwen independently, including their signed-in DOM selectors
   and completion signals, before adding them as operational fallbacks.
6. Only after those checks, select `inference.backend: "web"` and verify a Seth
   dry cycle before using the existing `--execute` mode.

Source references: [Lightpanda file-input implementation](https://github.com/lightpanda-io/browser/pull/2635)
and [cookie/image-loading options](https://lightpanda.io/docs/reference/cli/common-options).
