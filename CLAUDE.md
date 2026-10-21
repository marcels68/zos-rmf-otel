# Projektkontext für Claude Code

z/OS-Leistungsdaten aus dem RMF Data Portal (DDS, GPMSERVE) als OpenTelemetry-Metriken,
Anzeige in Grafana. Entstanden am 21.10.2026 als Prototyp (zuerst in zos-cobol-llvm/otel,
dann eigenes Repo, weil es mit GnuCOBOL nichts zu tun hat).

## Stand
- Weg A: `rmf_otel_agent.py` (nur Python-Stdlib, auch unter z/OS UNIX) fragt `/gpm/perform`
  (JSON; Rückfall `/gpm/perform.xml`) ab und sendet OTLP/HTTP. Weg B: Collector scrapt DDS
  `/metrics/m3` (OpenMetrics, APAR OA67701; Inhalt über DDS-Konfiguration GPMOMC).
- DDS-JSON-Format und Endpunkte aus IBMs RMF-Grafana-Plugin (github.com/IBM/RMF,
  grafana/rmf-app/pkg/plugin/dds); XML-Format nachgebildet. Metriknamen von /metrics/m3 aus
  IBMs Beispieldashboards (grafana/dashboards/prometheus).
- Demo `docker compose up -d`: mock_dds.py -> Collector 0.139 -> Prometheus 3.7 (OTLP-Empfang)
  -> Grafana 12.2. IBM-Dashboards (`fetch-ibm-dashboards.sh`) laufen unverändert.
- Tests: `python3 -m unittest -v test_agent.py`.
- Echter DDS (21.10.2026, Nutzer per curl): `/metrics/m3` über HTTPS mit Basic-Auth ok,
  `text/plain`, Zeitstempel je Wert (ms), leere Familien, MINTIME 60. Die Cloud-Sitzung erreicht
  Port 8803 dort NICHT (auch nach Freigabe; vermutlich Firewall) und der Sandbox-Proxy schickt
  keine Basic-Auth über HTTP -> Tests gegen echte Systeme lokal beim Nutzer.
- Echtbetrieb: `docker-compose.real.yaml` + `.env` (Vorlage `.env.example`, nie einchecken);
  Probelauf gegen `mock_dds.py --auth` ok; Weg A und B beim Nutzer lokal am echten DDS
  bestätigt (21.10.2026). Host-Namen echter Systeme nicht ins Repo.
- **Ausbau (21.10.2026, Zweig `smf`):** 16 weitere Agent-Metriken (Using/Delay je Ursache,
  CSA/ECSA/SQA/ESQA, Central Storage, UIC; IDs aus IBM-Katalog, am echten DDS unbestätigt).
  SMF: `smf/smf_otel.py` liest Dumps (RDW, VBS-Segmente, optional BDW), wertet 30.4/30.5,
  80, 119.11/119.12 aus und sendet OTLP-Logs -> Collector -> Loki 3.5. Layouts von
  pacsys.com/smf (IBM-Tabellen); zIIP-Feld SMF 30 aus adapya-base (pacsys-SMF30 = V1R3, dort
  Tippfehler bei Offset 80-84). `smf/smf_gen.py` erzeugt Testsätze, `smf/test_smf.py` (18
  Tests, auch Handsätze nach Offsets). Dashboards per `grafana/build_dashboards.py` (5 Stück).
  Grafana-Eigenheit: Loki-Momentabfragen kommen als EINE Tabelle -> Pie/Bargauge brauchen
  `reduceOptions.values=true`, Wertspalte heißt "Value #A". Loki: gleiche Zeile + gleicher
  Zeitstempel = Duplikat (deshalb System-ID in jeder Zeile), `max_chunk_age: 48h` für alte
  Dumps. SMF noch nicht mit echten Sätzen getestet.

## Arbeitsweise
- Nutzer schreibt Deutsch; Antworten auf Deutsch.
- Host, User-ID und Kennwörter nie ins Repo schreiben; nur Platzhalter (`ddshost.example.com`,
  `DDSUSER`), Kennwort per `RMF_DDS_PASSWORD`.
- JCL-JOB-Karten immer mit `REGION=0M,LINES=500000`.
