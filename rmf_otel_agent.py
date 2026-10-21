#!/usr/bin/env python3
"""RMF -> OpenTelemetry: liest Monitor-III-Werte aus dem RMF Data Portal (DDS, GPMSERVE)
und schickt sie als OTLP-Metriken (HTTP/JSON) an einen OpenTelemetry Collector.

Nur Python-Standardbibliothek, damit der Agent sowohl auf einem beliebigen Linux-/Windows-
Rechner als auch direkt unter z/OS UNIX (IBM Open Enterprise SDK for Python) läuft.

    rmf_otel_agent.py --config agent.json            # Dauerbetrieb (alle mintime Sekunden)
    rmf_otel_agent.py --config agent.json --once     # ein Durchlauf
    rmf_otel_agent.py --config agent.json --once --dry-run   # OTLP-JSON nur ausgeben

DDS-Schnittstelle: GET <dds>/gpm/perform?resource=<res>&id=<metrik-id>
  (neuere DDS liefern JSON bei "Accept: application/json", ältere nur
  /gpm/perform.xml als XML; der Agent probiert beides wie IBMs Grafana-Plugin).
Antwort: report[].metric{description,format}, report[].resource{reslabel},
  report[].timeData{utcEnd}, report[].row[].col = [Name, Wert].

Kennwort nie in die Konfiguration schreiben: Umgebungsvariable RMF_DDS_PASSWORD.
"""

import argparse
import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

AGENT_NAME = "rmf-otel-agent"
AGENT_VERSION = "0.1.0"

# Zeilen bzw. Werte ohne verwertbare Zahl (DDS-Platzhalter)
NO_DATA_NAMES = {"*NoData*"}


# --------------------------------------------------------------------------- DDS


class DDSError(Exception):
    pass


class DDSClient:
    def __init__(self, url, user=None, password=None, insecure=False, timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        if user:
            token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
            self.headers["Authorization"] = "Basic " + token
        self.ctx = ssl.create_default_context()
        if insecure:  # selbstsigniertes AT-TLS-Zertifikat
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.use_xml = False  # wird beim ersten Abruf ermittelt

    def _get(self, path, params):
        full = f"{self.url}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full, headers=self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
            ctype = r.headers.get("Content-Type", "")
            return ctype, r.read()

    def perform(self, resource, metric_id):
        params = {"resource": resource, "id": metric_id}
        for attempt in range(2):
            path = "/gpm/perform.xml" if self.use_xml else "/gpm/perform"
            try:
                ctype, body = self._get(path, params)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise DDSError("DDS: nicht berechtigt (Benutzer/Kennwort prüfen)") from e
                if attempt == 0 and e.code in (404, 406):
                    self.use_xml = not self.use_xml
                    continue
                raise DDSError(f"DDS: HTTP {e.code} für {path}") from e
            if "json" in ctype:
                return json.loads(body.decode("utf-8"))
            if "xml" in ctype or body.lstrip().startswith(b"<"):
                return xml_to_response(body)
            if attempt == 0:
                self.use_xml = not self.use_xml
                continue
            raise DDSError(f"DDS: unerwarteter Inhaltstyp {ctype!r}")
        raise DDSError("DDS: keine verwertbare Antwort")


def _camel(tag):
    """utc-end -> utcEnd (XML-Elementnamen auf die JSON-Schlüssel abbilden)."""
    parts = tag.split("-")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def xml_to_response(body):
    """DDS-XML (ddsml) in dieselbe Struktur wie die JSON-Antwort umsetzen.

    Allgemein gehalten: Attribute und einfache Kindelemente werden zu Schlüsseln,
    <row><col>..</col></row> zu {"col": [...]}. Gegen einen echten DDS im XML-Modus
    noch nicht geprüft."""
    root = ET.fromstring(body)

    def conv(el):
        d = {_camel(k): v for k, v in el.attrib.items()}
        children = list(el)
        if not children:
            return el.text.strip() if el.text and el.text.strip() and not d else d or (el.text or "")
        for ch in children:
            key = _camel(ch.tag)
            if key == "row":
                d.setdefault("row", []).append({"col": [(c.text or "").strip() for c in ch]})
            else:
                d[key] = conv(ch)
        return d

    reports = [conv(r) for r in root.iter("report")]
    for r in reports:
        r.setdefault("row", [])
    return {"report": reports}


# --------------------------------------------------------------------------- Umsetzung


def parse_dds_time(s):
    """DDS-Zeitstempel JJJJMMTThhmmss (UTC) -> Nanosekunden seit 1970."""
    dt = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1_000_000_000


def parse_value(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None  # "Deact", "" ...
    return None if v != v else v  # NaN


def otel_name(description):
    """'% CPU utilization (CP) by MVS image' -> 'rmf.cpu_utilization_cp_by_mvs_image'."""
    s = description.lower().replace("%", "pct").replace("#", "num")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return "rmf." + s


def reslabel_parts(reslabel):
    """',SYSA,MVS_IMAGE' -> ('', 'SYSA', 'MVS_IMAGE'); ',,SYSPLEX' -> ('', '', 'SYSPLEX')."""
    p = (reslabel or "").split(",")
    while len(p) < 3:
        p.append("")
    return p[0], p[1], p[2]


def attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def report_to_metric(report, mdef, now_ns):
    """Ein DDS-Report -> eine OTLP-Gauge-Metrik (oder None ohne Datenpunkte).

    mdef: Eintrag aus der Konfiguration (id, resource, name?, unit?, row_attribute?)."""
    if "message" in report and not report.get("row"):
        msg = report["message"]
        raise DDSError(f"{msg.get('id', '?')}: {msg.get('description', '')}".strip())
    metric = report.get("metric") or {}
    desc = (metric.get("description") or mdef.get("description") or mdef["id"]).strip()
    name = mdef.get("name") or otel_name(desc)
    td = report.get("timeData") or {}
    ts = parse_dds_time(td["utcEnd"]) if td.get("utcEnd") else now_ns
    _, res_name, res_type = reslabel_parts((report.get("resource") or {}).get("reslabel")
                                           or mdef["resource"])
    common = [attr("rmf.metric.id", mdef["id"])]
    if res_type:
        common.append(attr("rmf.resource.type", res_type))
    if res_name:
        common.append(attr("rmf.resource.name", res_name))
        if res_type == "SYSPLEX":
            common.append(attr("zos.sysplex.name", res_name))
    row_key = mdef.get("row_attribute", "rmf.row")
    points = []
    for row in report.get("row") or []:
        cols = row.get("col") or []
        if len(cols) < 2 or cols[0] in NO_DATA_NAMES:
            continue
        v = parse_value(cols[1])
        if v is None:
            continue
        attrs = list(common)
        if metric.get("format") == "list" or cols[0]:
            attrs.append(attr(row_key, cols[0].strip()))
        points.append({"attributes": attrs, "timeUnixNano": str(ts), "asDouble": v})
    if not points:
        return None
    return {
        "name": name,
        "description": desc,
        "unit": mdef.get("unit", "%" if desc.startswith("%") else "1"),
        "gauge": {"dataPoints": points},
    }, ts


def build_otlp(metrics, resource_attrs):
    return {
        "resourceMetrics": [{
            "resource": {"attributes": [attr(k, v) for k, v in resource_attrs.items()]},
            "scopeMetrics": [{
                "scope": {"name": AGENT_NAME, "version": AGENT_VERSION},
                "metrics": metrics,
            }],
        }]
    }


# --------------------------------------------------------------------------- OTLP


def send_otlp(endpoint, payload, headers=None, timeout=30):
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/metrics"):
        url += "/v1/metrics"
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


# --------------------------------------------------------------------------- Ablauf


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    dds = cfg.setdefault("dds", {})
    dds["url"] = os.environ.get("RMF_DDS_URL", dds.get("url", "http://localhost:8803"))
    dds["user"] = os.environ.get("RMF_DDS_USER", dds.get("user"))
    dds["password"] = os.environ.get("RMF_DDS_PASSWORD", dds.get("password"))
    otlp = cfg.setdefault("otlp", {})
    otlp["endpoint"] = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT",
                                      otlp.get("endpoint", "http://localhost:4318"))
    cfg["interval"] = int(os.environ.get("RMF_INTERVAL", cfg.get("interval", 100)))
    cfg.setdefault("resource", {})
    if not cfg.get("metrics"):
        raise SystemExit("Konfiguration: Liste 'metrics' fehlt oder ist leer")
    return cfg


def collect(client, cfg, last_ts, log):
    """Alle konfigurierten Metriken abfragen; nur neue Monitor-III-Intervalle melden."""
    out = []
    now_ns = time.time_ns()
    for mdef in cfg["metrics"]:
        key = (mdef["resource"], mdef["id"])
        try:
            resp = client.perform(mdef["resource"], mdef["id"])
            for report in resp.get("report") or []:
                r = report_to_metric(report, mdef, now_ns)
                if r is None:
                    continue
                metric, ts = r
                if last_ts.get(key) == ts:
                    continue  # DDS liefert noch dasselbe Intervall
                last_ts[key] = ts
                out.append(metric)
        except (DDSError, urllib.error.URLError, OSError, ValueError, KeyError) as e:
            log(f"Fehler bei {mdef['id']} {mdef['resource']}: {e}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--once", action="store_true", help="nur ein Durchlauf")
    ap.add_argument("--dry-run", action="store_true", help="OTLP-JSON ausgeben statt senden")
    args = ap.parse_args(argv)

    def log(msg):
        print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", file=sys.stderr,
              flush=True)

    cfg = load_config(args.config)
    d = cfg["dds"]
    client = DDSClient(d["url"], d.get("user"), d.get("password"), d.get("insecure", False))
    res_attrs = {"service.name": AGENT_NAME, "service.version": AGENT_VERSION,
                 "os.type": "zos"}
    res_attrs.update(cfg["resource"])
    last_ts = {}
    log(f"DDS {d['url']} -> OTLP {cfg['otlp']['endpoint']}, "
        f"{len(cfg['metrics'])} Metriken, Intervall {cfg['interval']}s")
    while True:
        t0 = time.monotonic()
        metrics = collect(client, cfg, last_ts, log)
        if metrics:
            payload = build_otlp(metrics, res_attrs)
            if args.dry_run:
                json.dump(payload, sys.stdout, indent=1)
                print()
            else:
                try:
                    send_otlp(cfg["otlp"]["endpoint"], payload, cfg["otlp"].get("headers"))
                    n = sum(len(m["gauge"]["dataPoints"]) for m in metrics)
                    log(f"{len(metrics)} Metriken, {n} Datenpunkte gesendet")
                except (urllib.error.URLError, OSError) as e:
                    log(f"OTLP-Senden fehlgeschlagen: {e}")
        if args.once:
            return 0
        time.sleep(max(1.0, cfg["interval"] - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
