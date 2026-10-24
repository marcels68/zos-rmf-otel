#!/usr/bin/env python3
"""Gleichheitstest: SMF-Auswertung in Java (zsmf-otel) und Python (smf/smf_otel.py) müssen
für dieselben Sätze dieselben Logs liefern (Zeit, Text, alle Attribute mit Typ).

    python3 java/parity.py [--minutes 120] [--seed 5]
Braucht ein übersetztes java/build/classes (java/build.sh).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def records(text):
    """Alle OTLP-JSON-Dokumente in einer Ausgabe -> Liste (Zeit, Text, Attribute)."""
    dec, pos, out = json.JSONDecoder(), 0, []
    text = text.strip()
    while pos < len(text):
        doc, end = dec.raw_decode(text, pos)
        pos = end
        while pos < len(text) and text[pos].isspace():
            pos += 1
        for rl in doc["resourceLogs"]:
            svc = rl["resource"]["attributes"][0]["value"]["stringValue"]
            for sl in rl["scopeLogs"]:
                for r in sl["logRecords"]:
                    attrs = {a["key"]: a["value"] for a in r["attributes"]}
                    out.append((svc, r["timeUnixNano"], r["body"]["stringValue"],
                                r["severityText"], json.dumps(attrs, sort_keys=True)))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=120)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--tz", default="Europe/Berlin")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as d:
        dump = os.path.join(d, "smf.bin")
        subprocess.run([sys.executable, os.path.join(ROOT, "smf", "smf_gen.py"), dump,
                        "--minutes", str(a.minutes), "--seed", str(a.seed),
                        "--end", "2026-10-21T12:00"], check=True, capture_output=True)
        py = subprocess.run([sys.executable, os.path.join(ROOT, "smf", "smf_otel.py"), dump,
                             "--dry-run", "--tz", a.tz], check=True, capture_output=True,
                            text=True).stdout
        jv = subprocess.run(["java", "-cp", os.path.join(HERE, "build", "classes"),
                             "zsmf.Main", "--source", "file:" + dump, "--dry-run",
                             "--tz", a.tz], check=True, capture_output=True,
                            text=True).stdout
    p, j = records(py), records(jv)
    print(f"Python: {len(p)} Sätze, Java: {len(j)} Sätze")
    diff = [(x, y) for x, y in zip(p, j) if x != y]
    if len(p) != len(j) or diff:
        for x, y in diff[:5]:
            print("PY  ", x)
            print("JAVA", y)
            for k in ("svc", "zeit", "text", "sev"):
                pass
        print(f"UNTERSCHIEDE: {len(diff)} (+ Anzahl {len(p) - len(j)})")
        return 1
    print("gleich")
    return 0


if __name__ == "__main__":
    sys.exit(main())
