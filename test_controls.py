"""Control helpers tested with a fake capture; never opens a camera."""
import math
import unittest

import camera_controls as controls


class FakeCapture:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.rejected = set()
        self.errors = set()
        self.override = {}

    def set(self, prop, value):
        self.calls.append((prop, value))
        if prop in self.errors:
            raise RuntimeError('driver error')
        if prop in self.rejected:
            return False
        self.values[prop] = self.override.get(prop, value)
        return True

    def get(self, prop):
        value = self.values.get(prop, 0.0)
        if isinstance(value, Exception):
            raise value
        return value


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.cap = FakeCapture()
        self.specs = {item['key']: item for item in controls.CONTROL_SPECS}

    def test_specs_and_raw_auto_exposure_hint(self):
        self.assertIn('brightness', self.specs)
        self.assertEqual(len(self.specs), len(controls.CONTROL_SPECS))
        if 'auto_exposure' in self.specs:
            self.assertIn('驱动', self.specs['auto_exposure']['hint'])
            self.assertIn('先读取', self.specs['auto_exposure']['hint'])

    def test_read_invalid_and_zero(self):
        brightness = self.specs['brightness']['prop']
        contrast = self.specs['contrast']['prop']
        self.cap.values[brightness] = math.nan
        self.cap.values[contrast] = RuntimeError('unavailable')
        values = controls.read_controls(self.cap)
        self.assertIsNone(values['brightness'])
        self.assertIsNone(values['contrast'])
        self.assertEqual(values['saturation'], 0.0)

    def test_invalid_and_unknown_do_not_write(self):
        for value in (float('nan'), float('inf'), '-inf', 'invalid', None):
            result = controls.apply_controls(self.cap, {'brightness': value})['brightness']
            self.assertFalse(result['accepted'])
            self.assertIn('error', result)
        result = controls.apply_controls(self.cap, {'unknown': 2})['unknown']
        self.assertFalse(result['accepted'])
        self.assertIn('error', result)
        self.assertEqual(self.cap.calls, [])

    def test_partial_failure_and_accepted_differs_from_matched(self):
        brightness = self.specs['brightness']['prop']
        contrast = self.specs['contrast']['prop']
        saturation = self.specs['saturation']['prop']
        self.cap.override[brightness] = 3.0
        self.cap.rejected.add(contrast)
        self.cap.errors.add(saturation)
        result = controls.apply_controls(self.cap, {'brightness': 5, 'contrast': 4, 'saturation': 2, 'hue': 1})
        self.assertTrue(result['brightness']['accepted'])
        self.assertFalse(result['brightness']['matched'])
        self.assertFalse(result['contrast']['accepted'])
        self.assertIn('error', result['contrast'])
        self.assertFalse(result['saturation']['accepted'])
        self.assertIn('driver error', result['saturation']['error'])
        self.assertTrue(result['hue']['accepted'])
        self.assertTrue(result['hue']['matched'])
        self.assertEqual(len(self.cap.calls), 4)

    def test_unavailable_readback_and_tolerance(self):
        prop = self.specs['brightness']['prop']
        self.cap.override[prop] = float('nan')
        result = controls.apply_controls(self.cap, {'brightness': 1})['brightness']
        self.assertTrue(result['accepted'])
        self.assertIsNone(result['matched'])
        self.cap.override[prop] = 1.000001
        self.assertTrue(controls.apply_controls(self.cap, {'brightness': 1})['brightness']['matched'])


if __name__ == '__main__':
    unittest.main()
