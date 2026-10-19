`native-light-arrow.png` contains only an anonymous 18 × 897 native scrollbar
strip from the stopped whole-page capture on 2026-10-18. The arrow mixes light
gray (209) and medium gray (159) pixels. The previous 170-level threshold split
the up arrow into a five-pixel fragment and failed to detect the scrollbar.
No question text, account information or answer controls are retained.

`timer-sticky.png` and `timer-flow.png` contain only the anonymous countdown/hide
widget at its sticky and in-flow positions. Chromium rerasterizes the timer label
slightly between those positions. They verify unique widget tracking without
loosening the separate question/answer content checks.
