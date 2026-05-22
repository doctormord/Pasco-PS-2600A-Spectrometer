"""
Hardware layer, USB protocol, ctypes bindings, calibration constants,
reference-library autogeneration, the background acquisition thread,
and a small ContextHelp tooltip helper.

All purely-electronics code lives here so the GUI modules can stay
focused on layout & presentation.
"""

import sys
import os
import ctypes
from ctypes import wintypes
import struct
import time
import numpy as np
import csv

from PyQt6.QtWidgets import QToolTip
from PyQt6.QtCore import QThread, pyqtSignal, QObject, QTimer, QEvent


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
DEFAULT_X_MIN_NM = 380
DEFAULT_X_MAX_NM = 1050
DEFAULT_Y_MAX_ADC = 4000
Y_AXIS_BOTTOM_MARGIN_ADC = -20  

POLL_INTERVAL_SEC = 0.002
THREAD_PAUSE_SLEEP_SEC = 0.05
# =========================================================================================


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
