"""Chinese Tk control panel for a local UVC camera; capture has one owner thread."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Direct execution (including a .py double-click) uses the installed project
# environment. Importing this module for testing never switches interpreters.
if __name__ == '__main__':
    project_python = Path(__file__).resolve().parent / '.venv' / 'Scripts' / 'python.exe'
    if project_python.is_file() and Path(sys.prefix).resolve() != project_python.parent.parent.resolve():
        raise SystemExit(subprocess.call([str(project_python), str(Path(__file__).resolve()), *sys.argv[1:]]))

import cv2
from PIL import Image, ImageTk

from camera import BACKENDS, ROOT, Recording, describe, open_camera, read_frame, snapshot, stamp
from camera_controls import CONTROL_SPECS, apply_controls, read_controls

LOG = logging.getLogger('camera.panel')
PROFILE = ROOT / 'settings.json'
DEFAULTS = dict(vid='32E4', pid='3415', width=3840, height=2160, fps=60,
                backend='dshow', format='H264', codec='MJPG', photo_format='PNG',
                decoder='pyav-d3d11va', output=str(ROOT / 'captures'))
CODECS = {'MJPG / AVI': 'MJPG', 'XVID / AVI': 'XVID', 'MPEG-4 / MP4': 'mp4v'}
DECODERS = {'OpenCV / 兼容模式': 'opencv', 'PyAV / CPU 软件解码': 'pyav-software',
            'PyAV / D3D11VA 显卡解码': 'pyav-d3d11va'}


def load_settings():
    settings = DEFAULTS.copy()
    if PROFILE.exists():
        try:
            data = json.loads(PROFILE.read_text(encoding='utf-8'))
            settings.update({key: data[key] for key in settings if key in data})
        except (OSError, ValueError, TypeError) as exc:
            LOG.warning('Cannot load settings: %s', exc)
    return settings


class CaptureSession(threading.Thread):
    """Only this thread calls VideoCapture and VideoWriter for this session."""
    def __init__(self, settings, events, frames):
        super().__init__(daemon=True, name='uvc-capture')
        self.settings = settings
        self.events = events
        self.frames = frames
        self.commands = queue.Queue()
        self.stop_event = threading.Event()

    def emit(self, kind, payload):
        self.events.put((kind, payload))

    def run(self):
        cap = None
        recorder = None
        try:
            s = self.settings
            args = argparse.Namespace(vid=int(s['vid'], 16), pid=int(s['pid'], 16),
                index=None, device_path=None, backend=s['backend'], width=s['width'],
                height=s['height'], fps=s['fps'], format=s['format'], decoder=s.get('decoder', 'opencv'))
            cap, frame, info, backend = open_camera(args)
            actual_fps = cap.get(cv2.CAP_PROP_FPS)
            if not math.isfinite(actual_fps) or not 1 <= actual_fps <= 240:
                actual_fps = s['fps']
            fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
            actual_format = ''.join(chr((fourcc >> (8 * n)) & 0xFF) for n in range(4)).strip('\x00')
            self.emit('opened', dict(device=describe(info), backend=backend,
                width=frame.shape[1], height=frame.shape[0], fps=actual_fps,
                format=actual_format or '驱动未报告',
                decode_info=getattr(cap, 'decode_info', dict(decoder_requested='opencv', hardware_active=False)),
                controls_supported=s.get('decoder', 'opencv') == 'opencv'))
            self.emit('controls', read_controls(cap))
            frame_count = 0
            started = time.monotonic()
            stats_at = started
            while not self.stop_event.is_set():
                while True:
                    try:
                        name, payload = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        if name == 'photo':
                            if payload == 'PNG':
                                path = snapshot(frame, Path(s['output']) / 'photos')
                            else:
                                directory = Path(s['output']) / 'photos'
                                directory.mkdir(parents=True, exist_ok=True)
                                path = directory / f'photo_{stamp()}.jpg'
                                ok, data = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                                if not ok:
                                    raise RuntimeError('JPEG 编码失败')
                                path.write_bytes(data.tobytes())
                            self.emit('message', f'照片已保存：{path}')
                        elif name == 'record':
                            if recorder is None:
                                recorder = Recording(frame, actual_fps, Path(s['output']) / 'videos', codec=payload)
                                self.emit('recording', True)
                                self.emit('message', f'正在录像：{recorder.path.name}（不含音频）')
                            else:
                                self.emit('message', '正在封装并逐帧校验录像，请稍候…')
                                finished, recorder = recorder, None
                                try:
                                    result = finished.close()
                                    self.emit('message', f'录像已通过解码校验：{result["path"]}')
                                finally:
                                    self.emit('recording', False)
                        elif name == 'refresh':
                            self.emit('controls', read_controls(cap))
                        elif name == 'set':
                            result = apply_controls(cap, payload)
                            self.emit('applied', result)
                        elif name == 'export':
                            report = dict(time=datetime.now().astimezone().isoformat(),
                                device=describe(info), backend=backend,
                                requested=s, actual=dict(width=frame.shape[1], height=frame.shape[0],
                                reported_fps=actual_fps, format=actual_format), controls=read_controls(cap),
                                decode_info=getattr(cap, 'decode_info', dict(decoder_requested='opencv', hardware_active=False)))
                            path = Path(s['output']) / 'diagnostics' / f'panel_{stamp()}.json'
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                            self.emit('message', f'当前设备参数已导出：{path}')
                    except (RuntimeError, ValueError, OSError, cv2.error) as exc:
                        self.emit('error', str(exc))
                if self.stop_event.is_set():
                    break
                if recorder is not None:
                    recorder.write(frame)
                # One pending frame only: a slow UI cannot grow the image queue.
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.frames.put_nowait(frame)
                except queue.Full:
                    pass
                frame = read_frame(cap)
                frame_count += 1
                now = time.monotonic()
                if now - stats_at >= 1:
                    self.emit('fps', frame_count / (now - started))
                    stats_at = now
        except Exception as exc:
            LOG.exception('Camera session failed')
            self.emit('error', str(exc))
        finally:
            try:
                if recorder is not None:
                    self.emit('message', '正在结束并校验录像…')
                    result = recorder.close()
                    self.emit('message', f'录像已校验：{result["path"]}')
            except Exception as exc:
                LOG.exception('Recording finalization failed')
                self.emit('error', f'录像结束失败：{exc}')
            finally:
                if cap is not None:
                    cap.release()
                self.emit('recording', False)
                self.emit('stopped', None)


class CameraPanel:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.session = None
        self.events = queue.Queue()
        self.frames = queue.Queue(maxsize=1)
        self.last_frame = None
        self.last_image = None
        self.recording = False
        self.closing = False
        self.restart = False
        self.connected = False
        self.controls_supported = False
        self.pending_settings = None
        self.errors = []
        self.destroyed = False
        self.param_vars = {}
        self.current_vars = {}
        self.device_info = None
        root.title('UVC 摄像头控制台 · USB 32E4:3415')
        root.geometry('1220x820')
        root.minsize(860, 620)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.setup_style()
        self.build()
        self.root.after(40, self.poll)

    def setup_style(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        self.root.configure(bg='#edf1f7')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#edf1f7')
        style.configure('TButton', padding=(10, 6))
        style.configure('Accent.TButton', background='#225ec9', foreground='white')
        style.map('Accent.TButton', background=[('active', '#194ca7')])
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 18, 'bold'), foreground='#163052')
        style.configure('Muted.TLabel', foreground='#52647c')
        style.configure('TLabelframe', padding=10)
        style.configure('TNotebook.Tab', padding=(12, 8))

    def build(self):
        shell = ttk.Frame(self.root, padding=16)
        shell.pack(fill='both', expand=True)
        ttk.Label(shell, text='UVC 摄像头控制台', style='Title.TLabel').pack(anchor='w')
        ttk.Label(shell, text='本地预览、图像控制与视频录制', style='Muted.TLabel').pack(anchor='w', pady=(2, 12))
        body = ttk.Panedwindow(shell, orient='horizontal')
        body.pack(fill='both', expand=True)
        left = ttk.Frame(body, width=380)
        right = ttk.Frame(body)
        body.add(left, weight=0)
        body.add(right, weight=1)
        self.tabs = ttk.Notebook(left)
        self.tabs.pack(fill='both', expand=True, padx=(0, 12))
        self.capture_page = ttk.Frame(self.tabs)
        capture_canvas = tk.Canvas(self.capture_page, highlightthickness=0, bg='#edf1f7', width=355)
        capture_scroll = ttk.Scrollbar(self.capture_page, orient='vertical', command=capture_canvas.yview)
        capture_canvas.configure(yscrollcommand=capture_scroll.set)
        capture_scroll.pack(side='right', fill='y')
        capture_canvas.pack(side='left', fill='both', expand=True)
        self.capture_tab = ttk.Frame(capture_canvas, padding=10)
        capture_item = capture_canvas.create_window((0, 0), window=self.capture_tab, anchor='nw')
        self.capture_tab.bind('<Configure>', lambda e: capture_canvas.configure(scrollregion=capture_canvas.bbox('all')))
        capture_canvas.bind('<Configure>', lambda e: capture_canvas.itemconfigure(capture_item, width=e.width))
        self.params_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.capture_page, text='采集与保存')
        self.tabs.add(self.params_tab, text='图像参数')
        self.build_capture_tab()
        self.build_params_tab()
        self.preview = tk.Label(right, text='选择采集设置后，点击「连接摄像头」', bg='#122037',
                                fg='#b8c8dd', font=('Microsoft YaHei UI', 12))
        self.preview.pack(fill='both', expand=True)
        self.actual_var = tk.StringVar(value='尚未连接 · 实际输出尺寸和帧率将在连接后显示')
        actual_label = ttk.Label(right, textvariable=self.actual_var, wraplength=650, style='Muted.TLabel')
        actual_label.pack(fill='x', pady=8)
        actions = ttk.Frame(right)
        actions.pack(fill='x')
        self.photo_button = ttk.Button(actions, text='拍照', command=self.take_photo)
        self.photo_button.pack(side='left')
        self.record_button = ttk.Button(actions, text='开始录像', command=self.toggle_record)
        self.record_button.pack(side='left', padx=8)
        ttk.Button(actions, text='打开保存目录', command=self.open_output).pack(side='left')
        self.export_button = ttk.Button(actions, text='导出参数', command=lambda: self.command('export', None))
        self.export_button.pack(side='right')
        encoding_label = ttk.Label(right, text='采集像素格式控制 USB 输出；录像编码控制保存文件。录像不含音频。',
                                   wraplength=650, style='Muted.TLabel')
        encoding_label.pack(fill='x', pady=(10, 4))
        right.bind('<Configure>', lambda e: [label.configure(wraplength=max(150, e.width - 4))
                                            for label in (actual_label, encoding_label)])
        self.status_var = tk.StringVar(value='就绪。曝光等参数使用设备原始单位，不同设备可调范围不同。')
        status_label = ttk.Label(shell, textvariable=self.status_var, wraplength=1100)
        status_label.pack(side='bottom', fill='x', pady=(12, 0), before=body)
        shell.bind('<Configure>', lambda e: status_label.configure(wraplength=max(200, e.width - 32)))
        self.set_controls_enabled(False)

    def build_capture_tab(self):
        tab = self.capture_tab
        tab.columnconfigure(1, weight=1)
        self.vars = {key: tk.StringVar(value=str(value)) for key, value in self.settings.items()}
        row = 0
        for label, key in [('USB VID（十六进制）', 'vid'), ('USB PID（十六进制）', 'pid')]:
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky='w', pady=4)
            ttk.Entry(tab, textvariable=self.vars[key], width=16).grid(row=row, column=1, sticky='ew', padx=(8, 0))
            row += 1
        self.mode_var = tk.StringVar(value=f'{self.settings["width"]} × {self.settings["height"]}')
        ttk.Label(tab, text='常用分辨率').grid(row=row, column=0, sticky='w', pady=5)
        modes = ttk.Combobox(tab, textvariable=self.mode_var, state='readonly', width=18,
                            values=['640 × 480', '800 × 600', '1280 × 720', '1920 × 1080', '2560 × 1440', '3840 × 2160'])
        modes.grid(row=row, column=1, sticky='ew', padx=(8, 0))
        modes.bind('<<ComboboxSelected>>', self.choose_resolution)
        row += 1
        for label, key in [('宽度（px）', 'width'), ('高度（px）', 'height'), ('请求帧率（FPS）', 'fps')]:
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky='w', pady=5)
            if key == 'fps':
                field = ttk.Combobox(tab, textvariable=self.vars[key], width=16,
                                     values=['15', '24', '25', '30', '50', '59.94', '60'])
            else:
                field = ttk.Entry(tab, textvariable=self.vars[key], width=16)
            field.grid(row=row, column=1, sticky='ew', padx=(8, 0))
            row += 1
        self.decoder_var = tk.StringVar(value=next((label for label, value in DECODERS.items()
                                                   if value == self.settings['decoder']), next(iter(DECODERS))))
        ttk.Label(tab, text='解码器').grid(row=row, column=0, sticky='w', pady=5)
        decoder_box = ttk.Combobox(tab, textvariable=self.decoder_var, values=list(DECODERS), state='readonly', width=16)
        decoder_box.grid(row=row, column=1, sticky='ew', padx=(8, 0))
        decoder_box.bind('<<ComboboxSelected>>', self.choose_decoder)
        row += 1
        for label, key, choices in [('采集像素格式', 'format', ['auto', 'MJPG', 'YUY2', 'H264']),
                                     ('Windows 后端', 'backend', ['auto', 'dshow', 'msmf']),
                                     ('照片格式', 'photo_format', ['PNG', 'JPEG'])]:
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky='w', pady=5)
            ttk.Combobox(tab, textvariable=self.vars[key], values=choices, state='readonly', width=16).grid(
                row=row, column=1, sticky='ew', padx=(8, 0))
            row += 1
        self.codec_var = tk.StringVar(value=next((k for k, v in CODECS.items() if v == self.settings['codec']), 'MJPG / AVI'))
        ttk.Label(tab, text='录像编码 / 容器').grid(row=row, column=0, sticky='w', pady=5)
        ttk.Combobox(tab, textvariable=self.codec_var, values=list(CODECS), state='readonly', width=16).grid(
            row=row, column=1, sticky='ew', padx=(8, 0))
        row += 1
        ttk.Label(tab, text='保存目录').grid(row=row, column=0, sticky='w', pady=(10, 3))
        ttk.Button(tab, text='选择…', command=self.choose_output).grid(row=row, column=1, sticky='e')
        row += 1
        ttk.Entry(tab, textvariable=self.vars['output']).grid(row=row, column=0, columnspan=2, sticky='ew', pady=4)
        row += 1
        self.connect_button = ttk.Button(tab, text='连接摄像头', style='Accent.TButton', command=self.connect)
        self.connect_button.grid(row=row, column=0, columnspan=2, sticky='ew', pady=(12, 4))
        row += 1
        buttons = ttk.Frame(tab)
        buttons.grid(row=row, column=0, columnspan=2, sticky='ew', pady=4)
        self.apply_button = ttk.Button(buttons, text='应用采集设置', command=self.apply_capture)
        self.apply_button.pack(side='left', fill='x', expand=True)
        ttk.Button(buttons, text='保存配置', command=self.save_profile).pack(side='left', padx=(6, 0))
        row += 1
        ttk.Label(tab, text='常用分辨率是请求预设，并非设备支持列表。\n修改宽高、帧率、像素格式后需重新连接。\n保存目录在下次连接后生效。',
                  style='Muted.TLabel', wraplength=335).grid(row=row, column=0, columnspan=2, sticky='w', pady=8)

    def build_params_tab(self):
        ttk.Label(self.params_tab, text='图像参数需使用 OpenCV 兼容模式调整。\nPyAV 模式仅采集/解码，不提供 UVC 参数设置。\n数值使用设备原始单位；0 不代表支持。',
                  style='Muted.TLabel', wraplength=335).pack(anchor='w', pady=(0, 6))
        self.refresh_button = ttk.Button(self.params_tab, text='读取设备当前参数', command=lambda: self.command('refresh', None))
        self.refresh_button.pack(fill='x', pady=4)
        container = ttk.Frame(self.params_tab)
        container.pack(fill='both', expand=True)
        canvas = tk.Canvas(container, highlightthickness=0, bg='#edf1f7', width=340)
        scrollbar = ttk.Scrollbar(container, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        rows = ttk.Frame(canvas)
        item = canvas.create_window((0, 0), window=rows, anchor='nw')
        rows.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(item, width=e.width))
        self.param_buttons = []
        for number, spec in enumerate(CONTROL_SPECS):
            row = ttk.Frame(rows, padding=(0, 3))
            row.pack(fill='x')
            ttk.Label(row, text=spec['label'], width=13).grid(row=0, column=0, sticky='w')
            var = tk.StringVar()
            current = tk.StringVar(value='当前：未读取')
            self.param_vars[spec['key']] = var
            self.current_vars[spec['key']] = current
            ttk.Entry(row, textvariable=var, width=9).grid(row=0, column=1, padx=4)
            button = ttk.Button(row, text='设置', width=5,
                                command=lambda key=spec['key']: self.apply_param(key))
            button.grid(row=0, column=2)
            self.param_buttons.append(button)
            ttk.Label(row, textvariable=current, style='Muted.TLabel', wraplength=305).grid(
                row=1, column=0, columnspan=3, sticky='w')
            if spec['key'] in ('auto_exposure', 'auto_wb', 'autofocus'):
                ttk.Label(row, text=spec['hint'], style='Muted.TLabel', wraplength=305).grid(
                    row=2, column=0, columnspan=3, sticky='w')
        ttk.Label(self.params_tab, text='驱动可能拒绝或修正请求；请以读回结果为准。\n修改直接作用于设备，程序不自动恢复出厂参数。',
                  style='Muted.TLabel', wraplength=335).pack(anchor='w', pady=6)

    def choose_resolution(self, event=None):
        width, height = self.mode_var.get().split('×')
        self.vars['width'].set(width.strip())
        self.vars['height'].set(height.strip())

    def choose_decoder(self, event=None):
        decoder = DECODERS[self.decoder_var.get()]
        self.vars['decoder'].set(decoder)
        if decoder == 'pyav-d3d11va':
            self.vars['format'].set('H264')
            self.vars['backend'].set('dshow')
        elif decoder == 'opencv' and self.vars['format'].get() == 'H264':
            self.vars['format'].set('auto')
        self.status_var.set('解码器设置将在点击「应用采集设置」后生效。')

    def read_settings(self):
        s = {key: var.get().strip() for key, var in self.vars.items()}
        for key in ('width', 'height'):
            s[key] = int(s[key])
            if not 1 <= s[key] <= 16384:
                raise ValueError('宽高须在 1～16384 像素之间')
        s['fps'] = float(s['fps'])
        if not math.isfinite(s['fps']) or not 0 < s['fps'] <= 1000:
            raise ValueError('帧率须为 0～1000 之间的有限正数')
        for key in ('vid', 'pid'):
            value = int(s[key], 16)
            if not 0 <= value <= 0xFFFF:
                raise ValueError('VID / PID 须为 16 位十六进制数')
        s['decoder'] = DECODERS[self.decoder_var.get()]
        if s['backend'] not in ('auto', *BACKENDS) or s['format'] not in ('auto', 'MJPG', 'YUY2', 'H264'):
            raise ValueError('采集格式或后端无效')
        if s['decoder'] == 'opencv' and s['format'] == 'H264':
            raise ValueError('H264 采集请选择 PyAV 解码器')
        if s['decoder'] == 'pyav-d3d11va' and s['format'] not in ('auto', 'H264'):
            raise ValueError('显卡解码请选择 H264；本机没有可用的 MJPEG D3D11VA 硬解路径')
        if s['decoder'] != 'opencv' and s['backend'] == 'msmf':
            raise ValueError('PyAV 采集请选择 dshow 或 auto 后端')
        if s['photo_format'] not in ('PNG', 'JPEG'):
            raise ValueError('照片格式无效')
        if not s['output']:
            raise ValueError('请选择保存目录')
        s['output'] = str(Path(s['output']).expanduser().resolve())
        s['codec'] = CODECS[self.codec_var.get()]
        return s

    def show_error(self, error):
        text = str(error)
        self.errors.append(text)
        self.status_var.set(f'操作失败：{text}')
        LOG.error('%s', text)
        if not self.closing:
            messagebox.showerror('摄像头操作失败', text, parent=self.root)

    def set_controls_enabled(self, enabled):
        for widget in (self.photo_button, self.record_button, self.export_button):
            widget.configure(state='normal' if enabled else 'disabled')
        for widget in (self.refresh_button, *self.param_buttons):
            widget.configure(state='normal' if enabled and self.controls_supported else 'disabled')

    def connect(self):
        if self.session is not None:
            self.restart = False
            self.disconnect()
            return
        try:
            settings = self.read_settings()
        except (ValueError, KeyError, OSError) as exc:
            self.show_error(exc)
            return
        self.start_session(settings)

    def start_session(self, settings):
        self.settings = settings
        self.mode_var.set(f'{settings["width"]} × {settings["height"]}')
        self.connected = False
        self.set_controls_enabled(False)
        while not self.frames.empty():
            self.frames.get_nowait()
        self.last_frame = None
        self.session = CaptureSession(settings.copy(), self.events, self.frames)
        self.connect_button.configure(text='停止连接')
        self.status_var.set('正在打开指定 USB 摄像头，请稍候…')
        self.session.start()

    def disconnect(self):
        if self.session is not None:
            self.set_controls_enabled(False)
            self.connect_button.configure(state='disabled')
            self.status_var.set('正在停止采集并结束录像，请稍候…')
            self.session.stop_event.set()

    def apply_capture(self):
        try:
            settings = self.read_settings()
        except (ValueError, KeyError, OSError) as exc:
            self.show_error(exc)
            return
        if self.recording:
            self.show_error('请先停止录像并等待校验，再更改采集设置。')
            return
        if self.session is None:
            self.start_session(settings)
        else:
            self.pending_settings = settings
            self.restart = True
            self.disconnect()

    def command(self, name, payload):
        if self.session is None or not self.connected or self.session.stop_event.is_set():
            self.status_var.set('请先连接摄像头。')
            return
        self.session.commands.put((name, payload))

    def take_photo(self):
        self.command('photo', self.vars['photo_format'].get())

    def toggle_record(self):
        self.command('record', CODECS[self.codec_var.get()])

    def apply_param(self, key):
        try:
            value = float(self.param_vars[key].get())
            if not math.isfinite(value):
                raise ValueError('参数须为有限数值')
        except ValueError as exc:
            self.show_error(f'参数格式错误：{exc}')
            return
        self.command('set', {key: value})

    def choose_output(self):
        value = filedialog.askdirectory(parent=self.root, title='选择照片、录像和诊断保存目录')
        if value:
            self.vars['output'].set(value)

    def open_output(self):
        try:
            # Show the directory actually used by the active session.
            path = Path(self.settings['output'] if self.session else self.vars['output'].get())
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path.resolve()))
        except OSError as exc:
            self.show_error(exc)

    def save_profile(self):
        try:
            settings = self.read_settings()
            temporary = PROFILE.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(PROFILE)
            self.status_var.set('采集与保存配置已保存，下次启动自动读取。图像参数需连接后单独设置。')
        except (ValueError, KeyError, OSError) as exc:
            self.show_error(exc)

    def on_event(self, kind, payload):
        if kind == 'opened':
            self.connected = True
            self.device_info = payload
            self.controls_supported = payload.get('controls_supported', True)
            self.set_controls_enabled(True)
            self.connect_button.configure(text='断开摄像头', state='normal')
            self.actual_var.set(f'{payload["device"]["name"]} · {payload["width"]}×{payload["height"]} · '
                                f'驱动 {payload["fps"]:.2f} FPS · {payload["format"]} · {payload["backend"]}')
            decode = payload.get('decode_info', {})
            mode = 'D3D11VA 硬解已验证（禁止软件回退）' if decode.get('hardware_active') else decode.get('decoder_requested', 'opencv')
            self.status_var.set(f'已连接 · {mode}。采集设置为请求值；下方显示实际输出。')
        elif kind == 'controls':
            for key, value in payload.items():
                self.current_vars[key].set(f'当前：{value:g}（支持状态由设置结果判断）' if value is not None else '当前：驱动未提供读值')
                self.param_vars[key].set(f'{value:g}' if value is not None else '')
        elif kind == 'applied':
            for key, result in payload.items():
                actual = result['reported']
                value = f'{actual:g}' if actual is not None else '未提供'
                status = '驱动接受' if result['accepted'] else '驱动拒绝 / 不支持'
                if result.get('error'):
                    status = result['error']
                elif result['accepted'] and result['matched'] is False:
                    status = '读回不同，可能被驱动修正或受自动模式影响'
                self.current_vars[key].set(f'读回：{value}；{status}')
                self.status_var.set(f'{key} 请求 {result["requested"]}，读回 {value}；{status}')
        elif kind == 'recording':
            self.recording = payload
            self.record_button.configure(text='停止录像并校验' if payload else '开始录像')
        elif kind == 'fps':
            if self.device_info:
                info = self.device_info
                self.actual_var.set(f'{info["device"]["name"]} · {info["width"]}×{info["height"]} · '
                                    f'实测 {payload:.1f} FPS / 流 {info["fps"]:.1f} · {info["format"]} · {info["backend"]}')
        elif kind == 'message':
            self.status_var.set(payload)
            LOG.info('%s', payload)
        elif kind == 'error':
            self.show_error(payload)
        elif kind == 'stopped':
            self.connected = False
            self.set_controls_enabled(False)
            self.connect_button.configure(text='连接摄像头', state='normal')
            # run() posts this last; joining with zero timeout never freezes Tk.
            old = self.session
            if old is not None and old.is_alive():
                self.root.after(20, lambda: self.finish_stop(old))
            else:
                self.finish_stop(old)

    def finish_stop(self, old):
        if old is not None and old.is_alive():
            self.root.after(20, lambda: self.finish_stop(old))
            return
        self.session = None
        if self.closing:
            self.destroyed = True
            self.root.destroy()
        elif self.restart:
            self.restart = False
            self.start_session(self.pending_settings)
        else:
            self.status_var.set('摄像头已断开。')

    def poll(self):
        if self.destroyed:
            return
        while True:
            try:
                self.on_event(*self.events.get_nowait())
                if self.destroyed:
                    return
            except queue.Empty:
                break
        try:
            self.last_frame = self.frames.get_nowait()
            width = max(self.preview.winfo_width(), 100)
            height = max(self.preview.winfo_height(), 100)
            frame = self.last_frame
            scale = min(width / frame.shape[1], height / frame.shape[0])
            resized = cv2.resize(frame, (max(1, int(frame.shape[1] * scale)), max(1, int(frame.shape[0] * scale))))
            photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)))
            self.last_image = photo
            self.preview.configure(image=photo, text='')
        except queue.Empty:
            pass
        if not self.destroyed:
            self.root.after(40, self.poll)

    def close(self):
        self.closing = True
        self.restart = False
        if self.session is not None:
            self.disconnect()
            self.status_var.set('正在关闭，录像会在校验完成后释放。若驱动卡住需手动结束进程。')
        else:
            self.destroyed = True
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gpu', action='store_true', help='Preselect PyAV D3D11VA / H264 / 4K60 without changing saved settings')
    args = parser.parse_args()
    (ROOT / 'logs').mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / 'logs' / 'camera.log', encoding='utf-8')])
    root = tk.Tk()
    app = CameraPanel(root)
    if args.gpu:
        app.decoder_var.set('PyAV / D3D11VA 显卡解码')
        app.choose_decoder()
        app.vars['width'].set('3840')
        app.vars['height'].set('2160')
        app.vars['fps'].set('60')
        app.mode_var.set('3840 × 2160')
    root.mainloop()


if __name__ == '__main__':
    main()
