"""UVC control helpers operating on an already-open capture object."""
import math

import cv2


_CONTROL_DEFINITIONS = [
    ('brightness', '亮度', 'CAP_PROP_BRIGHTNESS', '调整画面亮度。'),
    ('contrast', '对比度', 'CAP_PROP_CONTRAST', '调整画面对比度。'),
    ('saturation', '饱和度', 'CAP_PROP_SATURATION', '调整画面色彩饱和度。'),
    ('hue', '色调', 'CAP_PROP_HUE', '调整画面色调。'),
    ('gain', '增益', 'CAP_PROP_GAIN', '调整图像增益。'),
    ('sharpness', '锐度', 'CAP_PROP_SHARPNESS', '调整画面锐度。'),
    ('gamma', '伽马', 'CAP_PROP_GAMMA', '调整画面伽马。'),
    ('exposure', '曝光', 'CAP_PROP_EXPOSURE', '调整曝光值，具体含义取决于驱动。'),
    ('auto_exposure', '自动曝光', 'CAP_PROP_AUTO_EXPOSURE', '使用驱动原始数值；先读取当前值再调整，数值含义取决于驱动和后端。'),
    ('white_balance', '白平衡', 'CAP_PROP_WB_TEMPERATURE', '调整白平衡色温，具体行为取决于驱动。'),
    ('auto_wb', '自动白平衡', 'CAP_PROP_AUTO_WB', '调整自动白平衡状态，具体数值取决于驱动。'),
    ('focus', '对焦', 'CAP_PROP_FOCUS', '调整焦点，具体行为取决于驱动。'),
    ('autofocus', '自动对焦', 'CAP_PROP_AUTOFOCUS', '调整自动对焦状态，具体数值取决于驱动。'),
    ('zoom', '缩放', 'CAP_PROP_ZOOM', '调整缩放。'),
    ('backlight', '背光补偿', 'CAP_PROP_BACKLIGHT', '调整背光补偿。'),
]
CONTROL_SPECS = [
    {'key': key, 'label': label, 'prop': getattr(cv2, prop), 'hint': hint}
    for key, label, prop, hint in _CONTROL_DEFINITIONS if hasattr(cv2, prop)
]
_SPECS_BY_KEY = {spec['key']: spec for spec in CONTROL_SPECS}


def _read(cap, prop):
    try:
        value = float(cap.get(prop))
        return value if math.isfinite(value) else None
    except (Exception,):
        return None


def read_controls(cap):
    """Read all known properties; unavailable or non-finite values become None."""
    return {spec['key']: _read(cap, spec['prop']) for spec in CONTROL_SPECS}


def apply_controls(cap, values: dict):
    """Apply only requested controls and report driver acceptance and readback.

    Invalid inputs that cannot be converted to float have requested=None. A driver
    accepting a value does not imply that it actually applied the requested value.
    """
    results = {}
    for key, raw in values.items():
        try:
            requested = float(raw)
        except (TypeError, ValueError, OverflowError):
            requested = None
        result = {'requested': requested, 'reported': None, 'accepted': False, 'matched': None}
        results[key] = result
        if key not in _SPECS_BY_KEY:
            result['error'] = '未知控制参数'
            continue
        if requested is None or not math.isfinite(requested):
            result['error'] = '参数值必须是有限数字'
            continue
        prop = _SPECS_BY_KEY[key]['prop']
        try:
            result['accepted'] = bool(cap.set(prop, requested))
            if not result['accepted']:
                result['error'] = '设备或驱动拒绝设置'
        except Exception as exc:
            result['error'] = f'设置失败: {exc}'
        result['reported'] = _read(cap, prop)
        if result['reported'] is not None:
            result['matched'] = math.isclose(requested, result['reported'], rel_tol=1e-5, abs_tol=1e-5)
    return results
