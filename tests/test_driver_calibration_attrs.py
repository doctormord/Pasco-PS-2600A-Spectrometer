"""Driver bindings to the calibration base interface (no hardware)."""
from __future__ import annotations

import re
import time

import numpy as np
import pytest

from app_config import Config
from calibration_utils import demo_lamp_irradiance


def _noop(*a, **k):
    pass


def _classes():
    out = []
    from _device_demo import DemoSpectrometer
    out.append((DemoSpectrometer, 65535.0))
    for mod, name, fs in (("_device_ocean", "OceanHDX", 65535.0),
                          ("_device_pasco", "PascoPS2600A", 4095.0),
                          ("_device_lr2t", "LasertrackLR2T", 65535.0)):
        try:
            m = __import__(mod)
            out.append((getattr(m, name), fs))
        except Exception:
            pass
    return out


@pytest.mark.parametrize("cls,fs", _classes())
def test_base_attrs(cls, fs):
    assert cls.ADC_FULL_SCALE == fs
    dev = cls(on_frame=_noop)
    assert dev.adc_full_scale == fs
    sat = dev.adc_saturation_level
    assert isinstance(sat, float) and 0 < sat <= fs
    assert re.fullmatch(r"[0-9a-f]{16}", dev.wl_fingerprint())


def _demo(**cfg):
    from _device_demo import DemoSpectrometer
    for k, v in cfg.items():
        Config._data[k] = v
    d = DemoSpectrometer(on_frame=_noop)
    d.connect("Demo Spectrometer")
    return d


def test_demo_serial_and_default():
    d = _demo()
    assert d.serial == Config.get("demo_serial", "DEMO-0001")
    d = _demo(demo_serial="XYZ-9")
    assert d.serial == "XYZ-9"


def test_demo_saturation_level():
    from _device_demo import DEMO_ADC_MAX
    d = _demo()
    assert d.adc_saturation_level == min(d._knee_counts, 0.98 * DEMO_ADC_MAX)


def test_demo_shutter_dark():
    d = _demo()
    assert d.SUPPORTS_SHUTTER and d.set_shutter(True) is True
    t = 100_000
    frames = np.array([d._expose(t) for _ in range(40)])
    expect = d._dark_bias + d._dark_current * t / 1000.0
    assert abs(frames.mean() - expect) < 3.0
    d.set_shutter(False)
    assert d._expose(t).max() > expect + 1000


def test_demo_sensitivity_none_unchanged():
    from _device_demo import _resolve_spectrum
    d = _demo(demo_spectrum="led_white")
    assert np.array_equal(d._shape, _resolve_spectrum("led_white", d._wl))


def test_demo_synthetic_cal_lamp():
    from _device_demo import demo_true_sensitivity
    d = _demo(demo_spectrum="cal_lamp", demo_sensitivity="synthetic")
    wl = d.wavelength_array
    s = demo_true_sensitivity(wl)
    assert abs(s.max() - 1.0) < 1e-12
    ref = demo_lamp_irradiance(wl) * s
    ref = ref / ref.max()
    assert np.max(np.abs(d._shape - ref) / ref.clip(1e-12)) < 1e-9


def test_demo_linear_in_t():
    d = _demo(demo_spectrum="flat")
    d._read_noise = 0.0
    d._shot_noise = 0.0
    d._knee_counts = 1.0 * 65535
    def net(t):
        dark = d._dark_bias + d._dark_current * t / 1000.0
        return d._expose(t) - dark
    a, b = net(50_000), net(100_000)
    m = a > 100
    assert np.max(np.abs(b[m] / a[m] - 2.0)) < 1e-3


def test_demo_frame_tap():
    d = _demo()
    got = []
    d.add_frame_tap(lambda *a: got.append(a))
    d.start()
    t0 = time.time()
    while not got and time.time() - t0 < 5:
        time.sleep(0.05)
    d.stop()
    assert got
    pixels, dark, t_us, kind, ratio = got[0]
    assert len(pixels) == d.PIXEL_COUNT and t_us > 0 and ratio == 1.0
