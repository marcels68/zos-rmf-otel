#!/usr/bin/env python3
"""Tests für rmf_otel_agent.py ohne Netz zu z/OS: simulierter DDS (mock_dds.py, JSON und
XML) und ein kleiner OTLP-Empfänger im selben Prozess.

    python3 -m unittest -v test_agent.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rmf_otel_agent as agent  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_mock(*extra):
    port = free_port()
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_dds.py"), "--port",
                          str(port), *extra], stdout=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return p, f"http://127.0.0.1:{port}"
        except OSError:
            time.sleep(0.1)
    p.kill()
    raise RuntimeError("mock_dds startet nicht")


class Receiver(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Receiver.received.append((self.path, json.loads(body)))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class ConvertTest(unittest.TestCase):
    def test_list_report(self):
        report = {
            "metric": {"description": "% CPU utilization (CP) by MVS image", "format": "list"},
            "resource": {"reslabel": ",PLEX1,SYSPLEX"},
            "timeData": {"utcEnd": "20261021120000"},
            "row": [{"col": ["SYSA", "12.5"]}, {"col": ["SYSB", "Deact"]},
                    {"col": ["SYSC", "NaN"]}, {"col": ["*NoData*", "0"]}],
        }
        m, ts = agent.report_to_metric(report, {"id": "8D0450", "resource": ",,SYSPLEX",
                                                "row_attribute": "zos.system.name"}, 0)
        self.assertEqual(m["name"], "rmf.pct_cpu_utilization_cp_by_mvs_image")
        self.assertEqual(m["unit"], "%")
        pts = m["gauge"]["dataPoints"]
        self.assertEqual(len(pts), 1)  # Deact, NaN, *NoData* fallen weg
        self.assertEqual(pts[0]["asDouble"], 12.5)
        self.assertEqual(ts, 1792584000 * 10**9)
        attrs = {a["key"]: a["value"]["stringValue"] for a in pts[0]["attributes"]}
        self.assertEqual(attrs["zos.system.name"], "SYSA")
        self.assertEqual(attrs["rmf.resource.name"], "PLEX1")
        self.assertEqual(attrs["zos.sysplex.name"], "PLEX1")

    def test_empty(self):
        r = {"metric": {"format": "single", "description": "x"}, "row": [{"col": ["", "NaN"]}]}
        self.assertIsNone(agent.report_to_metric(r, {"id": "1", "resource": ",,SYSPLEX"}, 0))

    def test_dds_error(self):
        r = {"message": {"id": "GPM0505E", "severity": 8, "description": "bad"}}
        with self.assertRaises(agent.DDSError):
            agent.report_to_metric(r, {"id": "1", "resource": ",,SYSPLEX"}, 0)

    def test_xml(self):
        xml = (b'<ddsml><report><metric id="8D0E90" description="i/o activity rate" '
               b'format="single"/><resource><reslabel>,PLEX1,SYSPLEX</reslabel></resource>'
               b'<time-data><utc-end>20261021120000</utc-end></time-data>'
               b'<row><col></col><col>2500.5</col></row></report></ddsml>')
        resp = agent.xml_to_response(xml)
        r = resp["report"][0]
        self.assertEqual(r["timeData"]["utcEnd"], "20261021120000")
        self.assertEqual(r["metric"]["format"], "single")
        self.assertEqual(r["row"], [{"col": ["", "2500.5"]}])


class EndToEndTest(unittest.TestCase):
    def run_agent(self, mock_args):
        mock, url = start_mock(*mock_args)
        port = free_port()
        srv = ThreadingHTTPServer(("127.0.0.1", port), Receiver)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        Receiver.received = []
        try:
            with open(os.path.join(HERE, "agent.example.json"), encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["dds"] = {"url": url}
            cfg["otlp"] = {"endpoint": f"http://127.0.0.1:{port}"}
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(cfg, f)
            rc = agent.main(["--config", f.name, "--once"])
            os.unlink(f.name)
        finally:
            mock.kill()
            mock.wait()
            srv.shutdown()
            srv.server_close()
        self.assertEqual(rc, 0)
        self.assertEqual(len(Receiver.received), 1)
        path, payload = Receiver.received[0]
        self.assertEqual(path, "/v1/metrics")
        metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        names = {m["name"]: len(m["gauge"]["dataPoints"]) for m in metrics}
        self.assertEqual(names["rmf.cpu.utilization"], 3)  # SYSA, SYSB, SYSC
        self.assertEqual(names["rmf.io.rate"], 1)
        self.assertEqual(len(names), len(cfg["metrics"]))

    def test_json_dds(self):
        self.run_agent([])

    def test_xml_only_dds(self):
        self.run_agent(["--xml-only"])


class MockTest(unittest.TestCase):
    def test_auth_and_m3_format(self):
        import base64
        import urllib.error
        import urllib.request
        mock, url = start_mock("--auth", "TEST:geheim")
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(url + "/metrics/m3", timeout=5)
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()
            tok = base64.b64encode(b"TEST:geheim").decode()
            req = urllib.request.Request(url + "/metrics/m3",
                                         headers={"Authorization": "Basic " + tok})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertTrue(r.headers["Content-Type"].startswith("text/plain"))
                text = r.read().decode()
        finally:
            mock.kill()
            mock.wait()
        line = next(x for x in text.splitlines() if x.startswith("mvs_rmf_m3_mintime{"))
        name, value, ts = line.rsplit(" ", 2)
        self.assertEqual(value, "100")
        self.assertEqual(int(ts) % 100000, 0)  # ms, Ende des Intervalls
        self.assertIn("# TYPE mvs_volume_io_percent gauge", text)  # Familie ohne Werte


if __name__ == "__main__":
    unittest.main()
