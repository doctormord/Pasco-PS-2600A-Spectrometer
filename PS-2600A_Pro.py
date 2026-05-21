import sys
import os
import ctypes
from ctypes import wintypes
import struct
import time
import numpy as np
import csv
from datetime import datetime
from collections import deque

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QPushButton, QLabel,
                             QSpinBox, QDoubleSpinBox, QMessageBox, QComboBox,
                             QTabWidget, QSizePolicy, QToolTip)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QRectF, QTimer, QObject, QEvent
from PyQt6.QtGui import QTransform
import pyqtgraph as pg


# =========================================================================================
# ================================== CONFIGURATION BLOCK ==================================
# =========================================================================================

# --- 1. HARDWARE IDENTIFICATION ---
DEVICE_GUID_STRING = "{DEE824EF-729B-4A0E-9C14-B7117D33A817}"
TARGET_VID_STRING = "vid_0945"
TARGET_PID_STRING = "pid_0002"

# --- 2. WINUSB & OS CONSTANTS ---
DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 0x00000003
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = -1

# --- 3. USB PROTOCOL & COMMANDS ---
USB_REQ_TYPE_VENDOR_OUT = 0x40
USB_REQ_TYPE_VENDOR_IN = 0xC0
USB_ENDPOINT_BULK_IN = 0x82

CMD_INIT_DEVICE = 1
CMD_SET_EXPOSURE = 2
CMD_TRIGGER_READ = 9
CMD_POLL_STATUS = 131

BUFFER_SIZE_SPECTRUM_BYTES = 7360
BUFFER_SIZE_DRAIN_BYTES = 28
PAYLOAD_HEADER_SIZE_BYTES = 64
PIXEL_COUNT = 3648
BYTES_PER_PIXEL = 2
POLL_READY_VALUE = 0

# --- 4. SPECTROMETER CALIBRATION & LIMITS ---
# Custom Cadmium Calibration (Fit based on 467.8, 480.0, 508.6, 643.8 nm + 2nd Orders)
WAVELENGTH_COEFFS = [75.45192816, 0.345838363, -2.33680103e-05, 1.22593755e-09]
ADC_SATURATION_THRESHOLD = 3800

# --- 5. ALGORITHMS (EXPOSURE, DARK CURRENT & PEAKS) ---
DEFAULT_DARK_BIAS_ADC = 61.57
DEFAULT_DARK_RATE_ADC_PER_SEC = 40.94

OB_CORRECTION_SLOPE = 1.008180
OB_CORRECTION_OFFSET = -0.6377
OB_PIXEL_BYTE_START = 4          
OB_PIXEL_BYTE_END = 64           
OB_PIXEL_COUNT = (OB_PIXEL_BYTE_END - OB_PIXEL_BYTE_START) // 2

DARK_MODE_OPTICAL_BLACK = "optical_black"
DARK_MODE_PARAMETRIC = "parametric"
DEFAULT_DARK_MODE = DARK_MODE_OPTICAL_BLACK

OB_TEMP_REFERENCE_ADC = 61.5     

AUTO_EXP_TARGET_ADC = 3400
AUTO_EXP_DEADZONE_ADC = 100
AUTO_EXP_MIN_RATIO = 0.2
AUTO_EXP_MAX_RATIO = 5.0
AUTO_EXP_EMERGENCY_DROP_RATIO = 0.2

MIN_INTEGRATION_TIME_US = 1_000         
MAX_INTEGRATION_TIME_US = 2_500_000     
START_INTEGRATION_TIME_US = 20_000      
PEAK_MIN_DISTANCE_PIXELS = 40          
MIN_PEAK_HEIGHT_ADC = 15.0  

# --- 6. ADVANCED FEATURES (HEATMAP & REFERENCES) ---
HEATMAP_HISTORY_SIZE = 100
REFERENCE_LIBRARY_FILENAME = "reference_spectra.csv"

# --- 7. GUI PARAMETERS ---
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 850
DEFAULT_X_MIN_NM = 320
DEFAULT_X_MAX_NM = 1050
DEFAULT_Y_MAX_ADC = 4000
Y_AXIS_BOTTOM_MARGIN_ADC = -20  

POLL_INTERVAL_SEC = 0.002
THREAD_PAUSE_SLEEP_SEC = 0.05
# =========================================================================================

# ============================================================
# THEME STYLESHEETS
# ============================================================
LIGHT_STYLESHEET = ""   # Fusion default

DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
}
QLabel { color: #cdd6f4; }
QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 4px 8px;
}
QPushButton:hover { background-color: #45475a; }
QPushButton:checked { background-color: #a6e3a1; color: #1e1e2e; font-weight: bold; }
QPushButton:disabled { background-color: #181825; color: #585b70; border-color: #313244; }
QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 3px;
    padding: 2px;
}
QSpinBox:disabled, QDoubleSpinBox:disabled { color: #585b70; }
QComboBox QAbstractItemView {
    background-color: #313244;
    color: #cdd6f4;
    selection-background-color: #45475a;
}
QTabWidget::pane { border: 1px solid #45475a; }
QTabBar::tab {
    background-color: #313244;
    color: #a6adc8;
    padding: 6px 16px;
    border: 1px solid #45475a;
}
QTabBar::tab:selected { background-color: #1e1e2e; color: #cdd6f4; }
QToolTip {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #89b4fa;
    padding: 6px;
    font-size: 12px;
}
QMessageBox { background-color: #1e1e2e; color: #cdd6f4; }
"""

# Calculate absolute wavelength array for the hardware pixels
wavelength_array = np.array([
    WAVELENGTH_COEFFS[0] + 
    WAVELENGTH_COEFFS[1] * i + 
    WAVELENGTH_COEFFS[2] * (i**2) + 
    WAVELENGTH_COEFFS[3] * (i**3) 
    for i in range(PIXEL_COUNT)
])


# ============================================================
# HELPER: AUTO-GENERATE DUMMY REFERENCES
# ============================================================
def ensure_reference_library_exists():
    """ 
    Creates a dummy CSV with generic wavelength steps (300 to 1100 nm).
    This file is completely decoupled from the hardware pixels and is 
    never overwritten if it already exists, protecting user modifications.
    """
    if os.path.exists(REFERENCE_LIBRARY_FILENAME):
        return

    print(f"Generating generic reference library: {REFERENCE_LIBRARY_FILENAME}")

    W = 0.5   

    spectral_defs = {
        "Hydrogen (H)": [
            (410.2, 1500, W), (434.0, 2000, W), (486.1, 3000, W), (656.3, 3500, W)
        ],
        "Mercury (Hg)": [
            (365.0, 800, W), (404.7, 1500, W), (407.8, 400, W),
            (435.8, 3000, W), (546.1, 3500, W), (576.9, 2000, W), (579.1, 1800, W)
        ],
        "Sodium (Na)": [
            (568.8, 200, W), (589.0, 3500, W), (589.6, 3000, W),
            (615.4, 300, W), (616.1, 250, W)
        ],
        "Neon (Ne)": [
            (585.2, 800, W), (594.5, 600, W), (597.6, 700, W), (607.4, 900, W),
            (614.3, 500, W), (616.4, 600, W), (621.7, 800, W), (626.6, 700, W),
            (630.5, 1200, W), (633.4, 900, W), (638.3, 1500, W), (640.2, 2000, W),
            (650.6, 3000, W), (659.9, 1000, W), (667.8, 600, W), (671.7, 500, W),
            (692.9, 1200, W), (703.2, 2500, W), (717.4, 600, W), (724.5, 500, W),
            (743.9, 800, W)
        ],
        "Argon (Ar)": [
            (696.5, 1200, W), (706.7, 1000, W), (714.7, 600, W), (727.3, 800, W),
            (738.4, 2000, W), (750.4, 3000, W), (763.5, 3500, W), (772.4, 800, W),
            (794.8, 600, W), (800.6, 500, W), (811.5, 2500, W), (826.5, 600, W),
            (840.8, 1000, W), (842.5, 1200, W)
        ],
        "Helium (He)": [
            (388.9, 500, W), (396.5, 300, W), (447.1, 1500, W), (471.3, 800, W),
            (492.2, 1000, W), (501.6, 1200, W), (587.6, 3000, W),
            (667.8, 1000, W), (706.5, 800, W), (728.1, 500, W)
        ],
        "Krypton (Kr)": [
            (427.4, 500, W), (431.9, 600, W), (436.3, 700, W), (450.2, 400, W),
            (461.5, 500, W), (473.9, 800, W), (476.2, 600, W), (482.5, 700, W),
            (557.0, 600, W), (587.1, 800, W), (602.0, 500, W),
            (760.2, 1200, W), (769.5, 1800, W), (785.5, 600, W),
            (810.4, 900, W), (819.0, 1500, W), (829.8, 2000, W)
        ],
        "Xenon (Xe)": [
            (450.1, 600, W), (462.4, 700, W), (467.1, 500, W),
            (473.4, 800, W), (480.7, 600, W), (492.3, 500, W),
            (823.2, 2000, W), (828.0, 1500, W), (834.7, 1800, W), (880.0, 1200, W)
        ],
        "Cadmium (Cd)": [
            (346.6, 300, W), (361.1, 400, W), (467.8, 1500, W),
            (480.0, 2000, W), (508.6, 1200, W), (643.8, 3000, W)
        ],
        "Neon-Argon Mix": [
            (614.3, 400, W), (621.7, 600, W), (638.3, 1200, W), (640.2, 1500, W),
            (650.6, 2500, W), (692.9, 900, W), (703.2, 2000, W), (743.9, 600, W),
            (696.5, 800, W), (706.7, 700, W), (738.4, 1200, W),
            (750.4, 1800, W), (763.5, 2000, W), (811.5, 1500, W)
        ],
        "Fluorescent 2-band": [
            (365.0, 400, W), (404.7, 600, W), (435.8, 1500, W),
            (546.1, 1000, W), (576.9, 800, W),
            (452.0, 2000, 20.0), (612.0, 2500, 25.0)
        ],
        "Fluorescent 3-band": [
            (365.0, 300, W), (404.7, 500, W), (435.8, 1000, W),
            (546.1, 800, W), (576.9, 600, W),
            (453.0, 2500, 15.0), (542.0, 2000, 18.0), (611.0, 3000, 20.0)
        ],
        "Fluorescent 4-band": [
            (365.0, 300, W), (404.7, 500, W), (435.8, 1000, W),
            (546.1, 800, W), (576.9, 600, W),
            (405.0, 1200, 10.0), (453.0, 2000, 15.0),
            (542.0, 1800, 18.0), (611.0, 2800, 20.0)
        ],
        "LED Red":    [(638.0, 3500, 20.0)],
        "LED Orange": [(617.0, 3500, 18.0)],
        "LED Amber":  [(593.0, 3500, 15.0)],
        "LED Yellow": [(578.0, 3500, 14.0)],
        "LED Green":  [(520.0, 3500, 35.0)],
        "LED Cyan":   [(500.0, 3500, 20.0)],
        "LED Blue":   [(465.0, 3500, 25.0)],
        "LED Violet": [(405.0, 3500, 15.0)],
        "LED White Warm":    [(455.0, 1400, 25.0), (570.0, 3200, 120.0)],
        "LED White Neutral": [(450.0, 1200, 22.0), (545.0, 3000, 100.0)],
        "LED White Cool":    [(445.0, 1100, 20.0), (530.0, 2800,  85.0)],
    }

    headers = ["Wavelength_nm"] + list(spectral_defs.keys())
    
    # Generate generic wavelength axis from 300 to 1100 nm
    generic_waves = np.arange(300.0, 1100.5, 0.5)

    with open(REFERENCE_LIBRARY_FILENAME, 'w', newline='') as file:
        writer = csv.writer(file, delimiter=';')
        writer.writerow(headers)

        sigma_cache = {}
        for wave in generic_waves:
            row = [round(float(wave), 2)]
            for peaks in spectral_defs.values():
                val = 0.0
                for center, intensity, fwhm in peaks:
                    if fwhm not in sigma_cache:
                        sigma_cache[fwhm] = fwhm / (2.0 * np.sqrt(2.0 * np.log(2.0)))
                    sigma = sigma_cache[fwhm]
                    val += intensity * np.exp(-((wave - center) ** 2) / (2.0 * sigma ** 2))
                row.append(round(min(val, 3990.0), 1))
            writer.writerow(row)


# ============================================================
# CTYPES SETUP FOR WINUSB
# ============================================================
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winusb = ctypes.WinDLL("winusb", use_last_error=True)

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), 
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID), 
                ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]

class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 1024)]

class WINUSB_SETUP_PACKET(ctypes.Structure):
    _fields_ = [("RequestType", ctypes.c_ubyte), ("Request", ctypes.c_ubyte), 
                ("Value", ctypes.c_ushort), ("Index", ctypes.c_ushort), ("Length", ctypes.c_ushort)]

def convert_string_to_guid(guid_string):
    from uuid import UUID
    u = UUID(guid_string)
    data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
    return GUID(u.time_low, u.time_mid, u.time_hi_version, data4)

setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
winusb.WinUsb_Initialize.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
winusb.WinUsb_Initialize.restype = wintypes.BOOL
winusb.WinUsb_ControlTransfer.argtypes = [ctypes.c_void_p, WINUSB_SETUP_PACKET, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p]
winusb.WinUsb_ControlTransfer.restype = wintypes.BOOL
winusb.WinUsb_ReadPipe.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p]
winusb.WinUsb_ReadPipe.restype = wintypes.BOOL

global_usb_handle = None
global_device_handle = None

# ============================================================
# WINUSB DEVICE MANAGER FUNCTIONS
# ============================================================
def scan_usb_devices():
    found_devices = []
    guid = convert_string_to_guid(DEVICE_GUID_STRING)
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    
    index = 0
    while True:
        iface = SP_DEVICE_INTERFACE_DATA()
        iface.cbSize = ctypes.sizeof(iface)
        if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), index, ctypes.byref(iface)): 
            break
            
        required_size = wintypes.DWORD()
        setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), None, 0, ctypes.byref(required_size), None)
        
        buffer = ctypes.create_string_buffer(required_size.value)
        detail = ctypes.cast(buffer, ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W))
        detail.contents.cbSize = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        
        if setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(iface), detail, required_size, None, None):
            path = detail.contents.DevicePath
            if TARGET_VID_STRING in path.lower() and TARGET_PID_STRING in path.lower():
                found_devices.append(path)
        index += 1
    return found_devices

def connect_usb_device(device_path):
    global global_device_handle, global_usb_handle
    free_usb_resources()
    
    global_device_handle = kernel32.CreateFileW(
        device_path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, 
        None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None
    )
    if global_device_handle == wintypes.HANDLE(INVALID_HANDLE_VALUE).value: 
        raise Exception(f"Access Denied or Device in Use. Error Code: {ctypes.get_last_error()}")

    global_usb_handle = ctypes.c_void_p()
    if not winusb.WinUsb_Initialize(global_device_handle, ctypes.byref(global_usb_handle)):
        free_usb_resources()
        raise Exception("WinUsb Initialization Failed.")

def usb_control_transfer_out(request_code, value=0, index=0):
    if not global_usb_handle: return False
    setup_packet = WINUSB_SETUP_PACKET(USB_REQ_TYPE_VENDOR_OUT, request_code, value, index, 0)
    bytes_transferred = wintypes.ULONG()
    return winusb.WinUsb_ControlTransfer(global_usb_handle, setup_packet, None, 0, ctypes.byref(bytes_transferred), None)

def usb_control_transfer_in(request_code, response_length):
    if not global_usb_handle: return None
    setup_packet = WINUSB_SETUP_PACKET(USB_REQ_TYPE_VENDOR_IN, request_code, 0, 0, response_length)
    buffer = (ctypes.c_ubyte * response_length)()
    bytes_transferred = wintypes.ULONG()
    if winusb.WinUsb_ControlTransfer(global_usb_handle, setup_packet, buffer, response_length, ctypes.byref(bytes_transferred), None):
        return bytes(buffer)
    return None

def usb_drain_trailing_bytes():
    if not global_usb_handle: return
    buffer_trail = (ctypes.c_ubyte * BUFFER_SIZE_DRAIN_BYTES)()
    bytes_read = wintypes.ULONG()
    winusb.WinUsb_ReadPipe(global_usb_handle, USB_ENDPOINT_BULK_IN, buffer_trail, BUFFER_SIZE_DRAIN_BYTES, ctypes.byref(bytes_read), None)

def free_usb_resources():
    global global_usb_handle, global_device_handle
    if global_usb_handle:
        winusb.WinUsb_Free(global_usb_handle)
        global_usb_handle = None
    if global_device_handle:
        kernel32.CloseHandle(global_device_handle)
        global_device_handle = None


# ============================================================
# CONTEXT-SENSITIVE HELP (2-second delayed tooltip)
# ============================================================
class ContextHelp(QObject):
    """Installs a 2-second delayed tooltip on any QWidget."""
    def __init__(self, widget, text, delay_ms=2000):
        super().__init__(widget)
        self._widget = widget
        self._text = text
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._show)
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.Type.Enter:
            self._timer.start()
        elif t in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress):
            self._timer.stop()
            QToolTip.hideText()
        return False

    def _show(self):
        if self._widget.underMouse():
            pos = self._widget.mapToGlobal(self._widget.rect().bottomLeft())
            QToolTip.showText(pos, self._text, self._widget)


def add_help(widget, text, delay_ms=2000):
    """Attach a 2-second context tooltip to a widget. Returns the widget."""
    ContextHelp(widget, text, delay_ms)
    return widget


# ============================================================
# 🧵 BACKGROUND HARDWARE THREAD
# ============================================================
class SpectrometerHardwareThread(QThread):
    # pixel array, optical-black mean (ADC), integration time (us) for this frame
    signal_new_spectrum_data = pyqtSignal(np.ndarray, float, int)
    signal_auto_exposure_adjusted = pyqtSignal(float)
    signal_connection_lost = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.is_thread_running = True
        self.is_measurement_paused = False
        
        self.current_integration_time_us = START_INTEGRATION_TIME_US
        self.is_auto_exposure_active = False
        
        usb_control_transfer_out(CMD_INIT_DEVICE)
        self.apply_hardware_exposure_time(self.current_integration_time_us)

    def apply_hardware_exposure_time(self, microseconds):
        low_word = microseconds & 0xFFFF
        high_word = (microseconds >> 16) & 0xFFFF
        usb_control_transfer_out(CMD_SET_EXPOSURE, value=low_word, index=high_word)

    def run(self):
        while self.is_thread_running:
            if self.is_measurement_paused:
                time.sleep(THREAD_PAUSE_SLEEP_SEC)
                continue

            if not usb_control_transfer_out(CMD_TRIGGER_READ):
                self.signal_connection_lost.emit()
                break
            
            is_hardware_ready = False
            timeout_ms = self.current_integration_time_us / 1000.0
            max_polling_attempts = int((timeout_ms + 1000) / 2)
            
            for _ in range(max_polling_attempts):
                if not self.is_thread_running: return
                status_byte = usb_control_transfer_in(CMD_POLL_STATUS, response_length=1)
                if status_byte is None:
                    self.signal_connection_lost.emit()
                    return
                if status_byte[0] != POLL_READY_VALUE:
                    is_hardware_ready = True
                    break
                time.sleep(POLL_INTERVAL_SEC) 
                
            if not is_hardware_ready: continue

            buffer_bulk = (ctypes.c_ubyte * BUFFER_SIZE_SPECTRUM_BYTES)()
            bytes_read = wintypes.ULONG()
            read_success = winusb.WinUsb_ReadPipe(global_usb_handle, USB_ENDPOINT_BULK_IN, buffer_bulk, BUFFER_SIZE_SPECTRUM_BYTES, ctypes.byref(bytes_read), None)
            
            if read_success and bytes_read.value >= BUFFER_SIZE_SPECTRUM_BYTES:
                raw_data = bytes(buffer_bulk)
                usb_drain_trailing_bytes()

                ob_bytes = raw_data[OB_PIXEL_BYTE_START:OB_PIXEL_BYTE_END]
                ob_pixels = np.array(struct.unpack(f"<{OB_PIXEL_COUNT}H", ob_bytes), dtype=float)
                ob_mean = float(np.mean(ob_pixels))

                payload_size = PIXEL_COUNT * BYTES_PER_PIXEL
                spectrum_bytes = raw_data[PAYLOAD_HEADER_SIZE_BYTES : PAYLOAD_HEADER_SIZE_BYTES + payload_size]
                unpack_format = f"<{PIXEL_COUNT}H"
                pixel_array = np.array(struct.unpack(unpack_format, spectrum_bytes), dtype=float)
                
                if self.is_auto_exposure_active:
                    max_adc_value = np.max(pixel_array)
                    calculated_time_us = self.current_integration_time_us
                    
                    if max_adc_value >= ADC_SATURATION_THRESHOLD:
                        calculated_time_us = int(self.current_integration_time_us * AUTO_EXP_EMERGENCY_DROP_RATIO)
                    else:
                        if max_adc_value < 10: max_adc_value = 10 
                        if abs(max_adc_value - AUTO_EXP_TARGET_ADC) > AUTO_EXP_DEADZONE_ADC:
                            adjustment_ratio = AUTO_EXP_TARGET_ADC / max_adc_value
                            adjustment_ratio = max(AUTO_EXP_MIN_RATIO, min(AUTO_EXP_MAX_RATIO, adjustment_ratio)) 
                            calculated_time_us = int(self.current_integration_time_us * adjustment_ratio)
                            
                    calculated_time_us = max(MIN_INTEGRATION_TIME_US, min(MAX_INTEGRATION_TIME_US, calculated_time_us))
                    
                    if calculated_time_us != self.current_integration_time_us:
                        self.current_integration_time_us = calculated_time_us
                        self.apply_hardware_exposure_time(self.current_integration_time_us)
                        self.signal_auto_exposure_adjusted.emit(self.current_integration_time_us / 1000.0)

                self.signal_new_spectrum_data.emit(
                    pixel_array, ob_mean, self.current_integration_time_us
                )
            else:
                self.signal_connection_lost.emit()
                break

    def stop_thread(self):
        self.is_thread_running = False
        self.wait()


# ============================================================
# 🖥️ GUI / MAIN WINDOW
# ============================================================
class DashboardWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PASCO PS-2600A - Advanced Laboratory Dashboard")
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self.spectrum_history = deque(maxlen=1)
        self.current_averaged_pixels = np.zeros(PIXEL_COUNT)

        # State Flags
        self.is_dark_correction_enabled = False
        self.is_despeckle_enabled = False 
        self.is_auto_y_axis_enabled = True
        self.is_peak_finding_enabled = False
        self.is_measure_mode_enabled = False
        self.hardware_thread = None

        # Dark current / optical black state
        self.dark_correction_mode = DEFAULT_DARK_MODE
        self.latest_ob_mean = 0.0
        self.latest_integration_us = START_INTEGRATION_TIME_US

        # Theme
        self.is_dark_mode = False

        self.heatmap_buffer = np.zeros((PIXEL_COUNT, HEATMAP_HISTORY_SIZE))
        self.reference_library = {}
        
        ensure_reference_library_exists()
        self.load_reference_library()

        self.build_user_interface()
        self.refresh_device_list() 

    def load_reference_library(self):
        if not os.path.exists(REFERENCE_LIBRARY_FILENAME): return
        try:
            with open(REFERENCE_LIBRARY_FILENAME, 'r') as file:
                reader = csv.reader(file, delimiter=';')
                headers = next(reader)
                
                # Dynamic Interpolation Logic:
                # We read the arbitrary CSV wavelengths and map them to our highly specific hardware pixels
                csv_waves = []
                csv_columns = {col_name: [] for col_name in headers[1:]}
                
                for row in reader:
                    if not row or len(row) < 2: continue
                    try:
                        wave = float(row[0])
                        csv_waves.append(wave)
                        for col_idx, col_name in enumerate(headers[1:]):
                            if col_idx + 1 < len(row):
                                csv_columns[col_name].append(float(row[col_idx + 1]))
                            else:
                                csv_columns[col_name].append(0.0)
                    except ValueError:
                        continue
                
                # Interpolate the raw CSV columns to match the device's exact wavelength_array
                for col_name in headers[1:]:
                    mapped_intensities = np.interp(
                        wavelength_array, 
                        csv_waves, 
                        csv_columns[col_name], 
                        left=0.0, right=0.0
                    )
                    self.reference_library[col_name] = mapped_intensities
                    
        except Exception as e:
            print(f"Error loading reference library: {e}")

    def build_user_interface(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- DEVICE CONNECTION MANAGER ---
        connection_layout = QHBoxLayout()
        self.button_scan = QPushButton("🔄 Scan USB")
        self.button_scan.clicked.connect(self.refresh_device_list)
        connection_layout.addWidget(self.button_scan)

        self.combo_devices = QComboBox()
        self.combo_devices.setMinimumWidth(300)
        connection_layout.addWidget(self.combo_devices)

        self.button_connect = QPushButton("Connect")
        self.button_connect.setMinimumWidth(120)
        self.button_connect.clicked.connect(self.toggle_connection)
        connection_layout.addWidget(self.button_connect)

        self.label_status = QLabel("🔴 Disconnected")
        self.label_status.setStyleSheet("font-weight: bold; padding: 0px 15px;")
        connection_layout.addWidget(self.label_status)
        connection_layout.addStretch()

        self.button_theme = QPushButton("Dark Mode")
        self.button_theme.setCheckable(True)
        self.button_theme.setMinimumHeight(35)
        self.button_theme.setMinimumWidth(120)
        self.button_theme.clicked.connect(self.toggle_theme)
        connection_layout.addWidget(self.button_theme)

        self.button_exit = QPushButton("🚪 EXIT")
        self.button_exit.setStyleSheet("background-color: #ff4c4c; color: white; font-weight: bold; padding: 5px 15px;")
        self.button_exit.clicked.connect(self.close)
        connection_layout.addWidget(self.button_exit)
        main_layout.addLayout(connection_layout)

        # --- INFO BAR (Peaks & Cursors) ---
        top_layout = QHBoxLayout()
        self.label_measure_info = QLabel("")
        self.label_measure_info.setStyleSheet("font-size: 16px; font-weight: bold; color: #8A2BE2;") 
        top_layout.addWidget(self.label_measure_info)

        self.label_peaks_info = QLabel("")
        self.label_peaks_info.setStyleSheet("font-size: 16px; font-weight: bold; color: #D72800;")
        self.label_peaks_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(self.label_peaks_info, stretch=1)

        self.label_cursor_info = QLabel("No Data")
        self.label_cursor_info.setStyleSheet("font-size: 16px; font-weight: bold; color: #0078D7;")
        self.label_cursor_info.setAlignment(Qt.AlignmentFlag.AlignRight)
        top_layout.addWidget(self.label_cursor_info)
        main_layout.addLayout(top_layout)

        # --- TAB WIDGET (Scope vs Heatmap) ---
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs, stretch=1)
        
        pg.setConfigOptions(antialias=True)
        
        # TAB 1: SCOPE MODE
        self.plot_canvas = pg.PlotWidget()
        self.plot_canvas.setBackground('w')
        self.plot_canvas.showGrid(x=True, y=True, alpha=0.3)
        self.plot_canvas.setLabel('bottom', 'Wavelength', units='nm', color='k', bold=True)
        self.plot_canvas.setLabel('left', 'Intensity', units='ADC', color='k', bold=True)
        self.plot_canvas.getAxis('left').enableAutoSIPrefix(False)
        self.plot_canvas.getAxis('bottom').enableAutoSIPrefix(False)
        self.plot_canvas.setXRange(DEFAULT_X_MIN_NM, DEFAULT_X_MAX_NM, padding=0)
        self.plot_canvas.setYRange(Y_AXIS_BOTTOM_MARGIN_ADC, 100, padding=0)
        
        self.spectrum_curve = self.plot_canvas.plot(pen=pg.mkPen('b', width=2))
        self.reference_curve = self.plot_canvas.plot(pen=pg.mkPen('g', width=2, style=Qt.PenStyle.DashLine))
        self.reference_curve.setVisible(False)
        self.scatter_peaks = pg.ScatterPlotItem(size=15, pen=pg.mkPen('r', width=3), symbol='x')
        self.plot_canvas.addItem(self.scatter_peaks)
        
        self.cursor_vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('k', style=Qt.PenStyle.DashLine))
        self.cursor_hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('k', style=Qt.PenStyle.DashLine))
        self.plot_canvas.addItem(self.cursor_vline, ignoreBounds=True)
        self.plot_canvas.addItem(self.cursor_hline, ignoreBounds=True)
        
        self.measure_line_a = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen('m', width=2)) 
        self.measure_line_b = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen('c', width=2)) 
        self.measure_line_a.setPos(500)
        self.measure_line_b.setPos(600)
        self.measure_line_a.setVisible(False)
        self.measure_line_b.setVisible(False)
        self.plot_canvas.addItem(self.measure_line_a, ignoreBounds=True)
        self.plot_canvas.addItem(self.measure_line_b, ignoreBounds=True)
        
        self.measure_line_a.sigPositionChanged.connect(self.update_measurement_display)
        self.measure_line_b.sigPositionChanged.connect(self.update_measurement_display)

        self.mouse_proxy = pg.SignalProxy(self.plot_canvas.scene().sigMouseMoved, rateLimit=60, slot=self.handle_mouse_movement)
        self.tabs.addTab(self.plot_canvas, "📊 Scope Mode")

        # TAB 2: HEATMAP
        self.heatmap_canvas = pg.PlotWidget()
        self.heatmap_canvas.setBackground('w')
        self.heatmap_canvas.setLabel('bottom', 'Wavelength', units='nm', color='k', bold=True)
        self.heatmap_canvas.setLabel('left', 'History (Frames)', color='k', bold=True)
        self.heatmap_canvas.getAxis('bottom').enableAutoSIPrefix(False)
        self.heatmap_canvas.getAxis('left').enableAutoSIPrefix(False)
        self.heatmap_canvas.setXRange(DEFAULT_X_MIN_NM, DEFAULT_X_MAX_NM, padding=0)
        self.heatmap_canvas.setYRange(0, HEATMAP_HISTORY_SIZE, padding=0)
        
        self.image_item = pg.ImageItem()
        
        transform = QTransform()
        transform.translate(wavelength_array[0], 0)
        x_scale = (wavelength_array[-1] - wavelength_array[0]) / PIXEL_COUNT
        transform.scale(x_scale, 1.0)
        self.image_item.setTransform(transform)
        
        colormap = pg.colormap.get('inferno')
        self.image_item.setLookupTable(colormap.getLookupTable())
        self.heatmap_canvas.addItem(self.image_item)

        self.heatmap_cursor_vline = pg.InfiniteLine(angle=90, movable=False,
                                                    pen=pg.mkPen('w', style=Qt.PenStyle.DashLine, width=1))
        self.heatmap_cursor_hline = pg.InfiniteLine(angle=0, movable=False,
                                                    pen=pg.mkPen('w', style=Qt.PenStyle.DashLine, width=1))
        self.heatmap_canvas.addItem(self.heatmap_cursor_vline, ignoreBounds=True)
        self.heatmap_canvas.addItem(self.heatmap_cursor_hline, ignoreBounds=True)

        self.heatmap_mouse_proxy = pg.SignalProxy(
            self.heatmap_canvas.scene().sigMouseMoved,
            rateLimit=60, slot=self.handle_heatmap_mouse_movement
        )

        self.tabs.addTab(self.heatmap_canvas, "🌊 Time-Lapse Heatmap")

        # --- CONTROL PANEL ---
        self.control_widget = QWidget() 
        control_grid = QGridLayout(self.control_widget)
        main_layout.addWidget(self.control_widget)
        self.control_widget.setEnabled(False) 

        # ROW 0
        self.button_pause = QPushButton("Pause")
        self.button_pause.setCheckable(True)
        self.button_pause.clicked.connect(self.toggle_measurement_pause)
        control_grid.addWidget(self.button_pause, 0, 0)
        
        self.button_save_csv = QPushButton("💾 Save CSV")
        self.button_save_csv.clicked.connect(self.export_data_csv)
        control_grid.addWidget(self.button_save_csv, 0, 1)

        self.button_save_png = QPushButton("📸 Save PNG")
        self.button_save_png.clicked.connect(self.export_plot_png)
        control_grid.addWidget(self.button_save_png, 0, 2)

        control_grid.addWidget(QLabel("Frames to Avg:"), 0, 3, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_averaging = QSpinBox()
        self.spinbox_averaging.setRange(1, 100)
        self.spinbox_averaging.valueChanged.connect(self.update_averaging_frames)
        control_grid.addWidget(self.spinbox_averaging, 0, 4)

        control_grid.addWidget(QLabel("Exposure (ms):"), 0, 5, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_exposure = QDoubleSpinBox()
        self.spinbox_exposure.setRange(MIN_INTEGRATION_TIME_US / 1000.0, MAX_INTEGRATION_TIME_US / 1000.0)
        self.spinbox_exposure.setDecimals(1)
        self.spinbox_exposure.setValue(START_INTEGRATION_TIME_US / 1000.0)
        self.spinbox_exposure.setKeyboardTracking(False) 
        self.spinbox_exposure.valueChanged.connect(self.apply_manual_exposure)
        control_grid.addWidget(self.spinbox_exposure, 0, 6)

        self.button_auto_exposure = QPushButton("Auto Exp: OFF")
        self.button_auto_exposure.setCheckable(True)
        self.button_auto_exposure.clicked.connect(self.toggle_auto_exposure)
        control_grid.addWidget(self.button_auto_exposure, 0, 7)

        control_grid.addWidget(QLabel("Library:"), 0, 8, alignment=Qt.AlignmentFlag.AlignRight)
        self.combo_reference = QComboBox()
        self.combo_reference.addItem("None")
        for key in self.reference_library.keys():
            self.combo_reference.addItem(key)
        self.combo_reference.currentTextChanged.connect(self.apply_reference_overlay)
        control_grid.addWidget(self.combo_reference, 0, 9)

        # ROW 1
        control_grid.addWidget(QLabel("X Min (nm):"), 1, 0, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_x_min = QSpinBox()
        self.spinbox_x_min.setRange(100, 1500)
        self.spinbox_x_min.setValue(DEFAULT_X_MIN_NM)
        self.spinbox_x_min.setKeyboardTracking(False)
        self.spinbox_x_min.valueChanged.connect(self.apply_axes_limits)
        control_grid.addWidget(self.spinbox_x_min, 1, 1)

        control_grid.addWidget(QLabel("X Max (nm):"), 1, 2, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_x_max = QSpinBox()
        self.spinbox_x_max.setRange(100, 1500)
        self.spinbox_x_max.setValue(DEFAULT_X_MAX_NM)
        self.spinbox_x_max.setKeyboardTracking(False)
        self.spinbox_x_max.valueChanged.connect(self.apply_axes_limits)
        control_grid.addWidget(self.spinbox_x_max, 1, 3)

        self.button_auto_y_axis = QPushButton("Auto Y: ON")
        self.button_auto_y_axis.setCheckable(True)
        self.button_auto_y_axis.setChecked(True)
        self.button_auto_y_axis.setStyleSheet("background-color: lightgreen;")
        self.button_auto_y_axis.clicked.connect(self.toggle_auto_y_axis)
        control_grid.addWidget(self.button_auto_y_axis, 1, 4)

        control_grid.addWidget(QLabel("Y Max (ADC):"), 1, 5, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_y_max = QSpinBox()
        self.spinbox_y_max.setRange(10, 65000)
        self.spinbox_y_max.setValue(DEFAULT_Y_MAX_ADC)
        self.spinbox_y_max.setKeyboardTracking(False)
        self.spinbox_y_max.valueChanged.connect(self.apply_manual_y_axis)
        control_grid.addWidget(self.spinbox_y_max, 1, 6)

        self.button_show_peaks = QPushButton("Show Peaks: OFF")
        self.button_show_peaks.setCheckable(True)
        self.button_show_peaks.clicked.connect(self.toggle_peak_finder)
        control_grid.addWidget(self.button_show_peaks, 1, 7)

        self.button_measure_mode = QPushButton("Measure Mode: OFF")
        self.button_measure_mode.setCheckable(True)
        self.button_measure_mode.clicked.connect(self.toggle_measure_mode)
        control_grid.addWidget(self.button_measure_mode, 1, 8, 1, 2)

        # ROW 2
        self.button_dark_correct = QPushButton("Dark Correct: OFF")
        self.button_dark_correct.setCheckable(True)
        self.button_dark_correct.clicked.connect(self.toggle_dark_correction)
        control_grid.addWidget(self.button_dark_correct, 2, 0)

        control_grid.addWidget(QLabel("Dark Mode:"), 2, 1, alignment=Qt.AlignmentFlag.AlignRight)
        self.combo_dark_mode = QComboBox()
        self.combo_dark_mode.addItem("Optical Black (live)", DARK_MODE_OPTICAL_BLACK)
        self.combo_dark_mode.addItem("Parametric (Bias+Rate)", DARK_MODE_PARAMETRIC)
        self.combo_dark_mode.currentIndexChanged.connect(self.apply_dark_mode)
        control_grid.addWidget(self.combo_dark_mode, 2, 2)

        control_grid.addWidget(QLabel("Bias (ADC):"), 2, 3, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_dark_bias = QDoubleSpinBox()
        self.spinbox_dark_bias.setRange(0.0, 1000.0)
        self.spinbox_dark_bias.setValue(DEFAULT_DARK_BIAS_ADC)
        control_grid.addWidget(self.spinbox_dark_bias, 2, 4)

        control_grid.addWidget(QLabel("Rate (ADC/s):"), 2, 5, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_dark_rate = QDoubleSpinBox()
        self.spinbox_dark_rate.setRange(0.0, 1000.0)
        self.spinbox_dark_rate.setValue(DEFAULT_DARK_RATE_ADC_PER_SEC)
        control_grid.addWidget(self.spinbox_dark_rate, 2, 6)

        self.button_despeckle = QPushButton("Hot-Pixel Filter: OFF")
        self.button_despeckle.setCheckable(True)
        self.button_despeckle.clicked.connect(self.toggle_despeckle_filter)
        control_grid.addWidget(self.button_despeckle, 2, 7)

        control_grid.addWidget(QLabel("Filter Width:"), 2, 8, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_despeckle_width = QSpinBox()
        self.spinbox_despeckle_width.setRange(3, 15)
        self.spinbox_despeckle_width.setSingleStep(2) 
        self.spinbox_despeckle_width.setValue(5)      
        control_grid.addWidget(self.spinbox_despeckle_width, 2, 9)

        # ROW 3
        self.label_ob_readout = QLabel("Dark Level: ---- ADC   |   Integration: ----- ms   |   Sensor temp (relative): baseline (cold)")
        self.label_ob_readout.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #1F6F1F; "
            "padding: 4px 10px; border: 1px solid #B0C0B0; border-radius: 4px;"
        )
        self.label_ob_readout.setFixedHeight(32)
        self.label_ob_readout.setMinimumWidth(700)
        self.label_ob_readout.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        control_grid.addWidget(self.label_ob_readout, 3, 0, 1, 10)

        self.apply_dark_mode()

        for btn in [self.button_pause, self.button_save_csv, self.button_save_png,
                    self.button_auto_exposure, self.button_dark_correct, self.button_auto_y_axis,
                    self.button_despeckle, self.button_show_peaks, self.button_measure_mode,
                    self.button_theme]:
            btn.setMinimumHeight(35)

        # --- CONTEXT-SENSITIVE HELP ---
        add_help(self.button_scan, "Scan the USB bus for connected PASCO PS-2600A spectrometers.\nThe device must have the WinUSB driver installed via Zadig.")
        add_help(self.combo_devices, "List of detected spectrometers. If empty, click Scan USB.\nThe device must not be open in any other application.")
        add_help(self.button_connect, "Connect to the selected spectrometer and start live acquisition.\nClick again to disconnect.")
        add_help(self.button_theme, "Switch between Light and Dark interface themes.")
        add_help(self.button_pause, "Pause or resume live acquisition.\nThe last acquired frame stays visible while paused.")
        add_help(self.button_save_csv, "Export the current averaged spectrum to a timestamped CSV file.\nIncludes OB mean, integration time and correction mode as metadata.\nIf the Heatmap tab is active, the full frame history is exported instead.")
        add_help(self.button_save_png, "Save a high-resolution screenshot of the currently active plot tab.")
        add_help(self.spinbox_averaging, "Number of consecutive frames to average before display.\nHigher values reduce noise but slow the response to changing signals.\nRange: 1 (no averaging) to 100 frames.")
        add_help(self.spinbox_exposure, "Integration time in milliseconds.\nLonger integration collects more photons but also more dark charge.\nRange: 1 ms to 2500 ms. Above 2500 ms the sensor enters a non-linear regime.")
        add_help(self.button_auto_exposure, "Automatically adjusts integration time to keep the strongest peak\nnear the target ADC level (default: 3400 counts).\nIncludes saturation protection with an emergency drop-back.")
        add_help(self.combo_reference, "Overlay a reference spectrum from the library as a dashed green line.\nIncludes gas discharge lamps, fluorescent tubes, and LED colors.\nDelete reference_spectra.csv and restart to regenerate the full library.")
        add_help(self.spinbox_x_min, "Left edge of the visible wavelength range on the x-axis (nm).")
        add_help(self.spinbox_x_max, "Right edge of the visible wavelength range on the x-axis (nm).")
        add_help(self.button_auto_y_axis, "When ON: the Y-axis scales automatically to the tallest visible peak.\nWhen OFF: the Y Max spinbox sets a fixed upper limit.")
        add_help(self.spinbox_y_max, "Manual upper limit of the Y-axis in ADC counts.\nOnly active when Auto Y is OFF.")
        add_help(self.button_show_peaks, "Find and label the three dominant spectral peaks in the visible range.\nUses Non-Maximum Suppression with a configurable minimum distance.")
        add_help(self.button_measure_mode, "Place two draggable vertical cursors on the spectrum.\nThe info bar shows delta-wavelength (dx) and delta-intensity (dy) between them.")
        add_help(self.button_dark_correct, "Enable dark current subtraction from the live spectrum.\nUse Optical Black mode for automatic per-frame correction,\nor Parametric mode for the static Bias + Rate x time model.")
        add_help(self.combo_dark_mode, "Optical Black (recommended): uses the 30 masked sensor pixels in the\ntransfer header as a live, per-frame dark reference. Automatically\ncompensates for temperature drift without any model parameters.\n\nParametric: subtracts I_dark = Bias + Rate x t. Accurate within\nthe validated 0–2500 ms linear regime but cannot track temperature drift.")
        add_help(self.spinbox_dark_bias, "Constant dark current offset at zero integration time (ADC counts).\nOnly used in Parametric correction mode.\nMeasure with the sensor covered using the Dark Current Characterization script.")
        add_help(self.spinbox_dark_rate, "Thermal dark current accumulation rate (ADC counts per second).\nOnly used in Parametric correction mode.\nMeasure with the sensor covered using the Dark Current Characterization script.")
        add_help(self.button_despeckle, "Apply a sliding median filter to suppress hot pixels and cosmic ray spikes.\nDoes not affect genuine narrow spectral lines wider than the kernel.")
        add_help(self.spinbox_despeckle_width, "Kernel width for the median despeckle filter (pixels, must be odd).\nWider kernels suppress more aggressive spikes but slightly blur narrow peaks.\nRecommended: 3–7 for most uses.")
        add_help(self.label_ob_readout, "Live readout of the 30 masked optical black CCD pixels in the transfer header.\nThese pixels never see light and provide an instantaneous dark reference.\nThe temperature indicator shows the thermal component relative to\nthe cold-start baseline measured during characterization.")


    # --- CONNECTION MANAGER LOGIC ---
    def refresh_device_list(self):
        self.combo_devices.clear()
        devices = scan_usb_devices()
        if not devices:
            self.combo_devices.addItem("No PASCO PS-2600A found")
            self.button_connect.setEnabled(False)
        else:
            for d in devices:
                self.combo_devices.addItem(f"PASCO PS-2600A ({d[-20:]})", d)
            self.button_connect.setEnabled(True)

    def toggle_connection(self):
        if self.hardware_thread is not None and self.hardware_thread.isRunning():
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self):
        device_path = self.combo_devices.currentData()
        if not device_path: return
        
        try:
            connect_usb_device(device_path)
            self.hardware_thread = SpectrometerHardwareThread()
            self.hardware_thread.signal_new_spectrum_data.connect(self.process_new_spectrum)
            self.hardware_thread.signal_auto_exposure_adjusted.connect(self.handle_auto_exposure_update)
            self.hardware_thread.signal_connection_lost.connect(self.handle_connection_lost)
            self.hardware_thread.start()
            
            self.button_connect.setText("Disconnect")
            self._style_connect_button(connected=True)
            self.label_status.setText("🟢 Connected")
            self.combo_devices.setEnabled(False)
            self.button_scan.setEnabled(False)
            self.control_widget.setEnabled(True)
            self.spectrum_history.clear()
            
        except Exception as e:
            QMessageBox.critical(self, "Connection Error", str(e))

    def disconnect_device(self):
        if self.hardware_thread:
            self.hardware_thread.stop_thread()
            self.hardware_thread = None
            
        free_usb_resources()
        self.button_connect.setText("Connect")
        self._style_connect_button(connected=False)
        self.label_status.setText("🔴 Disconnected")
        self.combo_devices.setEnabled(True)
        self.button_scan.setEnabled(True)
        self.control_widget.setEnabled(False)
        self.label_peaks_info.setText("")
        self.scatter_peaks.clear()

    def handle_connection_lost(self):
        self.disconnect_device()
        QMessageBox.warning(self, "Connection Lost", "The device was unplugged or stopped responding.")

    # --- UI INTERACTION LOGIC ---
    def handle_mouse_movement(self, event):
        position = event[0]
        if self.plot_canvas.sceneBoundingRect().contains(position):
            mapped_point = self.plot_canvas.getPlotItem().vb.mapSceneToView(position)
            x_hover = mapped_point.x()
            closest_index = (np.abs(wavelength_array - x_hover)).argmin()
            snapped_x = wavelength_array[closest_index]
            snapped_y = self.current_averaged_pixels[closest_index]
            
            self.cursor_vline.setPos(snapped_x)
            self.cursor_hline.setPos(snapped_y)
            self.label_cursor_info.setText(f"🔍 Pixel: {closest_index}  |  Wavelength: {snapped_x:.1f} nm  |  Intensity: {snapped_y:.1f} ADC")

    def handle_heatmap_mouse_movement(self, event):
        position = event[0]
        if not self.heatmap_canvas.sceneBoundingRect().contains(position):
            return

        mapped_point = self.heatmap_canvas.getPlotItem().vb.mapSceneToView(position)
        x_hover = mapped_point.x()
        y_hover = mapped_point.y()

        closest_index = (np.abs(wavelength_array - x_hover)).argmin()
        snapped_x = wavelength_array[closest_index]

        frame_index = int(np.clip(round(y_hover), 0, HEATMAP_HISTORY_SIZE - 1))
        intensity = self.heatmap_buffer[closest_index, frame_index]

        self.heatmap_cursor_vline.setPos(snapped_x)
        self.heatmap_cursor_hline.setPos(frame_index)
        self.label_cursor_info.setText(
            f"🔍 Pixel: {closest_index}  |  Wavelength: {snapped_x:.1f} nm  |  "
            f"Frame: {frame_index}  |  Intensity: {intensity:.1f} ADC"
        )

    def toggle_measurement_pause(self, is_checked):
        if self.hardware_thread: self.hardware_thread.is_measurement_paused = is_checked
        self.button_pause.setText("GO (Paused)" if is_checked else "Pause")
        self.button_pause.setStyleSheet("background-color: lightcoral;" if is_checked else "")

    def update_averaging_frames(self, value):
        self.spectrum_history = deque(self.spectrum_history, maxlen=value)

    def apply_manual_exposure(self, ms_value):
        if not self.hardware_thread: return
        if self.hardware_thread.is_auto_exposure_active:
            self.hardware_thread.is_auto_exposure_active = False
            self.button_auto_exposure.setChecked(False)
            self.button_auto_exposure.setText("Auto Exp: OFF")
            self.button_auto_exposure.setStyleSheet("")
            
        target_us = int(ms_value * 1000)
        self.hardware_thread.current_integration_time_us = target_us
        self.hardware_thread.apply_hardware_exposure_time(target_us)
        self.spectrum_history.clear()

    def handle_auto_exposure_update(self, new_ms_value):
        self.spinbox_exposure.blockSignals(True)
        self.spinbox_exposure.setValue(new_ms_value)
        self.spinbox_exposure.blockSignals(False)
        self.spectrum_history.clear()

    def toggle_auto_exposure(self, is_checked):
        if self.hardware_thread: self.hardware_thread.is_auto_exposure_active = is_checked
        self.button_auto_exposure.setText("Auto Exp: ON" if is_checked else "Auto Exp: OFF")
        self.button_auto_exposure.setStyleSheet("background-color: lightgreen;" if is_checked else "")
        self.spectrum_history.clear()

    def apply_reference_overlay(self, ref_name):
        if ref_name == "None" or ref_name not in self.reference_library:
            self.reference_curve.setVisible(False)
        else:
            self.reference_curve.setData(wavelength_array, self.reference_library[ref_name])
            self.reference_curve.setVisible(True)

    def toggle_dark_correction(self, is_checked):
        self.is_dark_correction_enabled = is_checked
        self.button_dark_correct.setText("Dark Correct: ON" if is_checked else "Dark Correct: OFF")
        self.button_dark_correct.setStyleSheet("background-color: lightgreen;" if is_checked else "")

    def apply_dark_mode(self, *args):
        self.dark_correction_mode = self.combo_dark_mode.currentData()
        is_parametric = (self.dark_correction_mode == DARK_MODE_PARAMETRIC)
        self.spinbox_dark_bias.setEnabled(is_parametric)
        self.spinbox_dark_rate.setEnabled(is_parametric)

    def _style_connect_button(self, connected=None):
        if connected is None:
            connected = (self.hardware_thread is not None
                         and self.hardware_thread.isRunning())
        if self.is_dark_mode:
            if connected:
                style = ("background-color: #f38ba8; color: #1e1e2e; "
                         "font-weight: bold; border-radius: 4px;")
            else:
                style = ("background-color: #45475a; color: #cdd6f4; "
                         "font-weight: bold; border-radius: 4px;")
        else:
            if connected:
                style = "background-color: lightcoral; font-weight: bold;"
            else:
                style = "background-color: lightgray; font-weight: bold;"
        self.button_connect.setStyleSheet(style)

    def toggle_theme(self, is_checked):
        self.is_dark_mode = is_checked
        self.apply_theme()

    def apply_theme(self):
        app = QApplication.instance()
        if self.is_dark_mode:
            app.setStyleSheet(DARK_STYLESHEET)
            self.button_theme.setText("Light Mode")
            self.plot_canvas.setBackground('#1e1e2e')
            self.plot_canvas.getAxis('bottom').setPen(pg.mkPen('#cdd6f4'))
            self.plot_canvas.getAxis('left').setPen(pg.mkPen('#cdd6f4'))
            self.plot_canvas.getAxis('bottom').setTextPen(pg.mkPen('#cdd6f4'))
            self.plot_canvas.getAxis('left').setTextPen(pg.mkPen('#cdd6f4'))
            self.spectrum_curve.setPen(pg.mkPen('#89b4fa', width=2))
            self.reference_curve.setPen(pg.mkPen('#a6e3a1', width=2,
                                                  style=Qt.PenStyle.DashLine))
            self.heatmap_canvas.setBackground('#1e1e2e')
            self.heatmap_canvas.getAxis('bottom').setPen(pg.mkPen('#cdd6f4'))
            self.heatmap_canvas.getAxis('left').setPen(pg.mkPen('#cdd6f4'))
            self.heatmap_canvas.getAxis('bottom').setTextPen(pg.mkPen('#cdd6f4'))
            self.heatmap_canvas.getAxis('left').setTextPen(pg.mkPen('#cdd6f4'))
            self.label_ob_readout.setStyleSheet(
                "font-size: 13px; font-weight: bold; color: #a6e3a1; "
                "padding: 4px 10px; border: 1px solid #3a7a3a; border-radius: 4px; "
                "background-color: #1e1e2e;"
            )
            self.label_peaks_info.setStyleSheet("font-size: 16px; font-weight: bold; color: #f38ba8;")
            self.label_measure_info.setStyleSheet("font-size: 16px; font-weight: bold; color: #cba6f7;")
            self.label_cursor_info.setStyleSheet("font-size: 16px; font-weight: bold; color: #89b4fa;")
        else:
            app.setStyleSheet(LIGHT_STYLESHEET)
            self.button_theme.setText("Dark Mode")
            self.plot_canvas.setBackground('w')
            self.plot_canvas.getAxis('bottom').setPen(pg.mkPen('k'))
            self.plot_canvas.getAxis('left').setPen(pg.mkPen('k'))
            self.plot_canvas.getAxis('bottom').setTextPen(pg.mkPen('k'))
            self.plot_canvas.getAxis('left').setTextPen(pg.mkPen('k'))
            self.spectrum_curve.setPen(pg.mkPen('b', width=2))
            self.reference_curve.setPen(pg.mkPen('g', width=2,
                                                  style=Qt.PenStyle.DashLine))
            self.heatmap_canvas.setBackground('w')
            self.heatmap_canvas.getAxis('bottom').setPen(pg.mkPen('k'))
            self.heatmap_canvas.getAxis('left').setPen(pg.mkPen('k'))
            self.heatmap_canvas.getAxis('bottom').setTextPen(pg.mkPen('k'))
            self.heatmap_canvas.getAxis('left').setTextPen(pg.mkPen('k'))
            self.label_ob_readout.setStyleSheet(
                "font-size: 13px; font-weight: bold; color: #1F6F1F; "
                "padding: 4px 10px; border: 1px solid #B0C0B0; border-radius: 4px;"
            )
            self.label_peaks_info.setStyleSheet("font-size: 16px; font-weight: bold; color: #D72800;")
            self.label_measure_info.setStyleSheet("font-size: 16px; font-weight: bold; color: #8A2BE2;")
            self.label_cursor_info.setStyleSheet("font-size: 16px; font-weight: bold; color: #0078D7;")

        self._style_connect_button()

    def update_ob_readout(self):
        ob = self.latest_ob_mean
        if ob <= 0.0:
            self.label_ob_readout.setText(
                "Dark Level: ---- ADC   |   Integration: ----- ms   |   Sensor temp (relative): --"
            )
            return

        integration_ms = self.latest_integration_us / 1000.0
        thermal_part = ob - (DEFAULT_DARK_RATE_ADC_PER_SEC * integration_ms / 1000.0)
        delta = thermal_part - OB_TEMP_REFERENCE_ADC

        if delta < 1.0:
            temp_hint = "baseline (cold)    "   
        elif delta < 4.0:
            temp_hint = f"{delta:+5.1f} ADC (slightly warm)"
        else:
            temp_hint = f"{delta:+5.1f} ADC (warm)         "

        self.label_ob_readout.setText(
            f"Dark Level: {ob:6.2f} ADC   |   "
            f"Integration: {integration_ms:7.1f} ms   |   "
            f"Sensor temp (relative): {temp_hint}"
        )

    def toggle_auto_y_axis(self, is_checked):
        self.is_auto_y_axis_enabled = is_checked
        self.button_auto_y_axis.setText("Auto Y: ON" if is_checked else "Auto Y: OFF")
        self.button_auto_y_axis.setStyleSheet("background-color: lightgreen;" if is_checked else "")
        if not is_checked:
            self.plot_canvas.setYRange(Y_AXIS_BOTTOM_MARGIN_ADC, self.spinbox_y_max.value(), padding=0)
            
    def toggle_despeckle_filter(self, is_checked):
        self.is_despeckle_enabled = is_checked
        self.button_despeckle.setText("Hot-Pixel Filter: ON" if is_checked else "Hot-Pixel Filter: OFF")
        self.button_despeckle.setStyleSheet("background-color: lightgreen;" if is_checked else "")
        
    def toggle_peak_finder(self, is_checked):
        self.is_peak_finding_enabled = is_checked
        self.button_show_peaks.setText("Show Peaks: ON" if is_checked else "Show Peaks: OFF")
        self.button_show_peaks.setStyleSheet("background-color: lightgreen;" if is_checked else "")
        if not is_checked:
            self.scatter_peaks.clear()
            self.label_peaks_info.setText("")
            
    def toggle_measure_mode(self, is_checked):
        self.is_measure_mode_enabled = is_checked
        self.button_measure_mode.setText("Measure Mode: ON" if is_checked else "Measure Mode: OFF")
        self.button_measure_mode.setStyleSheet("background-color: #DDA0DD; font-weight: bold;" if is_checked else "")
        self.measure_line_a.setVisible(is_checked)
        self.measure_line_b.setVisible(is_checked)
        if not is_checked:
            self.label_measure_info.setText("")
        else:
            self.update_measurement_display()

    def update_measurement_display(self):
        if not self.is_measure_mode_enabled: return
        
        pos_a_x = self.measure_line_a.value()
        pos_b_x = self.measure_line_b.value()
        
        idx_a = (np.abs(wavelength_array - pos_a_x)).argmin()
        idx_b = (np.abs(wavelength_array - pos_b_x)).argmin()
        
        val_a_y = self.current_averaged_pixels[idx_a]
        val_b_y = self.current_averaged_pixels[idx_b]
        
        dx = abs(wavelength_array[idx_b] - wavelength_array[idx_a])
        dy = abs(val_b_y - val_a_y)
        
        self.label_measure_info.setText(f"📐 A: {wavelength_array[idx_a]:.1f}nm | B: {wavelength_array[idx_b]:.1f}nm  ➔  Δx: {dx:.2f} nm  |  Δy: {dy:.1f} ADC")

    def apply_manual_y_axis(self, max_y_value):
        if self.is_auto_y_axis_enabled:
            self.button_auto_y_axis.setChecked(False)
            self.toggle_auto_y_axis(False)
        self.plot_canvas.setYRange(Y_AXIS_BOTTOM_MARGIN_ADC, max_y_value, padding=0)

    def apply_axes_limits(self):
        xmin = self.spinbox_x_min.value()
        xmax = self.spinbox_x_max.value()
        self.plot_canvas.setXRange(xmin, xmax, padding=0)
        self.heatmap_canvas.setXRange(xmin, xmax, padding=0)

    # --- EXPORT LOGIC ---
    def export_data_csv(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        is_heatmap_active = (self.tabs.currentIndex() == 1)
        
        prefix = "heatmap_data" if is_heatmap_active else "spectrum_data"
        filename = f"{prefix}_{timestamp}.csv"
        
        try:
            with open(filename, 'w', newline='') as file:
                writer = csv.writer(file, delimiter=';')
                
                if is_heatmap_active:
                    header = ["Time_Frame"] + [f"{w:.2f}" for w in wavelength_array]
                    writer.writerow(header)
                    for frame_idx in range(HEATMAP_HISTORY_SIZE):
                        row_data = [frame_idx] + [round(val, 2) for val in self.heatmap_buffer[:, frame_idx]]
                        writer.writerow(row_data)
                else:
                    writer.writerow(["# Optical Black mean (ADC)", round(self.latest_ob_mean, 3)])
                    writer.writerow(["# Integration time (ms)", round(self.latest_integration_us / 1000.0, 1)])
                    writer.writerow(["# Dark correction", "ON" if self.is_dark_correction_enabled else "OFF"])
                    writer.writerow(["# Dark mode", self.dark_correction_mode])
                    writer.writerow(["Pixel_ID", "Wavelength_nm", "ADC_Counts"])
                    for idx, (wave, pixel) in enumerate(zip(wavelength_array, self.current_averaged_pixels)):
                        writer.writerow([idx, round(wave, 2), round(pixel, 2)])
                        
            QMessageBox.information(self, "Success", f"Data saved to:\n{filename}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save CSV: {e}")

    def export_plot_png(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        is_heatmap_active = (self.tabs.currentIndex() == 1)
        
        prefix = "heatmap_plot" if is_heatmap_active else "spectrum_plot"
        filename = f"{prefix}_{timestamp}.png"
        
        try:
            screenshot = self.heatmap_canvas.grab() if is_heatmap_active else self.plot_canvas.grab()
            screenshot.save(filename, "PNG")
            QMessageBox.information(self, "Success", f"Image saved as:\n{filename}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save PNG: {e}")

    # --- CORE DRAWING ROUTINE ---
    def process_new_spectrum(self, raw_pixels, ob_mean, integration_us):
        self.spectrum_history.append(raw_pixels)
        self.current_averaged_pixels = np.mean(self.spectrum_history, axis=0)

        self.latest_ob_mean = ob_mean
        self.latest_integration_us = integration_us
        self.update_ob_readout()

        # 1. Apply Dark Correction
        if self.is_dark_correction_enabled:
            if self.dark_correction_mode == DARK_MODE_OPTICAL_BLACK:
                dark_estimate = OB_CORRECTION_SLOPE * ob_mean + OB_CORRECTION_OFFSET
            else:
                current_bias = self.spinbox_dark_bias.value()
                current_rate = self.spinbox_dark_rate.value()
                integration_seconds = integration_us / 1_000_000.0
                dark_estimate = current_bias + (current_rate * integration_seconds)

            self.current_averaged_pixels = np.maximum(
                0, self.current_averaged_pixels - dark_estimate
            )

        # 2. Apply Despeckle Filter
        if self.is_despeckle_enabled:
            window_size = self.spinbox_despeckle_width.value()
            if window_size % 2 == 0: window_size += 1 
            pad_width = window_size // 2
            padded_array = np.pad(self.current_averaged_pixels, (pad_width, pad_width), mode='edge')
            sliding_windows = np.lib.stride_tricks.sliding_window_view(padded_array, window_shape=window_size)
            self.current_averaged_pixels = np.median(sliding_windows, axis=1)

        # 3. Calculate Dynamic Y-Axis limits
        current_y_max_scope = self.plot_canvas.getViewBox().viewRange()[1][1]
        ideal_heatmap_max = ADC_SATURATION_THRESHOLD
        
        if self.is_auto_y_axis_enabled:
            viewport_x_min, viewport_x_max = self.plot_canvas.getViewBox().viewRange()[0]
            visible_data_mask = (wavelength_array >= viewport_x_min) & (wavelength_array <= viewport_x_max)
            
            if np.any(visible_data_mask):
                max_visible_value = np.max(self.current_averaged_pixels[visible_data_mask])
                ideal_y_max = max(max_visible_value * 1.1, 50)
                ideal_heatmap_max = ideal_y_max 
                
                if ideal_y_max > current_y_max_scope or ideal_y_max < current_y_max_scope * 0.7:
                    self.plot_canvas.setYRange(Y_AXIS_BOTTOM_MARGIN_ADC, ideal_y_max, padding=0)
        else:
            ideal_heatmap_max = self.spinbox_y_max.value()

        # 4. Update Scope Data
        self.spectrum_curve.setData(wavelength_array, self.current_averaged_pixels)
        
        # 5. Update Heatmap (Waterfall Plot)
        self.heatmap_buffer = np.roll(self.heatmap_buffer, 1, axis=1)
        self.heatmap_buffer[:, 0] = self.current_averaged_pixels
        
        safe_heatmap_max = max(10.0, ideal_heatmap_max) 
        self.image_item.setImage(self.heatmap_buffer, autoLevels=False, levels=(0, safe_heatmap_max))
        
        # 6. Update Cursors
        if self.is_measure_mode_enabled:
            self.update_measurement_display()
        
        # 7. Update Peaks
        if self.is_peak_finding_enabled:
            y = self.current_averaged_pixels
            local_maxima_mask = (y[1:-1] > y[:-2]) & (y[1:-1] > y[2:]) & (y[1:-1] > MIN_PEAK_HEIGHT_ADC)
            peak_indices = np.where(local_maxima_mask)[0] + 1
            
            peak_heights = y[peak_indices]
            sorted_arguments = np.argsort(peak_heights)[::-1]
            sorted_peak_indices = peak_indices[sorted_arguments]
            
            top_3_indices = []
            for current_idx in sorted_peak_indices:
                is_valid = True
                for accepted_idx in top_3_indices:
                    if abs(current_idx - accepted_idx) < PEAK_MIN_DISTANCE_PIXELS:
                        is_valid = False
                        break
                if is_valid:
                    top_3_indices.append(current_idx)
                if len(top_3_indices) >= 3:
                    break
                    
            if top_3_indices:
                top_3_indices.sort()
                peak_x = wavelength_array[top_3_indices]
                peak_y = y[top_3_indices]
                self.scatter_peaks.setData(peak_x, peak_y)
                
                display_texts = []
                for idx, (x_val, y_val) in enumerate(zip(peak_x, peak_y)):
                    display_texts.append(f"P{idx+1}: {x_val:.1f}nm ({y_val:.0f})")
                self.label_peaks_info.setText("   |   ".join(display_texts))
            else:
                self.scatter_peaks.clear()
                self.label_peaks_info.setText("No dominant peaks found")

    def closeEvent(self, event):
        self.disconnect_device()
        event.accept()

# ============================================================
# APPLICATION ENTRY POINT
# ============================================================
if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    dashboard = DashboardWindow()
    dashboard.show()
    
    application_exit_code = app.exec()
    free_usb_resources()
    sys.exit(application_exit_code)
