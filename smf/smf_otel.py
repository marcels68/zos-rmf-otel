#!/usr/bin/env python3
"""SMF -> OpenTelemetry: liest SMF-Sätze aus einem Dump (IFASMFDP/IFASMFDL, binär mit RDW
übertragen) und schickt jeden ausgewerteten Satz als OTLP-Log (HTTP/JSON) an einen
OpenTelemetry Collector. Grafana/Loki wertet die Logs aus (Zählungen, Summen, Tabellen).

    smf_otel.py DUMP [DUMP ...] [--otlp http://localhost:4318] [--tz Europe/Berlin]
    smf_otel.py DUMP --dry-run            # OTLP-JSON ausgeben statt senden
    smf_otel.py DUMP --print              # eine Zeile je Satz (zum Prüfen)
    smf_otel.py DUMP --hexdump 80         # Sätze eines Typs als Hex/EBCDIC (Layout prüfen)

Nur Python-Standardbibliothek. Ausgewertet werden:
  SMF 30 Subtyp 4/5 (Step-/Job-Ende): Job, Step, Programm, Benutzer, CPU (TCB/SRB/zIIP),
         Dauer, Completion-Code (RC/Abend)
  SMF 80 (RACF): Ereignis/Qualifier (RACINIT = Logon/Jobstart, Zugriffe, Befehle),
         Benutzer, Gruppe, Terminal, Job, Verstoß
  SMF 119 Subtyp 11/12 (zERT): Schutz (TLS/SSH/IPsec/keiner), TLS-Version, Cipher,
         Verfahren, Endpunkte, Ports, Job, Verbindungs- und Byte-Zähler
Andere Sätze werden gezählt und übersprungen.

Herkunft der Feldlagen: IBM-SMF-Tabellen, wiedergegeben auf pacsys.com/smf/ (smf30.htm,
smf80.htm, smf119_subtype11.htm, smf119_subtype12.htm); zIIP-Zeit in SMF 30 (Offset 116 im
Prozessorabschnitt) aus adapya-base (Software AG, Apache 2.0), weil die pacsys-Seite zu
SMF 30 den Stand z/OS V1R3 hat.
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
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
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


def completion(scc, sti):
    """SMF30SCC/SMF30STI -> (Status, Code). STI Bit 6 (X'0200') = Abend, Bit 7 (X'0100') =
    Step übersprungen (flushed). SCC bei Abend X'0ccc' = Systemabend, X'8ccc' = Userabend,
    sonst Returncode."""
    if sti & 0x0200:
        if scc & 0x8000:
            return "abend", f"U{scc & 0x0FFF:04d}"
        return "abend", f"S{scc & 0x0FFF:03X}"
    if sti & 0x0100:
        return "flushed", "FLUSH"
    return ("ok" if scc == 0 else "rc"), f"RC{scc:04d}"


def parse_30(rec, h, tz):
    if h.get("subtype") not in (4, 5):
        return None
    t_sub, t_id, t_cmp, t_cpu = (triplet(rec, 24), triplet(rec, 32), triplet(rec, 48),
                                 triplet(rec, 56))
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
        "zos.user.id": ebc(rec, o + 108, 8),
        "racf.group": ebc(rec, o + 100, 8),
    })
    try:  # Dauer: Initiator hat Step/Job ausgewählt (SMF30SIT/STD) -> Satz geschrieben
        start = smf_time(rec, o + 56, o + 60, tz)
        a["smf30.elapsed_seconds"] = round((h["time"] - start).total_seconds(), 2)
    except ValueError:
        pass
    o = section(rec, t_cmp)
    if o is not None and t_cmp[1] >= 8:
        scc, sti, arc = u16(rec, o), u16(rec, o + 2), u32(rec, o + 4)
        status, code = completion(scc, sti)
        a["smf30.completion.status"] = status
        a["smf30.completion.code"] = code
        if status == "abend":
            a["smf30.abend.reason"] = f"{arc:08X}"
    o = section(rec, t_cpu)
    if o is not None and t_cpu[1] >= 12:
        tcb, srb = seconds100(rec, o + 4), seconds100(rec, o + 8)
        a["smf30.cpu.tcb_seconds"] = tcb
        a["smf30.cpu.srb_seconds"] = srb
        a["smf30.cpu.total_seconds"] = round(tcb + srb, 2)
        if t_cpu[1] >= 120:  # SMF30_TIME_ON_zIIP (neuere z/OS-Stände)
            a["smf30.cpu.ziip_seconds"] = seconds100(rec, o + 116)
    kind = "STEP" if h["subtype"] == 4 else "JOB"
    cpu = a.get("smf30.cpu.total_seconds")
    body = (f"SMF30.{h['subtype']} {kind} {a['zos.job.name']} {a['zos.job.id']} "
            f"step {a['zos.step.name'] or '-'} pgm {a['zos.program.name'] or '-'} "
            f"user {a['zos.user.id'] or '-'} {a.get('smf30.completion.code', '')}"
            + (f" CPU {cpu:.2f}s" if cpu is not None else ""))
    return a, body.replace("  ", " ")


# --------------------------------------------------------------------------- SMF 80

RACF_EVENTS = {
    1: "JOBINIT", 2: "ACCESS", 3: "ADDVOL", 4: "RENAMEDS", 5: "DELRES", 6: "DELVOL",
    7: "DEFINE", 8: "ADDSD", 9: "ADDGROUP", 10: "ADDUSER", 11: "ALTDSD", 12: "ALTGROUP",
    13: "ALTUSER", 14: "CONNECT", 15: "DELDSD", 16: "DELGROUP", 17: "DELUSER",
    18: "PASSWORD", 19: "PERMIT", 20: "RALTER", 21: "RDEFINE", 22: "RDELETE",
    23: "REMOVE", 24: "SETROPTS", 25: "RVARY",
}
# Event 1 = RACINIT (TSO-Logon, Jobstart, Anmeldung einer Anwendung)
JOBINIT_QUAL = {
    0: "erfolgreich", 1: "ungültiges Kennwort", 2: "ungültige Gruppe",
    3: "ungültige OIDCARD", 4: "ungültiges Terminal", 5: "ungültige Anwendung",
    6: "Benutzer gesperrt (revoked)", 7: "automatisch gesperrt", 8: "erfolgreiche Beendigung",
    9: "unbekannter Benutzer",
}
ACCESS_QUAL = {0: "erlaubt", 1: "keine Berechtigung", 2: "kein Profil",
               3: "erlaubt (WARNING)"}


def racf_relocates(rec, rel, cnt):
    """Relocate-Abschnitte ab SMF80REL (Typ 1 Byte, Länge 1 Byte, Daten). Bezugspunkt:
    Satzanfang (pacsys: 'Offset from beginning of record'); zur Sicherheit wird auch
    SMF80FLG (+4) probiert und die Variante genommen, bei der die Kette aufgeht."""
    for base in (0, 4):
        pos, out, ok = base + rel, [], True
        for _ in range(cnt):
            if pos + 2 > len(rec) or pos + 2 + rec[pos + 1] > len(rec):
                ok = False
                break
            typ, ln = rec[pos], rec[pos + 1]
            out.append((typ, rec[pos + 2:pos + 2 + ln]))
            pos += 2 + ln
        if ok:
            return out
    return []


def parse_80(rec, h, tz):
    """Feldlagen laut pacsys.com/smf/smf80.htm (IBM SMF-Handbuch)."""
    if len(rec) < 62:
        return None
    evt, evq, des = u8(rec, 20), u8(rec, 21), u16(rec, 18)
    a = {
        "racf.event.code": evt,
        "racf.event.qualifier": evq,
        "racf.event": RACF_EVENTS.get(evt, f"EVENT{evt}"),
        "racf.user": ebc(rec, 22, 8),
        "racf.group": ebc(rec, 30, 8),
        "racf.violation": bool(des & 0x8000),
        "racf.user_undefined": bool(des & 0x4000),
        "racf.warning": bool(des & 0x1000),
        "racf.terminal": ebc(rec, 46, 8),
        "zos.job.name": ebc(rec, 54, 8),
    }
    if evt == 1:
        a["racf.result"] = "success" if evq in (0, 8) else "failure"
        a["racf.reason"] = JOBINIT_QUAL.get(evq, f"Qualifier {evq}")
    elif evt == 2:
        a["racf.result"] = "success" if evq in (0, 3) else "failure"
        a["racf.reason"] = ACCESS_QUAL.get(evq, f"Qualifier {evq}")
    else:
        a["racf.result"] = "failure" if a["racf.violation"] else "success"
        a["racf.reason"] = f"Qualifier {evq}"
    for typ, data in racf_relocates(rec, u16(rec, 38), u16(rec, 40)):
        txt = data.decode(EBCDIC, errors="replace").strip(" \x00")
        printable = txt and all(c.isprintable() and c != "�" for c in txt)
        a[f"racf.reloc.{typ}"] = txt if printable else data.hex().upper()
    body = (f"RACF {a['racf.event']} {a['racf.result']} user {a['racf.user'] or '-'}"
            f" ({a['racf.reason']})"
            + (f" terminal {a['racf.terminal']}" if a["racf.terminal"] else "")
            + (f" job {a['zos.job.name']}" if a["zos.job.name"] else ""))
    return a, body


# --------------------------------------------------------------------------- SMF 119 zERT
# Feldlagen laut pacsys.com/smf/smf119_subtype11.htm und smf119_subtype12.htm

TLS_VERSIONS = {0x0000: "unbekannt", 0x0200: "SSLv2", 0x0300: "SSLv3", 0x0301: "TLSv1.0",
                0x0302: "TLSv1.1", 0x0303: "TLSv1.2", 0x0304: "TLSv1.3"}
ENC_ALG = {
    0: "Unknown", 1: "None", 2: "DES", 3: "DES 40", 4: "3DES", 5: "RC2 40", 6: "RC2 128",
    7: "RC2", 8: "RC4 40", 9: "RC4 128", 10: "RC4 256", 11: "RC4", 12: "AES CBC 128",
    13: "AES CBC 192", 14: "AES CBC 256", 15: "AES CTR 128", 16: "AES CTR 192",
    17: "AES CTR 256", 18: "AES GCM 128", 19: "AES GCM 256", 20: "AES CCM 128",
    21: "AES CCM 256", 22: "AES CCM8 128", 23: "AES CCM8 256", 24: "AES 256",
    25: "Blowfish", 26: "Blowfish CBC", 27: "CAST 128 CBC", 28: "ARCFOUR 128",
    29: "ARCFOUR 256", 30: "ARCFOUR", 31: "Rijndael CBC", 32: "ACSS", 33: "ARIA 128 CBC",
    34: "ARIA 256 CBC", 35: "ARIA 128 GCM", 36: "ARIA 256 GCM", 37: "Camellia 128 CBC",
    38: "Camellia 256 CBC", 39: "Camellia 128 GCM", 40: "Camellia 256 GCM",
    41: "ChaCha20 Poly1305", 42: "IDEA CBC", 43: "SEED CBC", 44: "Fortezza",
    45: "GOST28147", 46: "TwoFish CBC 256", 47: "TwoFish CBC", 48: "TwoFish CBC 192",
    49: "TwoFish CBC 128", 50: "Serpent CBC 256", 51: "Serpent CBC 192",
    52: "Serpent CBC 128",
}
# Verfahren, die heute als schwach gelten (für die Auswertung "schwache Verschlüsselung")
WEAK_ENC = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 28, 29, 30, 44}
MSG_AUTH = {
    0: "Unknown", 1: "None/AEAD", 2: "MD2", 3: "HMAC-MD5", 4: "HMAC-SHA1",
    5: "HMAC-SHA2-224", 6: "HMAC-SHA2-256", 7: "HMAC-SHA2-384", 8: "HMAC-SHA2-512",
    9: "AES-GMAC-128", 10: "AES-GMAC-256", 11: "AES-128-XCBC-96", 12: "HMAC-SHA2-256-128",
    13: "HMAC-SHA2-384-192", 14: "HMAC-SHA2-512-256", 15: "HMAC-MD5-96",
    16: "HMAC-SHA1-96", 17: "UMAC-64", 18: "UMAC-128", 19: "RIPEMD-160",
}
KEX_ALG = {
    0: "Unknown", 1: "None", 2: "RSA", 3: "RSA_EXPORT", 4: "RSA_PSK", 5: "DH_RSA",
    6: "DH_RSA_EXPORT", 7: "DH_DSS", 8: "DH_ANON", 9: "DH_ANON_EXPORT", 10: "DH_DSS_EXPORT",
    11: "DHE_RSA", 12: "DHE_RSA_EXPORT", 13: "DHE_DSS", 14: "DHE_DSS_EXPORT", 15: "DHE_PSK",
    16: "ECDH_ECDSA", 17: "ECDH_RSA", 18: "ECDH_ANON", 19: "ECDHE_ECDSA", 20: "ECDHE_RSA",
    21: "ECDHE_PSK", 22: "KRB5", 23: "KRB5_EXPORT", 24: "PSK", 25: "SRP_SHA_RSA",
    26: "SRP_SHA_DSS", 27: "SRP_SHA",
}
KEY_TYPE = {0: "Unknown", 1: "None", 2: "RSA", 3: "DSA", 4: "DH", 5: "ECC"}
# Häufige TLS-Cipher-Suites (IANA-Register); unbekannte bleiben als Hex-Kennung
CIPHERS = {
    "1301": "TLS_AES_128_GCM_SHA256", "1302": "TLS_AES_256_GCM_SHA384",
    "1303": "TLS_CHACHA20_POLY1305_SHA256",
    "C02B": "ECDHE-ECDSA-AES128-GCM-SHA256", "C02C": "ECDHE-ECDSA-AES256-GCM-SHA384",
    "C02F": "ECDHE-RSA-AES128-GCM-SHA256", "C030": "ECDHE-RSA-AES256-GCM-SHA384",
    "C027": "ECDHE-RSA-AES128-SHA256", "C028": "ECDHE-RSA-AES256-SHA384",
    "C013": "ECDHE-RSA-AES128-SHA", "C014": "ECDHE-RSA-AES256-SHA",
    "009C": "RSA-AES128-GCM-SHA256", "009D": "RSA-AES256-GCM-SHA384",
    "003C": "RSA-AES128-SHA256", "003D": "RSA-AES256-SHA256",
    "002F": "RSA-AES128-SHA", "0035": "RSA-AES256-SHA", "000A": "RSA-3DES-EDE-CBC-SHA",
    "0005": "RSA-RC4-128-SHA", "0004": "RSA-RC4-128-MD5",
}
SEC_PROTO = {0x00: "none", 0x80: "TLS", 0x40: "SSH", 0x20: "IPSec"}
ZERT11_EVENTS = {1: "Verbindungsaufbau", 2: "Änderung Krypto-Attribute",
                 3: "Verbindungsende", 4: "kurze Verbindung (<10 s)",
                 5: "zERT eingeschaltet", 6: "zERT ausgeschaltet"}
IP_PROTO = {6: "TCP", 17: "UDP"}


def ip_addr(b, o, v6):
    raw = b[o:o + 16]
    if v6:
        import ipaddress
        return str(ipaddress.IPv6Address(raw))
    return ".".join(str(x) for x in raw[:4])


def sec_proto(flags):
    names = [n for bit, n in ((0x80, "TLS"), (0x40, "SSH"), (0x20, "IPSec")) if flags & bit]
    return "+".join(names) or "none"


def tls_attrs(rec, o, ver_off, cipher_off, enc_off, a, key_off=None):
    ver = u16(rec, o + ver_off)
    a["zert.tls.version"] = TLS_VERSIONS.get(ver, f"0x{ver:04X}")
    cid = ebc(rec, o + cipher_off, 6).strip()
    a["zert.tls.cipher"] = CIPHERS.get(cid.upper(), cid)
    enc = u16(rec, o + enc_off)
    a["zert.tls.encryption"] = ENC_ALG.get(enc, str(enc))
    a["zert.tls.mac"] = MSG_AUTH.get(u16(rec, o + enc_off + 2), "?")
    a["zert.tls.key_exchange"] = KEX_ALG.get(u16(rec, o + enc_off + 4), "?")
    if key_off is not None:
        a["zert.tls.server_key"] = (f"{KEY_TYPE.get(u16(rec, o + key_off), '?')} "
                                    f"{u16(rec, o + key_off + 2)}")
    return enc


def protection_class(a, enc):
    """Grobe Einstufung für Dashboards: unprotected / weak / strong."""
    if a.get("zert.protocol") == "none":
        return "unprotected"
    ver = a.get("zert.tls.version", "")
    if ver in ("SSLv2", "SSLv3", "TLSv1.0", "TLSv1.1") or (enc is not None and enc in WEAK_ENC):
        return "weak"
    return "strong"


def parse_119(rec, h, tz):
    sty = h.get("subtype")
    if sty not in (11, 12) or len(rec) < 84:
        return None
    t_id = triplet(rec, 28)
    a = {"zert.record": "connection" if sty == 11 else "summary"}
    o = section(rec, t_id)
    if o is not None and t_id[1] >= 61:
        a.update({
            "zos.system.name": ebc(rec, o, 8),
            "zos.sysplex.name": ebc(rec, o + 8, 8),
            "zos.tcpip.stack": ebc(rec, o + 16, 8),
        })
    enc = None
    if sty == 12:
        t_c, t_tls, t_ssh, t_ips = (triplet(rec, 36), triplet(rec, 44), triplet(rec, 52),
                                    triplet(rec, 60))
        o = section(rec, t_c)
        if o is None or t_c[1] < 204:
            return None
        flags, prot = u8(rec, o + 9), u8(rec, o + 10)
        if u8(rec, o + 8) != 1:  # nur Summary-Intervallsätze, keine Ein-/Aus-Ereignisse
            return None
        v6 = bool(flags & 0x80)
        lport, hport = u16(rec, o + 60), u16(rec, o + 62)
        a.update({
            "zert.protocol": SEC_PROTO.get(prot, f"0x{prot:02X}"),
            "zos.job.name": ebc(rec, o + 11, 8),
            "zos.user.id": ebc(rec, o + 19, 8),
            "network.transport": IP_PROTO.get(u8(rec, o + 27), str(u8(rec, o + 27))),
            "server.address": ip_addr(rec, o + 28, v6),
            "client.address": ip_addr(rec, o + 44, v6),
            "server.port": lport if lport == hport else f"{lport}-{hport}",
            "zert.local_role": ("server" if flags & 0x40 else
                                "client" if flags & 0x20 else "-"),
            "zert.session_id": ebc(rec, o + 64, 42),
            "zert.interval_seconds": round(struct.unpack_from(">Q", rec, o)[0] / 4096e6, 1),
        })
        d = lambda off, n: (struct.unpack_from(">Q" if n == 8 else ">I", rec, o + off)[0])
        a["zert.connections"] = d(156, 4) - d(108, 4)        # neu im Intervall
        a["zert.connections.short"] = d(164, 4) - d(116, 4)
        a["zert.connections.active"] = d(168, 4)
        a["zert.bytes.in"] = d(172, 8) - d(124, 8)
        a["zert.bytes.out"] = d(180, 8) - d(132, 8)
        o = section(rec, t_tls)
        if prot == 0x80 and o is not None and t_tls[1] >= 26:
            enc = tls_attrs(rec, o, 2, 4, 10, a, key_off=22)
        o = section(rec, t_ssh)
        if prot == 0x40 and o is not None and t_ssh[1] >= 22:
            a["zert.ssh.version"] = u8(rec, o + 2)
            enc = u16(rec, o + 8)
            a["zert.ssh.encryption"] = ENC_ALG.get(enc, str(enc))
            a["zert.ssh.mac"] = MSG_AUTH.get(u16(rec, o + 10), "?")
        o = section(rec, t_ips)
        if prot == 0x20 and o is not None and t_ips[1] >= 42:
            a["zert.ipsec.ike_version"] = f"{u8(rec, o) & 15}.{u8(rec, o + 1) & 15}"
            enc = u16(rec, o + 40)
            a["zert.ipsec.encryption"] = ENC_ALG.get(enc, str(enc))
    else:
        t_c, t_tls, t_ssh = triplet(rec, 36), triplet(rec, 52), triplet(rec, 60)
        o = section(rec, t_c)
        if o is None or t_c[1] < 120:
            return None
        evt, prot, flags = u8(rec, o), u8(rec, o + 1), u8(rec, o + 2)
        if evt in (5, 6):
            return None
        v6 = bool(flags & 0x80)
        a.update({
            "zert.event": ZERT11_EVENTS.get(evt, str(evt)),
            "zert.protocol": sec_proto(prot),
            "network.transport": IP_PROTO.get(u8(rec, o + 4), str(u8(rec, o + 4))),
            "zos.job.name": ebc(rec, o + 8, 8),
            "zos.job.id": ebc(rec, o + 16, 8),
            "zos.user.id": ebc(rec, o + 24, 8),
            "network.peer.address": ip_addr(rec, o + 48, v6),
            "network.local.address": ip_addr(rec, o + 64, v6),
            "network.peer.port": u16(rec, o + 80),
            "network.local.port": u16(rec, o + 82),
            "zert.bytes.in": struct.unpack_from(">Q", rec, o + 88)[0],
            "zert.bytes.out": struct.unpack_from(">Q", rec, o + 96)[0],
        })
        o = section(rec, t_tls)
        if prot & 0x80 and o is not None and t_tls[1] >= 72:
            enc = tls_attrs(rec, o, 0, 56, 62, a, key_off=116 if t_tls[1] >= 120 else None)
        o = section(rec, t_ssh)
        if prot & 0x40 and o is not None and t_ssh[1] >= 1:
            a["zert.ssh.version"] = u8(rec, o)
    a["zert.protection"] = protection_class(a, enc)
    detail = a.get("zert.tls.version") or (f"SSHv{a['zert.ssh.version']}"
                                           if "zert.ssh.version" in a else "")
    if sty == 12:
        peers = (f"{a['client.address']} -> {a['server.address']}:{a['server.port']}")
    else:
        peers = (f"{a['network.local.address']}:{a['network.local.port']} <-> "
                 f"{a['network.peer.address']}:{a['network.peer.port']}")
    body = (f"zERT {a['zert.record']} {a.get('zert.protocol', '?')} {detail} "
            f"{a.get('zert.tls.cipher', '')} {a.get('zos.job.name', '')} {peers} "
            f"[{a['zert.protection']}]")
    return a, " ".join(body.split())


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
    body = f"{h['sid']} {body}"  # System-ID vorn: lesbar, und Loki verwirft sonst
    #                              gleiche Zeilen mit gleichem Zeitstempel als Duplikat
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
        d = h["time"] - EPOCH  # ganzzahlig rechnen (float verliert ~100 ns)
        ts = (d.days * 86400 + d.seconds) * 10**9 + d.microseconds * 1000
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


def send_otlp(endpoint, payload, timeout=60, retries=6):
    """POST an <endpoint>/v1/logs; bei Verbindungsfehlern bis zu `retries` Wiederholungen
    (2, 4, 8 ... s), z. B. wenn der Collector gerade startet."""
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/logs"):
        url += "/v1/logs"
    data = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as e:
            if attempt == retries:
                raise
            wait = 2 ** (attempt + 1)
            print(f"OTLP nicht erreichbar ({e}), neuer Versuch in {wait} s", file=sys.stderr)
            time.sleep(wait)


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
