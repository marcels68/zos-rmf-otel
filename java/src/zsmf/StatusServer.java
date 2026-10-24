package zsmf;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.BlockingQueue;

/** Kleine Statusseite: /health, /status (JSON), /metrics (Prometheus-Textformat). */
final class StatusServer {

    private final HttpServer server;

    private StatusServer(HttpServer s) {
        this.server = s;
    }

    static StatusServer start(int port, Stats st, BlockingQueue<?> queue) throws IOException {
        HttpServer s = HttpServer.create(new InetSocketAddress(port), 0);
        s.createContext("/health", ex -> reply(ex, 200, "text/plain", "ok\n"));
        s.createContext("/status", ex -> reply(ex, 200, "application/json", json(st, queue)));
        s.createContext("/metrics", ex -> reply(ex, 200, "text/plain; version=0.0.4",
                metrics(st, queue)));
        s.setExecutor(null);
        s.start();
        Main.log("Statusseite auf Port " + s.getAddress().getPort());
        return new StatusServer(s);
    }

    void stop() {
        server.stop(0);
    }

    private static void reply(HttpExchange ex, int code, String type, String body)
            throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", type);
        ex.sendResponseHeaders(code, b.length);
        try (OutputStream o = ex.getResponseBody()) {
            o.write(b);
        }
    }

    static String json(Stats st, BlockingQueue<?> q) {
        Map<Integer, Long> t = new TreeMap<>();
        st.readByType.forEach((k, v) -> t.put(k, v.get()));
        StringBuilder sb = new StringBuilder("{");
        sb.append("\"source\":");
        OtlpExporter.str(sb, st.source);
        sb.append(",\"uptime_seconds\":").append((System.currentTimeMillis() - st.started) / 1000)
          .append(",\"read\":{");
        boolean first = true;
        for (Map.Entry<Integer, Long> e : t.entrySet()) {
            sb.append(first ? "" : ",").append('"').append(e.getKey()).append("\":")
              .append(e.getValue());
            first = false;
        }
        sb.append("},\"skipped\":").append(st.skipped).append(",\"errors\":").append(st.errors)
          .append(",\"queued\":").append(q.size()).append(",\"queue_full\":")
          .append(st.queueFull).append(",\"sent\":").append(st.sent).append(",\"batches\":")
          .append(st.batches).append(",\"send_failures\":").append(st.sendFailures)
          .append(",\"last_record\":");
        if (st.lastEventNanos > 0) {
            OtlpExporter.str(sb, Instant.ofEpochSecond(0, st.lastEventNanos).toString());
        } else {
            sb.append("null");
        }
        sb.append(",\"last_error\":");
        if (st.lastError != null) {
            OtlpExporter.str(sb, st.lastError);
        } else {
            sb.append("null");
        }
        return sb.append("}\n").toString();
    }

    static String metrics(Stats st, BlockingQueue<?> q) {
        StringBuilder sb = new StringBuilder();
        sb.append("# TYPE zsmf_records_read_total counter\n");
        st.readByType.forEach((k, v) -> sb.append("zsmf_records_read_total{smf_type=\"")
                .append(k).append("\"} ").append(v.get()).append('\n'));
        sb.append("# TYPE zsmf_records_skipped_total counter\nzsmf_records_skipped_total ")
          .append(st.skipped).append('\n')
          .append("# TYPE zsmf_records_errors_total counter\nzsmf_records_errors_total ")
          .append(st.errors).append('\n')
          .append("# TYPE zsmf_records_sent_total counter\nzsmf_records_sent_total ")
          .append(st.sent).append('\n')
          .append("# TYPE zsmf_send_failures_total counter\nzsmf_send_failures_total ")
          .append(st.sendFailures).append('\n')
          .append("# TYPE zsmf_queue_depth gauge\nzsmf_queue_depth ").append(q.size()).append('\n')
          .append("# TYPE zsmf_last_record_timestamp_seconds gauge\n"
                  + "zsmf_last_record_timestamp_seconds ")
          .append(st.lastEventNanos / 1_000_000_000L).append('\n');
        return sb.toString();
    }
}
