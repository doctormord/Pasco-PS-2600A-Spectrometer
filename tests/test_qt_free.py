"""The measurement core and the servers must import without any Qt binding."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BLOCKER = r"""
import sys, importlib.abc

class _Block(importlib.abc.MetaPathFinder):
    BLOCKED = ("PyQt6", "PyQt5", "pyqtgraph")
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.BLOCKED:
            raise ImportError("blocked by test: " + name)
        return None

sys.meta_path.insert(0, _Block())
%s
bad = [m for m in sys.modules if m.split(".")[0] in ("PyQt6", "PyQt5", "pyqtgraph")]
assert not bad, bad
print("OK")
"""


def _run(imports: str):
    code = BLOCKER % imports
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "OK" in r.stdout


def _exists(*parts):
    return os.path.exists(os.path.join(ROOT, *parts))


def test_engine_import_qt_free():
    _run("import engine")


@pytest.mark.skipif(not _exists("api.py"), reason="api.py not present")
def test_api_import_qt_free():
    _run("import engine\nimport api")


@pytest.mark.skipif(not _exists("server_runner.py"), reason="server_runner.py not present")
def test_server_runner_import_qt_free():
    _run("import server_runner")


@pytest.mark.skipif(not _exists("mcp_server", "server.py"),
                    reason="mcp_server/server.py not present")
def test_mcp_server_import_qt_free():
    _run("import mcp_server.server")


def test_core_calibration_modules_qt_free():
    _run("import calibration_linearity\nimport calibration_workflow\n"
         "import calibration_capture\nimport processing\n"
         "import calibration_sphere\nimport reference_sources\nimport color_science")
