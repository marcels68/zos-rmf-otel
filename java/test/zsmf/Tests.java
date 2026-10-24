package zsmf;

import com.sun.net.httpserver.HttpServer;
import java.io.ByteArrayOutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * Tests ohne Fremdbibliotheken:  java -cp build/classes:build/test zsmf.Tests TESTDUMP
 * TESTDUMP = SMF-Dump aus smf/smf_gen.py (java/build.sh erzeugt ihn).
 */
public final class Tests {

    static int run = 0, failed = 0;

    static void check(boolean ok, String what) {
        run++;
        if (!ok) {
            failed++;
            System.out.println("FEHLER: " + what);
        }
    }

    static void eq(Object exp, Object act, String what) {
        check(exp == null ? act == null : exp.equals(act), what + ": erwartet <" + exp
                + "> war <" + act + ">");
    }

    static String hex(ByteBuffer b, int off, int len) {
        StringBuilder sb = new StringBuilder();
        for (int i = off; i < off + len; i++) {
            sb.append(String.format("%02X", b.get(i) & 0xFF));
        }
        return sb.toString();
    }

    // ------------------------------------------------------------------ Nachbau RTI

    /** Spielt IFAMCON/IFAMGET/IFAMDSC nach und prüft die Parameterblöcke. */
    static final class FakeRti implements RtiApi {
        final List<List<byte[]>> batches;      // je IFAMGET ein Paket Sätze
        final Map<Long, ByteBuffer> buffers = new HashMap<>();
        final List<String> calls = new CopyOnWriteArrayList<>();
        int[] connectRc = {0, 0};
        boolean stripSeg;                      // Sätze ohne 2-Byte-Segmentfeld liefern
        int gets;
        volatile boolean disconnected;
        String lastCnpb, lastGtpbHead;

        FakeRti(List<List<byte[]>> batches) {
            this.batches = batches;
        }

        @Override
        public long addressOf(ByteBuffer direct) {
            long a = 0x1_0000_0000L + buffers.size() * 0x10000L;
            buffers.put(a, direct);
            return a;
        }

        @Override
        public synchronized int[] call(int service, ByteBuffer p) {
            switch (service) {
                case IFAMCON:
                    calls.add("CON");
                    lastCnpb = hex(p, 0, 106);
                    if (connectRc[0] != 0) {
                        return connectRc;
                    }
                    for (int i = 0; i < 16; i++) {
                        p.put(56 + i, (byte) (0xA0 + i));
                    }
                    return new int[] {0, 0};
                case IFAMGET: {
                    calls.add("GET");
                    lastGtpbHead = hex(p, 0, 36);
                    if (disconnected) {
                        return new int[] {4, 0x0408};
                    }
                    if (gets >= batches.size()) {
                        boolean nowait = (p.getInt(8) & 0x10) != 0;
                        if (nowait && gets == batches.size()) {
                            gets++;
                            return new int[] {4, 0x0401};
                        }
                        return new int[] {4, 0x0408};   // Ende des Tests: getrennt
                    }
                    ByteBuffer out = buffers.get(p.getLong(56));
                    int pos = 0;
                    for (byte[] r : batches.get(gets)) {
                        byte[] w = stripSeg ? strip(r) : r;
                        for (byte x : w) {
                            out.put(pos++, x);
                        }
                    }
                    gets++;
                    p.putInt(52, pos);
                    return new int[] {0, 0};
                }
                case IFAMDSC:
                    calls.add("DSC");
                    disconnected = true;
                    return new int[] {0, 0};
                default:
                    return new int[] {16, 0};
            }
        }

        static byte[] strip(byte[] r) {   // Länge (2 Byte) + Daten, ohne Segmentfeld
            byte[] w = new byte[r.length - 2];
            int len = w.length;
            w[0] = (byte) (len >> 8);
            w[1] = (byte) len;
            System.arraycopy(r, 4, w, 2, r.length - 4);
            return w;
        }
    }

    // ------------------------------------------------------------------ Tests

    static List<byte[]> readAll(Path dump) throws Exception {
        List<byte[]> all = new ArrayList<>();
        try (FileSource f = new FileSource(dump, false)) {
            List<byte[]> b;
            while ((b = f.next()) != null) {
                all.addAll(b);
            }
        }
        return all;
    }

    static void testBlocks(List<byte[]> recs) throws Exception {
        FakeRti rti = new FakeRti(List.of(recs.subList(0, 3)));
        InMemorySource s = new InMemorySource("ifasmf.otel", rti, 65536, false);
        List<byte[]> got = s.next();
        // CNPB: "CNPB" EBCDIC, Länge 106, unbenutzt 0, Version 1, Reserve 0, Namenslänge 11,
        // Name in EBCDIC mit Leerzeichen (X'40') auf 26 Byte
        eq("C3D5D7C2" + "006A" + "00" + "01" + "00000000" + "000B"
                + "C9C6C1E2D4C64BD6E3C5D3" + "40".repeat(15), rti.lastCnpb.substring(0, 80),
                "CNPB-Kopf und Name");
        eq("00".repeat(66), rti.lastCnpb.substring(80), "CNPB-Rest null vor dem Aufruf");
        // GTPB: "GTPB", 64, 0, 1, Flags X'80' (mehrere Sätze), Reserve, Token, Pufferlänge
        eq("C7E3D7C2" + "0040" + "00" + "01" + "00000080" + "00000000"
                + "A0A1A2A3A4A5A6A7A8A9AAABACADAEAF" + "00010000", rti.lastGtpbHead,
                "GTPB-Kopf, Token, Pufferlänge");
        eq(3, got.size(), "Anzahl Sätze aus einem IFAMGET");
        check(java.util.Arrays.equals(recs.get(0), got.get(0)), "Satz unverändert");
        eq(null, s.next(), "Ende nach Trennung (RSN 0408)");
        s.close();
        eq(List.of("CON", "GET", "GET"), rti.calls, "kein DSC nach erzwungener Trennung");
    }

    static void testNoSegAndNowait(List<byte[]> recs) {
        FakeRti rti = new FakeRti(List.of(recs.subList(0, 5)));
        rti.stripSeg = true;
        InMemorySource s = new InMemorySource("IFASMF.X", rti, 32768, true);
        List<byte[]> got = s.next();
        for (int i = 0; i < 5; i++) {
            check(java.util.Arrays.equals(recs.get(i), got.get(i)),
                    "Satz ohne Segmentfeld wird zu RDW-Form ergänzt (" + i + ")");
        }
        eq(null, s.next(), "nicht blockierend: 0401 wird übergangen, dann Ende");
        check(rti.lastGtpbHead.startsWith("C7E3D7C2004000010000009"), "Flag X'10' gesetzt");
    }

    static void testErrors() {
        FakeRti rti = new FakeRti(List.of());
        rti.connectRc = new int[] {8, 0x0807};
        InMemorySource s = new InMemorySource("IFASMF.NOPE", rti, 32768, false);
        try {
            s.next();
            check(false, "Fehler bei IFAMCON erwartet");
        } catch (InMemorySource.RtiException e) {
            check(e.getMessage().contains("RC 08 RSN 0807") && e.getMessage().contains("SAF"),
                    "Klartext für 0807: " + e.getMessage());
        }
        try {
            new InMemorySource("X", rti, 1000, false);
            check(false, "zu kleiner Puffer muss abgelehnt werden");
        } catch (IllegalArgumentException e) {
            check(true, "");
        }
    }

    static void testCloseUnblocks(List<byte[]> recs) throws Exception {
        FakeRti rti = new FakeRti(List.of(recs.subList(0, 2)));
        InMemorySource s = new InMemorySource("IFASMF.Y", rti, 32768, false);
        s.next();
        s.close();
        eq(null, s.next(), "nach close() liefert next() null");
        check(rti.calls.contains("DSC"), "close() ruft IFAMDSC");
    }

    static void testNormalize(byte[] rec) {
        byte[] noRdw = java.util.Arrays.copyOfRange(rec, 4, rec.length);
        check(java.util.Arrays.equals(rec, Records.normalize(noRdw)), "Satz ohne RDW ergänzt");
        check(Records.normalize(rec) == rec, "Satz mit RDW bleibt unverändert");
    }

    static void testFileSpanned(List<byte[]> recs, Path dir) throws Exception {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        byte[] r = recs.get(0);
        int half = (r.length - 4) / 2;
        o.write(new byte[] {(byte) ((half + 4) >> 8), (byte) (half + 4), 1, 0});
        o.write(r, 4, half);
        int rest = r.length - 4 - half;
        o.write(new byte[] {(byte) ((rest + 4) >> 8), (byte) (rest + 4), 2, 0});
        o.write(r, 4 + half, rest);
        byte[] block = o.toByteArray();
        ByteArrayOutputStream withBdw = new ByteArrayOutputStream();
        withBdw.write(new byte[] {(byte) ((block.length + 4) >> 8), (byte) (block.length + 4),
            0, 0});
        withBdw.write(block);
        Path f1 = dir.resolve("span.bin"), f2 = dir.resolve("bdw.bin");
        Files.write(f1, block);
        Files.write(f2, withBdw.toByteArray());
        try (FileSource a = new FileSource(f1, false); FileSource b = new FileSource(f2, true)) {
            check(java.util.Arrays.equals(r, a.next().get(0)), "gespannter Satz zusammengesetzt");
            check(java.util.Arrays.equals(r, b.next().get(0)), "Blockdeskriptor übersprungen");
        }
    }

    static void testEnvArgs() {
        String[] a = Main.withEnvArgs(new String[] {"--print"}, "  --source  file:x --tz UTC ");
        eq(List.of("--source", "file:x", "--tz", "UTC", "--print"), List.of(a), "ZSMF_ARGS");
        eq(1, Main.withEnvArgs(new String[] {"--print"}, null).length, "ohne ZSMF_ARGS");
    }

    static void testJsonEscape() {
        StringBuilder sb = new StringBuilder();
        OtlpExporter.str(sb, "a\"b\\c\n\u0001");
        eq("\"a\\\"b\\\\c\\n\\u0001\"", sb.toString(), "JSON-Maskierung");
    }

    /** Ende zu Ende: Nachbau-RTI -> Main -> OTLP-Empfänger; Statusseite und Checkpoint. */
    static void testEndToEnd(List<byte[]> recs, Path dir) throws Exception {
        List<String> posts = new CopyOnWriteArrayList<>();
        HttpServer otlp = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        int[] fails = {1};   // erster Versuch scheitert -> Wiederholung prüfen
        otlp.createContext("/v1/logs", ex -> {
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            int code = fails[0]-- > 0 ? 503 : 200;
            if (code == 200) {
                posts.add(body);
            }
            ex.sendResponseHeaders(code, -1);
            ex.close();
        });
        otlp.start();
        List<List<byte[]>> batches = new ArrayList<>();
        for (int i = 0; i < recs.size(); i += 40) {
            batches.add(recs.subList(i, Math.min(recs.size(), i + 40)));
        }
        FakeRti rti = new FakeRti(batches);
        Path ckpt = dir.resolve("ckpt.txt");
        Main m = Main.parseArgs(new String[] {"--source", "inmem:IFASMF.OTEL",
            "--otlp", "http://127.0.0.1:" + otlp.getAddress().getPort(), "--tz", "UTC",
            "--batch", "250", "--flush-ms", "300", "--status-port", "0",
            "--checkpoint", ckpt.toString(), "--types", "30,80,119"});
        int rc = m.run(rti);
        otlp.stop(0);
        eq(0, rc, "Rückgabecode");
        SmfParser parser = new SmfParser(ZoneId.of("UTC"));
        long parsed = recs.stream().filter(r -> parser.parse(r) != null).count();
        eq(parsed, m.stats.sent.get(), "alle ausgewerteten Sätze gesendet");
        eq((long) recs.size(), m.stats.readTotal(), "alle Sätze gelesen");
        int logs = 0;
        for (String p : posts) {
            logs += p.split("\"timeUnixNano\"", -1).length - 1;
        }
        eq((int) parsed, logs, "Logs beim Empfänger");
        check(posts.get(0).contains("\"service.name\"") && posts.get(0).contains("zos-smf-"),
                "Ressource service.name=zos-smf-TYP");
        check(m.stats.sendFailures.get() >= 1, "Fehlversuch wurde wiederholt");
        String c = Files.readString(ckpt).trim();
        check(c.matches("\\d{4}/\\d{3},\\d{2}:\\d{2}:\\d{2}"), "Checkpoint IFASEXIT-Form: " + c);
    }

    static void testStatusPage() throws Exception {
        Stats st = new Stats();
        st.read(30);
        st.sent.set(5);
        StatusServer s = StatusServer.start(0, st, new java.util.concurrent.ArrayBlockingQueue<>(1));
        java.lang.reflect.Field f = StatusServer.class.getDeclaredField("server");
        f.setAccessible(true);
        int port = ((HttpServer) f.get(s)).getAddress().getPort();
        HttpClient c = HttpClient.newHttpClient();
        String status = c.send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port
                + "/status")).build(), HttpResponse.BodyHandlers.ofString()).body();
        String metrics = c.send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port
                + "/metrics")).build(), HttpResponse.BodyHandlers.ofString()).body();
        s.stop();
        check(status.contains("\"read\":{\"30\":1}") && status.contains("\"sent\":5"),
                "Status-JSON: " + status);
        check(metrics.contains("zsmf_records_read_total{smf_type=\"30\"} 1"),
                "Metriken: " + metrics);
    }

    public static void main(String[] args) throws Exception {
        Path dump = Path.of(args[0]);
        Path dir = Files.createTempDirectory("zsmf");
        List<byte[]> recs = readAll(dump);
        check(recs.size() > 100, "Testdump hat Sätze");
        testBlocks(recs);
        testNoSegAndNowait(recs);
        testErrors();
        testCloseUnblocks(recs);
        testNormalize(recs.get(0));
        testFileSpanned(recs, dir);
        testJsonEscape();
        testEnvArgs();
        testStatusPage();
        testEndToEnd(recs, dir);
        System.out.println(run + " Prüfungen, " + failed + " Fehler");
        System.exit(failed == 0 ? 0 : 1);
    }
}
