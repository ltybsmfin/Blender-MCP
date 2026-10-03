"""Presentation guards; successful view/focus still needs a live GUI check."""
import unittest
from unittest.mock import patch
from test_unit_validation import addon


class PresentationGuards(unittest.TestCase):
    def test_invalid_direction_does_not_access_the_scene(self):
        with patch.object(addon, 'get_object') as lookup:
            with self.assertRaises(addon.CommandError) as raised:
                addon._cmd_present_object('Chair', view='unsupported')
            self.assertEqual(raised.exception.code, 'invalid_view')
            lookup.assert_not_called()

    def test_flags_must_be_booleans(self):
        with patch.object(addon, 'get_object') as lookup:
            with self.assertRaises(addon.CommandError) as raised:
                addon._cmd_present_object('Chair', foreground='yes')
            self.assertEqual(raised.exception.code, 'invalid_argument')
            lookup.assert_not_called()

    def test_no_viewport_does_not_change_selection_or_focus(self):
        with patch.object(addon, 'get_object', return_value=object()), \
             patch.object(addon, '_find_view3d_context', return_value=None), \
             patch.object(addon, '_request_own_window_foreground') as focus:
            with self.assertRaises(addon.CommandError) as raised:
                addon._cmd_present_object('Chair', foreground=True)
            self.assertEqual(raised.exception.code, 'viewport_unavailable')
            focus.assert_not_called()


if __name__ == '__main__':
    unittest.main()
