package zsmf;

import java.io.IOException;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * zsmf-otel: SMF-Sätze von z/OS als OpenTelemetry-Logs an einen Collector schicken.
 *
 * <pre>
 *   java -jar zsmf-otel.jar --source inmem:IFASMF.OTEL   --otlp http://collector:4318
 *   java -jar zsmf-otel.jar --source logstream:DD:SMFIN  --otlp http://collector:4318
 *   java -jar zsmf-otel.jar --source file:smf.bin --print
 * </pre>
 *
 * Quellen: inmem (SMF Real-Time Interface, live), logstream (IFASEXIT-DD, Nachladen),
 * file (Dump mit RDW, zum Testen). Siehe java/README.md.
 */
public final class Main {

    static final String USAGE = String.join("\n",
        "zsmf-otel --source inmem:NAME|logstream:DD:NAME|file:PFAD [Optionen]",
        "  --otlp URL           OTLP/HTTP-Endpunkt (Standard http://localhost:4318)",
        "  --header K=V         zusätzlicher HTTP-Header (mehrfach möglich)",
        "  --tz ZONE            Zeitzone der SMF-Zeitstempel (Standard: Systemzone)",
        "  --types 30,80,119    nur diese SMF-Typen auswerten (Standard: alle bekannten)",
        "  --batch N            Sätze je OTLP-Sendung (Standard 1000)",
        "  --flush-ms N         spätestens nach N ms senden (Standard 2000)",
        "  --queue N            Warteschlange in Sätzen (Standard 100000)",
        "  --buffer N           IFAMGET-Puffer in Byte (Standard 4194304, mind. 32768)",
        "  --nowait             IFAMGET nicht blockierend (pollt alle 200 ms)",
        "  --status-port N      Statusseite http://host:N/status, /metrics, /health",
        "  --checkpoint PFAD    Zeit des letzten Satzes speichern (für IFASEXIT FROM=)",
        "  --bdw                Datei mit Blockdeskriptoren (nur file:)",
        "  --print              eine Zeile je Satz ausgeben statt senden",
        "  --dry-run            OTLP-JSON ausgeben statt senden",
        "Optionen können auch in der Umgebungsvariable ZSMF_ARGS stehen.");

    // ------------------------------------------------------------------ Optionen

    String source;
    String otlp = "http://localhost:4318";
    Map<String, String> headers = new LinkedHashMap<>();
    ZoneId zone = ZoneId.systemDefault();
    Set<Integer> types = new HashSet<>(List.of(30, 80, 119));
    int batch = 1000;
    long flushMs = 2000;
    int queueSize = 100_000;
    int bufferSize = 4 << 20;
    boolean nowait, bdw, print, dryRun;
    int statusPort = -1;
    Path checkpoint;
    PrintStream out = System.out;

    final Stats stats = new Stats();
    volatile boolean readerDone;
    volatile RecordSource src;

    static Main parseArgs(String[] args) {
        Main m = new Main();
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            switch (a) {
                case "--source": m.source = args[++i]; break;
                case "--otlp": m.otlp = args[++i]; break;
                case "--header": {
                    String[] kv = args[++i].split("=", 2);
                    m.headers.put(kv[0], kv.length > 1 ? kv[1] : "");
                    break;
                }
                case "--tz": m.zone = ZoneId.of(args[++i]); break;
                case "--types": {
                    m.types.clear();
                    for (String t : args[++i].split(",")) {
                        m.types.add(Integer.parseInt(t.trim()));
                    }
                    break;
                }
                case "--batch": m.batch = Integer.parseInt(args[++i]); break;
                case "--flush-ms": m.flushMs = Long.parseLong(args[++i]); break;
                case "--queue": m.queueSize = Integer.parseInt(args[++i]); break;
                case "--buffer": m.bufferSize = Integer.parseInt(args[++i]); break;
                case "--nowait": m.nowait = true; break;
                case "--status-port": m.statusPort = Integer.parseInt(args[++i]); break;
                case "--checkpoint": m.checkpoint = Path.of(args[++i]); break;
                case "--bdw": m.bdw = true; break;
                case "--print": m.print = true; break;
                case "--dry-run": m.dryRun = true; break;
                case "-h": case "--help": System.out.println(USAGE); System.exit(0); break;
                default: throw new IllegalArgumentException("unbekannte Option " + a);
            }
        }
        if (m.source == null) {
            throw new IllegalArgumentException("--source fehlt");
        }
        return m;
    }

    RecordSource openSource(RtiApi rti) throws Exception {
        String s = source;
        if (s.startsWith("inmem:")) {
            return new InMemorySource(s.substring(6), rti != null ? rti : new RtiNative(),
                    bufferSize, nowait);
        }
        if (s.startsWith("logstream:")) {
            return new LogstreamSource(s.substring(10));
        }
        if (s.startsWith("file:")) {
            return new FileSource(Path.of(s.substring(5)), bdw);
        }
        throw new IllegalArgumentException("unbekannte Quelle " + s);
    }

    static void log(String msg) {
        System.err.println(Instant.now().toString().substring(0, 19) + " " + msg);
    }

    // ------------------------------------------------------------------ Ablauf

    /** Startet Lesen und Senden; kehrt zurück, wenn die Quelle erschöpft ist. */
    int run(RtiApi rti) throws Exception {
        SmfParser parser = new SmfParser(zone);
        src = openSource(rti);
        BlockingQueue<SmfParser.Event> queue = new ArrayBlockingQueue<>(queueSize);
        stats.source = src.describe();
        StatusServer status = statusPort >= 0 ? StatusServer.start(statusPort, stats, queue) : null;
        boolean local = print || dryRun;
        OtlpExporter exporter = local ? null : new OtlpExporter(otlp, headers);
        log("Quelle " + src.describe() + " -> " + (local ? "Ausgabe" : exporter.uri())
                + ", Typen " + types + ", Zeitzone " + zone);

        Thread sender = new Thread(() -> sendLoop(queue, exporter), "zsmf-sender");
        sender.start();
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            if (!readerDone) {
                log("Beenden: Quelle wird getrennt");
                src.close();
                try {
                    sender.join(15_000);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
        }, "zsmf-shutdown"));

        try {
            List<byte[]> recs;
            while ((recs = src.next()) != null) {
                for (byte[] rec : recs) {
                    int type = rec.length > 5 ? rec[5] & 0xFF : -1;
                    stats.read(type);
                    if (!types.contains(type)) {
                        stats.skipped.incrementAndGet();
                        continue;
                    }
                    SmfParser.Event e;
                    try {
                        e = parser.parse(rec);
                    } catch (RuntimeException ex) {
                        stats.errors.incrementAndGet();
                        if (stats.errors.get() <= 20) {
                            log("Satz Typ " + type + " nicht lesbar: " + ex);
                        }
                        continue;
                    }
                    if (e == null) {
                        stats.skipped.incrementAndGet();
                        continue;
                    }
                    if (!queue.offer(e)) {
                        stats.queueFull.incrementAndGet();
                        queue.put(e);        // Gegendruck: Lesen wartet auf den Versand
                    }
                }
            }
        } finally {
            readerDone = true;
            src.close();
        }
        sender.join();
        if (status != null) {
            status.stop();
        }
        log("fertig: " + stats.summary());
        return 0;   // deliver() wiederholt bis zum Erfolg, es geht nichts verloren
    }

    void sendLoop(BlockingQueue<SmfParser.Event> queue, OtlpExporter exporter) {
        List<SmfParser.Event> buf = new ArrayList<>(batch);
        long lastCheckpoint = 0;
        while (true) {
            try {
                long deadline = System.currentTimeMillis() + flushMs;
                while (buf.size() < batch) {
                    long wait = deadline - System.currentTimeMillis();
                    SmfParser.Event e = wait > 0 ? queue.poll(wait, TimeUnit.MILLISECONDS) : null;
                    if (e == null) {
                        break;
                    }
                    buf.add(e);
                }
                if (!buf.isEmpty()) {
                    deliver(buf, exporter);
                    stats.lastEventNanos = buf.get(buf.size() - 1).timeNanos;
                    buf.clear();
                }
                if (checkpoint != null && stats.lastEventNanos > 0
                        && System.currentTimeMillis() - lastCheckpoint > 10_000) {
                    writeCheckpoint();
                    lastCheckpoint = System.currentTimeMillis();
                }
                if (readerDone && queue.isEmpty()) {
                    if (checkpoint != null && stats.lastEventNanos > 0) {
                        writeCheckpoint();
                    }
                    return;
                }
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }

    /** Senden mit Wiederholung (2, 4, 8 ... höchstens 60 s), bis es klappt. */
    void deliver(List<SmfParser.Event> events, OtlpExporter exporter) throws InterruptedException {
        if (print) {
            for (SmfParser.Event e : events) {
                out.println(Instant.ofEpochSecond(0, e.timeNanos).atZone(zone)
                        .toOffsetDateTime() + " " + e.body);
            }
            stats.sent.addAndGet(events.size());
            return;
        }
        if (dryRun) {
            out.println(OtlpExporter.toJson(events));
            stats.sent.addAndGet(events.size());
            return;
        }
        long wait = 2000;
        while (true) {
            try {
                exporter.send(events);
                stats.sent.addAndGet(events.size());
                stats.batches.incrementAndGet();
                stats.lastError = null;
                return;
            } catch (Exception ex) {
                stats.sendFailures.incrementAndGet();
                stats.lastError = ex.toString();
                log("OTLP-Senden fehlgeschlagen (" + ex.getMessage() + "), neuer Versuch in "
                        + wait / 1000 + " s");
                Thread.sleep(wait);
                wait = Math.min(wait * 2, 60_000);
            }
        }
    }

    static final DateTimeFormatter CKPT = DateTimeFormatter.ofPattern("yyyy/DDD,HH:mm:ss");

    /** Checkpoint in IFASEXIT-Form (lokale SMF-Zeit): yyyy/ddd,hh:mm:ss */
    void writeCheckpoint() {
        ZonedDateTime t = Instant.ofEpochSecond(0, stats.lastEventNanos).atZone(zone);
        try {
            Path tmp = checkpoint.resolveSibling(checkpoint.getFileName() + ".tmp");
            Files.write(tmp, (CKPT.format(t) + "\n").getBytes(StandardCharsets.US_ASCII));
            Files.move(tmp, checkpoint, StandardCopyOption.REPLACE_EXISTING,
                    StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException e) {
            log("Checkpoint nicht geschrieben: " + e);
        }
    }

    /** Optionen aus der Umgebungsvariable ZSMF_ARGS vor die Kommandozeile stellen (für JCL:
     *  der PARM ist auf 100 Zeichen begrenzt, STDENV kann ZSMF_ARGS setzen). */
    static String[] withEnvArgs(String[] args, String env) {
        if (env == null || env.isBlank()) {
            return args;
        }
        List<String> all = new ArrayList<>(List.of(env.trim().split("\\s+")));
        all.addAll(List.of(args));
        return all.toArray(new String[0]);
    }

    public static void main(String[] args) {
        try {
            Main m = parseArgs(withEnvArgs(args, System.getenv("ZSMF_ARGS")));
            System.exit(m.run(null));
        } catch (IllegalArgumentException e) {
            System.err.println("Fehler: " + e.getMessage() + "\n\n" + USAGE);
            System.exit(2);
        } catch (Exception e) {
            System.err.println("Fehler: " + e);
            System.exit(1);
        }
    }
}
