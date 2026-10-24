#!/bin/sh
# zsmf-otel auf z/OS UNIX starten (Echtzeit über das SMF Real-Time Interface).
#   zsmf-live.sh                 Vordergrund
#   nohup zsmf-live.sh &         Hintergrund (oder als Started Task: ZSMFLIVE.jcl)
# Verzeichnis: zsmf-otel.jar und libzsmfrti.so liegen neben diesem Skript.
D=$(cd "$(dirname "$0")" && pwd)
: "${ZSMF_RESOURCE:=IFASMF.OTEL}"            # In-Memory-Ressource (SMFPRMxx INMEM)
: "${ZSMF_OTLP:=http://collector.example.com:4318}"
: "${ZSMF_TZ:=Europe/Berlin}"               # Zeitzone der SMF-Zeitstempel
export LIBPATH="$D:${LIBPATH:-}"
exec java -Xms64m -Xmx512m -Djava.library.path="$D" -jar "$D/zsmf-otel.jar" \
  --source "inmem:$ZSMF_RESOURCE" --otlp "$ZSMF_OTLP" --tz "$ZSMF_TZ" \
  --status-port "${ZSMF_STATUS_PORT:-9464}" --checkpoint "$D/zsmf.checkpoint" "$@"
