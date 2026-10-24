package zsmf;

import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/** Zähler für Log und Statusseite. */
final class Stats {
    final long started = System.currentTimeMillis();
    final Map<Integer, AtomicLong> readByType = new ConcurrentHashMap<>();
    final AtomicLong skipped = new AtomicLong();
    final AtomicLong errors = new AtomicLong();
    final AtomicLong queueFull = new AtomicLong();
    final AtomicLong sent = new AtomicLong();
    final AtomicLong batches = new AtomicLong();
    final AtomicLong sendFailures = new AtomicLong();
    volatile long lastEventNanos;
    volatile String lastError;
    volatile String source = "";

    void read(int type) {
        readByType.computeIfAbsent(type, t -> new AtomicLong()).incrementAndGet();
    }

    long readTotal() {
        return readByType.values().stream().mapToLong(AtomicLong::get).sum();
    }

    String summary() {
        Map<Integer, Long> t = new TreeMap<>();
        readByType.forEach((k, v) -> t.put(k, v.get()));
        return "gelesen " + t + ", übersprungen " + skipped + ", fehlerhaft " + errors
                + ", gesendet " + sent + ", Sendefehler " + sendFailures;
    }
}
