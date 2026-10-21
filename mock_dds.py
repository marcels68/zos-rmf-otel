#!/usr/bin/env python3
"""Simulierter RMF Distributed Data Server (DDS) für Demo und Tests ohne z/OS.

Liefert erfundene, aber plausibel schwankende Monitor-III-Werte für einen Sysplex
PLEX1 mit den Systemen SYSA, SYSB, SYSC:

  GET /gpm/perform?resource=..&id=..       JSON wie neuere DDS (Accept: application/json)
  GET /gpm/perform.xml?resource=..&id=..   XML (ddsml) wie ältere DDS
  GET /metrics/m3                          Prometheus-Textformat wie DDS mit APAR OA67701;
                                           Metrik- und Labelnamen wie in IBMs
                                           Grafana-Beispieldashboards (github.com/IBM/RMF)

    mock_dds.py [--port 8803] [--mintime 100] [--xml-only] [--auth USER:PASS]

Innerhalb eines Monitor-III-Intervalls (mintime) bleiben die Werte gleich, wie beim
echten DDS. Das XML-Format ist nachgebildet, nicht gegen einen echten DDS geprüft.
"""

import argparse
import base64
import json
import math
import random
import time
import urllib.parse
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

SYSPLEX = "PLEX1"
SYSTEMS = ["SYSA", "SYSB", "SYSC"]
CPC = "8561.ZCPC1"
JOBS = [("CICSPRD1", 61), ("DB2PDBM1", 72), ("IMSCTL", 85), ("BATCHJ01", 140),
        ("ZOSMF", 93), ("TCPIP", 51)]
WLM_GROUPS = ["CICSHIGH", "DB2HIGH", "BATCHLOW", "STCMED", "TSOUSER"]
CFS = ["CF01", "CF02"]

# id -> (Beschreibung, Format, Basiswert, Schwankung)
METRICS = {
    "8D0450": ("% CPU utilization (CP) by MVS image", "list", 55, 25),
    "8D0630": ("# active users by MVS image", "list", 120, 40),
    "8D0D60": ("# users by MVS image", "list", 400, 30),
    "8D1210": ("transaction ended rate by MVS image", "list", 80, 50),
    "8D0570": ("% workflow by MVS image", "list", 70, 20),
    "8D0220": ("% delay by MVS image", "list", 20, 15),
    "8D0520": ("% using by MVS image", "list", 60, 20),
    "8D1E00": ("% using for processor by MVS image", "list", 35, 15),
    "8D1DA0": ("% using for i/o by MVS image", "list", 25, 10),
    "8D1B90": ("% delay for processor by MVS image", "list", 8, 6),
    "8D1AD0": ("% delay for i/o by MVS image", "list", 5, 4),
    "8D1BF0": ("% delay for storage by MVS image", "list", 2, 2),
    "8D1A70": ("% delay for enqueue by MVS image", "list", 1.5, 1.5),
    "8D1B30": ("% delay for operator by MVS image", "list", 0.5, 0.5),
    "8D1F80": ("% workflow for processor by MVS image", "list", 75, 15),
    "8D1F20": ("% workflow for i/o by MVS image", "list", 80, 10),
    "8D2410": ("% CSA utilization by MVS image", "list", 40, 8),
    "8D2430": ("% ECSA utilization by MVS image", "list", 55, 8),
    "8D2470": ("% SQA utilization by MVS image", "list", 30, 6),
    "8D2450": ("% ESQA utilization by MVS image", "list", 45, 6),
    "8D1650": ("% central storage frames active by MVS image", "list", 70, 10),
    "8D1FE0": ("unreferenced interval count by MVS image", "list", 65000, 500),
    "8D0460": ("% CPU utilization (CP)", "single", 55, 25),
    "8D0E90": ("i/o activity rate", "single", 2500, 900),
    "8D1200": ("transaction ended rate", "single", 240, 120),
}


def wave(t, seed, base, amp, period=900.0):
    """Gleichmäßig schwankender Wert: Sinus je Serie + reproduzierbares Rauschen."""
    rnd = random.Random(f"{seed}-{int(t)}")
    phase = (zlib.crc32(seed.encode()) % 1000) / 1000 * 2 * math.pi
    v = base + amp * math.sin(2 * math.pi * t / period + phase) + rnd.uniform(-amp, amp) * 0.25
    return max(0.0, v)


class State:
    mintime = 100
    xml_only = False
    auth = None  # erwarteter Authorization-Header oder None

    @classmethod
    def interval_end(cls):
        return int(time.time()) // cls.mintime * cls.mintime


def dds_time(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d%H%M%S")


def perform(resource, mid):
    t = State.interval_end()
    td = {"utcStart": dds_time(t - State.mintime), "utcEnd": dds_time(t),
          "localStart": dds_time(t - State.mintime), "localEnd": dds_time(t),
          "numSamples": State.mintime, "gathererInterval": {"unit": "seconds",
                                                            "value": State.mintime}}
    _, name, rtype = (resource.split(",") + ["", "", ""])[:3]
    if mid not in METRICS:
        return {"report": [{"message": {"id": "GPM0505E", "severity": 8,
                                        "description": f"metric {mid} not supported (mock)"}}]}
    desc, fmt, base, amp = METRICS[mid]
    if fmt == "list":
        rows = [{"col": [s, f"{wave(t, mid + s, base, amp):.1f}"]} for s in SYSTEMS]
    else:
        rows = [{"col": ["", f"{wave(t, mid + resource, base, amp):.1f}"]}]
    label = resource if name else f",{SYSPLEX},{rtype or 'SYSPLEX'}"
    return {"report": [{
        "metric": {"id": mid, "description": desc, "format": fmt, "numCols": 2},
        "resource": {"reslabel": label, "restype": rtype or "SYSPLEX"},
        "timeData": td,
        "columnHeaders": {"col": [{"value": "Name", "type": "T"},
                                  {"value": "Value", "type": "N"}]},
        "row": rows,
    }]}


def to_xml(resp):
    """JSON-Struktur -> ddsml-XML (Nachbildung)."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<ddsml>"]
    for r in resp["report"]:
        out.append("<report>")
        if "message" in r:
            m = r["message"]
            out.append(f'<message id="{m["id"]}" severity="{m["severity"]}" '
                       f'description="{escape(m["description"])}"/>')
        if "metric" in r:
            m = r["metric"]
            out.append(f'<metric id="{m["id"]}" description="{escape(m["description"])}" '
                       f'format="{m["format"]}" numcols="{m["numCols"]}"/>')
            out.append(f"<resource><reslabel>{r['resource']['reslabel']}</reslabel>"
                       f"<restype>{r['resource']['restype']}</restype></resource>")
            td = r["timeData"]
            out.append("<time-data>"
                       f"<local-start>{td['localStart']}</local-start>"
                       f"<local-end>{td['localEnd']}</local-end>"
                       f"<utc-start>{td['utcStart']}</utc-start>"
                       f"<utc-end>{td['utcEnd']}</utc-end>"
                       f'<gatherer-interval unit="seconds">{State.mintime}</gatherer-interval>'
                       "</time-data>")
            for row in r["row"]:
                out.append("<row>" + "".join(f"<col>{escape(c)}</col>" for c in row["col"])
                           + "</row>")
        out.append("</report>")
    out.append("</ddsml>")
    return "\n".join(out)


def openmetrics():
    """Monitor-III-Werte im OpenMetrics-Format (Auszug, Namen wie IBMs Dashboards)."""
    t = State.interval_end()
    lines = []

    def fam(name, help_, samples):
        # wie der echte DDS: HELP vor TYPE, Zeitstempel (ms) = Ende des Intervalls
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        for labels, v in samples:
            ls = ",".join(f'{k}="{val}"' for k, val in labels.items())
            lines.append(f"{name}{{{ls}}} {v:.10g} {t * 1000}")

    pl = {"sysplex": SYSPLEX}
    # Der echte DDS meldet auch Familien ohne Werte (z. B. ohne Volume-Daten)
    fam("mvs_volume_io_percent", "I/O Percent", [])
    fam("mvs_rmf_m3_mintime", "Monitor III MINTIME in seconds",
        [(pl, State.mintime)])
    frames = 4 * 1024 * 1024  # 16 GB realer Speicher je System
    fam("mvs_system_storage_real_storage_frames", "Real storage frames",
        [({**pl, "sysname": s}, frames) for s in SYSTEMS])
    fam("mvs_system_storage_frames_available_total", "Available frames",
        [({**pl, "sysname": s}, frames * wave(t, "avail" + s, 0.35, 0.1)) for s in SYSTEMS])
    for area, base in (("csa", 40), ("ecsa", 55), ("sqa", 30), ("esqa", 45)):
        fam(f"mvs_system_storage_{area}_average_percent", f"{area.upper()} utilization %",
            [({**pl, "sysname": s}, wave(t, area + s, base, 10)) for s in SYSTEMS])
    for kind, base in (("cp", 3), ("iip", 2), ("total", 5)):
        fam(f"mvs_job_{kind}_eappl_percent", f"Job {kind.upper()} EAppl %",
            [({**pl, "sysname": s, "job_name": j, "asid_dec": str(a)},
              wave(t, kind + s + j, base * (1 + a % 7), base)) for s in SYSTEMS
             for j, a in JOBS])
    fam("mvs_job_working_set_frames_total", "Job working set frames",
        [({**pl, "sysname": s, "job_name": j, "asid_dec": str(a)},
          wave(t, "ws" + s + j, 20000 + 3000 * (a % 9), 4000)) for s in SYSTEMS
         for j, a in JOBS])
    delays = ["dev", "enq", "hsm", "jes", "oper", "proc", "stor", "subs", "xcf"]
    fam("mvs_wlm_group_delay_percent", "WLM group total delay %",
        [({**pl, "sysname": s, "group_name": g, "group_type": "W"},
          wave(t, "dly" + s + g, 25, 15)) for s in SYSTEMS for g in WLM_GROUPS])
    for d in delays:
        fam(f"mvs_wlm_group_delay_{d}_percent", f"WLM group {d.upper()} delay %",
            [({**pl, "sysname": s, "group_name": g, "group_type": "W"},
              wave(t, d + s + g, 3, 3)) for s in SYSTEMS for g in WLM_GROUPS])
    cpc = {"cpc_id": CPC, "cpc_sequence_number": "0000000ABCDE"}
    fam("mvs_cpc_capacity_msu_h", "CPC capacity MSU/h", [({**pl, **cpc}, 1500)])
    fam("mvs_lpar_actual_msu_h", "LPAR actual MSU/h",
        [({**pl, **cpc, "lpar_name": f"LP{s[-1]}", "line_type": "CP"},
          wave(t, "msu" + s, 300, 120)) for s in SYSTEMS])
    fam("mvs_cf_processor_utilization_percent", "CF processor utilization %",
        [({**pl, "name": c}, wave(t, "cfp" + c, 30, 15)) for c in CFS])
    fam("mvs_cf_request_rate", "CF request rate",
        [({**pl, "name": c}, wave(t, "cfr" + c, 12000, 4000)) for c in CFS])
    size = 64 * 2**30
    fam("mvs_cf_storage_size_bytes", "CF storage size", [({**pl, "name": c}, size) for c in CFS])
    fam("mvs_cf_storage_available_bytes", "CF storage available",
        [({**pl, "name": c}, size * wave(t, "cfa" + c, 0.4, 0.1)) for c in CFS])
    fam("mvs_cf_storage_utilized_percent", "CF storage utilized %",
        [({**pl, "name": c}, 100 * (1 - wave(t, "cfa" + c, 0.4, 0.1))) for c in CFS])
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if State.auth and self.headers.get("Authorization") != State.auth:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="DDS"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/metrics/m3":
            return self._send(200, "text/plain;charset=UTF-8", openmetrics())
        if u.path == "/gpm/perform" and not State.xml_only:
            resp = perform(q.get("resource", ",,SYSPLEX"), q.get("id", ""))
            return self._send(200, "application/json", json.dumps(resp))
        if u.path == "/gpm/perform.xml":
            resp = perform(q.get("resource", ",,SYSPLEX"), q.get("id", ""))
            return self._send(200, "text/xml", to_xml(resp))
        self._send(404, "text/plain", "not found\n")

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8803)
    ap.add_argument("--mintime", type=int, default=100)
    ap.add_argument("--xml-only", action="store_true", help="wie ein älterer DDS ohne JSON")
    ap.add_argument("--auth", metavar="USER:PASS", help="Basic-Auth verlangen (sonst 401)")
    a = ap.parse_args()
    State.mintime, State.xml_only = a.mintime, a.xml_only
    if a.auth:
        State.auth = "Basic " + base64.b64encode(a.auth.encode()).decode()
    print(f"mock DDS auf Port {a.port}, mintime {a.mintime}s", flush=True)
    ThreadingHTTPServer(("", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
