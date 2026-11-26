import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from camera import Recording, RECORDING_CODECS


class RecordingTests(unittest.TestCase):
    def test_real_codec_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            for codec, suffix in RECORDING_CODECS.items():
                with self.subTest(codec=codec):
                    frame = np.zeros((48, 64, 3), dtype=np.uint8)
                    recording = Recording(frame, 20.0, Path(directory), codec)
                    for n in range(5):
                        frame[:] = n * 40
                        recording.write(frame)
                    result = recording.close()
                    self.assertEqual(recording.path.suffix, suffix)
                    self.assertEqual(result['requested_codec'], codec)
                    self.assertEqual(result['verification'], {'decoded_frames': 5, 'passed': True})
                    self.assertTrue(result['actual_fourcc'])
                    self.assertEqual(json.loads(recording.path.with_suffix('.json').read_text(encoding='utf-8')), result)
                    self.assertIs(recording.close(), result)

    def test_reject_invalid_input(self):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            for fps in (0, -1, float('nan'), float('inf')):
                with self.subTest(fps=fps), self.assertRaises(ValueError):
                    Recording(frame, fps, Path(directory))
            with self.assertRaises(ValueError):
                Recording(frame, 20, Path(directory), 'invalid')
            with self.assertRaises(ValueError):
                Recording(np.zeros((0, 64, 3), dtype=np.uint8), 20, Path(directory))

    def test_failed_verification_closes_once_and_records_failure(self):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            recording = Recording(frame, 20, Path(directory))
            recording.write(frame)
            with patch('camera.verify_video', side_effect=RuntimeError('decode failed')) as verify:
                with self.assertRaisesRegex(RuntimeError, 'decode failed'):
                    recording.close()
                result = recording.close()
                verify.assert_called_once()
            self.assertFalse(result['verification']['passed'])
            self.assertEqual(json.loads(recording.path.with_suffix('.json').read_text(encoding='utf-8')), result)


if __name__ == '__main__':
    unittest.main()
