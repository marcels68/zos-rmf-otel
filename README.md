# z/OS-Leistungs- und SMF-Daten in OpenTelemetry

Prototyp: z/OS-Daten herstellerneutral über **OpenTelemetry** bereitstellen und in Grafana
auswerten:

- **RMF Monitor III** aus dem **RMF Data Portal / Distributed Data Server (DDS)** als Metriken
  (CPU, Workflow/Delay je Ursache, Speicher, Jobs, WLM, Coupling Facility, MSU).
- **SMF-Sätze** als Logs: **SMF 30** (Jobs/Steps: CPU, zIIP, Laufzeit, Return- und
  Abend-Codes), **SMF 80** (RACF: Logons, Fehlversuche, Zugriffsverletzungen) und
  **SMF 119 zERT** (Verschlüsselung jeder TCP/IP-Verbindung: TLS-Version, Cipher, ungeschützte
  Verbindungen).

Lokal komplett lauffähig mit simuliertem DDS und erfundenen SMF-Sätzen; für ein echtes System
nur Zugang eintragen bzw. einen SMF-Dump einspielen.

![Dashboard z/OS Übersicht mit simulierten Daten](doc/dashboard.png)

## OpenTelemetry in fünf Sätzen

- **OpenTelemetry (OTel)** ist ein herstellerneutraler Standard (CNCF) dafür, wie Programme und
  Systeme ihre Messdaten abliefern: **Metriken** (Zahlen über die Zeit), **Traces**
  (Weg einer Anfrage durch mehrere Systeme) und **Logs**.
- Das Übertragungsprotokoll heißt **OTLP** (HTTP oder gRPC). Jedes moderne Monitoring-Produkt
  nimmt OTLP an: Prometheus, Grafana, Dynatrace, Datadog, Splunk, Elastic, New Relic, Instana ...
- Zentrales Bauteil ist der **OpenTelemetry Collector**: Er nimmt Daten an (receiver),
  bereitet sie auf (processor) und verteilt sie an beliebige Ziele (exporter).
- Wer einmal OTLP liefert, muss sich nicht mehr für ein Monitoring-Produkt entscheiden –
  das Ziel ist eine Zeile in der Collector-Konfiguration.
- Für den Mainframe heißt das: z/OS erscheint in denselben Dashboards und Alarmen wie die
  verteilte Welt (Kubernetes, Cloud, Datenbanken), statt in einer eigenen Insel.

## Architektur

```
 z/OS                                        Linux/Windows/Cloud (oder z/OS UNIX)
 ─────────────────────────────               ────────────────────────────────────────────────
 RMF Monitor III                                                       ┌─▶ Prometheus ─▶ Grafana
   │                                         Weg A                     │
 DDS (GPMSERVE, Port 8803)  ── /gpm/perform ──▶ rmf_otel_agent.py ─OTLP─┤
   │                                                                   │   OpenTelemetry
   └──────────────────────── /metrics/m3 ────────────────────────────▶ Collector ─▶ Dynatrace,
                             (OpenMetrics,   Weg B: Collector scrapt     │            Splunk, ...
                              APAR OA67701)  direkt (keine eigene SW)    └─▶ (beliebig viele Ziele)
```

**Weg B – ohne eigene Software (empfohlen, wenn verfügbar).** Seit APAR **OA67701**
(RMF 3.1 und 3.2) bietet der DDS den Endpunkt `/metrics/m3` im OpenMetrics-Format. Der
OpenTelemetry Collector holt ihn mit seinem `prometheus`-Receiver ab. Welche Werte erscheinen,
steuert die DDS-Konfiguration **GPMOMC** (oder Parameter `groups` am Endpunkt).
Metriknamen wie `mvs_cf_processor_utilization_percent`, `mvs_job_total_eappl_percent`,
`mvs_wlm_group_delay_percent` (Labels `sysplex`, `sysname`, `job_name` ...).

**Weg A – Agent `rmf_otel_agent.py`.** Für Systeme ohne OA67701 oder für Monitor-III-Werte,
die nicht in GPMOMC stehen: Der Agent fragt beliebige Monitor-III-Metriken über die klassische
DDS-Schnittstelle ab (`/gpm/perform?resource=,,SYSPLEX&id=8D0450`, JSON oder XML) und schickt
sie per OTLP. Er braucht nur die Python-Standardbibliothek und läuft damit auch direkt unter
**z/OS UNIX** (IBM Open Enterprise SDK for Python) – dann ist z/OS selbst der Absender.

Beide Wege lassen sich kombinieren; das Demo macht genau das.

## Demo starten (ohne z/OS)

Voraussetzung: Docker mit `docker compose`.

```sh
docker compose up -d
./fetch-ibm-dashboards.sh      # optional: IBMs RMF-Beispieldashboards dazu
```

Dann <http://localhost:3000> öffnen (anonymer Zugang, Ordner **z/OS**, Menü **z/OS** oben
rechts wechselt zwischen den Dashboards):

| Dashboard | Inhalt |
|---|---|
| **z/OS Übersicht** | Kennzahlen aus RMF und SMF auf einen Blick |
| **z/OS RMF Detail** | Using/Delay je Ursache und System, Common Storage, realer Speicher, UIC, Top-Jobs (CPU, zIIP, Working Set), WLM-Delays je Dienstklasse und Ursache, Coupling Facility, MSU je LPAR, CPC-Auslastung |
| **z/OS Jobs (SMF 30)** | beendete Jobs/Steps, Abends, RC > 0, CPU/zIIP, Top-Jobs/-Benutzer/-Programme, Laufzeiten, Jobklassen, Completion-Codes, Abend-Tabelle, Einzelsätze |
| **z/OS Sicherheit (RACF, SMF 80)** | Logons erfolgreich/fehlgeschlagen, Gründe (Kennwort, gesperrt, unbekannt), Kennwort-Raten (≥ 5 Fehlversuche in 10 min), Fehlversuche je Benutzer/Terminal, Zugriffsverletzungen, Einzelsätze |
| **z/OS Verschlüsselung (zERT, SMF 119)** | Anteil geschützter Verbindungen, ungeschützt/schwach, TLS-Versionen, Cipher-Suites, Verfahren, Schlüsselaustausch, Tabellen mit Handlungsbedarf (Job, Port, Client), Einzelsätze |

Bilder (simulierte Daten): [RMF Detail](doc/rmf-detail.png), [Jobs](doc/smf-jobs.png),
[RACF](doc/smf-racf.png), [zERT](doc/smf-zert.png).

- **IBM-Dashboards** (nach `fetch-ibm-dashboards.sh`): z/OS Central/Common Storage,
  Coupling Facility, MSU Capacity, Usage by Job, WLM Workload Delays. Sie laufen unverändert,
  weil die Metriknamen durch den Collector erhalten bleiben.

Bausteine im Compose:

| Dienst | Aufgabe |
|---|---|
| `mock-dds` | simulierter DDS (`mock_dds.py`): Sysplex PLEX1, Systeme SYSA/SYSB/SYSC, erfundene Werte |
| `rmf-agent` | `rmf_otel_agent.py` mit `demo/agent.demo.json` (Weg A) |
| `smf-demo` | erzeugt erfundene SMF-Sätze (`smf/smf_gen.py`, zuerst 6 h rückwirkend, dann jede Minute) und spielt sie mit `smf/smf_otel.py` ein |
| `otel-collector` | OpenTelemetry Collector contrib 0.139: OTLP-Empfang (Metriken und Logs) + Scrape `/metrics/m3` (Weg B) |
| `prometheus` | Prometheus 3.7, nimmt OTLP direkt an (`--web.enable-otlp-receiver`) |
| `loki` | Grafana Loki 3.5 für die SMF-Sätze (OTLP-Empfang unter `/otlp`) |
| `grafana` | Grafana 12.2 mit vorkonfigurierten Datenquellen und Dashboards |

Prüfen ohne Browser: `curl 'localhost:9090/api/v1/query?query=rmf_cpu_utilization_percent'`.
Beenden: `docker compose down`.

## An ein echtes z/OS anschließen

1. **DDS erreichbar?** Mit AT-TLS `https`, sonst `http`; ohne DDS-Anmeldung `-u` weglassen.
   ```sh
   curl -u DDSUSER https://<dds>:8803/metrics/m3 | head -20
   curl -u DDSUSER 'https://<dds>:8803/gpm/perform?resource=,,SYSPLEX&id=8D0D60'
   ```
   `/metrics/m3` beginnt mit `mvs_rmf_m3_mintime{sysplex="..."} 60 <Zeitstempel>`; die Zahl
   ist die Monitor-III-MINTIME in Sekunden.
2. **Zugang eintragen:** `cp .env.example .env` und `RMF_DDS_HOST` (`host:8803`),
   `RMF_DDS_SCHEME`, `RMF_DDS_USER`, `RMF_DDS_PASSWORD` sowie `RMF_INTERVAL` /
   `RMF_SCRAPE_INTERVAL` (= MINTIME) setzen. `.env` wird nicht eingecheckt.
3. **Starten** – Weg A und B gegen den echten DDS, ohne Simulator:
   ```sh
   docker compose -f docker-compose.yaml -f docker-compose.real.yaml up -d
   ```
   Erste Werte nach ein bis zwei MINTIME-Intervallen in Grafana (<http://localhost:3000>).
   Prüfen: `docker compose logs otel-collector | tail` (Fehler beim Abholen von `/metrics/m3`),
   `docker compose logs rmf-agent | tail` (Fehler je Metrik-ID), in Prometheus
   (<http://localhost:9090>) die Abfrage `mvs_rmf_m3_mintime`.
4. **Nur der Agent, ohne Docker:**
   ```sh
   export RMF_DDS_URL=https://<dds>:8803 RMF_DDS_USER=DDSUSER RMF_DDS_PASSWORD=...
   python3 rmf_otel_agent.py --config agent.example.json --once --dry-run   # Probe
   ```
   Metrik-IDs am eigenen DDS prüfen: `https://<dds>:8803/gpm/index` bzw. Metrikbrowser des
   IBM-RMF-Grafana-Plugins.
5. **Weitere Ziele** (Dynatrace, Splunk, Elastic, Grafana Cloud ...) als `otlphttp/...`-Exporter
   in `collector/otel-collector-real.yaml` eintragen – an z/OS ändert sich nichts.

Beobachtet an einem echten DDS (RMF mit OA67701): `/metrics/m3` kommt als
`text/plain` (Prometheus-Textformat), jeder Wert trägt den Zeitstempel des Monitor-III-
Intervalls, und Familien ohne Werte (z. B. Volumes) erscheinen nur mit `# HELP`/`# TYPE`.
Der Simulator bildet das nach.

### Agent direkt unter z/OS UNIX

```sh
# Python (IBM Open Enterprise SDK for Python) im PATH; Dateien als UTF-8/ASCII markieren
chtag -tc 819 rmf_otel_agent.py agent.json
export RMF_DDS_PASSWORD=...           # oder DDS ohne Anmeldung auf localhost
python3 rmf_otel_agent.py --config agent.json
```

Als Dauerläufer z. B. per BPXBATCH-Started-Task. Der DDS ist dann `http://localhost:8803`;
der OTLP-Endpunkt ist der Collector außerhalb (Firewall: ausgehend TCP 4318).

## SMF-Sätze auswerten

`smf/smf_otel.py` liest einen SMF-Dump und schickt jeden ausgewerteten Satz als
OpenTelemetry-Log an den Collector; der leitet ihn an Loki weiter. Jedes Feld wird ein
Attribut (z. B. `zos.job.name`, `smf30.cpu.total_seconds`, `racf.result`,
`zert.tls.version`), in Loki als strukturierte Metadaten abfragbar. Nur
Python-Standardbibliothek.

| SMF | ausgewertet |
|---|---|
| 30 Subtyp 4/5 (Step-/Job-Ende) | Job, Job-ID, Step, Programm, Klasse, RACF-Benutzer/-Gruppe, System, Sysplex, CPU TCB/SRB/zIIP, Laufzeit, Completion-Code (`RC0008`, `S0C4`, `U4038`, `FLUSH`), Abend-Reason |
| 80 (RACF) | Ereignis (`JOBINIT` = RACINIT: TSO-Logon/Jobstart/Anwendung, `ACCESS`, Befehle wie `ALTUSER`), Ergebnis und Grund, Benutzer, Gruppe, Terminal, Job, Verstoß/Warnung, Relocate-Abschnitte (roh) |
| 119 Subtyp 12 (zERT-Summary) | Schutz (TLS/SSH/IPsec/keiner), Job, Benutzer, Server-/Client-Adresse, Port, Rolle, neue/kurze/aktive Verbindungen, Bytes; TLS: Version, Cipher, Verschlüsselung, MAC, Schlüsselaustausch, Serverschlüssel; SSH: Version, Verfahren; IPsec: IKE-Version, Verfahren |
| 119 Subtyp 11 (zERT-Verbindung) | Ereignis (Aufbau, Ende, kurze Verbindung), Schutz, Job, Benutzer, lokale/entfernte Adresse und Port, Bytes, TLS-Version und Cipher |

Einstufung `zert.protection`: **unprotected** (keine Verschlüsselung erkannt), **weak**
(SSLv2/v3, TLS 1.0/1.1 oder DES, 3DES, RC2, RC4, keine Verschlüsselung), sonst **strong**.

### Echte SMF-Daten einspielen

1. **Sätze herausziehen:** `smf/jcl/SMFDUMP.jcl` anpassen (Log Stream mit `IFASMFDL` oder
   SYS1.MANx mit `IFASMFDP`) und abschicken. Ausgewählt werden nur 30(4,5), 80 und
   119(11,12). zERT muss in TCP/IP aktiv sein (`GLOBALCONFIG ZERT AGGREGATION`,
   `SMFCONFIG TYPE119 ZERTSUMMARY ZERTDETAIL`).
2. **Binär mit RDW holen** – sonst fehlen die Satzlängen:
   ```
   ftp> binary
   ftp> quote site rdw
   ftp> get 'USERID.SMF.OTEL' smf.bin
   ```
3. **Prüfen und einspielen** (Collector aus dem Compose lauscht auf `localhost:4318`):
   ```sh
   python3 smf/smf_otel.py smf.bin --print --tz Europe/Berlin | head   # lesbar?
   python3 smf/smf_otel.py smf.bin --tz Europe/Berlin                   # nach Loki
   ```
   `--tz` ist die Zeitzone, in der das System SMF-Zeitstempel schreibt (meist Ortszeit).
   Loki nimmt je Datenstrom Sätze bis 24 Stunden hinter dem neuesten an
   (`loki/loki.yaml`, `max_chunk_age`); ältere Dumps zuerst bzw. in zeitlicher Reihenfolge
   einspielen. Satzlayout prüfen: `smf_otel.py smf.bin --hexdump 80`.

### SMF direkt auf z/OS lesen (Java, ohne Dump)

`java/` enthält **zsmf-otel**: einen Java-Dienst für z/OS, der SMF-Sätze **live** über das
SMF Real-Time Interface (`IFAMCON`/`IFAMGET`, In-Memory-Ressource) oder zum **Nachladen** aus
einem SMF-Log-Stream über den Subsystem-Exit `IFASEXIT` liest und per OTLP an den Collector
schickt – ohne z/OSMF, Dump und Dateiübertragung. Gleiche Felder wie `smf_otel.py` (per
Gleichheitstest geprüft), die Dashboards bleiben unverändert. Einrichtung, Bauen und Start:
[java/README.md](java/README.md).

### Herkunft der Satzlayouts

IBM-Tabellen aus dem SMF-Handbuch, wiedergegeben auf
[pacsys.com/smf](https://www.pacsys.com/smf/): `smf30.htm`, `smf80.htm`,
`smf119_subtype11.htm`, `smf119_subtype12.htm`. Die Seite zu SMF 30 hat den Stand z/OS V1R3
und kennt das zIIP-Feld nicht; die zIIP-Zeit (`SMF30_TIME_ON_zIIP`, Offset 116 im
Prozessorabschnitt) stammt aus `adapya-base` (Software AG, Apache 2.0). Die Tests in
`smf/test_smf.py` bauen Sätze direkt nach diesen Offsets, unabhängig vom Generator.

## Konfiguration des Agenten

```json
{
  "dds":   {"url": "http://ddshost:8803", "user": "DDSUSER", "insecure": false},
  "otlp":  {"endpoint": "http://collector:4318", "headers": {}},
  "interval": 100,
  "resource": {},
  "metrics": [
    {"id": "8D0450", "resource": ",,SYSPLEX", "name": "rmf.cpu.utilization",
     "unit": "%", "row_attribute": "zos.system.name"}
  ]
}
```

- `metrics[].resource`: DDS-Ressource, z. B. `,,SYSPLEX` oder `,SYSA,MVS_IMAGE`.
- `name`/`unit`: OTel-Name und Einheit; ohne `name` wird er aus der DDS-Beschreibung gebildet.
- `row_attribute`: Attribut für die Zeilennamen von Listenmetriken („... by MVS image“).
- `resource`: zusätzliche OTel-Ressourcenattribute. Den Sysplex-Namen setzt der Agent selbst
  aus der DDS-Antwort (`zos.sysplex.name`).
- Umgebungsvariablen überschreiben: `RMF_DDS_URL`, `RMF_DDS_USER`, `RMF_DDS_PASSWORD`,
  `RMF_INTERVAL`, `OTEL_EXPORTER_OTLP_ENDPOINT`.
- Der Agent meldet jedes Monitor-III-Intervall genau einmal (Zeitstempel = `utcEnd` des DDS);
  Werte wie `NaN`, `Deact` und `*NoData*`-Zeilen werden übersprungen.

In Prometheus heißen die Metriken nach OTel-Regeln z. B. `rmf_cpu_utilization_percent`,
`rmf_transaction_rate_per_second` (Label `zos_system_name`, `zos_sysplex_name`).

## Tests

```sh
python3 -m unittest -v test_agent.py
(cd smf && python3 -m unittest -v test_smf.py)
python3 grafana/build_dashboards.py      # Dashboards neu erzeugen (nach Änderungen)
```

Umsetzung DDS-Report -> OTLP, Fehlerfälle, XML-Antwort, und Ende-zu-Ende gegen `mock_dds.py`
(JSON- und XML-Modus) mit einem eingebauten OTLP-Empfänger. Das Demo-Compose wurde
vollständig durchgespielt (alle Dashboard-Abfragen liefern Daten), ebenso
`docker-compose.real.yaml` gegen den Simulator mit verlangter Anmeldung (`mock_dds.py --auth`).

## Stand und Grenzen

- **RMF:** Weg A (Agent) und Weg B (`/metrics/m3`) liefern am echten DDS Daten (lokal beim
  Nutzer, `docker-compose.real.yaml`). Die 16 zusätzlichen Agent-Metriken (Using/Delay je
  Ursache, Common Storage, UIC) stammen aus dem Metrikkatalog des IBM-RMF-Grafana-Plugins
  und sind am echten DDS noch nicht bestätigt; eine ID, die der DDS nicht kennt, erscheint
  im Agent-Log und fehlt im Dashboard, die übrigen laufen weiter.
- **SMF:** noch nicht mit echten Sätzen getestet, nur mit dem Generator und mit den von Hand
  nach den IBM-Tabellen gebauten Testsätzen. Erster Schritt: `SMFDUMP.jcl`, dann
  `smf_otel.py --print`.
- SMF 80: Relocate-Abschnitte werden nur roh als `racf.reloc.<typ>` mitgegeben (die
  Bedeutung der Datentypen steht in *RACF Macros and Interfaces*, nicht auf pacsys).
- SMF kommt per Dump (Batch). Ausbaustufe: echtzeitnah über die SMF-In-Memory-Schnittstelle
  (IFAMCON/IFAMGET) direkt auf z/OS.
- Weitere Ausbaustufen:
  - **Traces aus Anwendungen:** z. B. Programmaufrufe (CALL) als Spans melden –
    ein Batchlauf oder eine Transaktion wird dann im selben Trace-Werkzeug sichtbar wie der
    aufrufende Java-/Cloud-Dienst.
  - **Logs:** Joblog-/SYSLOG-Meldungen als OTel-Logs.
  - weitere SMF-Typen, z. B. 70–79 (RMF-Langzeit), 14/15/17 (Datasets), 119.2 (TCP-Verbindungen).
- IBM bietet eigene Wege: RMF-Grafana-Plugin (direkt auf den DDS, ohne OTel) und kommerziell
  z. B. IBM Z Common Data Provider / Z Operational Log and Data Analytics. Dieser Prototyp
  ist die herstellerneutrale, kostenlose Variante.
