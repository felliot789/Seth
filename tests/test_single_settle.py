"""Single capture must settle the final viewport, including newly exposed text."""

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import ImageDraw

from capture import SethCapture
from scroll_capture import Scrollbar
from single_capture import prepare_single
from tests.test_single_capture import SingleVM
from vbox_backend import VMError


class ExpandingVM(SingleVM):
    def __init__(self):
        super().__init__()
        self.captures = 0
        self.continuation = 'Question continuation A'
        self.unstable = False

    @property
    def bottom(self):
        if self.unstable:
            return 180 if self.captures % 2 == 0 else 200
        return 200 if self.captures >= 3 else 180

    def image(self):
        image = super().image()
        if self.bottom == 200:
            ImageDraw.Draw(image).text((10, 181), self.continuation, fill='black')
        return image

    def capture(self, path):
        self.captures += 1
        return super().capture(path)

    def scrollbars(self, image):
        # Geometry changes between the neutral and candidate settled capture,
        # while GuestDisplay and all pixels in the old scope remain unchanged.
        return [Scrollbar((307, 50, 317, 100), 40, self.bottom - 10,
                          (0, 32, 320, self.bottom), viewport=(32, self.bottom))]


class SingleSettleTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = {'project_dir': temporary.name, 'capture': {'max_age_seconds': 90}}
        self.vm = ExpandingVM()
        self.capture = SethCapture(self.config, self.vm)

    def test_expanded_final_viewport_settles_and_new_question_content_is_verified(self):
        with patch('single_capture.find_scrollbars', side_effect=self.vm.scrollbars), \
                patch('single_capture.time.sleep'):
            question = prepare_single(self.config, self.capture, self.capture.capture_frame())
            self.assertEqual(question.regions, [(0, 32, 307, 200)])
            # At least one additional capture must settle the newly exposed area.
            self.assertGreaterEqual(self.vm.captures, 4)
            question.verify()
            self.vm.continuation = 'Question continuation CHANGED'
            with self.assertRaisesRegex(VMError, 'content changed'):
                question.verify()
        self.assertEqual(self.vm.clicks, [])

    def test_continuously_changing_viewport_stops_at_settling_deadline(self):
        self.vm.unstable = True
        elapsed = [0.0]
        def tick():
            elapsed[0] += .25
            return elapsed[0]
        # Age is separately covered; the synthetic monotonic clock here only
        # advances the settling deadline without waiting or dispatching input.
        with patch('single_capture.find_scrollbars', side_effect=self.vm.scrollbars), \
                patch('single_capture.time.sleep'), \
                patch('single_capture.time.monotonic', side_effect=tick), \
                patch('single_capture.validate_age'):
            with self.assertRaisesRegex(VMError, 'settle'):
                prepare_single(self.config, self.capture, self.capture.capture_frame())
        self.assertLess(self.vm.captures, 30)
        self.assertEqual(self.vm.clicks, [])


if __name__ == '__main__':
    unittest.main()
