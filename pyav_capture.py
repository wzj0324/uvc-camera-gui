"""DirectShow stream copy via FFmpeg, decoded explicitly by PyAV.

Pipe reads can block inside native code; the packet deadline is not a hard
timeout and cannot guarantee interruption of a stalled FFmpeg or device.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import math
from pathlib import Path
import shutil
import subprocess
import time

import av
from av.codec.hwaccel import HWAccel
import cv2
from cv2_enumerate_cameras import enumerate_cameras

from camera import select_camera


class PipeReader:
    """Expose a non-seekable stream to PyAV on Windows."""

    def __init__(self, pipe):
        self.pipe = pipe

    def read(self, size):
        return self.pipe.read(size)

    def readable(self):
        return True

    def seekable(self):
        return False


class PyAVCapture:
    """Small VideoCapture-compatible adapter; image controls are unsupported."""

    def __init__(self, container, stream, decoder, input_format, process=None, log_file=None):
        self.container = container
        self.process = process
        self.log_file = log_file
        self.stream = stream
        self.decoder = decoder
        self.input_format = input_format
        self.packets = iter(container.demux(stream))
        self.frames = deque()
        self.closed = False
        self.width = math.nan
        self.height = math.nan
        self.fps = float(stream.average_rate) if stream.average_rate else math.nan
        self.decode_info = dict(decoder_requested=decoder, hardware_active=False,
                                codec=stream.codec_context.name,
                                frame_format=None, output_format=None,
                                transport='ffmpeg-stream-copy', decoder='pyav',
                                allow_software_fallback=False if decoder == 'pyav-d3d11va' else None)

    def isOpened(self):
        return not self.closed

    def read(self):
        if self.closed:
            return False, None
        # Limits apply between reads; a native pipe read can still block.
        deadline = time.monotonic() + 10
        invalid = 0
        packets_seen = 0
        try:
            while time.monotonic() < deadline and invalid < 300 and packets_seen < 2000:
                if self.frames:
                    frame = self.frames.popleft()
                    active = bool(self.stream.codec_context.is_hwaccel)
                    if self.decoder == 'pyav-d3d11va' and not active:
                        raise RuntimeError('D3D11VA was requested but hardware decoding is not active')
                    self.decode_info['hardware_active'] = active
                    self.decode_info['frame_format'] = frame.format.name
                    image = frame.to_ndarray(format='bgr24')
                    self.decode_info['output_format'] = 'bgr24'
                    self.height, self.width = image.shape[:2]
                    return True, image
                try:
                    packet = next(self.packets)
                    packets_seen += 1
                    self.frames.extend(packet.decode())
                except av.error.InvalidDataError:
                    invalid += 1
                    continue
                except StopIteration:
                    return False, None
            raise RuntimeError(f'No valid video frame within decode limits ({invalid} invalid packets)')
        except av.error.FFmpegError as exc:
            self.release()
            raise RuntimeError(f'PyAV decode failed: {exc}') from exc
        except Exception:
            self.release()
            raise

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return self.width
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return self.height
        if prop == cv2.CAP_PROP_FPS:
            return self.fps
        if prop == cv2.CAP_PROP_FOURCC:
            codec = self.decode_info['codec']
            fourcc = {'h264': 'H264', 'mjpeg': 'MJPG', 'rawvideo': 'YUY2'}.get(codec)
            return float(cv2.VideoWriter_fourcc(*fourcc)) if fourcc else math.nan
        return math.nan

    def set(self, prop, value):
        return False

    def release(self):
        if not self.closed:
            self.closed = True
            _close_resources(self.process, self.container, self.log_file)


def _close_resources(process, container, log_file):
    # Stop the producer before closing the input, to unblock pipe readers.
    try:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
    finally:
        try:
            if container is not None:
                container.close()
        finally:
            try:
                if process is not None and process.stdout is not None:
                    process.stdout.close()
            finally:
                if log_file is not None:
                    log_file.close()


def open_pyav_camera(args):
    """Select exactly one camera and return capture, first frame, info and backend."""
    if args.decoder not in ('pyav-software', 'pyav-d3d11va'):
        raise RuntimeError(f'Unsupported PyAV decoder: {args.decoder}')
    if args.format not in ('auto', 'MJPG', 'YUY2', 'H264'):
        raise RuntimeError(f'Unsupported camera format: {args.format}')
    if args.decoder == 'pyav-d3d11va' and args.format not in ('auto', 'H264'):
        raise RuntimeError('D3D11VA requires H264 input; MJPG and YUY2 are unsupported')
    fmt = ('H264' if args.decoder == 'pyav-d3d11va' else 'MJPG') if args.format == 'auto' else args.format
    info = select_camera(enumerate_cameras(cv2.CAP_DSHOW), args.vid, args.pid,
                         args.index, args.device_path)
    if not info.path:
        raise RuntimeError('Selected camera has no DirectShow device path')
    executable = shutil.which('ffmpeg')
    if not executable:
        raise RuntimeError('FFmpeg executable not found on PATH')
    size = f'{args.width}x{args.height}'
    cmd = [executable, '-nostdin', '-hide_banner', '-loglevel', 'warning',
           '-rtbufsize', '256M', '-f', 'dshow']
    if fmt == 'YUY2':
        cmd += ['-pixel_format', 'yuyv422']
    else:
        cmd += ['-vcodec', {'H264': 'h264', 'MJPG': 'mjpeg'}[fmt]]
    output_format = {'H264': 'h264', 'MJPG': 'mjpeg', 'YUY2': 'rawvideo'}[fmt]
    cmd += ['-video_size', size, '-framerate', str(args.fps), '-i',
            'video=@device_pnp_' + info.path, '-an', '-c:v', 'copy']
    if fmt == 'H264':
        cmd += ['-copyinkf']
    cmd += ['-f', output_format, 'pipe:1']
    # Elementary streams otherwise default to 25 FPS in the raw demuxer.
    kwargs = dict(format=output_format, buffer_size=65536,
                  options={'framerate': str(args.fps)})
    if fmt == 'YUY2':
        kwargs['options'] = {'video_size': size, 'pixel_format': 'yuyv422',
                             'framerate': str(args.fps)}
    if args.decoder == 'pyav-d3d11va':
        kwargs['hwaccel'] = HWAccel('d3d11va', allow_software_fallback=False)
    container = cap = process = log_file = None
    try:
        log_dir = Path(__file__).resolve().parent / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / ('pyav_ffmpeg_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.log')
        log_file = log_path.open('xb')
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log_file,
                                   stdin=subprocess.DEVNULL, bufsize=0, shell=False,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        container = av.open(PipeReader(process.stdout), **kwargs)
        stream = container.streams.video[0]
        expected_codec = {'H264': 'h264', 'MJPG': 'mjpeg', 'YUY2': 'rawvideo'}[fmt]
        actual_codec = stream.codec_context.name
        if actual_codec != expected_codec:
            raise RuntimeError(f'Requested {fmt} input but DirectShow opened codec '
                               f'{actual_codec}; requested mode was not negotiated')
        cap = PyAVCapture(container, stream, args.decoder, fmt, process, log_file)
        cap.decode_info['ffmpeg_log'] = str(log_path)
        cap.decode_info['nominal_fps_source'] = 'requested capture rate, passed to raw demuxer'
        ok, first = cap.read()
        if not ok:
            raise RuntimeError('Camera ended before the first video frame')
        return cap, first, info, args.decoder
    except av.error.FFmpegError as exc:
        if cap is not None:
            cap.release()
        else:
            _close_resources(process, container, log_file)
        raise RuntimeError(f'PyAV camera open failed: {exc}; FFmpeg log: {log_path}') from exc
    except Exception:
        if cap is not None:
            cap.release()
        else:
            _close_resources(process, container, log_file)
        raise
