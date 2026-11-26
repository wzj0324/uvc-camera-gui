# UVC Camera GUI

Windows USB 摄像头 Python 图形界面程序，支持 OpenCV 采集和 PyAV / D3D11VA 显卡解码。直接运行 `camera_gui.py` 打开中文界面。

## 功能

- 实时预览、分辨率与帧率配置，按 USB VID/PID 定位设备。
- 采集格式：MJPEG、YUY2、H.264，取决于设备和采集后端。
- 解码模式：OpenCV 兼容、PyAV CPU、PyAV D3D11VA。显卡模式要求 H.264，失败明确报错，禁止静默软件回退。
- OpenCV 模式可读取和设置亮度、对比度、饱和度、曝光、白平衡、对焦等设备参数，并显示驱动接受状态和读回值。
- PNG / JPEG 拍照；MJPG/AVI、XVID/AVI、MPEG-4/MP4 录像；结束录像后逐帧解码校验。
- 保存配置、导出当前参数、检查 Windows USB 实际协商速度。

## 环境与安装

支持 Windows，建议 Python 3.12，需包含 Tkinter。PyAV 采集模式额外需要 FFmpeg 可执行文件位于 PATH。Python 的 av 包不会自动提供 ffmpeg.exe。

```powershell
git clone https://github.com/wzj0324/uvc-camera-gui.git
cd uvc-camera-gui
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe camera_gui.py
```

也可以下载 Releases 中的源码 ZIP，解压后从创建虚拟环境一步开始。FFmpeg 下载入口：[FFmpeg 官方下载页面](https://ffmpeg.org/download.html)。

在已安装依赖的环境可直接运行 `python camera_gui.py`。目录已有 `.venv` 时，直接启动主程序会自动使用该环境，不需要批处理。

## 使用

默认预选 VID `32E4` / PID `3415`、3840×2160 /60FPS、H.264 和 PyAV D3D11VA。可在界面修改 VID/PID，然后点击「连接摄像头」。这是一组请求预设，不能保证其他设备支持；不支持时请选择 OpenCV 兼容模式及合适的分辨率/帧率。

选择「应用采集设置」重新连接视频流。先停止录像并等待校验，再修改采集设置。采集格式控制 USB 输入，录像编码控制输出文件，两者独立。

PyAV 模式暂不支持 UVC 曝光/亮度等控制，相关按钮禁用；需要调整时切换到 OpenCV 兼容模式。参数使用驱动原始单位，不伪造最小值/最大值；读值为0不代表支持，驱动接受请求也不代表与请求完全一致。

拍照、录像、日志和诊断均保存在本地。录像无音频。停止长录像后的解码校验需要等待。

## 显卡解码路径与边界

```
摄像头 H.264
  → FFmpeg DirectShow（-c:v copy，仅转发压缩流）
  → PyAV / D3D11VA 解码（allow_software_fallback=False）
  → NV12 回读到系统内存 → BGR 图像 → 界面 / 拍照 / 录像
```

只有视频流硬件上下文启用并成功解码帧，才报告硬解。当前路径仍包含显存回读和 CPU 颜色转换，界面显示与录像编码仍使用 CPU，不是全程 GPU 零拷贝。界面预览约25FPS刷新，后台取帧速度单独显示。

开发设备 AMD Radeon 880M / Windows 环境曾对4K H.264摄像头测得约58～61FPS读取速度，包含取流、硬解、内存传输及BGR转换；这是特定环境的历史验证，不代表所有电脑、界面显示或录像重编码均达到4K60。原生管道/驱动读取仍可能阻塞，目前不承诺硬超时。

## 命令行

```powershell
.\.venv\Scripts\python.exe camera.py --list
.\.venv\Scripts\python.exe camera.py --test
.\.venv\Scripts\python.exe camera.py --snapshot
.\.venv\Scripts\python.exe camera.py --record-seconds 5 --record-codec mp4v
.\.venv\Scripts\python.exe camera.py --decoder pyav-d3d11va --format H264 --backend dshow --width 3840 --height 2160 --fps 60
.\.venv\Scripts\python.exe usb_link.py
```

`usb_link.py` 目前查询指定 VID/PID 32E4/3415，使用只读 USB Hub IOCTL，并核对返回设备身份；其他设备需修改脚本中 VID/PID 与匹配表达式。USB3控制器名称、bcdUSB或4K60模式本身不能证明当前连接为USB3，应检查实际SuperSpeed运行标志。诊断输出可能包含本机设备实例与路径，请自行决定是否分享。

## 测试

下列测试默认不打开摄像头；包含合成帧录像编码/解码。GPU样本测试默认跳过。

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_camera test_controls test_recording test_pyav_capture
```

显式执行真实设备验证（会打开摄像头并保存本地测试媒体）：

```powershell
.\.venv\Scripts\python.exe verify_panel_hardware.py --run
.\.venv\Scripts\python.exe verify_panel_hardware.py --run --pyav
.\.venv\Scripts\python.exe verify_pyav_hardware.py --camera --bgr --warmup 120 --frames 360
```

离线显卡测试需准备 `captures/diagnostics/h264_gpu_test_pattern.mp4`（1920×1080 H.264），再设置 `PYAV_TEST_D3D11VA=1` 执行测试。本仓库不包含采集画面或历史测试媒体。

## 文件

| 文件 | 用途 |
| --- | --- |
| camera_gui.py | 完整中文图形界面主程序 |
| camera.py | OpenCV采集、录像与命令行 |
| pyav_capture.py | FFmpeg流复制与PyAV解码适配器 |
| camera_controls.py | 图像参数读取、设置、读回反馈 |
| usb_link.py | 只读USB实际协商速度查询 |
| control_panel.py | 兼容旧Python入口 |
| test_*.py | 自动测试 |
| verify_*.py / probe_capture_formats.py | 显式硬件验证与格式协商诊断 |
| requirements.txt | 已验证依赖版本 |

## 原始资料与依赖

- [OpenCV](https://github.com/opencv/opencv)
- [PyAV](https://github.com/PyAV-Org/PyAV)
- [cv2-enumerate-cameras](https://github.com/lukehugh/cv2_enumerate_cameras)
- [Pillow](https://github.com/python-pillow/Pillow)
- [Microsoft UVC 驱动说明](https://learn.microsoft.com/en-us/windows-hardware/drivers/stream/usb-video-class-driver-overview)
- [USB-IF UVC 1.5 文档集](https://www.usb.org/document-library/video-class-v15-document-set)

UVC参考文档版本不等于摄像头实际协议版本。第三方依赖和规范遵循各自许可证；仓库不打包驱动、FFmpeg、虚拟环境或官方规范压缩包。
