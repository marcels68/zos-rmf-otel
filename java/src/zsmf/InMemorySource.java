package zsmf;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.List;

/**
 * SMF-Sätze in Echtzeit aus einer SMF-In-Memory-Ressource (SMF Real-Time Interface).
 * Parameterblöcke laut IBM "Callable Services for High-Level Languages" (IFAZSYSP):
 * CNPB (IFAMCON), GTPB (IFAMGET), DSPB (IFAMDSC).
 */
public final class InMemorySource implements RecordSource {

    static final int RC_OK = 0, RC_WARN = 4;
    static final int RSN_NO_DATA = 0x0401, RSN_GET_IN_PROGRESS = 0x0404,
            RSN_DSC_IN_PROGRESS = 0x0406, RSN_FORCED_OUT = 0x0408;
    static final int FLAG_MULTI = 0x80, FLAG_NOWAIT = 0x10;

    private final String resource;
    private final RtiApi api;
    private final boolean nowait;
    private final ByteBuffer out;
    private final ByteBuffer gtpb = ByteBuffer.allocateDirect(64).order(ByteOrder.BIG_ENDIAN);
    private final byte[] token = new byte[16];
    private volatile boolean connected;

    /** bufferSize mindestens 32768 (IBM-Vorgabe); größere Puffer holen mehr Sätze je Aufruf. */
    public InMemorySource(String resource, RtiApi api, int bufferSize, boolean nowait) {
        if (bufferSize < 32768) {
            throw new IllegalArgumentException("Puffer muss mindestens 32768 Byte haben");
        }
        String r = resource.toUpperCase();
        if (r.length() > 26) {
            throw new IllegalArgumentException("Ressourcenname länger als 26 Zeichen: " + r);
        }
        this.resource = r;
        this.api = api;
        this.nowait = nowait;
        this.out = ByteBuffer.allocateDirect(bufferSize);
    }

    static byte[] ebcdic(String s, int len) {
        byte[] b = new byte[len];
        java.util.Arrays.fill(b, (byte) 0x40);
        byte[] e = s.getBytes(SmfParser.EBCDIC);
        System.arraycopy(e, 0, b, 0, Math.min(len, e.length));
        return b;
    }

    /** CNPB, 106 Byte. */
    static ByteBuffer connectBlock(String name) {
        ByteBuffer p = ByteBuffer.allocateDirect(106).order(ByteOrder.BIG_ENDIAN);
        put(p, 0, ebcdic("CNPB", 4));
        p.putShort(4, (short) 106).put(6, (byte) 0).put(7, (byte) 1)
         .putShort(12, (short) name.length());
        byte[] n = ebcdic(name, 26);
        for (int i = 0; i < 26; i++) {
            p.put(14 + i, n[i]);
        }
        return p;
    }

    private static ByteBuffer put(ByteBuffer p, int off, byte[] b) {
        for (int i = 0; i < b.length; i++) {
            p.put(off + i, b[i]);
        }
        return p;
    }

    public synchronized void connect() {
        ByteBuffer p = connectBlock(resource);
        int[] r = api.call(RtiApi.IFAMCON, p);
        if (r[0] != RC_OK) {
            throw new RtiException("IFAMCON " + resource, r[0], r[1]);
        }
        for (int i = 0; i < 16; i++) {
            token[i] = p.get(56 + i);
        }
        // GTPB einmal vorbereiten, 64 Byte
        put(gtpb, 0, ebcdic("GTPB", 4));
        gtpb.putShort(4, (short) 64).put(6, (byte) 0).put(7, (byte) 1)
            .putInt(8, FLAG_MULTI | (nowait ? FLAG_NOWAIT : 0));
        put(gtpb, 16, token);
        gtpb.putInt(32, out.capacity()).putLong(56, api.addressOf(out));
        connected = true;
    }

    @Override
    public List<byte[]> next() {
        if (!connected) {
            connect();
        }
        while (connected) {
            gtpb.putInt(52, 0);
            int[] r = api.call(RtiApi.IFAMGET, gtpb);
            if (r[0] == RC_OK) {
                List<byte[]> recs = split(out, gtpb.getInt(52));
                if (!recs.isEmpty()) {
                    return recs;
                }
                continue;
            }
            if (r[0] == RC_WARN && r[1] == RSN_NO_DATA) {
                sleepQuietly(200);   // nur bei --nowait
                continue;
            }
            if (r[0] == RC_WARN && (r[1] == RSN_FORCED_OUT || r[1] == RSN_DSC_IN_PROGRESS)) {
                connected = false;   // Verbindung wurde getrennt (z. B. close())
                return null;
            }
            throw new RtiException("IFAMGET " + resource, r[0], r[1]);
        }
        return null;
    }

    /** Puffer mit hintereinander liegenden Sätzen zerlegen; je Satz die ersten 2 Byte = Länge. */
    static List<byte[]> split(ByteBuffer buf, int len) {
        List<byte[]> recs = new ArrayList<>();
        int pos = 0;
        while (pos + 4 <= len) {
            int rl = buf.getShort(pos) & 0xFFFF;
            if (rl < 6 || pos + rl > len) {
                throw new IllegalStateException("ungültige Satzlänge " + rl + " bei " + pos);
            }
            byte[] rec = new byte[rl];
            for (int i = 0; i < rl; i++) {
                rec[i] = buf.get(pos + i);
            }
            recs.add(Records.normalize(rec));
            pos += rl;
        }
        return recs;
    }

    @Override
    public synchronized void close() {
        if (!connected) {
            return;
        }
        ByteBuffer p = ByteBuffer.allocateDirect(24).order(ByteOrder.BIG_ENDIAN);
        put(p, 0, ebcdic("DSPB", 4));
        p.putShort(4, (short) 24).put(6, (byte) 0).put(7, (byte) 1);
        put(p, 8, token);
        int[] r = api.call(RtiApi.IFAMDSC, p);
        connected = false;
        if (r[0] != RC_OK) {
            System.err.println(new RtiException("IFAMDSC " + resource, r[0], r[1]).getMessage());
        }
    }

    @Override
    public String describe() {
        return "inmem:" + resource;
    }

    static void sleepQuietly(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    /** Fehler eines Real-Time-Dienstes mit Klartext laut IBM-Tabelle. */
    public static final class RtiException extends RuntimeException {
        private static final long serialVersionUID = 1L;
        public final int rc, rsn;

        RtiException(String what, int rc, int rsn) {
            super(String.format("%s: RC %02X RSN %04X (%s)", what, rc, rsn, text(rc, rsn)));
            this.rc = rc;
            this.rsn = rsn;
        }

        static String text(int rc, int rsn) {
            switch (rsn) {
                case 0x0401: return "keine Daten (nicht blockierend)";
                case 0x0404: return "GET läuft bereits für diese Verbindung";
                case 0x0406: return "Trennung läuft";
                case 0x0408: return "Verbindung während GET getrennt";
                case 0x0801: return "falscher Modus (Task, PASN=HASN, keine Sperren)";
                case 0x0802: return "Parameterblock ungültig";
                case 0x0803: return "keine freie Verbindung (max. 8 je Ressource, 128 je System)";
                case 0x0804: return "ungültiges Token";
                case 0x0805: return "nicht unterstützte Optionen";
                case 0x0806: return "Ausgabepuffer zu klein";
                case 0x0807: return "Ressource unbekannt oder kein SAF-READ auf IFA.<name>";
                case 0x0C02: return "SMF nicht aktiv";
                default: return rc == 0x10 ? "interner Fehler" : "siehe IBM-Doku";
            }
        }
    }
}
