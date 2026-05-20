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
                             QSpinBox, QDoubleSpinBox, QMessageBox, QComboBox, QTabWidget)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QRectF
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
WAVELENGTH_COEFFS = [130.755917, 0.262201464, 1.44855491e-05, -4.30320660e-09]
ADC_SATURATION_THRESHOLD = 3800

# --- 5. ALGORITHMS (EXPOSURE, DARK CURRENT & PEAKS) ---
DEFAULT_DARK_BIAS_ADC = 60.5
DEFAULT_DARK_RATE_ADC_PER_SEC = 45.0
AUTO_EXP_TARGET_ADC = 3400
AUTO_EXP_DEADZONE_ADC = 100
AUTO_EXP_MIN_RATIO = 0.2
AUTO_EXP_MAX_RATIO = 5.0
AUTO_EXP_EMERGENCY_DROP_RATIO = 0.2

MIN_INTEGRATION_TIME_US = 1_000         
MAX_INTEGRATION_TIME_US = 2_500_000     
START_INTEGRATION_TIME_US = 20_000      
PEAK_MIN_DISTANCE_PIXELS = 100          
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

# Calculate absolute wavelength array once
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
    if os.path.exists(REFERENCE_LIBRARY_FILENAME):
        return
        
    print(f"Creating default reference library: {REFERENCE_LIBRARY_FILENAME}")
    
    hydrogen_spectrum = np.zeros(PIXEL_COUNT)
    mercury_spectrum = np.zeros(PIXEL_COUNT)
    
    for i, wave in enumerate(wavelength_array):
        for h_peak in [410.1, 434.0, 486.1, 656.3]:
            if abs(wave - h_peak) < 5: hydrogen_spectrum[i] += 3000 * np.exp(-((wave - h_peak)**2)/2)
        for hg_peak in [404.6, 435.8, 546.1, 577.0, 579.0]:
            if abs(wave - hg_peak) < 5: mercury_spectrum[i] += 3000 * np.exp(-((wave - hg_peak)**2)/2)
            
    with open(REFERENCE_LIBRARY_FILENAME, 'w', newline='') as file:
        writer = csv.writer(file, delimiter=';')
        writer.writerow(["Wavelength_nm", "Hydrogen (Balmer)", "Mercury (Hg)"])
        for i in range(PIXEL_COUNT):
            writer.writerow([round(wavelength_array[i], 2), round(hydrogen_spectrum[i], 1), round(mercury_spectrum[i], 1)])


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
# 🧵 BACKGROUND HARDWARE THREAD
# ============================================================
class SpectrometerHardwareThread(QThread):
    signal_new_spectrum_data = pyqtSignal(np.ndarray)
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

                self.signal_new_spectrum_data.emit(pixel_array)
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
                
                for col_name in headers[1:]:
                    self.reference_library[col_name] = np.zeros(PIXEL_COUNT)
                
                for row_idx, row in enumerate(reader):
                    if row_idx >= PIXEL_COUNT: break
                    for col_idx, col_name in enumerate(headers[1:]):
                        self.reference_library[col_name][row_idx] = float(row[col_idx + 1])
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
        self.button_connect.setStyleSheet("background-color: lightgray; font-weight: bold;")
        self.button_connect.clicked.connect(self.toggle_connection)
        connection_layout.addWidget(self.button_connect)

        self.label_status = QLabel("🔴 Disconnected")
        self.label_status.setStyleSheet("font-weight: bold; padding: 0px 15px;")
        connection_layout.addWidget(self.label_status)
        connection_layout.addStretch()
        
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
        
        # APPLYING ROBUST MATHEMATICAL SCALING TO FIX THE "1 PIXEL" BUG
        transform = QTransform()
        transform.translate(wavelength_array[0], 0)
        x_scale = (wavelength_array[-1] - wavelength_array[0]) / PIXEL_COUNT
        transform.scale(x_scale, 1.0)
        self.image_item.setTransform(transform)
        
        colormap = pg.colormap.get('inferno')
        self.image_item.setLookupTable(colormap.getLookupTable())
        self.heatmap_canvas.addItem(self.image_item)
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

        control_grid.addWidget(QLabel("Bias (ADC):"), 2, 1, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_dark_bias = QDoubleSpinBox()
        self.spinbox_dark_bias.setRange(0.0, 1000.0)
        self.spinbox_dark_bias.setValue(DEFAULT_DARK_BIAS_ADC)
        control_grid.addWidget(self.spinbox_dark_bias, 2, 2)

        control_grid.addWidget(QLabel("Rate (ADC/s):"), 2, 3, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_dark_rate = QDoubleSpinBox()
        self.spinbox_dark_rate.setRange(0.0, 1000.0)
        self.spinbox_dark_rate.setValue(DEFAULT_DARK_RATE_ADC_PER_SEC)
        control_grid.addWidget(self.spinbox_dark_rate, 2, 4)

        self.button_despeckle = QPushButton("Hot-Pixel Filter: OFF")
        self.button_despeckle.setCheckable(True)
        self.button_despeckle.clicked.connect(self.toggle_despeckle_filter)
        control_grid.addWidget(self.button_despeckle, 2, 5)

        control_grid.addWidget(QLabel("Filter Width:"), 2, 6, alignment=Qt.AlignmentFlag.AlignRight)
        self.spinbox_despeckle_width = QSpinBox()
        self.spinbox_despeckle_width.setRange(3, 15)
        self.spinbox_despeckle_width.setSingleStep(2) 
        self.spinbox_despeckle_width.setValue(5)      
        control_grid.addWidget(self.spinbox_despeckle_width, 2, 7)

        for btn in [self.button_pause, self.button_save_csv, self.button_save_png, 
                    self.button_auto_exposure, self.button_dark_correct, self.button_auto_y_axis,
                    self.button_despeckle, self.button_show_peaks, self.button_measure_mode]:
            btn.setMinimumHeight(35)


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
            self.button_connect.setStyleSheet("background-color: lightcoral; font-weight: bold;")
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
        self.button_connect.setStyleSheet("background-color: lightgray; font-weight: bold;")
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
    def process_new_spectrum(self, raw_pixels):
        self.spectrum_history.append(raw_pixels)
        self.current_averaged_pixels = np.mean(self.spectrum_history, axis=0)

        # 1. Apply Dark Correction
        if self.is_dark_correction_enabled and self.hardware_thread:
            current_bias = self.spinbox_dark_bias.value()
            current_rate = self.spinbox_dark_rate.value()
            integration_seconds = self.hardware_thread.current_integration_time_us / 1_000_000.0
            total_calculated_noise = current_bias + (current_rate * integration_seconds)
            self.current_averaged_pixels = np.maximum(0, self.current_averaged_pixels - total_calculated_noise)

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
        
        safe_heatmap_max = max(10.0, ideal_heatmap_max) # Fallback to prevent math errors when completely dark
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