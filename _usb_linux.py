"""
libusb-based USB backend for non-Windows hosts (Linux, macOS, Raspberry Pi).

Mirrors the public function names used by `spectrometer_core.py` so the rest of
the codebase doesn't need to know which backend is active.

Requires:
    pip install pyusb
And on Linux, install the udev rule (udev/99-pasco-ps2600a.rules) so the
device can be opened without sudo.
"""
from __future__ import annotations
import threading
import time
import numpy as np

import usb.core
import usb.util
import usb.backend.libusb1 as _libusb1

def _find_backend():
    """
    Try to load libusb-1.0 explicitly from common macOS Homebrew locations
    before falling back to pyusb's automatic search.
    Returns a backend object or None (pyusb will then auto-detect).
    """
    import platform, ctypes.util, os
    if platform.system() != "Darwin":
        return None
    candidates = [
        # Apple Silicon Homebrew
        "/opt/homebrew/lib/libusb-1.0.dylib",
        "/opt/homebrew/lib/libusb-1.0.0.dylib",
        # Intel Homebrew
        "/usr/local/lib/libusb-1.0.dylib",
        "/usr/local/lib/libusb-1.0.0.dylib",
        # MacPorts
        "/opt/local/lib/libusb-1.0.dylib",
    ]
    for path in candidates:
        if os.path.exists(path):
            be = _libusb1.get_backend(find_library=lambda x: path)
            if be is not None:
                return be
    return None

_BACKEND = _find_backend()


# ── Device identification (matches WinUSB constants) ──
TARGET_VID = 0x0945
TARGET_PID = 0x0002

USB_ENDPOINT_BULK_IN     = 0x82
USB_REQ_TYPE_VENDOR_OUT  = 0x40
USB_REQ_TYPE_VENDOR_IN   = 0xC0

BUFFER_SIZE_SPECTRUM_BYTES = 7360
BUFFER_SIZE_DRAIN_BYTES    = 28


# Module-level handle (mirrors `global_usb_handle` from the WinUSB code).
_device: "usb.core.Device | None" = None
_lock = threading.Lock()


def _path_for(dev) -> str:
    """Synthetic device-path string for the GUI device picker."""
    return f"libusb:bus{dev.bus:03d}-addr{dev.address:03d}-vid_0945-pid_0002"


def scan_usb_devices() -> list[str]:
    """Returns the list of attached PS-2600A device paths (may be empty).

    Falls back gracefully if the device is already claimed and libusb raises
    a USBError (common on macOS when the acquisition thread is active).
    In that case the current device path is returned so the frontend can
    still show it in the device picker.
    """
    paths: list[str] = []
    try:
        found = usb.core.find(
            find_all=True, idVendor=TARGET_VID, idProduct=TARGET_PID,
            backend=_BACKEND
        )
        for dev in (found or []):
            try:
                paths.append(_path_for(dev))
            except Exception:
                # Descriptor access can fail on macOS if the device is busy.
                # Construct a fallback path from the already-open handle.
                if _device is not None and _device is dev:
                    try:
                        paths.append(_path_for(_device))
                    except Exception:
                        pass
    except usb.core.NoBackendError:
        raise RuntimeError(
            "libusb not found.\n\n"
            "On macOS:  brew install libusb\n"
            "On Linux:  sudo apt install libusb-1.0-0  (or equivalent)\n\n"
            "Then restart the application."
        )
    except Exception:
        # Any other USBError (e.g. device busy while acquisition thread runs).
        # If we already have an open handle, return its synthetic path.
        if _device is not None and not paths:
            try:
                paths.append(_path_for(_device))
            except Exception:
                pass
    return paths


def connect_usb_device(device_path: str) -> None:
    """Open and claim the device referenced by `device_path`. Raises on error."""
    global _device
    free_usb_resources()

    bus = addr = None
    if "bus" in device_path:
        try:
            bus  = int(device_path.split("bus")[1].split("-")[0])
            addr = int(device_path.split("addr")[1].split("-")[0])
        except Exception:
            pass

    target = None
    for dev in usb.core.find(find_all=True, idVendor=TARGET_VID, idProduct=TARGET_PID,
                              backend=_BACKEND):
        if bus is None or (dev.bus == bus and dev.address == addr):
            target = dev
            break

    if target is None:
        raise Exception(
            "PS-2600A not found on USB bus.\n"
            "Plug the spectrometer in and run Scan USB again."
        )

    # Some Linux desktops auto-bind a kernel driver — detach it so we can talk.
    try:
        if target.is_kernel_driver_active(0):
            target.detach_kernel_driver(0)
    except (usb.core.USBError, NotImplementedError):
        pass

    try:
        target.set_configuration()
    except usb.core.USBError as e:
        raise Exception(
            f"Failed to open USB device: {e}\n"
            f"On Linux: install udev/99-pasco-ps2600a.rules and re-plug "
            f"the spectrometer, OR run with sudo."
        )

    _device = target


def usb_control_transfer_out(request_code: int, value: int = 0, index: int = 0) -> bool:
    if _device is None:
        return False
    with _lock:
        try:
            _device.ctrl_transfer(
                USB_REQ_TYPE_VENDOR_OUT, request_code, value, index, b"", timeout=2000)
            return True
        except usb.core.USBError:
            return False


def usb_control_transfer_in(request_code: int, response_length: int) -> bytes | None:
    if _device is None:
        return None
    with _lock:
        try:
            data = _device.ctrl_transfer(
                USB_REQ_TYPE_VENDOR_IN, request_code, 0, 0, response_length, timeout=2000)
            return bytes(data)
        except usb.core.USBError:
            return None


def usb_drain_trailing_bytes() -> None:
    if _device is None:
        return
    try:
        _device.read(USB_ENDPOINT_BULK_IN, BUFFER_SIZE_DRAIN_BYTES, timeout=500)
    except usb.core.USBError:
        pass


def usb_bulk_read_spectrum() -> bytes | None:
    """Reads one spectrum payload. Mirrors WinUsb_ReadPipe for 7360 bytes."""
    if _device is None:
        return None
    try:
        data = _device.read(USB_ENDPOINT_BULK_IN, BUFFER_SIZE_SPECTRUM_BYTES, timeout=5000)
        if len(data) >= BUFFER_SIZE_SPECTRUM_BYTES:
            return bytes(data)
    except usb.core.USBError:
        pass
    return None


def free_usb_resources() -> None:
    global _device
    if _device is not None:
        try:
            usb.util.dispose_resources(_device)
        except Exception:
            pass
    _device = None

# Aliases for the renamed functions used in _device_pasco.py
usb_control_out = usb_control_transfer_out
usb_control_in  = usb_control_transfer_in
usb_bulk_read   = usb_bulk_read_spectrum
usb_drain       = usb_drain_trailing_bytes
