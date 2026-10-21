#!/usr/bin/env python3
"""SMF -> OpenTelemetry: liest SMF-Sätze aus einem Dump (IFASMFDP/IFASMFDL, binär mit RDW
übertragen) und schickt jeden ausgewerteten Satz als OTLP-Log (HTTP/JSON) an einen
OpenTelemetry Collector. Grafana/Loki wertet die Logs aus (Zählungen, Summen, Tabellen).

    smf_otel.py DUMP [DUMP ...] [--otlp http://localhost:4318] [--tz Europe/Berlin]
    smf_otel.py DUMP --dry-run            # OTLP-JSON ausgeben statt senden
    smf_otel.py DUMP --print              # eine Zeile je Satz (zum Prüfen)
    smf_otel.py DUMP --hexdump 80         # Sätze eines Typs als Hex/EBCDIC (Layout prüfen)

Nur Python-Standardbibliothek. Ausgewertet werden:
  SMF 30 Subtyp 4/5 (Step-/Job-Ende): Job, Step, Programm, Benutzer, CPU (TCB/SRB/zIIP), Dauer
  SMF 80 (RACF): Ereignis/Qualifier (z. B. RACINIT = Logon/Jobstart, Erfolg/Fehler), Benutzer
  SMF 119 Subtyp 11/12 (zERT): Stack, System; Schutzattribute noch NICHT (s. README)
Andere Sätze werden gezählt und übersprungen.

Herkunft der Feldlagen (README, Abschnitt SMF):
  Kopf, SMF 30: adapya-base (Software AG, Apache 2.0, adapya/base/smfrecordz.py)
  SMF 119 Kopf und TCP/IP-Identifikation: zCS-SMF (github.com/viccross/zCS-SMF, TCPCONN.rexx)
  SMF 80: SMF80EVT/EVQ an 20/21 laut IBM-Doku; Benutzer/Gruppe/Relocate-Zeiger danach
          (unbestätigt -> mit echten Sätzen und --hexdump prüfen)
Alle Offsets hier gelten ab Satzanfang EINSCHLIESSLICH RDW (wie in IBMs Tabellen).
"""

import argparse
import json
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

AGENT_NAME = "smf-otel"
AGENT_VERSION = "0.1.0"
EBCDIC = "cp037"  # für Namen (A-Z, 0-9, @#$) gleich IBM-1047


# --------------------------------------------------------------------------- Lesen


def read_records(f, bdw=False):
    """SMF-Sätze aus einer Datei mit RDW (variable Länge, ggf. gespannt = VBS).

    Liefert je Satz die Bytes MIT 4-Byte-RDW, damit die IBM-Offsets direkt passen.
    Gespannte Segmente (SDW-Steuerbyte 1 = erstes, 3 = mittleres, 2 = letztes) werden
    zusammengesetzt. bdw=True: Datei enthält zusätzlich Blockdeskriptoren (RECFM=VB(S),
    als RECFM=U übertragen)."""
    data = f.read()
    pos = 0
    pieces = None
    end = len(data)
    blk_end = 0  # nur mit bdw: Ende des aktuellen Blocks
    while pos + 4 <= end:
        if bdw and pos >= blk_end:
            blen = struct.unpack_from(">H", data, pos)[0]
            if blen < 8 or pos + blen > end:
                raise ValueError(f"ungültiger Blockdeskriptor bei Byte {pos}")
            blk_end = pos + blen
            pos += 4
            continue
        rlen, seg = struct.unpack_from(">HB", data, pos)
        if rlen < 4 or pos + rlen > end:
            raise ValueError(f"ungültiges RDW bei Byte {pos} (Länge {rlen}); "
                             "Datei binär mit RDW übertragen? (FTP: quote site rdw)")
        body = data[pos + 4:pos + rlen]
        pos += rlen
        if seg == 0:
            yield struct.pack(">HH", len(body) + 4, 0) + body
        elif seg == 1:
            pieces = [body]
        elif seg == 3 and pieces is not None:
            pieces.append(body)
        elif seg == 2 and pieces is not None:
            pieces.append(body)
            whole = b"".join(pieces)
            pieces = None
            if len(whole) + 4 <= 0xFFFF:
                yield struct.pack(">HH", len(whole) + 4, 0) + whole
            else:  # SMF-Sätze sind höchstens 32 KB; zur Sicherheit trotzdem liefern
                yield b"\xff\xff\x00\x00" + whole
        # sonst: verwaistes Folgesegment -> verwerfen


# --------------------------------------------------------------------------- Hilfen


def u8(b, o):
    return b[o]


def u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def ebc(b, o, n):
    return b[o:o + n].decode(EBCDIC, errors="replace").rstrip(" \x00")


def smf_date(b, o):
    """Gepacktes Datum 0cyydddF -> (Jahr, Tag im Jahr)."""
    h = b[o:o + 4].hex().upper()
    if len(h) != 8 or not h[1:7].isdigit():
        raise ValueError(f"ungültiges SMF-Datum {h}")
    year = 1900 + 100 * int(h[1]) + int(h[2:4])
    return year, int(h[4:7])


def smf_time(b, o_time, o_date, tz):
    """SMF-Zeit (1/100 s seit Mitternacht) + Datum -> datetime (lokal laut tz)."""
    year, day = smf_date(b, o_date)
    hs = u32(b, o_time)
    local = datetime(year, 1, 1) + timedelta(days=day - 1, milliseconds=hs * 10)
    return local.replace(tzinfo=tz)


def seconds100(b, o):
    return u32(b, o) / 100.0


def triplet(b, o):
    """Offset/Länge/Anzahl eines Abschnitts (4+2+2 Bytes)."""
    if o + 8 > len(b):
        return 0, 0, 0
    return u32(b, o), u16(b, o + 4), u16(b, o + 6)


def section(b, trip):
    off, ln, num = trip
    if num == 0 or off == 0 or off + ln > len(b):
        return None
    return off


# --------------------------------------------------------------------------- Kopf


def header(rec, tz):
    flg = u8(rec, 4)
    h = {"type": u8(rec, 5), "flags": flg, "sid": ebc(rec, 14, 4),
         "time": smf_time(rec, 6, 10, tz)}
    if flg & 0x40 and len(rec) >= 24:  # Subtypen gültig
        h["ssi"] = ebc(rec, 18, 4)
        h["subtype"] = u16(rec, 22)
    return h


# --------------------------------------------------------------------------- SMF 30

SMF30_SUBTYPES = {1: "Jobstart", 2: "Intervall", 3: "letztes Intervall", 4: "Step-Ende",
                  5: "Job-Ende", 6: "Systemadressraum"}


def parse_30(rec, h, tz):
    if h.get("subtype") not in (4, 5):
        return None
    t_sub, t_id, t_cpu = triplet(rec, 24), triplet(rec, 32), triplet(rec, 56)
    t_cmp = triplet(rec, 48)
    a = {}
    o = section(rec, t_sub)
    if o is not None and t_sub[1] >= 38:
        a["zos.system.name"] = ebc(rec, o + 22, 8)
        a["zos.sysplex.name"] = ebc(rec, o + 30, 8)
    o = section(rec, t_id)
    if o is None:
        return None
    a.update({
        "zos.job.name": ebc(rec, o, 8),
        "zos.program.name": ebc(rec, o + 8, 8),
        "zos.step.name": ebc(rec, o + 16, 8),
        "zos.job.id": ebc(rec, o + 32, 8),
        "zos.step.number": u16(rec, o + 40),
        "zos.job.class": ebc(rec, o + 140, 8) if t_id[1] >= 148 else ebc(rec, o + 42, 1),
        "zos.user.id": ebc(rec, o + 108, 8) or ebc(rec, o + 24, 8),
        "racf.group": ebc(rec, o + 100, 8),
    })
    try:  # Dauer: Initiator hat Step ausgewählt -> Satz geschrieben
        start = smf_time(rec, o + 56, o + 60, tz)
        a["smf30.elapsed_seconds"] = round((h["time"] - start).total_seconds(), 2)
    except ValueError:
        pass
    o = section(rec, t_cpu)
    if o is not None and t_cpu[1] >= 120:
        tcb, srb = seconds100(rec, o + 4), seconds100(rec, o + 8)
        a["smf30.cpu.tcb_seconds"] = tcb
        a["smf30.cpu.srb_seconds"] = srb
        a["smf30.cpu.ziip_seconds"] = seconds100(rec, o + 116)
        a["smf30.cpu.total_seconds"] = round(tcb + srb, 2)
    o = section(rec, t_cmp)
    if o is not None and t_cmp[1] >= 4:
        # Feldbedeutung unbestätigt (Step-Completion-Code und Flags): roh mitgeben
        a["smf30.completion.raw"] = rec[o:o + 4].hex().upper()
    kind = "STEP" if h["subtype"] == 4 else "JOB"
    cpu = a.get("smf30.cpu.total_seconds")
    body = (f"SMF30.{h['subtype']} {kind} {a['zos.job.name']} {a['zos.job.id']} "
            f"step {a['zos.step.name'] or '-'} pgm {a['zos.program.name'] or '-'} "
            f"user {a['zos.user.id'] or '-'}"
            + (f" CPU {cpu:.2f}s" if cpu is not None else ""))
    return a, body


# --------------------------------------------------------------------------- SMF 80

RACF_EVENTS = {
    1: "JOBINIT", 2: "ACCESS", 3: "ADDVOL", 4: "RENAMEDS", 5: "DELRES", 6: "DELVOL",
    7: "DEFINE", 8: "ADDSD", 9: "ADDGROUP", 10: "ADDUSER", 11: "ALTDSD", 12: "ALTGROUP",
    13: "ALTUSER", 14: "CONNECT", 15: "DELDSD", 16: "DELGROUP", 17: "DELUSER",
    18: "PASSWORD", 19: "PERMIT", 20: "RALTER", 21: "RDEFINE", 22: "RDELETE",
    23: "REMOVE", 24: "SETROPTS", 25: "RVARY",
}
# Event 1 (RACINIT: TSO-Logon, Jobstart, Anwendungs-Anmeldung)
JOBINIT_QUAL = {
    0: "erfolgreich", 1: "ungültiges Kennwort", 2: "ungültige Gruppe",
    3: "ungültige OIDCARD", 4: "ungültiges Terminal", 5: "ungültige Anwendung",
    6: "Benutzer gesperrt (revoked)", 7: "automatisch gesperrt", 8: "erfolgreiche Beendigung",
    9: "unbekannter Benutzer",
}
JOBINIT_OK = {0, 8}
# Event 2 (Zugriff auf Ressource)
ACCESS_QUAL = {0: "erlaubt", 1: "keine Berechtigung", 2: "kein Profil",
               3: "erlaubt (WARNING)"}


def racf_relocates(rec, rel, cnt):
    """Relocate-Abschnitte (Typ 1 Byte, Länge 1 Byte, Daten). Der Bezugspunkt von
    SMF80REL ist nicht bestätigt: es wird SMF80FLG (Offset 4) und Satzanfang probiert
    und die Variante genommen, bei der die Kette genau aufgeht."""
    for base in (4, 0):
        pos, out, ok = base + rel, [], True
        for _ in range(cnt):
            if pos + 2 > len(rec):
                ok = False
                break
            typ, ln = rec[pos], rec[pos + 1]
            if pos + 2 + ln > len(rec):
                ok = False
                break
            out.append((typ, rec[pos + 2:pos + 2 + ln]))
            pos += 2 + ln
        if ok and cnt and pos <= len(rec):
            return out
    return []


def parse_80(rec, h, tz):
    if len(rec) < 44:
        return None
    evt, evq = u8(rec, 20), u8(rec, 21)
    a = {
        "racf.event.code": evt,
        "racf.event.qualifier": evq,
        "racf.event": RACF_EVENTS.get(evt, f"EVENT{evt}"),
        "racf.user": ebc(rec, 22, 8),
        "racf.group": ebc(rec, 30, 8),
    }
    if evt == 1:
        a["racf.result"] = "success" if evq in JOBINIT_OK else "failure"
        a["racf.reason"] = JOBINIT_QUAL.get(evq, f"Qualifier {evq}")
    elif evt == 2:
        a["racf.result"] = "success" if evq in (0, 3) else "failure"
        a["racf.reason"] = ACCESS_QUAL.get(evq, f"Qualifier {evq}")
    else:
        a["racf.result"] = "success" if evq == 0 else "failure"
        a["racf.reason"] = f"Qualifier {evq}"
    for typ, data in racf_relocates(rec, u16(rec, 38), u16(rec, 40)):
        txt = data.decode(EBCDIC, errors="replace").strip(" \x00")
        printable = txt and all(c.isprintable() for c in txt)
        a[f"racf.reloc.{typ}"] = txt if printable else data.hex().upper()
    body = (f"RACF {a['racf.event']} {a['racf.result']} user {a['racf.user'] or '-'}"
            f" ({a['racf.reason']})")
    return a, body


# --------------------------------------------------------------------------- SMF 119


def parse_119(rec, h, tz):
    sty = h.get("subtype")
    if sty not in (11, 12) or len(rec) < 36:
        return None
    ntrip = u16(rec, 24)
    t_id = triplet(rec, 28)
    a = {"smf119.subtype": sty,
         "zert.record": "connection detail" if sty == 11 else "summary"}
    o = section(rec, t_id)
    if o is not None and t_id[1] >= 61:
        a.update({
            "zos.system.name": ebc(rec, o, 8),
            "zos.sysplex.name": ebc(rec, o + 8, 8),
            "zos.tcpip.stack": ebc(rec, o + 16, 8),
            "zos.tcpip.release": ebc(rec, o + 24, 8),
        })
    # Weitere Abschnitte (zERT-Verbindung, Schutzattribute TLS/SSH/IPsec, Statistik):
    # Lage und Länge mitgeben; Feldinhalte erst mit bestätigtem Layout auswerten.
    secs = []
    for i in range(1, max(ntrip, 1)):
        off, ln, num = triplet(rec, 28 + 8 * i)
        secs.append(f"{ln}x{num}" if num else "-")
    a["zert.sections"] = ",".join(secs)
    body = (f"zERT {a['zert.record']} stack {a.get('zos.tcpip.stack', '?')} "
            f"system {a.get('zos.system.name', '?')} sections {a['zert.sections']}")
    return a, body


PARSERS = {30: parse_30, 80: parse_80, 119: parse_119}


def parse(rec, tz):
    """-> (Kopf, Attribute, Text) oder None für nicht ausgewertete Sätze."""
    h = header(rec, tz)
    p = PARSERS.get(h["type"])
    if p is None:
        return None
    r = p(rec, h, tz)
    if r is None:
        return None
    a, body = r
    a = {"smf.record.type": h["type"], "zos.smf.id": h["sid"], **a}
    if "subtype" in h:
        a["smf.record.subtype"] = h["subtype"]
    return h, a, body


# --------------------------------------------------------------------------- OTLP


def attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def to_otlp(events):
    """events: Liste (Kopf, Attribute, Text). Je SMF-Typ eine Ressource
    service.name=zos-smf-<typ> (in Loki ein eigener Stream je Typ)."""
    by_type = {}
    now = str(time.time_ns())
    for h, a, body in events:
        ts = int(h["time"].timestamp() * 1e9)
        failure = a.get("racf.result") == "failure"
        by_type.setdefault(h["type"], []).append({
            "timeUnixNano": str(ts),
            "observedTimeUnixNano": now,
            "severityNumber": 13 if failure else 9,
            "severityText": "WARN" if failure else "INFO",
            "body": {"stringValue": body},
            "attributes": [attr(k, v) for k, v in a.items() if v != ""],
        })
    return {"resourceLogs": [{
        "resource": {"attributes": [attr("service.name", f"zos-smf-{t}"),
                                    attr("os.type", "zos")]},
        "scopeLogs": [{"scope": {"name": AGENT_NAME, "version": AGENT_VERSION},
                       "logRecords": recs}],
    } for t, recs in sorted(by_type.items())]}


def send_otlp(endpoint, payload, timeout=60):
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/logs"):
        url += "/v1/logs"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


# --------------------------------------------------------------------------- Ablauf


def hexdump(rec, width=16):
    lines = []
    for i in range(0, len(rec), width):
        chunk = rec[i:i + width]
        txt = chunk.decode(EBCDIC, errors="replace")
        txt = "".join(c if c.isprintable() and c != "�" else "." for c in txt)
        lines.append(f"{i:05X} {chunk.hex(' ').upper():<{width * 3}} {txt}")
    return "\n".join(lines)


def get_tz(name):
    if name.upper() == "UTC":
        return timezone.utc
    if name[:1] in "+-":
        sign = 1 if name[0] == "+" else -1
        hh, _, mm = name[1:].partition(":")
        return timezone(sign * timedelta(hours=int(hh), minutes=int(mm or 0)))
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dumps", nargs="+", help="SMF-Dump(s), binär mit RDW")
    ap.add_argument("--otlp", default="http://localhost:4318", help="OTLP/HTTP-Endpunkt")
    ap.add_argument("--tz", default="UTC",
                    help="Zeitzone der SMF-Zeitstempel, z. B. Europe/Berlin oder +02:00")
    ap.add_argument("--bdw", action="store_true", help="Datei enthält Blockdeskriptoren")
    ap.add_argument("--batch", type=int, default=500, help="Sätze je OTLP-Sendung")
    ap.add_argument("--dry-run", action="store_true", help="OTLP-JSON ausgeben")
    ap.add_argument("--print", action="store_true", help="eine Zeile je Satz ausgeben")
    ap.add_argument("--hexdump", type=int, metavar="TYP", help="Sätze dieses Typs hexadezimal")
    args = ap.parse_args(argv)
    tz = get_tz(args.tz)

    stats, events, sent = {}, [], 0

    def flush():
        nonlocal events, sent
        if not events:
            return
        payload = to_otlp(events)
        if args.dry_run:
            json.dump(payload, sys.stdout, indent=1, ensure_ascii=False)
            print()
        elif not args.print:
            send_otlp(args.otlp, payload)
        sent += len(events)
        events = []

    for path in args.dumps:
        with open(path, "rb") as f:
            for rec in read_records(f, args.bdw):
                rtype = rec[5] if len(rec) > 5 else -1
                if args.hexdump is not None:
                    if rtype == args.hexdump:
                        print(f"--- Satz Typ {rtype}, Länge {len(rec)}")
                        print(hexdump(rec))
                    continue
                key = str(rtype)
                try:
                    r = parse(rec, tz)
                except (ValueError, struct.error, IndexError) as e:
                    stats[key + " fehlerhaft"] = stats.get(key + " fehlerhaft", 0) + 1
                    print(f"Satz Typ {rtype} nicht lesbar: {e}", file=sys.stderr)
                    continue
                if r is None:
                    stats[key + " übersprungen"] = stats.get(key + " übersprungen", 0) + 1
                    continue
                stats[key] = stats.get(key, 0) + 1
                if args.print:
                    print(f"{r[0]['time'].isoformat()} {r[2]}")
                events.append(r)
                if len(events) >= args.batch:
                    flush()
    if args.hexdump is None:
        flush()
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(stats.items())) or "keine Sätze"
        target = "ausgegeben" if (args.dry_run or args.print) else f"an {args.otlp} gesendet"
        print(f"SMF-Sätze {summary}; {sent} {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)
