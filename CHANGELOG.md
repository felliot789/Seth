# Changelog

## Automatic Scroll mode

- Ctrl+Shift+S switches Single/Scroll; Ctrl+D+F runs the selected mode.
- Scroll automatically seeks the top, captures overlapping original screenshots
  to the bottom, and sends the ordered batch in one provider request.
- Shared prompts count overlapping options/table rows once across the question.
- Coverage/settling, VM identity, image/time limits, and cancellation are checked.
- Browser uploads verify all sections before submission. Incomplete batches stop.
- VM input is limited to button-free scrolling; answer operations remain disabled.
- Single mode remains the default and retains its capture-only behavior.

## Capture and answer-key revamp

Seth now follows a single path: capture the visible VM screen, ask the selected
provider, validate the answer keys, and display them in the host status bar.

### Current behavior

- Manual API, Qwen, and Gemini selection; one provider per request.
- Manual MC, MCMA, and table format selection.
- Numeric MC/MCMA keys and ordered L/R table keys, with no result prefix.
- Explicit uncertainty, cancellation, and descriptive errors.
- Complete wrapped answers in Full indicator mode.
- One shared decision format and cancellable provider transports.
- Persistent native browser sessions and separate listener/browser services.

### Removed

- VM mouse and keyboard operations, answer application, and execution mode.
- Control detection, coordinate mapping, badges, and selection verification.
- Automatic scrolling, page stitching, and layout discovery.
- Automatic format classification and provider fallback.
- The retired browser runtime, cookie-export login workflow, obsolete configuration
  aliases, control/scroll image fixtures, and their validation guides.

Earlier implementations remain available in Git history. The current guides
describe only the supported capture-to-answer workflow.
