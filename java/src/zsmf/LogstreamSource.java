package zsmf;

import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/**
 * SMF-Sätze aus einem SMF-Log-Stream über den IFASEXIT-Subsystem-Exit. Die DD wird in der
 * JCL zugeordnet (siehe java/jcl/ZSMFLOGR.jcl):
 *
 *   //SMFIN DD DSN=IFASMF.DEFAULT,RECFM=VB,LRECL=32756,BLKSIZE=32760,
 *   //  SUBSYS=(LOGR,IFASEXIT,'FROM=(2026/297,08:00),TO=YOUNGEST,LOCAL','SID(SYSA)')
 *
 * Gelesen wird mit JZOS (com.ibm.jzos.RecordReader, Teil von IBM Java auf z/OS), per
 * Reflection, damit das Programm auch ohne JZOS übersetzt werden kann.
 */
public final class LogstreamSource implements RecordSource {

    private static final int CHUNK = 500;
    private final String ddName;
    private final Object reader;
    private final Method read;
    private final Method close;
    private final byte[] buf;

    public LogstreamSource(String dd) throws Exception {
        this.ddName = dd.toUpperCase().replaceFirst("^(//)?DD:", "");
        Class<?> rr = Class.forName("com.ibm.jzos.RecordReader");
        Class<?> zc = Class.forName("com.ibm.jzos.ZFileConstants");
        int flags = zc.getField("FLAG_DISP_SHR").getInt(null);
        Method newReader = rr.getMethod("newReader", String.class, int.class);
        this.reader = newReader.invoke(null, "//DD:" + ddName, flags);
        this.read = rr.getMethod("read", byte[].class);
        this.close = rr.getMethod("close");
        int lrecl = (Integer) rr.getMethod("getLrecl").invoke(reader);
        this.buf = new byte[Math.max(lrecl, 32760) + 4];
    }

    @Override
    public List<byte[]> next() throws Exception {
        List<byte[]> out = new ArrayList<>();
        while (out.size() < CHUNK) {
            int n = (Integer) read.invoke(reader, (Object) buf);
            if (n < 0) {
                break;
            }
            // RecordReader liefert V-Sätze ohne RDW; normalize() erkennt beides
            out.add(Records.normalize(java.util.Arrays.copyOf(buf, n)));
        }
        return out.isEmpty() ? null : out;
    }

    @Override
    public String describe() {
        return "logstream:DD:" + ddName;
    }

    @Override
    public void close() {
        try {
            close.invoke(reader);
        } catch (Exception e) {
            // egal
        }
    }
}
