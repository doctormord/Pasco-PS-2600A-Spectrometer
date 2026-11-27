"""
PASCO PS-2600A Spectrum Analyzer — entry point.

    python PS-2600A_Pro.py                 desktop app (PyQt6)
    python PS-2600A_Pro.py --headless      engine + REST API / web UI, no Qt import
                          [--host 127.0.0.1] [--lan] [--port 8000] [--token T]
                          [--require-token] [--no-connect] [--backend NAME]
    python PS-2600A_Pro.py --data-dir PATH  use PATH as the user data directory
                                           (also: environment PS2600A_DATA_DIR)

The application is split into focused modules:

  engine.py             Shared Qt-free core: device, fusion, averaging,
                        processing, calibration runtime, heatmap, jobs
  api.py / api_v1.py    FastAPI app (legacy /api + versioned /api/v1)
  server_runner.py      uvicorn in a thread (desktop "Remote access") and
                        the headless entry point
  gui_dashboard.py      DashboardWindow (the main window)
  gui_theme.py          Palettes, stylesheets, custom widgets
  gui_cie_tab.py        CIE 1931 / CCT / CRI tab
  color_science.py      CIE 1931 colorimetry math
  app_config.py         JSON-backed user preferences

Runtime data (spectrometer_config.json, calibration profiles, dark spectra,
linearity records, lamp library, backups, protocols) lives in the per-user data
directory (data_paths.py: %APPDATA%\\PS-2600A, ~/Library/Application
Support/PS-2600A, $XDG_CONFIG_HOME/ps-2600a). On the first start existing files
of the program folder are copied there once (originals kept, logged).
"""

import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    import data_paths
    argv = data_paths.parse_cli(argv)          # --data-dir before any config import
    if "--headless" in argv:
        # No Qt import on this path (runs on machines without PyQt6).
        from server_runner import headless_main
        return headless_main(argv)

    from PyQt6.QtWidgets import QApplication

    from _device_pasco import free_usb_resources
    from gui_dashboard import DashboardWindow

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # Python's cyclic garbage collector runs in whichever thread allocates
    # (device / fusion / server threads). Collecting Qt objects there destroys
    # them outside the GUI thread and crashes sporadically. Automatic GC is
    # disabled and done on a timer in the GUI thread instead.
    from pyqtgraph.util.garbage_collector import GarbageCollector
    _gc = GarbageCollector(interval=1.0)                       # noqa: F841
    dashboard = DashboardWindow()
    dashboard.show()
    code = app.exec()
    free_usb_resources()
    return code


if __name__ == "__main__":
    sys.exit(main())
