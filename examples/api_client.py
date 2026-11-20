"""Example client for the PS-2600A REST API (/api/v1). Needs only httpx.

Start the app with Remote access (or `python PS-2600A_Pro.py --headless`), then:

  python examples/api_client.py status
  python examples/api_client.py connect --backend "Demo (virtual)"
  python examples/api_client.py exposure 50
  python examples/api_client.py measure --frames 5 --csv spectrum.csv
  python examples/api_client.py series --count 10 --interval 2
  python examples/api_client.py report --pdf color_report.pdf

Token / URL: --token / --url, or the environment variables SPECTRO_API_TOKEN /
SPECTRO_API_URL (default http://127.0.0.1:8000). User steps of a job (for example
covering the light path for a dark) are shown on the console; press Enter to
confirm.
"""
import argparse
import os
import sys

import httpx

FINAL = ("done", "failed", "cancelled")


class Api:
    def __init__(self, url: str, token: str | None, client_id: str = "example"):
        self.base = url.rstrip("/") + "/api/v1"
        headers = {"X-Client-Id": client_id}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(headers=headers, timeout=130)

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        r = self.http.request(method, self.base + path, **kw)
        if r.status_code >= 400:
            try:
                e = r.json()["error"]
                msg = f"{e['code']}: {e['message']}"
            except Exception:
                msg = r.text[:200]
            sys.exit(f"API error {r.status_code}: {msg}")
        return r

    def json(self, method: str, path: str, **kw):
        return self.call(method, path, **kw).json()

    def wait_job(self, job_id: str, poll_s: float = 1.0) -> dict:
        """Poll until the job is final; user steps are confirmed on the console."""
        last = ""
        while True:
            j = self.json("GET", f"/jobs/{job_id}", params={"wait_s": poll_s})
            line = f"{j['status']} {j['progress'] * 100:3.0f} % {j.get('message', '')}"
            if line != last:
                print(" ", line)
                last = line
            if j["status"] in FINAL:
                return j
            if j["status"] == "waiting_for_user":
                prompt = j.get("prompt") or {}
                input(f"\n>>> {prompt.get('message', 'User step')}\n    Press Enter when done ")
                self.json("POST", f"/jobs/{job_id}/continue")


def cmd_status(api: Api, a) -> None:
    s = api.json("GET", "/status")
    print(f"connected={s['connected']} backend={s.get('backend')} serial={s.get('serial')}")
    print(f"exposure={s['exposure_ms']} ms  averaging={s['frames_to_average']}  unit={s['unit']}")
    print(f"calibration: {s['calibration'].get('label')} ({s['calibration'].get('state')})")


def cmd_connect(api: Api, a) -> None:
    print(api.json("POST", "/connect", json={"backend": a.backend, "device_path": a.device}))


def cmd_exposure(api: Api, a) -> None:
    r = api.json("PUT", "/acquisition", json={"exposure_ms": a.ms})
    print("applied:", r["applied"])


def _print_measurement(m: dict) -> None:
    acq, col = m["acquisition"], m.get("color") or {}
    print(f"id {m['id']}  t_int {acq['integration_ms']} ms  frames {acq['frames']}  "
          f"peak {acq['peak_frac'] * 100:.0f} %  saturated px {acq['n_saturated']}")
    if col:
        print(f"CCT {col.get('cct'):.0f} K  Duv {col.get('duv'):.4f}  Ra {col.get('Ra'):.1f}")
    for w in m.get("warnings", []):
        print("warning:", w)


def cmd_measure(api: Api, a) -> None:
    body = {"integration_ms": "auto", "frames": a.frames, "wait": False}
    job = api.json("POST", "/measure", json=body)["job"]
    job = api.wait_job(job["id"])
    if job["status"] != "done":
        sys.exit(f"measurement {job['status']}: {job.get('error') or job.get('message')}")
    mid = job["result"]["measurement_id"]
    _print_measurement(api.json("GET", f"/measurements/{mid}", params={"arrays": False}))
    if a.csv:
        data = api.call("GET", f"/measurements/{mid}/export", params={"format": "csv"}).content
        with open(a.csv, "wb") as f:
            f.write(data)
        print(f"CSV saved: {a.csv} ({len(data)} bytes)")
    if a.pdf:
        _save_report(api, a.pdf, mid)


def cmd_series(api: Api, a) -> None:
    body = {"count": a.count, "interval_s": a.interval, "integration_ms": "auto",
            "frames": a.frames, "wait": False}
    job = api.json("POST", "/series", json=body)["job"]
    job = api.wait_job(job["id"], poll_s=max(1.0, a.interval))
    if job["status"] != "done":
        sys.exit(f"series {job['status']}: {job.get('error') or job.get('message')}")
    print(f"{'id':32} {'CCT/K':>8} {'peak %':>7}")
    for r in job["result"]["summary"]:
        cct = f"{r['cct']:.0f}" if r.get("cct") else "-"
        print(f"{r['id']:32} {cct:>8} {r['peak_frac'] * 100:7.1f}")


def _save_report(api: Api, path: str, measurement_id: str | None) -> None:
    body = {"kind": "color", "format": "pdf", "language": "en"}
    if measurement_id:
        body["measurement_id"] = measurement_id
    data = api.call("POST", "/reports", json=body).content
    with open(path, "wb") as f:
        f.write(data)
    print(f"report saved: {path} ({len(data)} bytes)")


def cmd_report(api: Api, a) -> None:
    _save_report(api, a.pdf, a.measurement_id)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="PS-2600A REST API example client")
    p.add_argument("--url", default=os.environ.get("SPECTRO_API_URL") or "http://127.0.0.1:8000")
    p.add_argument("--token", default=os.environ.get("SPECTRO_API_TOKEN") or None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    c = sub.add_parser("connect")
    c.add_argument("--backend", default=None, help='e.g. "Demo (virtual)"')
    c.add_argument("--device", default=None)
    c.set_defaults(fn=cmd_connect)
    e = sub.add_parser("exposure")
    e.add_argument("ms", type=float, help="integration time in ms")
    e.set_defaults(fn=cmd_exposure)
    m = sub.add_parser("measure", help="single measurement with auto exposure")
    m.add_argument("--frames", type=int, default=1)
    m.add_argument("--csv", help="save the spectrum as CSV")
    m.add_argument("--pdf", help="save a colour report PDF of this measurement")
    m.set_defaults(fn=cmd_measure)
    s = sub.add_parser("series", help="measurement series")
    s.add_argument("--count", type=int, default=5)
    s.add_argument("--interval", type=float, default=1.0, help="seconds, start to start")
    s.add_argument("--frames", type=int, default=1)
    s.set_defaults(fn=cmd_series)
    r = sub.add_parser("report", help="colour report PDF (live spectrum or a measurement)")
    r.add_argument("--pdf", default="color_report.pdf")
    r.add_argument("--measurement-id", default=None)
    r.set_defaults(fn=cmd_report)
    a = p.parse_args(argv)
    api = Api(a.url, a.token)
    try:
        a.fn(api, a)
    except httpx.ConnectError:
        sys.exit(f"Cannot connect to {a.url}. Is Remote access switched on?")


if __name__ == "__main__":
    main()
