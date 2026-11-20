"""Static checks of the web UI (static/): JS syntax, id consistency between
app.js and index.html, and that the app serves the files."""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import engine as eng
from api import ServerSettings, create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")


def _read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not in PATH")
def test_app_js_syntax():
    r = subprocess.run(["node", "--check", os.path.join(STATIC, "app.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ids created / addressed dynamically (templates, generated markup)
DYNAMIC_IDS: set[str] = set()


def test_js_ids_exist_in_html():
    js = _read("app.js")
    html = _read("index.html")
    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r"""\$\('#([A-Za-z][\w-]*)'\)""", js))
    used |= set(re.findall(r"""document\.querySelector\('#([A-Za-z][\w-]*)'\)""", js))
    used |= set(re.findall(r"""getElementById\('([A-Za-z][\w-]*)'\)""", js))
    used |= set(re.findall(r"""\$id\('([A-Za-z][\w-]*)'\)""", js))
    used |= set(re.findall(r"""\bel\('([A-Za-z][\w-]*)'\)""", js))
    missing = sorted(used - html_ids - DYNAMIC_IDS)
    assert not missing, f"ids used in app.js but absent in index.html: {missing}"


def test_html_ids_unique():
    html = _read("index.html")
    ids = re.findall(r'\bid="([^"]+)"', html)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    assert not dup, dup


def test_no_removed_response_toggle():
    assert 'data-key="spectral_response_compensation"' not in _read("index.html")


def test_static_served(isolated_config):
    e = eng.Engine()
    try:
        with TestClient(create_app(e, ServerSettings())) as c:
            r = c.get("/")
            assert r.status_code == 200 and "token-dialog" in r.text
            r = c.get("/static/app.js")
            assert r.status_code == 200 and "apiFetch" in r.text
            assert c.get("/static/styles.css").status_code == 200
    finally:
        e.close()


def test_no_undefined_module_refs():
    js = _read("app.js")
    defined = set(re.findall(r"^(?:const|let|var|function|async function)\s+([A-Za-z_$][\w$]*)", js, re.M))
    for name in ("Access", "Jobs", "Hm", "CalMgr", "CalRun", "Dark", "DataSec", "WlCal", "Cal"):
        assert name in defined, name
    assert not re.search(r"\bAuth\.", js)
