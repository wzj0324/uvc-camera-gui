"""Explicit PyAV/D3D11VA verification; --camera opens the target USB device."""
import argparse
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import time

import av
from av.codec.hwaccel import HWAccel, hwdevices_available
import cv2
from cv2_enumerate_cameras import enumerate_cameras

from camera import ROOT, describe, select_camera, stamp


class PipeReader:
    def __init__(self, pipe):
        self.pipe = pipe

    def read(self, size):
        return self.pipe.read(size)

    def seekable(self):
        return False

    def readable(self):
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--camera', action='store_true')
    source.add_argument('--file', type=Path)
    parser.add_argument('--software', action='store_true')
    parser.add_argument('--frames', type=int, default=120)
    parser.add_argument('--warmup', type=int, default=120, help='Discard this many decoded frames before timing')
    parser.add_argument('--width', type=int, default=3840)
    parser.add_argument('--height', type=int, default=2160)
    parser.add_argument('--fps', type=float, default=60)
    parser.add_argument('--bgr', action='store_true', help='Also convert every frame to CPU BGR')
    args = parser.parse_args()
    if args.frames <= 0:
        parser.error('--frames must be positive')
    result = dict(time=datetime.now().astimezone().isoformat(), pyav_version=av.__version__,
                  supported_hw_devices=hwdevices_available(), requested_decoder='software' if args.software else 'd3d11va',
                  allow_software_fallback=None if args.software else False, bgr_conversion=args.bgr,
                  passed=False)
    container = None
    process = None
    error_log = None
    messages = []
    try:
        accel = None if args.software else HWAccel('d3d11va', allow_software_fallback=False)
        if args.camera:
            info = select_camera(enumerate_cameras(cv2.CAP_DSHOW), 0x32E4, 0x3415)
            result['device'] = describe(info)
            filename = 'video=@device_pnp_' + info.path
            executable = shutil.which('ffmpeg')
            if not executable:
                raise RuntimeError('FFmpeg executable is required to select H264 on DirectShow')
            command = [executable, '-nostdin', '-hide_banner', '-loglevel', 'warning',
                       '-rtbufsize', '256M', '-f', 'dshow', '-vcodec', 'h264',
                       '-video_size', f'{args.width}x{args.height}', '-framerate', str(args.fps),
                       '-i', filename, '-an', '-c:v', 'copy', '-copyinkf', '-f', 'h264', 'pipe:1']
            path = ROOT / 'logs' / f'pyav_probe_ffmpeg_{stamp()}.log'
            error_log = path.open('w', encoding='utf-8')
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_log,
                                       bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW)
            result['transport'] = 'FFmpeg DirectShow H264 stream copy -> PyAV decode'
            result['ffmpeg_log'] = str(path)
            result['requested'] = dict(width=args.width, height=args.height, fps=args.fps, codec='h264')
            container = av.open(PipeReader(process.stdout), format='h264', hwaccel=accel, buffer_size=65536,
                                options={'framerate': str(args.fps)})
        else:
            result['file'] = str(args.file.resolve())
            container = av.open(str(args.file), hwaccel=accel)
        stream = container.streams.video[0]
        result['codec'] = stream.codec_context.name
        result['hardware_context_active'] = bool(stream.codec_context.is_hwaccel)
        if not args.software and not result['hardware_context_active']:
            raise RuntimeError('Requested D3D11VA, but the stream has no hardware context')
        invalid_packets = 0
        count = 0
        warmup = args.warmup if args.camera else 0
        discarded = 0
        first_time = None
        start = time.monotonic()
        formats = set()
        shape = None
        for packet in container.demux(stream):
            try:
                frames = packet.decode()
            except av.error.InvalidDataError as exc:
                invalid_packets += 1
                if time.monotonic() - start > 12 or invalid_packets > 600:
                    raise RuntimeError('Camera H264 did not supply decodable SPS/PPS/IDR') from exc
                continue
            for frame in frames:
                if discarded < warmup:
                    if args.bgr:
                        frame.to_ndarray(format='bgr24')
                    discarded += 1
                    if discarded == warmup:
                        start = time.monotonic()
                    continue
                if first_time is None:
                    first_time = time.monotonic()
                formats.add(frame.format.name)
                if args.bgr:
                    shape = frame.to_ndarray(format='bgr24').shape
                count += 1
                result.update(width=frame.width, height=frame.height)
                if count >= args.frames:
                    break
            if count >= args.frames:
                break
            if time.monotonic() - start > 20:
                raise RuntimeError('Verification did not receive enough frames within 20 seconds')
        elapsed = time.monotonic() - start
        if count < args.frames:
            raise RuntimeError(f'Only decoded {count}/{args.frames} frames')
        result.update(frames=count, discarded_warmup_frames=discarded, invalid_startup_packets=invalid_packets,
                      output_formats=sorted(formats), bgr_shape=shape,
                      elapsed_seconds=round(elapsed, 4),
                      steady_read_fps=round((count - 1) / max(time.monotonic() - first_time, 1e-6), 2),
                      note='Includes input pacing, hardware-to-CPU transfer, and optional BGR conversion; not pure GPU throughput.',
                      passed=True)
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if container is not None:
            container.close()
        if process is not None and process.stdout:
            process.stdout.close()
        if error_log:
            error_log.close()
    directory = ROOT / 'captures' / 'diagnostics'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'pyav_{stamp()}.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    print('Report:', path)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
