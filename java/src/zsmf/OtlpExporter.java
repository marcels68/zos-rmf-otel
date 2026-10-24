package zsmf;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * SMF-Ereignisse als OTLP/HTTP-JSON-Logs senden (gleiches Format wie smf/smf_otel.py:
 * je SMF-Typ eine Ressource service.name=zos-smf-TYP, Attribute = ausgewertete Felder).
 */
public final class OtlpExporter {

    static final String SCOPE = "zsmf-otel";
    static final String VERSION = "0.1.0";

    private final URI uri;
    private final HttpClient http;
    private final Map<String, String> headers;

    public OtlpExporter(String endpoint, Map<String, String> headers) {
        String url = endpoint.replaceAll("/+$", "");
        if (!url.endsWith("/v1/logs")) {
            url += "/v1/logs";
        }
        this.uri = URI.create(url);
        this.headers = headers;
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
    }

    public URI uri() {
        return uri;
    }

    /** Einmal senden; wirft bei Netzfehler oder HTTP-Status >= 300. */
    public void send(List<SmfParser.Event> events) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(uri).timeout(Duration.ofSeconds(60))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(toJson(events),
                        StandardCharsets.UTF_8));
        headers.forEach(b::header);
        HttpResponse<String> r = http.send(b.build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() >= 300) {
            String body = r.body();
            throw new java.io.IOException("OTLP HTTP " + r.statusCode() + ": "
                    + body.substring(0, Math.min(200, body.length())));
        }
    }

    // ------------------------------------------------------------------ JSON

    public static String toJson(List<SmfParser.Event> events) {
        long now = System.currentTimeMillis() * 1_000_000L;
        Map<Integer, StringBuilder> byType = new TreeMap<>();
        for (SmfParser.Event e : events) {
            StringBuilder sb = byType.computeIfAbsent(e.type, t -> new StringBuilder());
            if (sb.length() > 0) {
                sb.append(',');
            }
            boolean fail = e.failure();
            sb.append("{\"timeUnixNano\":\"").append(e.timeNanos)
              .append("\",\"observedTimeUnixNano\":\"").append(now)
              .append("\",\"severityNumber\":").append(fail ? 13 : 9)
              .append(",\"severityText\":\"").append(fail ? "WARN" : "INFO")
              .append("\",\"body\":{\"stringValue\":");
            str(sb, e.body);
            sb.append("},\"attributes\":[");
            boolean first = true;
            for (Map.Entry<String, Object> a : e.attrs.entrySet()) {
                if ("".equals(a.getValue())) {
                    continue;
                }
                if (!first) {
                    sb.append(',');
                }
                first = false;
                attr(sb, a.getKey(), a.getValue());
            }
            sb.append("]}");
        }
        StringBuilder out = new StringBuilder("{\"resourceLogs\":[");
        boolean first = true;
        for (Map.Entry<Integer, StringBuilder> t : byType.entrySet()) {
            if (!first) {
                out.append(',');
            }
            first = false;
            out.append("{\"resource\":{\"attributes\":[");
            attr(out, "service.name", "zos-smf-" + t.getKey());
            out.append(',');
            attr(out, "os.type", "zos");
            out.append("]},\"scopeLogs\":[{\"scope\":{\"name\":\"").append(SCOPE)
               .append("\",\"version\":\"").append(VERSION).append("\"},\"logRecords\":[")
               .append(t.getValue()).append("]}]}");
        }
        return out.append("]}").toString();
    }

    static void attr(StringBuilder sb, String key, Object v) {
        sb.append("{\"key\":");
        str(sb, key);
        sb.append(",\"value\":{");
        if (v instanceof Boolean) {
            sb.append("\"boolValue\":").append(v);
        } else if (v instanceof Long || v instanceof Integer) {
            sb.append("\"intValue\":\"").append(v).append('"');
        } else if (v instanceof Double) {
            sb.append("\"doubleValue\":").append(v);
        } else {
            sb.append("\"stringValue\":");
            str(sb, String.valueOf(v));
        }
        sb.append("}}");
    }

    static void str(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }
}
