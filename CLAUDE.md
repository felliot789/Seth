# Working on Seth

Follow the user's AGENTS.md and any referenced local instructions.

## Scope

Seth captures one question, asks one manually selected provider, and displays
answer keys in the host status bar. Single mode captures the visible framebuffer;
Scroll mode automatically collects overlapping views of one whole-page layout.

- MC: one positive option number; MCMA: comma-separated option numbers.
- Table: L/R for every visible row, top to bottom.
- Successful status text contains only the keys, with wrapping when needed.
- Provider selection and question classification are manual.
- Uncertainty stops the request; the user chooses whether to try another provider.
- VM access supports capture, status, and button-free scrolling only. Reject
  clicks, answer selection, typing, submission, and arbitrary pointer operations.
- Scroll mode uses ordered original images in one request, with confirmed
  overlap, a capture limit, and cancellation. No stitching or control detection.
- Keep Single mode available and preserve the removed execution flag's rejection.

## Maintenance

Read [CONTRIBUTING.md](CONTRIBUTING.md) for ownership, the reply contract, and local
checks. [README.md](README.md) describes the user workflow.

Preserve `.env`, captures, and native browser profiles. Avoid changing live
provider sessions during development. Isolated tests use temporary profiles and
local fake inference servers; they must not submit current VM contents to a real
provider. A listener restart retains browser sessions; a browser restart closes
them. Do not restart services for documentation-only changes.
