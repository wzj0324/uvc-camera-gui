"""Offline PyAV capture tests; never opens a camera."""
import math
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import av
import cv2
import numpy as np

import pyav_capture as capture


def args(**overrides):
    values = dict(vid=1, pid=2, index=None, device_path=None, width=1920,
                  height=1080, fps=30, format='auto', decoder='pyav-software')
    values.update(overrides)
    return SimpleNamespace(**values)


def stream(hw=False):
    return SimpleNamespace(average_rate=30, codec_context=SimpleNamespace(name='h264', is_hwaccel=hw))


def frame():
    return SimpleNamespace(format=SimpleNamespace(name='nv12'),
                           to_ndarray=lambda format: np.zeros((2, 3, 3), dtype=np.uint8))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.process = Mock()
        self.process.poll.return_value = None
        for target, value in [(capture, '__file__'), (capture.shutil, 'which'),
                              (capture.subprocess, 'Popen')]:
            replacement = ({'__file__': str(Path(temp.name) / 'adapter.py'),
                            'which': Mock(return_value='ffmpeg'),
                            'Popen': Mock(return_value=self.process)})[value]
            patcher = patch.object(target, value, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_strict_hardware_rejects_mjpeg_before_enumeration(self):
        with patch.object(capture, 'enumerate_cameras') as enum:
            with self.assertRaisesRegex(RuntimeError, 'requires H264'):
                capture.open_pyav_camera(args(format='MJPG', decoder='pyav-d3d11va'))
            enum.assert_not_called()

    def test_no_matching_camera(self):
        with patch.object(capture, 'enumerate_cameras', return_value=[]), patch.object(capture.av, 'open') as op:
            with self.assertRaisesRegex(RuntimeError, 'not found'):
                capture.open_pyav_camera(args())
            op.assert_not_called()

    def test_invalid_packet_recovers_and_release_is_idempotent(self):
        bad = Mock(); bad.decode.side_effect = av.error.InvalidDataError(1, 'bad data')
        good = Mock(); good.decode.return_value = [frame()]
        container = Mock(); container.demux.return_value = iter([bad, good])
        cap = capture.PyAVCapture(container, stream(), 'pyav-software', 'H264')
        ok, image = cap.read()
        self.assertTrue(ok)
        self.assertEqual(image.shape, (2, 3, 3))
        self.assertEqual(cap.decode_info['frame_format'], 'nv12')
        self.assertTrue(math.isnan(cap.get(cv2.CAP_PROP_BRIGHTNESS)))
        self.assertFalse(cap.set(cv2.CAP_PROP_BRIGHTNESS, 1))
        cap.release(); cap.release()
        container.close.assert_called_once()

    def test_strict_hardware_failure_closes_container(self):
        packet = Mock(); packet.decode.return_value = [frame()]
        container = Mock(); container.demux.return_value = iter([packet])
        cap = capture.PyAVCapture(container, stream(False), 'pyav-d3d11va', 'H264')
        with self.assertRaisesRegex(RuntimeError, 'not active'):
            cap.read()
        container.close.assert_called_once()

    def test_open_uses_unique_path_and_software_default(self):
        info = SimpleNamespace(index=1, vid=1, pid=2, path='unique-path')
        video = stream(); video.codec_context.name = 'mjpeg'
        packet = Mock(); packet.decode.return_value = [frame()]
        container = Mock(); container.streams.video = [video]; container.demux.return_value = iter([packet])
        with patch.object(capture, 'enumerate_cameras', return_value=[info]), patch.object(capture.av, 'open', return_value=container) as op:
            cap, image, selected, backend = capture.open_pyav_camera(args())
            cmd = capture.subprocess.Popen.call_args.args[0]
            self.assertIn('video=@device_pnp_unique-path', cmd)
            self.assertEqual(cmd[cmd.index('-vcodec') + 1], 'mjpeg')
            self.assertEqual(cmd[cmd.index('-c:v') + 1], 'copy')
            reader = op.call_args.args[0]
            self.assertIs(reader.pipe, self.process.stdout)
            self.assertFalse(reader.seekable())
            self.assertNotIn('hwaccel', op.call_args.kwargs)
            self.assertIs(selected, info)
            self.assertEqual(backend, 'pyav-software')
            cap.release()
            self.process.terminate.assert_called_once()
            self.process.stdout.close.assert_called_once()

    def test_open_failure_releases_container(self):
        info = SimpleNamespace(index=1, vid=1, pid=2, path='unique-path')
        video = stream(); video.codec_context.name = 'mjpeg'
        container = Mock(); container.streams.video = [video]; container.demux.return_value = iter([])
        with patch.object(capture, 'enumerate_cameras', return_value=[info]), patch.object(capture.av, 'open', return_value=container):
            with self.assertRaisesRegex(RuntimeError, 'first video frame'):
                capture.open_pyav_camera(args())
        container.close.assert_called_once()
        self.process.terminate.assert_called_once()

    def test_hardware_mode_uses_stream_copy_and_strict_pyav(self):
        info = SimpleNamespace(index=1, vid=1, pid=2, path='unique-path')
        video = stream(True)
        packet = Mock(); packet.decode.return_value = [frame()]
        container = Mock(); container.streams.video = [video]; container.demux.return_value = iter([packet])
        with patch.object(capture, 'enumerate_cameras', return_value=[info]), patch.object(capture.av, 'open', return_value=container) as op:
            cap, _, _, _ = capture.open_pyav_camera(args(decoder='pyav-d3d11va'))
            cmd = capture.subprocess.Popen.call_args.args[0]
            self.assertEqual(cmd[cmd.index('-vcodec') + 1], 'h264')
            self.assertEqual(cmd[cmd.index('-c:v') + 1], 'copy')
            self.assertIn('-copyinkf', cmd)
            self.assertEqual(op.call_args.kwargs['format'], 'h264')
            self.assertFalse(op.call_args.kwargs['hwaccel'].allow_software_fallback)
            self.assertTrue(cap.decode_info['hardware_active'])
            cap.release()

    def test_av_open_failure_cleans_up_process(self):
        info = SimpleNamespace(index=1, vid=1, pid=2, path='unique-path')
        with patch.object(capture, 'enumerate_cameras', return_value=[info]), patch.object(capture.av, 'open', side_effect=av.error.InvalidDataError(1, 'bad data')):
            with self.assertRaisesRegex(RuntimeError, 'PyAV camera open failed'):
                capture.open_pyav_camera(args())
        self.process.terminate.assert_called_once()
        self.process.stdout.close.assert_called_once()

    def test_unnegotiated_format_fails_and_closes(self):
        info = SimpleNamespace(index=1, vid=1, pid=2, path='unique-path')
        container = Mock(); container.streams.video = [stream()]
        with patch.object(capture, 'enumerate_cameras', return_value=[info]), patch.object(capture.av, 'open', return_value=container):
            with self.assertRaisesRegex(RuntimeError, 'not negotiated'):
                capture.open_pyav_camera(args())
        container.close.assert_called_once()
        self.process.terminate.assert_called_once()

    @unittest.skipUnless(os.environ.get('PYAV_TEST_D3D11VA') == '1', 'GPU test explicitly opt-in')
    def test_d3d11va_local_sample(self):
        path = Path(__file__).parent / 'captures/diagnostics/h264_gpu_test_pattern.mp4'
        with av.open(str(path), hwaccel=capture.HWAccel('d3d11va', allow_software_fallback=False)) as container:
            cap = capture.PyAVCapture(container, container.streams.video[0], 'pyav-d3d11va', 'H264')
            ok, image = cap.read()
            self.assertTrue(ok)
            self.assertTrue(cap.decode_info['hardware_active'])
            self.assertEqual(image.shape, (1080, 1920, 3))
            cap.release()


if __name__ == '__main__':
    unittest.main()
