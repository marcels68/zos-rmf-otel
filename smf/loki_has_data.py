#!/usr/bin/env python3
"""Exit 0, wenn Loki für service_name=<NAME> in den letzten 6 Stunden Einträge hat
(für den Demo-Dienst: Rückwirkend-Import nur einmal). Aufruf: loki_has_data.py URL NAME"""
import json
import sys
import time
import urllib.parse
import urllib.request

url, name = sys.argv[1].rstrip("/"), sys.argv[2]
q = urllib.parse.urlencode({"query": f'{{service_name="{name}"}}', "limit": 1,
                            "start": str((int(time.time()) - 6 * 3600) * 10**9)})
for _ in range(30):
    try:
        with urllib.request.urlopen(f"{url}/loki/api/v1/query_range?{q}", timeout=10) as r:
            sys.exit(0 if json.load(r)["data"]["result"] else 1)
    except OSError:
        time.sleep(2)
sys.exit(1)
