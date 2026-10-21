#!/bin/sh
# Holt IBMs Grafana-Beispieldashboards für den DDS-Endpunkt /metrics/m3 (Apache-2.0,
# github.com/IBM/RMF, grafana/dashboards/prometheus) nach grafana/dashboards/ibm/.
# Grafana lädt sie beim nächsten Scan (10 s) in den Ordner "z/OS".
set -eu
cd "$(dirname "$0")"
base=https://raw.githubusercontent.com/IBM/RMF/main/grafana/dashboards/prometheus
mkdir -p grafana/dashboards/ibm
for f in zos-central-storage zos-common-storage zos-coupling-facility zos-msu-capacity \
         zos-usage-by-job zos-wlm-delays; do
  curl -fsSL "$base/$f.json" -o "grafana/dashboards/ibm/$f.json"
  echo "grafana/dashboards/ibm/$f.json"
done
