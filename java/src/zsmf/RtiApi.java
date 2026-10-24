package zsmf;

import java.nio.ByteBuffer;

/**
 * Zugriff auf die SMF-Real-Time-Dienste (IFAMCON/IFAMGET/IFAMDSC). Die echte Umsetzung ist
 * RtiNative (JNI, nur auf z/OS); Tests verwenden einen Nachbau.
 */
public interface RtiApi {

    /** Offsets der Dienste in der Tabelle hinter der CVT-Kette (IBM-Doku, "alternative method"). */
    int IFAMCON = 4;
    int IFAMGET = 8;
    int IFAMDSC = 12;

    /** Adresse eines Direct-ByteBuffers (für das Pufferadressfeld im GTPB). */
    long addressOf(ByteBuffer direct);

    /** Dienst aufrufen; parm = Direct-ByteBuffer mit dem Parameterblock. -> {rc, rsn} */
    int[] call(int service, ByteBuffer parm);
}
