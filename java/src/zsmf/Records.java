package zsmf;

/** Hilfen rund um die Satzform (RDW). */
final class Records {

    private Records() {
    }

    static boolean packedDateAt(byte[] r, int o) {
        if (o + 4 > r.length) {
            return false;
        }
        int b0 = r[o] & 0xFF, b3 = r[o + 3] & 0xFF;
        if ((b0 >> 4) != 0 || (b0 & 0x0F) > 2 || (b3 & 0x0F) != 0x0F) {
            return false;
        }
        for (int i = 1; i < 3; i++) {
            int b = r[o + i] & 0xFF;
            if ((b >> 4) > 9 || (b & 0x0F) > 9) {
                return false;
            }
        }
        return (b3 >> 4) <= 9;
    }

    /**
     * Satz auf die Form "RDW + Daten" bringen. Erkennt am gepackten Datum (Offset 10 mit
     * RDW), ob die 2 Byte Segmentfeld fehlen (nur Länge vorn, Datum dann bei 8) oder das
     * RDW ganz fehlt (Datum bei 6), und ergänzt das Fehlende.
     */
    static byte[] normalize(byte[] r) {
        if (packedDateAt(r, 10)) {
            return r;
        }
        if (packedDateAt(r, 8)) {             // nur 2 Byte Länge vor den Daten
            return withRdw(java.util.Arrays.copyOfRange(r, 2, r.length));
        }
        if (packedDateAt(r, 6)) {             // gar kein RDW (z. B. JZOS RecordReader)
            return withRdw(r);
        }
        return r;                             // unbekannt: Parser meldet den Fehler
    }

    static byte[] withRdw(byte[] data) {
        byte[] r = new byte[data.length + 4];
        int len = r.length;
        r[0] = (byte) (len >> 8);
        r[1] = (byte) len;
        System.arraycopy(data, 0, r, 4, data.length);
        return r;
    }
}
