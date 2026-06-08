"""
PASCO PS-2600A Spectrum Analyzer — entry point.

The application is split into focused modules:

  spectrometer_core.py  Hardware (WinUSB), constants, acquisition thread
  gui_theme.py          Palettes, stylesheets, custom widgets
  gui_cie_tab.py        CIE 1931 / CCT / CRI tab
  gui_dashboard.py      DashboardWindow (the main window)
  color_science.py      CIE 1931 colorimetry math
  app_config.py         JSON-backed user preferences

Run with:
    python "PS-2600A_Pro_Redesigned.py"

A `spectrometer_config.json` file is created on first launch and
automatically updated as the user tweaks settings.
"""

import sys
from PyQt6.QtWidgets import QApplication

from _device_pasco import free_usb_resources
from gui_dashboard import DashboardWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    dashboard = DashboardWindow()
    dashboard.show()
    code = app.exec()
    free_usb_resources()
    return code


if __name__ == "__main__":
    sys.exit(main())
