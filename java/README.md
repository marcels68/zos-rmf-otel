# zsmf-otel – SMF-Sätze von z/OS direkt per OpenTelemetry (Java)

Java-Dienst, der **auf z/OS** läuft und SMF-Sätze ohne Umweg (kein z/OSMF, kein Dump, keine
Dateiübertragung) als **OpenTelemetry-Logs** an einen Collector schickt. Ausgewertet werden
dieselben Satztypen und Felder wie in `smf/smf_otel.py` (SMF 30.4/30.5, 80, 119.11/119.12) –
die vorhandenen Grafana-Dashboards funktionieren unverändert.

```
 z/OS                                                          irgendwo
 ┌──────────────────────────────────────────────┐
 │ SMF ──▶ In-Memory-Ressource IFASMF.OTEL ──┐  │
 │          (SMFPRMxx INMEM)                 │  │
 │                    IFAMCON/IFAMGET (JNI)  ▼  │   OTLP/HTTP
 │                                  zsmf-otel ──┼──────────────▶ OpenTelemetry Collector
 │ SMF ──▶ Log Stream ──IFASEXIT-DD──▶ (JZOS) ─┘│                 └─▶ Loki ─▶ Grafana
 └──────────────────────────────────────────────┘
```

Zwei Quellen:

| Quelle | Wofür | Schnittstelle |
|---|---|---|
| `inmem:IFASMF.OTEL` | **Live-Strom**: Sätze kommen an, sobald SMF sie schreibt | SMF Real-Time Interface (`IFAMCON`, `IFAMGET`, `IFAMDSC`), über JNI |
| `logstream:DD:SMFIN` | **Nachladen/Aufholen** eines Zeitraums (nach Ausfall, für Historie) | SMF-Log-Stream über den Subsystem-Exit `IFASEXIT` (DD mit `SUBSYS=(LOGR,IFASEXIT,...)`), gelesen mit JZOS |
| `file:smf.bin` | Test/Entwicklung, Dumps auf dem PC | Datei mit RDW (wie `smf_otel.py`) |

IBM beschreibt das Real-Time Interface als „high-speed data feed with low system overhead“
und als Ersatz für die Exits IEFU83–86; es braucht **keine APF-Berechtigung**, nur SAF-READ.
IFASEXIT dagegen liest je Task nur einen Log Stream und ist für hohe Volumen nicht gedacht –
deshalb hier nur zum Nachladen.

## Aufbau

- **Fast alles reines Java** (Java 11+, keine Fremdbibliotheken): Parameterblöcke,
  Pufferauswertung, SMF-Parser (Port von `smf_otel.py`), Warteschlange, OTLP-Versand,
  Statusseite, Checkpoint.
- **Nativ nur zwei kleine Funktionen** (`native/zsmfrti.c`, JNI): Adresse eines
  Direct-ByteBuffers und Aufruf eines Real-Time-Dienstes. Die Einsprungadressen kommen über
  die CVT-Kette, die IBM als „alternative method“ dokumentiert; Aufruf mit Standard-OS-Linkage
  in AMODE 64 (R1 → drei 8-Byte-Adressen, R13 → Sicherungsbereich). Kein `jni.h` nötig.
- **JZOS** (Teil von IBM Java auf z/OS) liest die IFASEXIT-DD; per Reflection angesprochen,
  daher übersetzt das Programm auch ohne z/OS.
- Ein Lese-Thread holt Sätze, ein Sende-Thread schickt Pakete (Standard 1000 Sätze oder alle
  2 s). Ist der Collector weg, wiederholt der Sender (2, 4 … 60 s) und die Warteschlange füllt
  sich; ist sie voll, wartet das Lesen. **Achtung:** Die In-Memory-Ressource ist ein
  Ringpuffer – bleibt der Collector zu lange weg, überschreibt SMF die ältesten Sätze. Dann
  mit der Log-Stream-Quelle nachladen (siehe Checkpoint).

## Einrichtung auf z/OS

1. **In-Memory-Ressource definieren** (SMF muss im Log-Stream-Modus laufen). SMFPRMxx:
   ```
   INMEM(IFASMF.OTEL,RESSIZMAX(256M),TYPE(30,80,119))
   LSNAME(IFASMF.OTELLS,TYPE(30,80,119))
   ```
   Wichtig: Satztypen, die einer INMEM-Ressource zugeordnet sind, schreibt SMF **nicht mehr
   in den DEFAULTLSNAME-Log-Stream**. Damit sie weiter in einem Log Stream landen (Archiv,
   Nachladen mit IFASEXIT), dieselben Typen bei einem `LSNAME` angeben. Aktivieren mit
   `SET SMF=xx`; anzeigen mit `D SMF,M`.
2. **SAF-Profil** (Name = `IFA.` + Ressourcenname; ohne Profil lehnt SMF das ganze
   SMFPRMxx-Member ab):
   ```
   RDEFINE FACILITY IFA.IFASMF.OTEL UACC(NONE)
   PERMIT IFA.IFASMF.OTEL CLASS(FACILITY) ID(ZSMFUSR) ACCESS(READ)
   SETROPTS RACLIST(FACILITY) REFRESH
   ```
   Für das Nachladen zusätzlich READ auf den Log Stream (Klasse `LOGSTRM`, Profil =
   Log-Stream-Name).
3. **Grenzen laut IBM:** höchstens 8 Verbindungen je Ressource, 128 je System, 32 Ressourcen.

## Bauen

```sh
java/build.sh            # JAR + alle Tests + Gleichheitstest mit Python
java/build.sh --no-test  # nur java/build/zsmf-otel.jar
```

Native DLL `libzsmfrti.so` (XPLINK-64, arch10/zEC12):

```sh
java/native/build-zos.sh openxl     # auf z/OS mit IBM Open XL C/C++
# oder quer übersetzen (clang mit SystemZ-Ziel, z. B. aus zos-cobol-llvm) und auf z/OS binden:
java/native/build-zos.sh cross      # -> java/build/zsmfrti.o (binär nach z/OS kopieren)
java/native/build-zos.sh bind       # auf z/OS: ld ... -> libzsmfrti.so + libzsmfrti.x
```

## Starten

**z/OS UNIX** (JAR, `libzsmfrti.so` und `zos/zsmf-live.sh` in ein Verzeichnis):

```sh
ZSMF_OTLP=http://collector:4318 ZSMF_RESOURCE=IFASMF.OTEL ./zsmf-live.sh
```

**Batch / Started Task** mit dem JZOS Batch Launcher: `jcl/ZSMFLIVE.jcl` (live) und
`jcl/ZSMFLOGR.jcl` (Nachladen mit IFASEXIT-DD). Weil `PARM` höchstens 100 Zeichen hat,
stehen die Optionen in der Umgebungsvariable `ZSMF_ARGS` (STDENV). Programmname
`JVMLDMnn` passend zur Java-Version wählen.

Optionen (`java -jar zsmf-otel.jar --help`):

| Option | Bedeutung |
|---|---|
| `--source inmem:NAME` / `logstream:DD:NAME` / `file:PFAD` | Quelle |
| `--otlp URL` | Collector (Standard `http://localhost:4318`), `--header K=V` für Token |
| `--tz ZONE` | Zeitzone der SMF-Zeitstempel (Standard: Systemzone) |
| `--types 30,80,119` | nur diese Typen auswerten |
| `--batch N`, `--flush-ms N`, `--queue N` | Paketgröße, spätestens senden nach, Warteschlange |
| `--buffer N` | IFAMGET-Puffer (Standard 4 MB, mindestens 32 KB) |
| `--nowait` | IFAMGET nicht blockierend (sonst wartet IFAMGET auf neue Sätze) |
| `--status-port N` | `/health`, `/status` (JSON), `/metrics` (Prometheus) |
| `--checkpoint PFAD` | Zeit des letzten gesendeten Satzes, alle 10 s |
| `--print`, `--dry-run` | Zeilen bzw. OTLP-JSON ausgeben statt senden |

**Checkpoint und Nachladen:** Der Live-Dienst schreibt mit `--checkpoint` laufend die Zeit
des letzten gesendeten Satzes in der Form `2026/297,08:15:42`. Nach einem Ausfall diese Zeit in
`ZSMFLOGR.jcl` als `FROM=(2026/297,08:15:42)` (mit `LOCAL`) eintragen und den Job abschicken.
Doppelt gesendete Sätze verwirft Loki selbst (gleiche Zeile, gleicher Zeitstempel).

## Tests

`java/build.sh` führt aus:

- **`Tests`** (35 Prüfungen, ohne z/OS): Ein Nachbau des Real-Time Interface prüft die
  Parameterblöcke **Byte für Byte** gegen IBMs Tabellen (CNPB, GTPB), spielt Sätze, „keine
  Daten“ (0401), erzwungene Trennung (0408) und Fehler (0807 mit Klartext) durch; Ende zu Ende
  mit OTLP-Empfänger, gescheitertem ersten Versand, Statusseite und Checkpoint.
- **`NativeSmoke`**: baut `zsmfrti.c` für Linux und prüft die JNI-Anbindung in der JVM
  (Tabellenindex 230 von `GetDirectBufferAddress` auch gegen `jni.h` geprüft); der
  Dienstaufruf selbst ist außerhalb z/OS abgeschaltet.
- **Gleichheitstest** `parity.py`: Java und Python werten dieselben Sätze aus; Zeit, Text und
  alle Attribute mit Typ müssen gleich sein (zuletzt 2178 bzw. 5325 Sätze: gleich).

Außerdem geprüft: Der C-Teil übersetzt mit clang für `s390x-ibm-zos -march=zEC12`; im
erzeugten Assembler stimmen CVT-Kette, R1/R13/R15 und die Sicherung von R6–R15. Ende zu Ende
lief `zsmf-otel` (Quelle Datei) gegen Collector und Loki aus `docker-compose.yaml`.

Durchsatz auf einem 4-Kern-x86-Rechner (kein z/OS, nur Anhaltspunkt): Auswerten und
OTLP-JSON-Erzeugung ~16 000 Sätze/s in einem Thread inkl. JVM-Start (Python: ~3 600/s).

## Noch nicht auf z/OS getestet

- **Der Dienstaufruf** (CVT-Kette, OS-Linkage aus XPLINK-64 per Inline-Assembler) ist nach
  IBMs Doku gebaut und im Assembler geprüft, lief aber noch nie auf z/OS. Erster Test:
  `zsmf-live.sh --print` mit einer kleinen Ressource; erwartet werden Sätze oder ein
  verständlicher Fehler wie `RC 08 RSN 0807` (Ressource fehlt/kein SAF-READ).
- **Satzform im IFAMGET-Puffer:** IBM schreibt nur „die ersten 2 Byte jedes Satzes sind seine
  Länge“. Ob danach wie im RDW 2 Byte Segmentfeld folgen, steht dort nicht; `Records.normalize`
  erkennt beides am gepackten Datum und ergänzt fehlende Bytes.
- **JZOS-`RecordReader` auf der IFASEXIT-DD** (liefert V-Sätze vermutlich ohne RDW; wird
  ebenfalls erkannt).
- Durchsatz und CPU auf z/OS.
