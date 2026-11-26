"""Device-selection and failed-recording checks; does not open a USB camera."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import camera


def device(index, vid, pid, path):
    return SimpleNamespace(index=index, vid=vid, pid=pid, path=path)


class CameraSafetyTests(unittest.TestCase):
    def setUp(self):
        self.builtin = device(0, 0x1BCF, 0x2087, 'builtin')
        self.usb = device(1, 0x32E4, 0x3415, 'target-one')

    def test_target_selection_ignores_builtin(self):
        self.assertIs(camera.select_camera([self.builtin, self.usb], 0x32E4, 0x3415), self.usb)

    def test_missing_target_never_falls_back_to_builtin(self):
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            camera.select_camera([self.builtin], 0x32E4, 0x3415)

    def test_same_model_requires_disambiguation(self):
        second = device(2, 0x32E4, 0x3415, 'target-two')
        with self.assertRaisesRegex(RuntimeError, 'Multiple'):
            camera.select_camera([self.usb, second], 0x32E4, 0x3415)
        self.assertIs(camera.select_camera([self.usb, second], 0x32E4, 0x3415,
                                          device_path='TARGET-TWO'), second)

    def test_explicit_index_requires_explicit_backend(self):
        with patch('sys.argv', ['camera.py', '--index', '0']):
            with self.assertRaises(SystemExit) as error:
                camera.main()
        self.assertEqual(error.exception.code, 2)

    def test_read_failure_is_reported(self):
        cap = SimpleNamespace(read=lambda: (False, None))
        with patch.object(camera.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'stopped delivering'):
                camera.read_frame(cap)

    def test_undecodable_video_cannot_pass_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broken.avi'
            path.write_bytes(b'broken video')
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                camera.verify_video(dict(path=str(path), frames=5, width=1280, height=720))


if __name__ == '__main__':
    unittest.main()
