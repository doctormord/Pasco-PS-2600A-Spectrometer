"""Headless entry point: `PS-2600A_Pro.py --headless` runs engine + API with
PyQt blocked (docs/PLAN_api_mcp.md Phase 2 acceptance)."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

httpx = pytest.importorskip("httpx")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_BLOCK_AND_RUN = r"""
import sys, importlib.abc, runpy, os
class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('PyQt6', 'PyQt5', 'pyqtgraph'):
            raise ImportError('blocked for test: ' + name)
        return None
sys.meta_path.insert(0, _Block())
import app_config
app_config.Config.path = os.environ['T_CFG']
app_config.Config._data = dict(app_config.DEFAULT_CONFIG)
app_config.Config._data['calibration_profiles_file'] = os.environ['T_DIR'] + '/cp.json'
app_config.Config._data['dark_spectra_file'] = os.environ['T_DIR'] + '/dk.json'
app_config.Config._data['reference_sources_file'] = os.environ['T_DIR'] + '/rs.json'
sys.argv = ['PS-2600A_Pro.py', '--headless', '--port', os.environ['T_PORT'],
            '--backend', 'Demo (virtual)', '--token', 'secret-token']
mod = runpy.run_path('PS-2600A_Pro.py', run_name='not_main')
mod['main'](sys.argv[1:])
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_headless_without_qt(tmp_path):
    port = _free_port()
    env = dict(os.environ, T_PORT=str(port), T_DIR=str(tmp_path),
               T_CFG=str(tmp_path / "cfg.json"), PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", _BLOCK_AND_RUN], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{port}"
        st = None
        for _ in range(150):
            if proc.poll() is not None:
                break
            try:
                st = httpx.get(base + "/api/v1/status", timeout=1.0).json()
                if st.get("connected"):
                    break
            except Exception:
                pass
            time.sleep(0.1)
        assert proc.poll() is None, proc.stdout.read().decode("utf-8", "replace")
        assert st is not None and st["connected"] and st["backend"] == "Demo (virtual)"
        # token not required on 127.0.0.1 by default …
        assert httpx.get(base + "/api/status").status_code == 200
        # … the web UI is served, and a steering call works
        assert httpx.get(base + "/").status_code == 200
        r = httpx.put(base + "/api/v1/acquisition", json={"exposure_ms": 12.0}, timeout=5)
        assert r.status_code == 200, r.text
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
