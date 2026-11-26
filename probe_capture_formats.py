"""Opt-in test of DirectShow property negotiation order for this camera."""
import json
import sys
import time
import cv2
from cv2_enumerate_cameras import enumerate_cameras
from camera import ROOT, select_camera


def main():
    if '--run' not in sys.argv:
        print('Use --run to open the target camera.')
        return 2
    info = select_camera(enumerate_cameras(cv2.CAP_DSHOW), 0x32E4, 0x3415)
    fields = {'W': (cv2.CAP_PROP_FRAME_WIDTH, 3840), 'H': (cv2.CAP_PROP_FRAME_HEIGHT, 2160),
              'F': (cv2.CAP_PROP_FPS, 60), 'C': (cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))}
    results = []
    for order in ('WHCF', 'WHFC', 'CWHFC', 'WHCFC'):
        cap = cv2.VideoCapture(info.index, info.backend)
        try:
            operations = []
            for key in order:
                accepted = cap.set(*fields[key])
                fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
                operations.append(dict(key=key, accepted=accepted,
                    fourcc=''.join(chr((fourcc >> (8 * i)) & 255) for i in range(4))))
            count = 0
            start = time.monotonic()
            for _ in range(5):
                ok, frame = cap.read()
                if ok:
                    count += 1
            result = dict(order=order, operations=operations, fps=cap.get(cv2.CAP_PROP_FPS),
                          actual_read_fps=count / (time.monotonic() - start))
            results.append(result)
            print(json.dumps(result), flush=True)
        finally:
            cap.release()
    (ROOT / 'docs' / 'capture_property_order.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
