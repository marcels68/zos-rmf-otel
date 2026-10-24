package zsmf;

import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * SMF-Dump als Datei (binär mit RDW, gespannte VBS-Segmente werden zusammengesetzt;
 * optional mit Blockdeskriptoren). Entspricht read_records() in smf/smf_otel.py.
 */
public final class FileSource implements RecordSource {

    private final Path path;
    private final boolean bdw;
    private final DataInputStream in;
    private int blockLeft = 0;
    private static final int CHUNK = 500;

    public FileSource(Path path, boolean bdw) throws IOException {
        this.path = path;
        this.bdw = bdw;
        this.in = new DataInputStream(new java.io.BufferedInputStream(
                Files.newInputStream(path), 1 << 20));
    }

    /** -1 am Dateiende (sauber zwischen Sätzen). */
    private int readU16OrEnd() throws IOException {
        int a = in.read();
        if (a < 0) {
            return -1;
        }
        int b = in.read();
        if (b < 0) {
            throw new EOFException("Datei endet mitten in einem RDW");
        }
        return (a << 8) | b;
    }

    @Override
    public List<byte[]> next() throws IOException {
        List<byte[]> out = new ArrayList<>();
        java.io.ByteArrayOutputStream pieces = null;
        while (out.size() < CHUNK) {
            if (bdw && blockLeft <= 0) {
                int blen = readU16OrEnd();
                if (blen < 0) {
                    break;
                }
                in.readUnsignedShort();
                if (blen < 8) {
                    throw new IOException("ungültiger Blockdeskriptor");
                }
                blockLeft = blen - 4;
                continue;
            }
            int rlen = readU16OrEnd();
            if (rlen < 0) {
                break;
            }
            int seg = in.readUnsignedByte();
            in.readUnsignedByte();
            if (rlen < 4) {
                throw new IOException("ungültiges RDW (Länge " + rlen + "); Datei binär mit RDW "
                        + "übertragen? (FTP: quote site rdw)");
            }
            byte[] body = new byte[rlen - 4];
            in.readFully(body);
            blockLeft -= rlen;
            if (seg == 0) {
                out.add(Records.withRdw(body));
            } else if (seg == 1) {
                pieces = new java.io.ByteArrayOutputStream();
                pieces.write(body);
            } else if (seg == 3 && pieces != null) {
                pieces.write(body);
            } else if (seg == 2 && pieces != null) {
                pieces.write(body);
                out.add(Records.withRdw(pieces.toByteArray()));
                pieces = null;
            }
        }
        return out.isEmpty() ? null : out;
    }

    @Override
    public String describe() {
        return "file:" + path;
    }

    @Override
    public void close() {
        try {
            in.close();
        } catch (IOException e) {
            // egal
        }
    }

}
