**Repairs for the 2026-10-19 behavior audit**

All nine findings have corresponding implementation changes and regression
coverage. The [original audit](audit-2026-10-19.md) retains the before-repair
reproductions and severity assessment.

| Finding | Repair | Regression coverage |
| --- | --- | --- |
| 1: Single question replacement | Compare the complete visible question and answer content before each click; preserve browser identity and verify earlier selections. | `tests/test_single_capture.py` changes question and option text outside the old target patch and requires zero clicks. |
| 2: Unchecked intermediate scroll content | Revisit every recorded section before the first selection; use verified forward wheel traversal. | `tests/test_scroll_audit.py` changes intermediate wheel and native sections and requires rejection before input. |
| 3: Ungrounded API/Gemini Single input | Use numbered controls for all default providers, ground every execution plan, and confirm every selection. | API/Gemini region and ID tests; blank-space/missed-click tests; real-helper ID and visible-selection integration tests. |
| 4: Initial hover causes Stopped (1) | Park the pointer and wait for a settled neutral Single baseline before inference. | Two-checkbox execution with an initially hovered later row completes for API, Gemini and Qwen. |
| 5: Cancellation relay window | Publish helper markers synchronously from engine interrupt and shutdown events; handle registration races and cleanup. | Deterministic cancellation after movement and before press, shutdown, registration, release and rearm tests. |
| 6: Contradictory selection schemas | Reject select-only fields in click responses and top-level click targets in selection plans. | Radio/table and checkbox contradictory response regressions. |
| 7: Wheel pixel-nudge exception | Resolve the active view before selecting the native/wheel verification branch. | One-pixel alignment succeeds; changed foreground still rejects. |
| 8: Nonstream completion metadata | Reject explicit non-stop/null finish reasons, refusals, tool calls and function calls. | Length/filter/tool/refusal/null responses reject; completed plain responses pass. |
| 9: Original capture-age bypass | Carry the earliest required inference timestamp through navigation and fresh execution frames; recheck during return and before input. | Expired earliest section, expiration during return, and clicker deadline regressions. |

Single mode uses an exact content comparison outside bounded previously
confirmed controls. A locally recognized native outer viewport excludes the
desktop clock and preserves browser chrome/rail identity. Without that geometry,
the complete frame must remain stable. Arbitrary changing widgets are not
silently masked. Fresh return pixels retain the original inference deadline.

The execution guard also confirms earlier Single selections after the last
click, detecting a later radio/input action that clears a previously confirmed
answer. Existing outline/focus and strict content checks remain in force.

Nonstream compatibility policy: a gateway may omit the finish-reason field;
when the field is present, it must be `stop`. Explicit null or unsuccessful
metadata, refusal content and tool/function responses are rejected. Default
configuration continues to use streaming with its completed-answer requirement.

The engine uses synchronous `CancellationEvent` sources. External callers that
provide a stock `threading.Event` retain the compatibility polling relay for
that source; engine interrupt and shutdown no longer use that relay. Already
issued native SDK calls still have unknown completion if the SDK hangs, and
cleanup continues to retain the cycle lock until completion or timeout.

The full regression suite passed **336 tests in 1280.622 seconds**, with all
three browser integration flags enabled and no skips. The private log is
`captures/audit-fixes-full-regression.log`.

Final review additionally found and repaired a viewport resize during initial
settling: scopes are now derived again from the final screenshot and must settle
under those scopes. The two resize regressions passed in 0.337 seconds. The final
Single and real-helper integration rerun passed eight tests in 42.250 seconds
after that refinement (`captures/audit-fixes-final-single-tests.log`). These
checks preserve complete question coverage when newly visible content appears.

The VM is running but was at its lock screen during the read-only host check.
The SDK status/capture path succeeded; no live quiz question, model request or
answer selection was exercised in that check. The listener is reloaded after
regression verification, preserving the existing startup defaults.
