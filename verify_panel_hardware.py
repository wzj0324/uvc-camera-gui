r"""Opt-in integration check: opens target camera and saves local test media.

Run explicitly with .venv\Scripts\python.exe verify_panel_hardware.py --run
"""
import json
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import time
import tkinter as tk

import cv2
from PIL import ImageGrab

from camera_gui import CameraPanel, ROOT


def main():
    if '--run' not in sys.argv:
        print('Use --run to open the USB camera for integration testing.')
        return 2
    root = tk.Tk()
    app = CameraPanel(root)
    use_pyav = '--pyav' in sys.argv
    # Explicit compatibility baseline, independent of the GUI default preset.
    app.decoder_var.set('OpenCV / 兼容模式')
    app.choose_decoder()
    output = ROOT / 'captures' / 'panel_verification'
    output.mkdir(parents=True, exist_ok=True)
    initial_png_count = len(list((output / 'photos').glob('*.png')))
    initial_jpg_count = len(list((output / 'photos').glob('*.jpg')))
    app.vars['output'].set(str(output))
    app.vars['width'].set('1280')
    app.vars['height'].set('720')
    app.vars['fps'].set('30')
    app.vars['backend'].set('dshow')
    app.vars['format'].set('auto')
    app.vars['photo_format'].set('PNG')
    if use_pyav:
        app.decoder_var.set('PyAV / D3D11VA 显卡解码')
        app.choose_decoder()
    failures = []
    report = {'stages': []}
    deadline = time.monotonic() + 80
    stage = 0
    next_step = 0

    def save_window(filename):
        # Capture this Tk window itself, even when another app covers it.
        root.update_idletasks()
        get_ancestor = ctypes.windll.user32.GetAncestor
        get_ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
        get_ancestor.restype = wintypes.HWND
        handle = get_ancestor(root.winfo_id(), 2)
        ImageGrab.grab(window=int(handle)).save(output / filename)

    def fail(exc):
        failures.append(str(exc))
        app.close()

    def advance():
        nonlocal stage, next_step
        if app.destroyed:
            return
        try:
            assert not app.errors, app.errors
            assert time.monotonic() < deadline, f'Timeout at stage {stage}'
            if stage == 0:
                app.connect_button.invoke()
                stage = 1
            elif stage == 1 and app.connected and app.last_frame is not None:
                report['initial'] = app.device_info
                if use_pyav:
                    assert app.device_info['decode_info']['hardware_active']
                    assert str(app.refresh_button['state']) == 'disabled'
                app.photo_button.invoke()
                stage = 2
            elif stage == 2 and len(list((output / 'photos').glob('*.png'))) > initial_png_count:
                app.tabs.select(app.params_tab)
                value = app.param_vars['brightness'].get()
                if value and not use_pyav:
                    app.apply_param('brightness')  # Write the value just read, never a guessed value.
                next_step = time.monotonic() + 1
                stage = 3
            elif stage == 3 and time.monotonic() > next_step:
                report['brightness_result'] = app.current_vars['brightness'].get()
                app.tabs.select(app.capture_page)
                app.codec_var.set('MPEG-4 / MP4')
                app.record_button.invoke()
                stage = 4
            elif stage == 4 and app.recording:
                next_step = time.monotonic() + 2
                stage = 5
            elif stage == 5 and time.monotonic() > next_step:
                app.record_button.invoke()
                stage = 6
            elif stage == 6 and not app.recording and list((output / 'videos').glob('*.mp4')):
                sidecar = sorted((output / 'videos').glob('*.json'))[-1]
                report['recording'] = json.loads(sidecar.read_text(encoding='utf-8'))
                assert report['recording']['verification']['passed']
                app.vars['width'].set('640')
                app.vars['height'].set('480')
                app.vars['fps'].set('30' if use_pyav else '15')
                app.vars['format'].set('H264' if use_pyav else 'MJPG')
                app.apply_button.invoke()
                stage = 7
            elif stage == 7 and app.connected and app.device_info['width'] == 640:
                report['changed_mode'] = app.device_info
                if use_pyav:
                    assert app.device_info['decode_info']['hardware_active']
                app.vars['photo_format'].set('JPEG')
                app.photo_button.invoke()
                app.export_button.invoke()
                next_step = time.monotonic() + 1
                stage = 8
            elif stage == 8 and time.monotonic() > next_step:
                assert len(list((output / 'photos').glob('*.jpg'))) > initial_jpg_count
                assert list((output / 'diagnostics').glob('*.json'))
                for path in (output / 'photos').iterdir():
                    assert cv2.imread(str(path)) is not None, f'Cannot decode {path}'
                save_window('control_panel.png')
                # Check the narrowest supported window layout as well.
                root.geometry('860x620')
                next_step = time.monotonic() + 1
                stage = 9
            elif stage == 9 and time.monotonic() > next_step:
                save_window('control_panel_narrow.png')
                report['stages'] = ['connect', 'PNG photo', 'parameter read/write same value',
                    'MP4 record/decode', 'change 640x480 MJPG 15FPS', 'JPEG photo',
                    'export device report', 'normal/narrow window render', 'close/release']
                if use_pyav:
                    report['stages'][2] = 'D3D11VA active; unsupported UVC controls disabled'
                    report['stages'][4] = 'change 640x480 H264 30FPS, hardware remains active'
                app.close()
                stage = 10
        except Exception as exc:
            fail(exc)
        if not app.destroyed:
            root.after(100, advance)

    root.after(100, advance)
    root.mainloop()
    report['passed'] = not failures and stage == 10
    report['failures'] = failures
    path = output / ('integration_report_pyav.json' if use_pyav else 'integration_report.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
