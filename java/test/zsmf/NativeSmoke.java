package zsmf;

import java.nio.ByteBuffer;

/**
 * Prüft die JNI-Anbindung (libzsmfrti) außerhalb von z/OS: Tabellenindex von
 * GetDirectBufferAddress, Rückgabeformat, Fehlerfälle. Ein echter Dienstaufruf (RC X'10'
 * außerhalb z/OS) findet nicht statt.
 *   java -Djava.library.path=DIR -cp build/classes:build/test zsmf.NativeSmoke
 */
public final class NativeSmoke {
    public static void main(String[] args) {
        RtiNative n = new RtiNative();
        ByteBuffer a = ByteBuffer.allocateDirect(64), b = ByteBuffer.allocateDirect(64);
        long pa = n.addressOf(a), pb = n.addressOf(b);
        int fail = 0;
        if (pa == 0 || pb == 0 || pa == pb || n.addressOf(a) != pa) {
            System.out.println("FEHLER: addressOf " + pa + " " + pb);
            fail++;
        }
        int[] r = n.call(99, a);
        if (r[0] != 0x10) {
            System.out.println("FEHLER: ungültiger Dienst -> RC " + r[0]);
            fail++;
        }
        r = n.call(RtiApi.IFAMGET, ByteBuffer.allocate(64));      // kein Direct-Buffer
        if (r[0] != 8 || r[1] != 0x0802) {
            System.out.println("FEHLER: Heap-Buffer -> " + r[0] + "/" + r[1]);
            fail++;
        }
        r = n.call(RtiApi.IFAMGET, a);
        if (r[0] != 0x10) {
            System.out.println("FEHLER: außerhalb z/OS RC X'10' erwartet, war " + r[0]);
            fail++;
        }
        System.out.println("JNI-Anbindung: " + (fail == 0 ? "ok" : fail + " Fehler"));
        System.exit(fail == 0 ? 0 : 1);
    }
}
