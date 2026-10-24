package zsmf;

import java.util.List;

/** Quelle für SMF-Sätze. Jeder Satz MIT 4-Byte-RDW (IBM-Offsets gelten direkt). */
public interface RecordSource extends AutoCloseable {

    /** Nächste Sätze (mindestens einer); null = Quelle erschöpft oder getrennt. */
    List<byte[]> next() throws Exception;

    /** Kurzbeschreibung für Log und Statusseite. */
    String describe();

    @Override
    void close();
}
