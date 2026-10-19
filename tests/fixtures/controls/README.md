`unchecked-radio.png` contains only a 40 × 40 native radio-button glyph cropped
from the missed-click regression. It contains no page text or account data.
The model point was 13 pixels left and 3 pixels above the control's center.

`unchecked-radio-large.png` and `selected-radio.png` are a second anonymous
40 × 40 pair from the successful retry at a different zoom. The selected
control is a filled blue circle with a white check mark.

`unchecked-radio-row.png`, `selected-radio-row-hover.png`, and
`selected-radio-row.png` are anonymous 40 × 40 glyphs from a scroll-table cycle.
The click succeeded, but the hovered row's gray background caused strict
confirmation to fail. The third glyph shows the same selected control after the
pointer leaves the row. No question text or account data is retained.

`native-radio-table-{unchecked,selected-focus}.png` and
`native-radio-single-{unchecked,selected-focus}.png` are anonymous 40 × 40 pairs
from the recurring October 18 confirmation failures. These native radio buttons
have a blue center dot and a persistent rectangular focus highlight extending
five to six pixels beyond the control. Parking the pointer does not clear it.
No page text or account data is retained.

`native-radio-ten-controls.png` contains just the ten native radio glyphs and
alternating row backgrounds from the same table. The final right-hand glyph was
missed when its outline was judged against the whole frame's white background
instead of its local gray row. It contains no labels, question or account text.

`native-checkbox-mcma-{unchecked,selected-focus}.png` is an anonymous 40 × 40 pair
from the subsequent MCMA stop in the still-running old listener. The checkbox
was selected and retained the same blue focus rectangle. The pair contains no
page text or account data.

## Diagram text

`native-diagram-word.png` is an anonymous crop of a few antialiased letters from
an image answer. Its round letter previously received a false radio badge. It
contains no question, answer meaning, account information or browser chrome.
