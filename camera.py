"""Local Windows UVC camera preview, snapshots, recording and diagnostics."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
import math
from pathlib import Path
import sys
import time

try:
    import cv2
    from cv2_enumerate_cameras import enumerate_cameras
except ImportError:
    print('Missing dependencies. Run: .venv\\Scripts\\python.exe -m pip install -r requirements.txt')
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parent
BACKENDS = {'dshow': cv2.CAP_DSHOW, 'msmf': cv2.CAP_MSMF}
RECORDING_CODECS = {'MJPG': '.avi', 'XVID': '.avi', 'mp4v': '.mp4'}
LOG = logging.getLogger('camera')


def stamp() -> str:
    return datetime.now().strftime('%Y%m%d_%H%M%S_%f')


def describe(info) -> dict:
    return dict(index=info.index, name=info.name.strip(), path=info.path,
                vid=info.vid, pid=info.pid, backend=info.backend)


def select_camera(infos, vid: int, pid: int, index=None, device_path=None):
    """Never silently open an unrelated built-in camera."""
    if index is not None:
        matches = [i for i in infos if i.index == index]
    else:
        matches = [i for i in infos if i.vid == vid and i.pid == pid]
    if device_path:
        matches = [i for i in matches if device_path.casefold() in i.path.casefold()]
    if not matches:
        raise RuntimeError('Target camera not found. Check USB connection; run --list. '
                           'Use --index only to explicitly select another device.')
    if len(matches) > 1:
        raise RuntimeError('Multiple matching cameras. Use --device-path to select one '
                           'of the paths shown by --list, or specify --index.')
    return matches[0]


def open_camera(args):
    decoder = getattr(args, 'decoder', 'opencv')
    if decoder != 'opencv':
        if args.backend == 'msmf':
            raise RuntimeError('PyAV camera transport uses DirectShow; select --backend dshow or auto')
        from pyav_capture import open_pyav_camera
        return open_pyav_camera(args)
    if args.format == 'H264':
        raise RuntimeError('H264 capture requires --decoder pyav-software or pyav-d3d11va')
    errors = []
    names = list(BACKENDS) if args.backend == 'auto' else [args.backend]
    for name in names:
        cap = None
        try:
            infos = enumerate_cameras(BACKENDS[name])
            info = select_camera(infos, args.vid, args.pid, args.index, args.device_path)
            LOG.info('Opening %s', json.dumps(describe(info), ensure_ascii=False))
            cap = cv2.VideoCapture(info.index, info.backend)
            if not cap.isOpened():
                raise RuntimeError('Device could not be opened')
            # Negotiation is best effort; report the actual frame dimensions below.
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
            cap.set(cv2.CAP_PROP_FPS, args.fps)
            # This driver resets the subtype when size OR FPS changes. Select
            # FOURCC last; WHFC was verified against WHCF on the real device.
            if args.format != 'auto':
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*args.format))
            # This deadline limits retries, not a blocking native driver read().
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ok, frame = cap.read()
                if ok and frame is not None and frame.size:
                    LOG.info('Opened %s: %dx%d, reported FPS=%s', name,
                             frame.shape[1], frame.shape[0], cap.get(cv2.CAP_PROP_FPS))
                    return cap, frame, info, name
                time.sleep(0.05)
            raise RuntimeError('No frame received during startup')
        except (RuntimeError, cv2.error, OSError, NotImplementedError) as exc:
            if cap is not None:
                cap.release()
            errors.append(f'{name}: {exc}')
            LOG.warning('%s failed: %s', name, exc)
    raise RuntimeError('; '.join(errors) + '. Close other camera apps and check '
                       'Windows Settings > Privacy & security > Camera > desktop apps.')


def read_frame(cap):
    for _ in range(10):
        ok, frame = cap.read()
        if ok and frame is not None and frame.size:
            return frame
        time.sleep(0.03)
    raise RuntimeError('Camera stopped delivering frames. Check USB / HDMI signal and '
                       'close other camera applications, then restart this program.')


def snapshot(frame, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'photo_{stamp()}.png'
    ok, data = cv2.imencode('.png', frame)
    if not ok:
        raise RuntimeError('PNG encoding failed')
    # Python file IO supports Chinese paths on Windows.
    path.write_bytes(data.tobytes())
    LOG.info('Photo saved: %s', path)
    return path


class Recording:
    def __init__(self, frame, fps: float, directory: Path, codec: str = 'MJPG'):
        if codec not in RECORDING_CODECS:
            raise ValueError(f'Unsupported recording codec: {codec}')
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('Recording FPS must be finite and positive')
        if frame is None or len(frame.shape) < 2 or frame.shape[0] <= 0 or frame.shape[1] <= 0:
            raise ValueError('Recording frame dimensions must be positive')
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f'video_{stamp()}{RECORDING_CODECS[codec]}'
        self.size = (frame.shape[1], frame.shape[0])
        self.codec = codec
        self.fps = fps
        self.frames = 0
        self.started = time.monotonic()
        self.closed = False
        self.result = None
        self.writer = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*codec),
                                      fps, self.size)
        if not self.writer.isOpened():
            self.writer.release()
            raise RuntimeError(f'Cannot create video with codec {codec}: {self.path}')
        LOG.info('Recording started: %s; nominal FPS=%g; video only, no audio', self.path, fps)

    def write(self, frame):
        if (frame.shape[1], frame.shape[0]) != self.size:
            raise RuntimeError('Camera frame size changed while recording; restart recording')
        self.writer.write(frame)
        self.frames += 1

    def close(self):
        if self.closed:
            return self.result
        self.writer.release()
        self.closed = True
        result = dict(path=str(self.path), frames=self.frames, nominal_fps=self.fps,
                      elapsed_seconds=round(time.monotonic() - self.started, 3),
                      width=self.size[0], height=self.size[1], audio=False,
                      requested_codec=self.codec, actual_fourcc=None)
        self.result = result
        LOG.info('Checking recorded video: %s', self.path)
        try:
            result['verification'] = verify_video(result)
        except RuntimeError as exc:
            result['verification'] = dict(passed=False, error=str(exc))
            LOG.error('Video verification failed: %s', self.path)
            raise
        finally:
            self.path.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                      encoding='utf-8')
        LOG.info('Recording saved: %s (%d frames)', self.path, self.frames)
        return result


def verify_video(result: dict) -> dict:
    reader = cv2.VideoCapture(result['path'])
    try:
        fourcc = int(reader.get(cv2.CAP_PROP_FOURCC))
        if fourcc:
            result['actual_fourcc'] = ''.join(chr((fourcc >> (8 * i)) & 0xff) for i in range(4))
        count = 0
        while True:
            ok, frame = reader.read()
            if not ok:
                break
            if (frame.shape[1], frame.shape[0]) != (result['width'], result['height']):
                raise RuntimeError('Recorded video size verification failed')
            count += 1
        if count == 0 or count != result['frames']:
            raise RuntimeError(f'Video verification failed: wrote {result["frames"]}, decoded {count}')
        return dict(decoded_frames=count, passed=True)
    finally:
        reader.release()


def diagnose(cap, frame, info, backend, args):
    # Drain startup/probe buffering before measuring the read path.
    warmup_until = time.monotonic() + 2
    while time.monotonic() < warmup_until:
        frame = read_frame(cap)
    started = time.monotonic()
    for _ in range(30):
        frame = read_frame(cap)
    elapsed = time.monotonic() - started
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    actual_format = ''.join(chr((fourcc >> (8 * n)) & 0xFF) for n in range(4)).strip('\x00')
    result = dict(time=datetime.now().astimezone().isoformat(), device=describe(info),
                  backend=backend, width=frame.shape[1], height=frame.shape[0],
                  actual_format=actual_format, requested_format=args.format,
                  decode_info=getattr(cap, 'decode_info', dict(decoder_requested='opencv',
                      hardware_active=False, note='No hardware decoding requested by this program')),
                  reported_fps=cap.get(cv2.CAP_PROP_FPS),
                  measured_fps=round(30 / max(elapsed, 0.001), 2),
                  frames_read=30, warmup_seconds=2, mean_brightness=round(float(frame.mean()), 2),
                  opencv_version=cv2.__version__, python_version=sys.version,
                  note='Read-path FPS includes input, decoding, memory transfer and BGR conversion; '
                       'not pure decoder throughput. Frame reads do not verify HDMI content.')
    path = args.output / 'diagnostics' / f'capture_{stamp()}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    LOG.info('Diagnostic report saved: %s', path)


def run(args):
    if args.list:
        for name, backend in BACKENDS.items():
            print(f'[{name}]')
            for info in enumerate_cameras(backend):
                print(json.dumps(describe(info), ensure_ascii=False))
        return
    cap, frame, info, backend = open_camera(args)
    recording = None
    window = 'USB Camera | S: photo | R: record | Q/Esc: exit'
    try:
        if args.test:
            diagnose(cap, frame, info, backend, args)
            return
        if args.snapshot:
            print(snapshot(frame, args.output / 'photos'))
            return
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(fps) or not 1 <= fps <= 240:
            fps = args.fps
        if args.record_seconds:
            recording = Recording(frame, fps, args.output / 'videos', args.record_codec)
            deadline = time.monotonic() + args.record_seconds
            while time.monotonic() < deadline:
                recording.write(frame)
                frame = read_frame(cap)
            result = recording.close()
            recording = None
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window, min(frame.shape[1], 1280), min(frame.shape[0], 720))
        started = time.monotonic()
        previous = started
        measured_fps = 0.0
        LOG.info('Preview ready. S=photo, R=start/stop video, Q/Esc=exit; click video first.')
        while True:
            now = time.monotonic()
            if recording is not None:
                recording.write(frame)
            view = frame.copy()
            cv2.putText(view, f'{frame.shape[1]}x{frame.shape[0]}  {measured_fps:.1f} fps',
                        (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
            if recording is not None:
                cv2.putText(view, 'REC', (12, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
            cv2.imshow(window, view)
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord('q'), ord('Q')) or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                break
            if args.preview_seconds and now - started >= args.preview_seconds:
                break
            if key in (ord('s'), ord('S')):
                try:
                    snapshot(frame, args.output / 'photos')
                except (OSError, RuntimeError, cv2.error) as exc:
                    LOG.error('Photo failed: %s', exc)
            if key in (ord('r'), ord('R')):
                if recording is not None:
                    recording.close()
                    recording = None
                else:
                    try:
                        recording = Recording(frame, fps, args.output / 'videos', args.record_codec)
                    except (OSError, RuntimeError, cv2.error) as exc:
                        LOG.error('Recording failed: %s', exc)
            frame = read_frame(cap)
            current = time.monotonic()
            measured_fps = 1 / max(current - previous, 0.001)
            previous = current
    finally:
        # Always release the device, even if disk writes or finalizing AVI fails.
        try:
            if recording is not None:
                recording.close()
        finally:
            cap.release()
            cv2.destroyAllWindows()


def positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return value


def positive_float(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return value


def usb_id(text):
    try:
        value = int(text, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('USB ID must be hexadecimal, e.g. 32E4') from exc
    if not 0 <= value <= 0xFFFF:
        raise argparse.ArgumentTypeError('USB ID must fit in 16 bits')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vid', type=usb_id, default=0x32E4)
    parser.add_argument('--pid', type=usb_id, default=0x3415)
    parser.add_argument('--index', type=int, help='Explicit index within the selected backend')
    parser.add_argument('--device-path', help='Case-insensitive substring of a --list device path')
    parser.add_argument('--backend', choices=['auto', *BACKENDS], default='auto')
    parser.add_argument('--width', type=positive_int, default=1280)
    parser.add_argument('--height', type=positive_int, default=720)
    parser.add_argument('--fps', type=positive_float, default=30.0)
    parser.add_argument('--format', choices=['auto', 'MJPG', 'YUY2', 'H264'], default='auto')
    parser.add_argument('--decoder', choices=['opencv', 'pyav-software', 'pyav-d3d11va'], default='opencv',
                        help='Explicit decoder; PyAV GPU mode uses H264 and prohibits CPU fallback')
    parser.add_argument('--output', type=Path, default=ROOT / 'captures')
    parser.add_argument('--record-codec', choices=list(RECORDING_CODECS), default='MJPG')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--list', action='store_true', help='List cameras without opening streams')
    modes.add_argument('--test', action='store_true', help='Read 30 frames and write a JSON report')
    modes.add_argument('--snapshot', action='store_true', help='Save one PNG without a preview')
    modes.add_argument('--record-seconds', type=positive_float, help='Record, close, then decode video to verify')
    modes.add_argument('--preview-seconds', type=positive_float, help='Preview with automatic exit')
    args = parser.parse_args()
    if args.index is not None and args.index < 0:
        parser.error('--index must be nonnegative')
    if args.index is not None and args.backend == 'auto' and args.decoder == 'opencv':
        parser.error('--index requires --backend dshow or --backend msmf; indices differ by backend')
    log_dir = ROOT / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(log_dir / 'camera.log', encoding='utf-8')])
    try:
        run(args)
        return 0
    except KeyboardInterrupt:
        LOG.info('Stopped by user')
        return 130
    except (RuntimeError, OSError, cv2.error) as exc:
        LOG.error('%s', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
