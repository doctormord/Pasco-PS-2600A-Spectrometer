"""Finding 20: the full suite crashed sporadically because GUI modules
created and destroyed their own QApplication. There is exactly ONE
QApplication per test session (tests/conftest.py); no test module may create
or own another one."""
from __future__ import annotations

import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def test_single_session_application(qapp):
    from PyQt6.QtWidgets import QApplication
    import conftest
    assert QApplication.instance() is qapp
    assert conftest._QT_APP is qapp            # referenced for the whole process


def test_gc_only_in_main_thread(qapp):
    """With Qt objects alive, automatic GC is off (it would run in background
    threads); collection happens in the main thread after each test. The
    application does the same with pyqtgraph's GarbageCollector."""
    import gc
    assert not gc.isenabled()
    src = open(os.path.join(os.path.dirname(HERE), "PS-2600A_Pro.py"),
               encoding="utf-8").read()
    assert "GarbageCollector(" in src


def _test_files():
    return [os.path.join(HERE, f) for f in sorted(os.listdir(HERE))
            if f.startswith("test_") and f.endswith(".py")]


@pytest.mark.parametrize("path", _test_files(), ids=os.path.basename)
def test_no_module_creates_its_own_application(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "qapp":
            pytest.fail(f"{os.path.basename(path)} defines its own qapp fixture")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("QApplication", "QGuiApplication")):
            pytest.fail(f"{os.path.basename(path)}:{node.lineno} creates a QApplication")


_TEARDOWN = """
import gc
from PyQt6.QtWidgets import QApplication
app = QApplication([])
from gui_cie_tab import CIETab
t = CIETab()
assert t._tm30_thread.isRunning()
del t
gc.collect()
app.processEvents()
t2 = CIETab()           # left running: stopped at interpreter exit
print("survived", flush=True)
"""


def test_colour_tab_worker_thread_survives_teardown(tmp_path):
    """Second cause of finding 20: the colour tab's TM-30 QThread was only
    stopped on aboutToQuit (never emitted in tests / when a window is just
    closed); destroying the running QThread aborted the whole process
    silently. Run in a subprocess, since a regression kills the interpreter."""
    import subprocess
    import sys
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               PS2600A_DATA_DIR=str(tmp_path), PS2600A_SKIP_MIGRATION="1")
    root = os.path.dirname(HERE)
    r = subprocess.run([sys.executable, "-X", "faulthandler", "-c", _TEARDOWN],
                       cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (r.returncode, r.stdout[-500:], r.stderr[-2000:])
    assert "survived" in r.stdout
