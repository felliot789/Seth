# Working on Seth

Follow the user's AGENTS.md and any referenced local instructions.

## Scope

Seth captures one visible VM framebuffer, asks one manually selected provider,
and displays answer keys in the host status bar.

- MC: one positive option number; MCMA: comma-separated option numbers.
- Table: L/R for every visible row, top to bottom.
- Successful status text contains only the keys, with wrapping when needed.
- Provider selection and question classification are manual.
- Uncertainty stops the request; the user chooses whether to try another provider.
- VM access is read-only: capture and status only. Keep input operations rejected.
- Preserve the single-capture flow; do not restore control detection, coordinates,
  answer selection, scrolling, stitching, or the removed execution flag.

## Maintenance

Read [CONTRIBUTING.md](CONTRIBUTING.md) for ownership, the reply contract, and local
checks. [README.md](README.md) describes the user workflow.

Preserve `.env`, captures, and native browser profiles. Avoid changing live
provider sessions during development. Isolated tests use temporary profiles and
local fake inference servers; they must not submit current VM contents to a real
provider. A listener restart retains browser sessions; a browser restart closes
them. Do not restart services for documentation-only changes.
