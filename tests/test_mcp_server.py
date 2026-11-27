"""MCP server (mcp_server/) against a real in-process REST API with the Demo
backend. The MCP layer talks HTTP only; tests call the tools through the SDK's
in-memory client."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("httpx")
pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = "Demo (virtual)"

EXPECTED_TOOLS = {
    "status", "list_devices", "connect", "disconnect", "set_acquisition", "auto_expose",
    "capture_dark", "measure", "start_series", "job_status", "cancel_job", "continue_job",
    "get_spectrum", "analyze_color", "find_peaks", "analyze_filter", "list_profiles",
    "select_profile", "calibration_status", "list_references", "start_calibration",
    "export_measurement", "create_report",
    "verify_profile", "linearity_status", "start_linearity",
    "start_sphere_characterization",
}
LIMIT = 8 * 1024


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self, engine, runner, server, data_dir):
        self.engine, self.runner, self.server, self.data_dir = engine, runner, server, data_dir

    def call(self, name: str, **args):
        """(is_error, text, parsed_json_or_None) of one tool call."""
        from mcp import Client

        async def go():
            async with Client(self.server) as c:
                return await c.call_tool(name, args)

        res = asyncio.run(go())
        text = "".join(getattr(p, "text", "") for p in res.content)
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        return res.is_error, text, data

    def ok(self, name: str, **args) -> dict:
        err, text, data = self.call(name, **args)
        assert not err, f"{name} failed: {text}"
        assert len(text.encode("utf-8")) <= LIMIT, f"{name}: {len(text)} bytes"
        assert isinstance(data, dict), text
        return data

    def wait_job(self, job_id: str, timeout: float = 30.0) -> dict:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            j = self.ok("job_status", job_id=job_id)
            if j["status"] in ("done", "failed", "cancelled"):
                return j
            time.sleep(0.1)
        raise AssertionError("job did not finish")


@pytest.fixture
def harness(isolated_config, tmp_path, monkeypatch):
    import api
    import engine as eng
    import server_runner
    isolated_config._data.update({"calibration_capture_frames": 4,
                                  "calibration_stability_delay_s": 0.1,
                                  "demo_fullscale_ms": 100.0})
    port = _free_port()
    e = eng.Engine()
    runner = server_runner.ServerRunner(
        e, api.ServerSettings(port=port, token="tt", require_token_local=True))
    runner.start()
    data_dir = tmp_path / "mcpdata"
    monkeypatch.setenv("SPECTRO_API_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SPECTRO_API_TOKEN", "tt")
    monkeypatch.setenv("SPECTRO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SPECTRO_CLIENT_ID", "pytest")
    from mcp_server.server import build_server
    try:
        yield Harness(e, runner, build_server(), data_dir)
    finally:
        runner.stop()
        e.close()


@pytest.fixture
def conn(harness):
    d = harness.ok("connect", backend=BACKEND)
    assert d["connected"] is True and d["backend"] == BACKEND
    t0 = time.monotonic()
    while harness.engine.last_frame is None and time.monotonic() - t0 < 10:
        time.sleep(0.05)
    assert harness.engine.last_frame is not None
    return harness


# ---------------------------------------------------------------- tools
def test_tool_list_and_annotations(harness):
    from mcp import Client

    async def go():
        async with Client(harness.server) as c:
            return (await c.list_tools()).tools, (await c.list_resources()).resources

    tools, resources = asyncio.run(go())
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS
    assert not [n for n in names if "delete" in n or "remove" in n]
    by = {t.name: t for t in tools}
    for n in ("status", "get_spectrum", "analyze_color", "list_profiles", "job_status"):
        assert by[n].annotations.read_only_hint is True
    assert by["measure"].annotations.read_only_hint is False
    assert by["cancel_job"].annotations.destructive_hint is True
    for t in tools:
        assert t.description and "Error" not in t.description
    assert {str(r.uri) for r in resources} == {"spectrometer://status",
                                               "spectrometer://config",
                                               "spectrometer://profiles"}


def test_connect_status_acquisition(harness, conn):
    st = harness.ok("status")
    assert st["connected"] and st["backend"] == BACKEND and st["serial"]
    assert set(st["calibration"]) == {"label", "state", "type", "valid_range_nm"}
    assert st["jobs"] == []
    dev = harness.ok("list_devices")
    assert BACKEND in dev["backends"] and dev["devices"]
    r = harness.ok("set_acquisition", exposure_ms=40, frames_to_average=2)
    assert r["status"]["exposure_ms"] == 40 and r["status"]["frames_to_average"] == 2
    assert "applied" in r
    err, text, _ = harness.call("set_acquisition")
    assert err and "at least one" in text
    ae = harness.ok("auto_expose")
    assert ae["converged"] and 0.6 < ae["peak_frac"] < 0.95


def test_measure_compact_and_csv(conn):
    h = conn
    m = h.ok("measure", frames=2, label="t1")
    assert m["acquisition"]["frames"] == 2 and m["label"] == "t1"
    assert {"integration_ms", "peak_frac", "n_saturated", "dark", "dark_source"} <= \
        set(m["acquisition"])
    assert m["device"]["backend"] == BACKEND
    assert {"cct", "duv", "x", "y", "Ra", "R9", "lux", "peak_nm", "dominant_nm",
            "centroid_nm", "fwhm_nm"} == set(m["color"])
    assert 1000 < m["color"]["cct"] < 20000
    assert len(m["peaks"]) <= 5 and 0 < len(m["spectrum_preview"]) <= 32
    assert "wavelengths" not in m and "values" not in m
    assert os.path.isfile(m["csv_path"])
    assert os.path.dirname(m["csv_path"]) == str(h.data_dir)
    assert os.path.getsize(m["csv_path"]) > 10_000
    ae = h.ok("measure", integration_ms="auto")
    assert 0.5 < ae["acquisition"]["peak_frac"] < 0.95


def test_get_spectrum(conn, tmp_path):
    h = conn
    live = h.ok("get_spectrum", max_points=40)
    assert live["source"]["live"] and live["n"] > 1000
    assert len(live["points"]) <= 40
    assert live["max"]["value"] >= max(p[1] for p in live["points"]) - 1e-9
    assert live["integral"] and live["unit"]
    win = h.ok("get_spectrum", wl_min=500, wl_max=600, max_points=10)
    assert all(500 <= p[0] <= 600 for p in win["points"]) and len(win["points"]) <= 10
    cap = h.ok("get_spectrum", max_points=100000)
    assert len(cap["points"]) <= 512

    full = h.ok("get_spectrum", save_full=True)
    with open(full["csv_path"]) as f:
        assert len(f.readlines()) > 1000

    mid = h.ok("measure")["id"]
    by_id = h.ok("get_spectrum", measurement_id=mid, max_points=16, save_full=True)
    assert by_id["source"]["measurement_id"] == mid and len(by_id["points"]) <= 16
    assert os.path.isfile(by_id["csv_path"])
    err, text, _ = h.call("get_spectrum", measurement_id="m-nope")
    assert err and "not_found" in text and "Traceback" not in text


def test_analysis(conn):
    h = conn
    mid = h.ok("measure", frames=2)["id"]
    c = h.ok("analyze_color", measurement_id=mid)
    assert c["metrics"]["cct"] > 1000 and len(c["metrics"]["Ri"]) == 15
    assert h.ok("analyze_color")["source"] == {"live": True}
    p = h.ok("find_peaks", measurement_id=mid, min_prominence=50, max_count=3)
    assert 1 <= len(p["peaks"]) <= 3

    ref = h.ok("measure", integration_ms=100)["id"]
    smp = h.ok("measure", integration_ms=100)["id"]
    f = h.ok("analyze_filter", reference_id=ref, sample_id=smp)
    assert f["rows"] and len(f.get("transmission_preview", [])) <= 50


def test_series_and_job_status(conn):
    h = conn
    j = h.ok("start_series", count=3, interval_s=0)["job"]
    assert j["kind"] == "series"
    done = h.wait_job(j["id"])
    assert done["status"] == "done"
    res = done["result"]
    assert res["n"] == 3 and len(res["measurements"]) == 3
    assert set(res["stats"]) >= {"cct", "peak_frac"}
    assert res["stats"]["cct"]["min"] <= res["stats"]["cct"]["max"]
    err, text, _ = h.call("job_status", job_id="nope")
    assert err and "not_found" in text


def test_series_summary_large_is_statistics_only():
    from mcp_server.server import _series_summary, fit
    rows = [{"id": f"m-{i:04d}", "cct": 4800 + i, "lux": None, "peak_frac": 0.5,
             "peak_nm": 450.0} for i in range(120)]
    s = _series_summary({"measurement_ids": [r["id"] for r in rows], "summary": rows})
    assert "measurements" not in s and s["n"] == 120 and s["stats"]["cct"]["max"] == 4919
    assert len(fit(s).encode()) <= LIMIT
    rows50 = rows[:50]
    s = _series_summary({"measurement_ids": [], "summary": rows50})
    assert len(s["measurements"]) == 50 and len(fit(s).encode()) <= LIMIT


def test_fit_truncates_oversize():
    from mcp_server.server import fit
    big = {"a": list(range(20000)), "b": "x" * 5000, "c": [{"k": "v" * 50}] * 400}
    text = fit(big)
    assert len(text.encode()) <= LIMIT
    d = json.loads(text)
    assert d["truncated"] is True


def test_capture_dark_user_step(conn):
    h = conn
    h.engine.acquisition.SUPPORTS_SHUTTER = False
    d = h.ok("capture_dark", integration_ms=30, frames=3)["job"]
    assert d["status"] == "waiting_for_user"
    assert d["prompt"]["action"] == "cover"
    assert "continue_job" in d["next"] and "Ask the user" in d["next"]
    assert h.ok("status")["jobs"][0]["prompt"]["action"] == "cover"
    j = h.ok("continue_job", job_id=d["id"])
    # second user step (uncover) or already done
    if j["status"] == "waiting_for_user":
        assert j["prompt"]["action"] == "uncover"
        j = h.ok("continue_job", job_id=d["id"])
    j = h.wait_job(d["id"])
    assert j["status"] == "done"


def test_cancel_job(conn):
    h = conn
    h.engine.acquisition.SUPPORTS_SHUTTER = False
    d = h.ok("capture_dark")["job"]
    assert d["status"] == "waiting_for_user"
    c = h.ok("cancel_job", job_id=d["id"])
    assert "next" not in c
    assert h.wait_job(d["id"])["status"] == "cancelled"


def test_calibration_tools(conn):
    h = conn
    p = h.ok("list_profiles")
    assert p["selection"] == "None"
    assert {"value", "label", "state", "type", "quantity", "input", "unit", "valid_range_nm",
            "plausible", "legacy_valid_range_nm"} == set(p["items"][0])
    refs = h.ok("list_references")["references"]
    assert refs and refs[0]["id"]
    cs = h.ok("calibration_status")
    assert cs["state"] == "none"
    sel = h.ok("select_profile", selection="None")
    assert sel
    err, text, _ = h.call("select_profile", selection="no-such-profile")
    assert err and "Traceback" not in text

    job = h.ok("start_calibration", reference_id=refs[0]["id"], optics_name="CC-test",
               optics_input="CC")["job"]
    assert job["kind"] == "calibration"
    assert job["status"] in ("running", "waiting_for_user", "queued", "done")
    h.call("cancel_job", job_id=job["id"])
    h.wait_job(job["id"])


def test_reports_and_export(conn):
    h = conn
    mid = h.ok("measure")["id"]
    e = h.ok("export_measurement", measurement_id=mid, format="json")
    assert e["path"].endswith(".json") and os.path.getsize(e["path"]) == e["bytes"]
    assert json.load(open(e["path"]))["id"] == mid
    r = h.ok("create_report", kind="color", format="csv", measurement_id=mid)
    assert os.path.isfile(r["path"]) and r["bytes"] > 100 and r["path"].endswith(".csv")
    r2 = h.ok("create_report", kind="measurement", format="csv", measurement_id=mid)
    assert os.path.isfile(r2["path"])
    if importlib.util.find_spec("matplotlib") is None:
        return
    pdf = h.ok("create_report", kind="color", format="pdf", measurement_id=mid, language="en")
    assert os.path.isfile(pdf["path"]) and pdf["bytes"] > 1000
    with open(pdf["path"], "rb") as f:
        assert f.read(4) == b"%PDF"
    err, text, _ = h.call("create_report", kind="measurement", format="pdf", measurement_id=mid)
    assert err and "unprocessable" in text


def test_wrong_token_is_a_clear_message(harness, monkeypatch):
    monkeypatch.setenv("SPECTRO_API_TOKEN", "wrong")
    from mcp_server.server import build_server
    h = Harness(harness.engine, harness.runner, build_server(), harness.data_dir)
    err, text, _ = h.call("connect", backend=BACKEND)
    assert err and "unauthorized" in text and "SPECTRO_API_TOKEN" in text
    assert "Traceback" not in text
    # read-only tools work without a valid token
    assert h.ok("status")["connected"] is False


def test_api_unreachable_message(monkeypatch, tmp_path):
    monkeypatch.setenv("SPECTRO_API_URL", f"http://127.0.0.1:{_free_port()}")
    monkeypatch.setenv("SPECTRO_DATA_DIR", str(tmp_path))
    from mcp_server.server import build_server
    h = Harness(None, None, build_server(), tmp_path)
    err, text, _ = h.call("status")
    assert err and "Cannot connect" in text and "Traceback" not in text


def test_resources_readable(conn):
    from mcp import Client
    h = conn

    async def go():
        async with Client(h.server) as c:
            return {u: (await c.read_resource(u)).contents[0].text
                    for u in ("spectrometer://status", "spectrometer://config",
                              "spectrometer://profiles")}

    out = asyncio.run(go())
    assert json.loads(out["spectrometer://status"])["connected"] is True
    assert "exposure_ms" in json.loads(out["spectrometer://config"])
    assert json.loads(out["spectrometer://profiles"])["items"]


def test_disconnect(conn):
    assert conn.ok("disconnect")["connected"] is False


# ------------------------------------------------------------- isolation
def test_import_pulls_in_no_engine_api_or_qt():
    code = (
        f"import sys; sys.path.insert(0, {ROOT!r})\n"
        "import mcp_server.server\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "('engine', 'api', 'api_v1', 'api_models', 'server_runner', 'PyQt6', 'PyQt5', "
        "'pyqtgraph', 'processing', 'color_science')]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_launcher_script_main_module_exists():
    assert os.path.isfile(os.path.join(ROOT, "mcp_server", "__main__.py"))
