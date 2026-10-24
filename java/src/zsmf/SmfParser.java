package zsmf;

import java.nio.charset.Charset;
import java.time.Duration;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * SMF-Sätze 30.4/30.5, 80 und 119.11/119.12 auswerten (Port von smf/smf_otel.py, gleiche
 * Attribute und Texte; der Gleichheitstest java/parity.sh vergleicht beide).
 *
 * Alle Offsets ab Satzanfang EINSCHLIESSLICH RDW (wie in IBMs Tabellen). Herkunft der
 * Feldlagen: IBM-SMF-Tabellen auf pacsys.com/smf; zIIP-Zeit SMF 30 aus adapya-base.
 */
public final class SmfParser {

    static final Charset EBCDIC = Charset.forName("IBM037");

    private final ZoneId zone;

    public SmfParser(ZoneId zone) {
        this.zone = zone;
    }

    /** Ein ausgewerteter SMF-Satz. */
    public static final class Event {
        public final int type;
        public final long timeNanos;
        public final Map<String, Object> attrs;
        public final String body;

        Event(int type, long timeNanos, Map<String, Object> attrs, String body) {
            this.type = type;
            this.timeNanos = timeNanos;
            this.attrs = attrs;
            this.body = body;
        }

        public boolean failure() {
            return "failure".equals(attrs.get("racf.result"));
        }
    }

    // ------------------------------------------------------------------ Hilfen

    static int u8(byte[] b, int o) {
        return b[o] & 0xFF;
    }

    static int u16(byte[] b, int o) {
        return ((b[o] & 0xFF) << 8) | (b[o + 1] & 0xFF);
    }

    static long u32(byte[] b, int o) {
        return ((long) u16(b, o) << 16) | u16(b, o + 2);
    }

    static long u64(byte[] b, int o) {
        return (u32(b, o) << 32) | u32(b, o + 4);
    }

    static String ebc(byte[] b, int o, int n) {
        String s = new String(b, o, Math.min(n, Math.max(0, b.length - o)), EBCDIC);
        int end = s.length();
        while (end > 0 && (s.charAt(end - 1) == ' ' || s.charAt(end - 1) == '\0')) {
            end--;
        }
        return s.substring(0, end);
    }

    static String hex(byte[] b, int o, int n) {
        StringBuilder sb = new StringBuilder();
        for (int i = o; i < o + n; i++) {
            sb.append(String.format("%02X", b[i] & 0xFF));
        }
        return sb.toString();
    }

    /** Gepacktes Datum 0cyydddF -> {Jahr, Tag}. */
    static int[] smfDate(byte[] b, int o) {
        String h = hex(b, o, 4);
        for (int i = 1; i < 7; i++) {
            if (!Character.isDigit(h.charAt(i))) {
                throw new IllegalArgumentException("ungültiges SMF-Datum " + h);
            }
        }
        int year = 1900 + 100 * (h.charAt(1) - '0') + Integer.parseInt(h.substring(2, 4));
        return new int[] {year, Integer.parseInt(h.substring(4, 7))};
    }

    ZonedDateTime smfTime(byte[] b, int oTime, int oDate) {
        int[] yd = smfDate(b, oDate);
        long hs = u32(b, oTime);
        LocalDateTime t = LocalDateTime.of(yd[0], 1, 1, 0, 0).plusDays(yd[1] - 1L)
                .plus(Duration.ofMillis(hs * 10));
        return t.atZone(zone);
    }

    static long nanos(ZonedDateTime t) {
        return t.toEpochSecond() * 1_000_000_000L + t.getNano();
    }

    static double sec100(byte[] b, int o) {
        return u32(b, o) / 100.0;
    }

    static double round(double v, int digits) {
        return java.math.BigDecimal.valueOf(v).setScale(digits, java.math.RoundingMode.HALF_EVEN)
                .doubleValue();
    }

    static long[] triplet(byte[] b, int o) {
        if (o + 8 > b.length) {
            return new long[] {0, 0, 0};
        }
        return new long[] {u32(b, o), u16(b, o + 4), u16(b, o + 6)};
    }

    static int section(byte[] b, long[] t) {
        if (t[2] == 0 || t[0] == 0 || t[0] + t[1] > b.length) {
            return -1;
        }
        return (int) t[0];
    }

    // ------------------------------------------------------------------ Kopf

    static final class Header {
        int type;
        int flags;
        String sid;
        ZonedDateTime time;
        Integer subtype;
    }

    Header header(byte[] rec) {
        Header h = new Header();
        h.flags = u8(rec, 4);
        h.type = u8(rec, 5);
        h.sid = ebc(rec, 14, 4);
        h.time = smfTime(rec, 6, 10);
        if ((h.flags & 0x40) != 0 && rec.length >= 24) {
            h.subtype = u16(rec, 22);
        }
        return h;
    }

    /** Satz auswerten; null für nicht ausgewertete Typen/Subtypen. */
    public Event parse(byte[] rec) {
        Header h = header(rec);
        Object[] r;
        switch (h.type) {
            case 30: r = parse30(rec, h); break;
            case 80: r = parse80(rec, h); break;
            case 119: r = parse119(rec, h); break;
            default: return null;
        }
        if (r == null) {
            return null;
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> a = (Map<String, Object>) r[0];
        Map<String, Object> all = new LinkedHashMap<>();
        all.put("smf.record.type", (long) h.type);
        all.put("zos.smf.id", h.sid);
        all.putAll(a);
        if (h.subtype != null) {
            all.put("smf.record.subtype", (long) h.subtype);
        }
        return new Event(h.type, nanos(h.time), all, h.sid + " " + r[1]);
    }

    // ------------------------------------------------------------------ SMF 30

    static String[] completion(int scc, int sti) {
        if ((sti & 0x0200) != 0) {
            if ((scc & 0x8000) != 0) {
                return new String[] {"abend", String.format("U%04d", scc & 0x0FFF)};
            }
            return new String[] {"abend", String.format("S%03X", scc & 0x0FFF)};
        }
        if ((sti & 0x0100) != 0) {
            return new String[] {"flushed", "FLUSH"};
        }
        return new String[] {scc == 0 ? "ok" : "rc", String.format("RC%04d", scc)};
    }

    Object[] parse30(byte[] rec, Header h) {
        if (h.subtype == null || (h.subtype != 4 && h.subtype != 5)) {
            return null;
        }
        long[] tSub = triplet(rec, 24), tId = triplet(rec, 32), tCmp = triplet(rec, 48),
                tCpu = triplet(rec, 56);
        Map<String, Object> a = new LinkedHashMap<>();
        int o = section(rec, tSub);
        if (o >= 0 && tSub[1] >= 38) {
            a.put("zos.system.name", ebc(rec, o + 22, 8));
            a.put("zos.sysplex.name", ebc(rec, o + 30, 8));
        }
        o = section(rec, tId);
        if (o < 0) {
            return null;
        }
        a.put("zos.job.name", ebc(rec, o, 8));
        a.put("zos.program.name", ebc(rec, o + 8, 8));
        a.put("zos.step.name", ebc(rec, o + 16, 8));
        a.put("zos.job.id", ebc(rec, o + 32, 8));
        a.put("zos.step.number", (long) u16(rec, o + 40));
        a.put("zos.job.class", tId[1] >= 148 ? ebc(rec, o + 140, 8) : ebc(rec, o + 42, 1));
        a.put("zos.user.id", ebc(rec, o + 108, 8));
        a.put("racf.group", ebc(rec, o + 100, 8));
        try {
            ZonedDateTime start = smfTime(rec, o + 56, o + 60);
            double el = Duration.between(start, h.time).toNanos() / 1e9;
            a.put("smf30.elapsed_seconds", round(el, 2));
        } catch (IllegalArgumentException e) {
            // kein gültiges Startdatum
        }
        o = section(rec, tCmp);
        if (o >= 0 && tCmp[1] >= 8) {
            int scc = u16(rec, o), sti = u16(rec, o + 2);
            long arc = u32(rec, o + 4);
            String[] c = completion(scc, sti);
            a.put("smf30.completion.status", c[0]);
            a.put("smf30.completion.code", c[1]);
            if (c[0].equals("abend")) {
                a.put("smf30.abend.reason", String.format("%08X", arc));
            }
        }
        o = section(rec, tCpu);
        if (o >= 0 && tCpu[1] >= 12) {
            double tcb = sec100(rec, o + 4), srb = sec100(rec, o + 8);
            a.put("smf30.cpu.tcb_seconds", tcb);
            a.put("smf30.cpu.srb_seconds", srb);
            a.put("smf30.cpu.total_seconds", round(tcb + srb, 2));
            if (tCpu[1] >= 120) {
                a.put("smf30.cpu.ziip_seconds", sec100(rec, o + 116));
            }
        }
        String kind = h.subtype == 4 ? "STEP" : "JOB";
        Object cpu = a.get("smf30.cpu.total_seconds");
        String body = "SMF30." + h.subtype + " " + kind + " " + a.get("zos.job.name") + " "
                + a.get("zos.job.id") + " step " + dash(a.get("zos.step.name")) + " pgm "
                + dash(a.get("zos.program.name")) + " user " + dash(a.get("zos.user.id")) + " "
                + a.getOrDefault("smf30.completion.code", "")
                + (cpu != null ? String.format(java.util.Locale.ROOT, " CPU %.2fs", cpu) : "");
        return new Object[] {a, body.replace("  ", " ")};
    }

    private static String dash(Object s) {
        return s == null || s.toString().isEmpty() ? "-" : s.toString();
    }

    // ------------------------------------------------------------------ SMF 80

    static final String[] RACF_EVENTS = {null, "JOBINIT", "ACCESS", "ADDVOL", "RENAMEDS",
        "DELRES", "DELVOL", "DEFINE", "ADDSD", "ADDGROUP", "ADDUSER", "ALTDSD", "ALTGROUP",
        "ALTUSER", "CONNECT", "DELDSD", "DELGROUP", "DELUSER", "PASSWORD", "PERMIT", "RALTER",
        "RDEFINE", "RDELETE", "REMOVE", "SETROPTS", "RVARY"};
    static final String[] JOBINIT_QUAL = {"erfolgreich", "ungültiges Kennwort",
        "ungültige Gruppe", "ungültige OIDCARD", "ungültiges Terminal", "ungültige Anwendung",
        "Benutzer gesperrt (revoked)", "automatisch gesperrt", "erfolgreiche Beendigung",
        "unbekannter Benutzer"};
    static final String[] ACCESS_QUAL = {"erlaubt", "keine Berechtigung", "kein Profil",
        "erlaubt (WARNING)"};

    static java.util.List<Object[]> racfRelocates(byte[] rec, int rel, int cnt) {
        for (int base : new int[] {0, 4}) {
            int pos = base + rel;
            java.util.List<Object[]> out = new java.util.ArrayList<>();
            boolean ok = true;
            for (int i = 0; i < cnt; i++) {
                if (pos + 2 > rec.length || pos + 2 + (rec[pos + 1] & 0xFF) > rec.length) {
                    ok = false;
                    break;
                }
                int typ = rec[pos] & 0xFF, ln = rec[pos + 1] & 0xFF;
                out.add(new Object[] {typ, java.util.Arrays.copyOfRange(rec, pos + 2,
                        pos + 2 + ln)});
                pos += 2 + ln;
            }
            if (ok) {
                return out;
            }
        }
        return java.util.Collections.emptyList();
    }

    private static String qual(String[] table, int q) {
        return q < table.length ? table[q] : "Qualifier " + q;
    }

    Object[] parse80(byte[] rec, Header h) {
        if (rec.length < 62) {
            return null;
        }
        int evt = u8(rec, 20), evq = u8(rec, 21), des = u16(rec, 18);
        Map<String, Object> a = new LinkedHashMap<>();
        a.put("racf.event.code", (long) evt);
        a.put("racf.event.qualifier", (long) evq);
        a.put("racf.event", evt > 0 && evt < RACF_EVENTS.length ? RACF_EVENTS[evt]
                : "EVENT" + evt);
        a.put("racf.user", ebc(rec, 22, 8));
        a.put("racf.group", ebc(rec, 30, 8));
        boolean violation = (des & 0x8000) != 0;
        a.put("racf.violation", violation);
        a.put("racf.user_undefined", (des & 0x4000) != 0);
        a.put("racf.warning", (des & 0x1000) != 0);
        a.put("racf.terminal", ebc(rec, 46, 8));
        a.put("zos.job.name", ebc(rec, 54, 8));
        if (evt == 1) {
            a.put("racf.result", evq == 0 || evq == 8 ? "success" : "failure");
            a.put("racf.reason", qual(JOBINIT_QUAL, evq));
        } else if (evt == 2) {
            a.put("racf.result", evq == 0 || evq == 3 ? "success" : "failure");
            a.put("racf.reason", qual(ACCESS_QUAL, evq));
        } else {
            a.put("racf.result", violation ? "failure" : "success");
            a.put("racf.reason", "Qualifier " + evq);
        }
        for (Object[] r : racfRelocates(rec, u16(rec, 38), u16(rec, 40))) {
            byte[] d = (byte[]) r[1];
            String txt = new String(d, EBCDIC).replaceAll("^[ \\u0000]+|[ \\u0000]+$", "");
            boolean printable = !txt.isEmpty() && txt.chars().allMatch(
                    c -> !Character.isISOControl(c) && c != 0xFFFD);
            a.put("racf.reloc." + r[0], printable ? txt : hex(d, 0, d.length));
        }
        String term = (String) a.get("racf.terminal"), job = (String) a.get("zos.job.name");
        String body = "RACF " + a.get("racf.event") + " " + a.get("racf.result") + " user "
                + dash(a.get("racf.user")) + " (" + a.get("racf.reason") + ")"
                + (term.isEmpty() ? "" : " terminal " + term)
                + (job.isEmpty() ? "" : " job " + job);
        return new Object[] {a, body};
    }

    // ------------------------------------------------------------------ SMF 119 zERT

    static final Map<Integer, String> TLS_VERSIONS = new HashMap<>();
    static final String[] ENC_ALG = {"Unknown", "None", "DES", "DES 40", "3DES", "RC2 40",
        "RC2 128", "RC2", "RC4 40", "RC4 128", "RC4 256", "RC4", "AES CBC 128", "AES CBC 192",
        "AES CBC 256", "AES CTR 128", "AES CTR 192", "AES CTR 256", "AES GCM 128", "AES GCM 256",
        "AES CCM 128", "AES CCM 256", "AES CCM8 128", "AES CCM8 256", "AES 256", "Blowfish",
        "Blowfish CBC", "CAST 128 CBC", "ARCFOUR 128", "ARCFOUR 256", "ARCFOUR", "Rijndael CBC",
        "ACSS", "ARIA 128 CBC", "ARIA 256 CBC", "ARIA 128 GCM", "ARIA 256 GCM",
        "Camellia 128 CBC", "Camellia 256 CBC", "Camellia 128 GCM", "Camellia 256 GCM",
        "ChaCha20 Poly1305", "IDEA CBC", "SEED CBC", "Fortezza", "GOST28147", "TwoFish CBC 256",
        "TwoFish CBC", "TwoFish CBC 192", "TwoFish CBC 128", "Serpent CBC 256",
        "Serpent CBC 192", "Serpent CBC 128"};
    static final java.util.Set<Integer> WEAK_ENC = java.util.Set.of(1, 2, 3, 4, 5, 6, 7, 8, 9,
            10, 11, 28, 29, 30, 44);
    static final String[] MSG_AUTH = {"Unknown", "None/AEAD", "MD2", "HMAC-MD5", "HMAC-SHA1",
        "HMAC-SHA2-224", "HMAC-SHA2-256", "HMAC-SHA2-384", "HMAC-SHA2-512", "AES-GMAC-128",
        "AES-GMAC-256", "AES-128-XCBC-96", "HMAC-SHA2-256-128", "HMAC-SHA2-384-192",
        "HMAC-SHA2-512-256", "HMAC-MD5-96", "HMAC-SHA1-96", "UMAC-64", "UMAC-128", "RIPEMD-160"};
    static final String[] KEX_ALG = {"Unknown", "None", "RSA", "RSA_EXPORT", "RSA_PSK",
        "DH_RSA", "DH_RSA_EXPORT", "DH_DSS", "DH_ANON", "DH_ANON_EXPORT", "DH_DSS_EXPORT",
        "DHE_RSA", "DHE_RSA_EXPORT", "DHE_DSS", "DHE_DSS_EXPORT", "DHE_PSK", "ECDH_ECDSA",
        "ECDH_RSA", "ECDH_ANON", "ECDHE_ECDSA", "ECDHE_RSA", "ECDHE_PSK", "KRB5", "KRB5_EXPORT",
        "PSK", "SRP_SHA_RSA", "SRP_SHA_DSS", "SRP_SHA"};
    static final String[] KEY_TYPE = {"Unknown", "None", "RSA", "DSA", "DH", "ECC"};
    static final Map<String, String> CIPHERS = new HashMap<>();
    static final String[] ZERT11_EVENTS = {null, "Verbindungsaufbau",
        "Änderung Krypto-Attribute", "Verbindungsende", "kurze Verbindung (<10 s)",
        "zERT eingeschaltet", "zERT ausgeschaltet"};

    static {
        TLS_VERSIONS.put(0x0000, "unbekannt");
        TLS_VERSIONS.put(0x0200, "SSLv2");
        TLS_VERSIONS.put(0x0300, "SSLv3");
        TLS_VERSIONS.put(0x0301, "TLSv1.0");
        TLS_VERSIONS.put(0x0302, "TLSv1.1");
        TLS_VERSIONS.put(0x0303, "TLSv1.2");
        TLS_VERSIONS.put(0x0304, "TLSv1.3");
        String[][] c = {
            {"1301", "TLS_AES_128_GCM_SHA256"}, {"1302", "TLS_AES_256_GCM_SHA384"},
            {"1303", "TLS_CHACHA20_POLY1305_SHA256"},
            {"C02B", "ECDHE-ECDSA-AES128-GCM-SHA256"}, {"C02C", "ECDHE-ECDSA-AES256-GCM-SHA384"},
            {"C02F", "ECDHE-RSA-AES128-GCM-SHA256"}, {"C030", "ECDHE-RSA-AES256-GCM-SHA384"},
            {"C027", "ECDHE-RSA-AES128-SHA256"}, {"C028", "ECDHE-RSA-AES256-SHA384"},
            {"C013", "ECDHE-RSA-AES128-SHA"}, {"C014", "ECDHE-RSA-AES256-SHA"},
            {"009C", "RSA-AES128-GCM-SHA256"}, {"009D", "RSA-AES256-GCM-SHA384"},
            {"003C", "RSA-AES128-SHA256"}, {"003D", "RSA-AES256-SHA256"},
            {"002F", "RSA-AES128-SHA"}, {"0035", "RSA-AES256-SHA"},
            {"000A", "RSA-3DES-EDE-CBC-SHA"}, {"0005", "RSA-RC4-128-SHA"},
            {"0004", "RSA-RC4-128-MD5"}};
        for (String[] e : c) {
            CIPHERS.put(e[0], e[1]);
        }
    }

    private static String tab(String[] t, int i, String dflt) {
        return i >= 0 && i < t.length && t[i] != null ? t[i] : dflt;
    }

    static String ipAddr(byte[] b, int o, boolean v6) {
        if (!v6) {
            return u8(b, o) + "." + u8(b, o + 1) + "." + u8(b, o + 2) + "." + u8(b, o + 3);
        }
        int[] g = new int[8];
        for (int i = 0; i < 8; i++) {
            g[i] = u16(b, o + 2 * i);
        }
        // RFC 5952 (wie Pythons ipaddress): längste Nullfolge (>= 2) durch :: ersetzen
        int bestS = -1, bestL = 0;
        for (int i = 0; i < 8; ) {
            if (g[i] == 0) {
                int j = i;
                while (j < 8 && g[j] == 0) {
                    j++;
                }
                if (j - i > bestL && j - i >= 2) {
                    bestS = i;
                    bestL = j - i;
                }
                i = j;
            } else {
                i++;
            }
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 8; i++) {
            if (i == bestS) {
                sb.append("::");
                i += bestL - 1;
                continue;
            }
            if (sb.length() > 0 && sb.charAt(sb.length() - 1) != ':') {
                sb.append(':');
            }
            sb.append(Integer.toHexString(g[i]));
        }
        return sb.toString();
    }

    static String secProto(int f) {
        StringBuilder sb = new StringBuilder();
        if ((f & 0x80) != 0) sb.append("TLS");
        if ((f & 0x40) != 0) sb.append(sb.length() > 0 ? "+SSH" : "SSH");
        if ((f & 0x20) != 0) sb.append(sb.length() > 0 ? "+IPSec" : "IPSec");
        return sb.length() > 0 ? sb.toString() : "none";
    }

    static String secProto12(int p) {
        switch (p) {
            case 0x00: return "none";
            case 0x80: return "TLS";
            case 0x40: return "SSH";
            case 0x20: return "IPSec";
            default: return String.format("0x%02X", p);
        }
    }

    static String ipProto(int p) {
        return p == 6 ? "TCP" : p == 17 ? "UDP" : String.valueOf(p);
    }

    static int tlsAttrs(byte[] rec, int o, int verOff, int cipherOff, int encOff,
                        Map<String, Object> a, Integer keyOff) {
        int ver = u16(rec, o + verOff);
        a.put("zert.tls.version", TLS_VERSIONS.getOrDefault(ver, String.format("0x%04X", ver)));
        String cid = ebc(rec, o + cipherOff, 6).trim();
        a.put("zert.tls.cipher", CIPHERS.getOrDefault(cid.toUpperCase(), cid));
        int enc = u16(rec, o + encOff);
        a.put("zert.tls.encryption", tab(ENC_ALG, enc, String.valueOf(enc)));
        a.put("zert.tls.mac", tab(MSG_AUTH, u16(rec, o + encOff + 2), "?"));
        a.put("zert.tls.key_exchange", tab(KEX_ALG, u16(rec, o + encOff + 4), "?"));
        if (keyOff != null) {
            a.put("zert.tls.server_key", tab(KEY_TYPE, u16(rec, o + keyOff), "?") + " "
                    + u16(rec, o + keyOff + 2));
        }
        return enc;
    }

    static String protectionClass(Map<String, Object> a, Integer enc) {
        if ("none".equals(a.get("zert.protocol"))) {
            return "unprotected";
        }
        Object ver = a.getOrDefault("zert.tls.version", "");
        if (java.util.List.of("SSLv2", "SSLv3", "TLSv1.0", "TLSv1.1").contains(ver)
                || (enc != null && WEAK_ENC.contains(enc))) {
            return "weak";
        }
        return "strong";
    }

    Object[] parse119(byte[] rec, Header h) {
        Integer sty = h.subtype;
        if (sty == null || (sty != 11 && sty != 12) || rec.length < 84) {
            return null;
        }
        long[] tId = triplet(rec, 28);
        Map<String, Object> a = new LinkedHashMap<>();
        a.put("zert.record", sty == 11 ? "connection" : "summary");
        int o = section(rec, tId);
        if (o >= 0 && tId[1] >= 61) {
            a.put("zos.system.name", ebc(rec, o, 8));
            a.put("zos.sysplex.name", ebc(rec, o + 8, 8));
            a.put("zos.tcpip.stack", ebc(rec, o + 16, 8));
        }
        Integer enc = null;
        if (sty == 12) {
            long[] tC = triplet(rec, 36), tTls = triplet(rec, 44), tSsh = triplet(rec, 52),
                    tIps = triplet(rec, 60);
            o = section(rec, tC);
            if (o < 0 || tC[1] < 204) {
                return null;
            }
            int flags = u8(rec, o + 9), prot = u8(rec, o + 10);
            if (u8(rec, o + 8) != 1) {
                return null;   // nur Summary-Intervallsätze
            }
            boolean v6 = (flags & 0x80) != 0;
            int lport = u16(rec, o + 60), hport = u16(rec, o + 62);
            a.put("zert.protocol", secProto12(prot));
            a.put("zos.job.name", ebc(rec, o + 11, 8));
            a.put("zos.user.id", ebc(rec, o + 19, 8));
            a.put("network.transport", ipProto(u8(rec, o + 27)));
            a.put("server.address", ipAddr(rec, o + 28, v6));
            a.put("client.address", ipAddr(rec, o + 44, v6));
            a.put("server.port", lport == hport ? (Object) (long) lport : lport + "-" + hport);
            a.put("zert.local_role", (flags & 0x40) != 0 ? "server"
                    : (flags & 0x20) != 0 ? "client" : "-");
            a.put("zert.session_id", ebc(rec, o + 64, 42));
            // Dauer in Mikrosekunden mit Bit 51 = 1 µs (STCK-Form) -> / 4096 / 1e6
            double iv = Double.parseDouble(Long.toUnsignedString(u64(rec, o))) / 4096e6;
            a.put("zert.interval_seconds", round(iv, 1));
            a.put("zert.connections", u32(rec, o + 156) - u32(rec, o + 108));
            a.put("zert.connections.short", u32(rec, o + 164) - u32(rec, o + 116));
            a.put("zert.connections.active", u32(rec, o + 168));
            a.put("zert.bytes.in", u64(rec, o + 172) - u64(rec, o + 124));
            a.put("zert.bytes.out", u64(rec, o + 180) - u64(rec, o + 132));
            o = section(rec, tTls);
            if (prot == 0x80 && o >= 0 && tTls[1] >= 26) {
                enc = tlsAttrs(rec, o, 2, 4, 10, a, 22);
            }
            o = section(rec, tSsh);
            if (prot == 0x40 && o >= 0 && tSsh[1] >= 22) {
                a.put("zert.ssh.version", (long) u8(rec, o + 2));
                enc = u16(rec, o + 8);
                a.put("zert.ssh.encryption", tab(ENC_ALG, enc, String.valueOf(enc)));
                a.put("zert.ssh.mac", tab(MSG_AUTH, u16(rec, o + 10), "?"));
            }
            o = section(rec, tIps);
            if (prot == 0x20 && o >= 0 && tIps[1] >= 42) {
                a.put("zert.ipsec.ike_version", (u8(rec, o) & 15) + "." + (u8(rec, o + 1) & 15));
                enc = u16(rec, o + 40);
                a.put("zert.ipsec.encryption", tab(ENC_ALG, enc, String.valueOf(enc)));
            }
        } else {
            long[] tC = triplet(rec, 36), tTls = triplet(rec, 52), tSsh = triplet(rec, 60);
            o = section(rec, tC);
            if (o < 0 || tC[1] < 120) {
                return null;
            }
            int evt = u8(rec, o), prot = u8(rec, o + 1), flags = u8(rec, o + 2);
            if (evt == 5 || evt == 6) {
                return null;
            }
            boolean v6 = (flags & 0x80) != 0;
            a.put("zert.event", tab(ZERT11_EVENTS, evt, String.valueOf(evt)));
            a.put("zert.protocol", secProto(prot));
            a.put("network.transport", ipProto(u8(rec, o + 4)));
            a.put("zos.job.name", ebc(rec, o + 8, 8));
            a.put("zos.job.id", ebc(rec, o + 16, 8));
            a.put("zos.user.id", ebc(rec, o + 24, 8));
            a.put("network.peer.address", ipAddr(rec, o + 48, v6));
            a.put("network.local.address", ipAddr(rec, o + 64, v6));
            a.put("network.peer.port", (long) u16(rec, o + 80));
            a.put("network.local.port", (long) u16(rec, o + 82));
            a.put("zert.bytes.in", u64(rec, o + 88));
            a.put("zert.bytes.out", u64(rec, o + 96));
            o = section(rec, tTls);
            if ((prot & 0x80) != 0 && o >= 0 && tTls[1] >= 72) {
                enc = tlsAttrs(rec, o, 0, 56, 62, a, tTls[1] >= 120 ? 116 : null);
            }
            o = section(rec, tSsh);
            if ((prot & 0x40) != 0 && o >= 0 && tSsh[1] >= 1) {
                a.put("zert.ssh.version", (long) u8(rec, o));
            }
        }
        a.put("zert.protection", protectionClass(a, enc));
        Object ver = a.get("zert.tls.version");
        String detail = ver != null ? ver.toString()
                : a.containsKey("zert.ssh.version") ? "SSHv" + a.get("zert.ssh.version") : "";
        String peers = sty == 12
                ? a.get("client.address") + " -> " + a.get("server.address") + ":"
                    + a.get("server.port")
                : a.get("network.local.address") + ":" + a.get("network.local.port") + " <-> "
                    + a.get("network.peer.address") + ":" + a.get("network.peer.port");
        String body = "zERT " + a.get("zert.record") + " " + a.getOrDefault("zert.protocol", "?")
                + " " + detail + " " + a.getOrDefault("zert.tls.cipher", "") + " "
                + a.getOrDefault("zos.job.name", "") + " " + peers + " ["
                + a.get("zert.protection") + "]";
        return new Object[] {a, body.trim().replaceAll("\\s+", " ")};
    }
}
