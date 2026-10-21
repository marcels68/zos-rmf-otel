#!/usr/bin/env python3
"""Erzeugt erfundene SMF-Sätze (Typ 30.4/30.5, 80, 119.11/119.12) als Dump mit RDW, für
Demo und Tests ohne z/OS. Feldlagen wie in smf_otel.py (IBM-Tabellen, s. dort).

    smf_gen.py OUT.bin [--minutes 60] [--end now] [--seed 1] [--rate 1.0]

Erzeugt Sätze für das Zeitfenster [end - minutes, end], Zeitstempel in UTC. Einige
Sätze werden gespannt geschrieben (VBS-Segmente), damit der Leser das mit abdeckt.
"""

import argparse
import random
import struct
import zlib
from datetime import datetime, timedelta, timezone

EBCDIC = "cp037"
SYSTEMS = [("SYSA", "PLEX1"), ("SYSB", "PLEX1")]
USERS = ["IBMUSER", "JMEIER", "KSCHULZ", "OPER01", "BATCH1", "DBADMIN", "WEBSRV"]
JOBS = [  # Job, Programm, Klasse, Benutzer, CPU-Sekunden (Mittel)
    ("PAYROLL1", "PAYCALC", "A", "BATCH1", 12.0), ("BKUPDB2", "ADRDSSU", "B", "DBADMIN", 40.0),
    ("REPORT07", "IKJEFT01", "A", "JMEIER", 3.5), ("CICSPRD1", "DFHSIP", "S", "OPER01", 90.0),
    ("SORTDAY", "SORT", "C", "BATCH1", 25.0), ("IEBCOPY1", "IEBCOPY", "A", "KSCHULZ", 1.2),
    ("COBTEST", "COBTEST", "A", "IBMUSER", 0.4), ("DB2REORG", "DSNUTILB", "B", "DBADMIN", 60.0),
]
TERMINALS = ["TCP00012", "TCP00034", "TCP00101", "LU000A1", ""]
ZERT_SESSIONS = [  # Job, Benutzer, Schutz, TLS-Version, Cipher, Verschl., Server-Port, Client-IP
    ("TN3270", "TCPIP", 0x80, 0x0303, "C02F", 18, 23, "10.1.2.31"),
    ("TN3270", "TCPIP", 0x80, 0x0304, "1302", 19, 992, "10.1.2.44"),
    ("ZOSMF", "IZUSVR", 0x80, 0x0303, "C030", 19, 443, "10.1.5.7"),
    ("FTPD1", "FTPD", 0x80, 0x0301, "000A", 4, 21, "192.168.7.20"),
    ("FTPD1", "*FTPUSR*", 0x00, 0, "", 0, 21, "192.168.7.21"),
    ("SSHD", "SSHD", 0x40, 0, "", 19, 22, "10.1.9.3"),
    ("DB2DDF", "DB2PROD", 0x80, 0x0303, "C02F", 18, 446, "10.2.0.15"),
    ("MQCHIN", "MQPROD", 0x00, 0, "", 0, 1414, "10.2.0.90"),
    ("IKED", "IKED", 0x20, 0, "", 19, 500, "10.9.0.1"),
]


def e(s, n):
    return s.ljust(n)[:n].encode(EBCDIC)


def packed_date(dt):
    c = (dt.year - 1900) // 100
    return bytes.fromhex(f"0{c}{dt.year % 100:02d}{dt.timetuple().tm_yday:03d}F")


def hs(dt):
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((dt - midnight).total_seconds() * 100)


def header(rtype, dt, sid, subtype=None, ssi="JES2"):
    """Kopf ab Offset 4 (ohne RDW)."""
    flg = 0x5E if subtype is not None else 0x1E
    h = struct.pack(">BBI", flg, rtype, hs(dt)) + packed_date(dt) + e(sid, 4)
    if subtype is not None:
        h += e(ssi, 4) + struct.pack(">H", subtype)
    return h


def with_rdw(body):
    return struct.pack(">HH", len(body) + 4, 0) + body


def build(head, triplet_count, sections, extra_after_header=b""):
    """Satz mit Abschnittstabelle: sections = Liste von Bytes oder None (fehlt).
    Offsets ab Satzanfang inkl. RDW."""
    start = 4 + len(head) + len(extra_after_header) + 8 * triplet_count
    trip, data, off = b"", b"", start
    for s in sections:
        if s is None:
            trip += struct.pack(">IHH", 0, 0, 0)
        else:
            trip += struct.pack(">IHH", off, len(s), 1)
            data += s
            off += len(s)
    trip += b"\0" * (8 * triplet_count - len(trip))
    return with_rdw(head + extra_after_header + trip + data)


# --------------------------------------------------------------------------- SMF 30


def smf30(dt, sid, plex, job, jobid, step, stepno, pgm, cls, user, cpu, ziip, elapsed,
          subtype, scc=0, sti=0, arc=0):
    head = header(30, dt, sid, subtype)
    pss = (struct.pack(">HH", subtype, 0) + e("05", 2) + e("ZOS", 8) + e("SP7.3.1", 8)
           + e(sid, 8) + e(plex, 8))
    start = dt - timedelta(seconds=elapsed)
    ident = bytearray(250)
    ident[0:8] = e(job, 8)
    ident[8:16] = e(pgm, 8)
    ident[16:24] = e(step, 8)
    ident[24:32] = e(user, 8)
    ident[32:40] = e(jobid, 8)
    struct.pack_into(">H", ident, 40, stepno)
    ident[42:43] = e(cls, 1)
    struct.pack_into(">I", ident, 56, hs(start))
    ident[60:64] = packed_date(start)
    ident[100:108] = e("SYS1", 8)
    ident[108:116] = e(user, 8)
    ident[140:148] = e(cls, 8)
    comp = struct.pack(">HHI", scc, sti, arc) + b"\0" * 8
    cpu_sec = bytearray(188)
    tcb, srb = int(cpu * 90), int(cpu * 10)  # 1/100 s
    struct.pack_into(">II", cpu_sec, 4, tcb, srb)
    struct.pack_into(">I", cpu_sec, 116, int(ziip * 100))
    # Reihenfolge der Triplets: SOF IOF UOF TOF COF AOF ROF POF OOF EOF
    return build(head, 10, [pss, bytes(ident), None, comp, bytes(cpu_sec)])


# --------------------------------------------------------------------------- SMF 80


def smf80(dt, sid, evt, evq, user, group, terminal, job, violation=False, relocs=()):
    head = header(80, dt, sid)                         # Offset 4..17
    des = 0x8000 if violation else 0
    fixed = bytearray(98 - 18)                         # Offset 18..97
    struct.pack_into(">HBB", fixed, 0, des, evt, evq)  # 18 DES, 20 EVT, 21 EVQ
    fixed[4:12] = e(user, 8)                           # 22 USR
    fixed[12:20] = e(group, 8)                         # 30 GRP
    reloc = b"".join(bytes([t, len(d)]) + d for t, d in relocs)
    struct.pack_into(">HH", fixed, 20, 98, len(relocs))  # 38 REL (ab Satzanfang), 40 CNT
    fixed[28:36] = e(terminal, 8) if terminal else b"\0" * 8   # 46 TRM
    fixed[36:44] = e(job, 8)                           # 54 JBN
    fixed[62:66] = e("7740", 4)                        # 80 VRM
    return with_rdw(head + bytes(fixed) + reloc)


# --------------------------------------------------------------------------- SMF 119


def ip4(s):
    return bytes(int(x) for x in s.split(".")) + b"\0" * 12


def tcpip_id(sid, plex, comp="STACK", reason=0x80):
    s = bytearray(64)
    s[0:8], s[8:16], s[16:24] = e(sid, 8), e(plex, 8), e("TCPIP", 8)
    s[24:32], s[32:40], s[40:48], s[48:56] = (e("CS V3R1", 8), e(comp, 8), e("TCPIP", 8),
                                              e("TCPIP", 8))
    struct.pack_into(">HB", s, 58, 0x0021, reason)
    return bytes(s)


def head119(dt, sid, sty):
    # 24: SMF119TRN, 26: reserviert -> als "extra_after_header" vor den Triplets
    return header(119, dt, sid, sty, ssi="TCP")


def zert12(dt, sid, plex, sess, conns, bytes_in, bytes_out, life):
    job, user, prot, ver, cipher, enc, port, cip = sess
    c = bytearray(204)
    struct.pack_into(">QBBB", c, 0, 300 * 4096 * 1_000_000, 1, 0x40, prot)
    c[11:19], c[19:27] = e(job, 8), e(user, 8)
    c[27] = 17 if job == "IKED" else 6
    c[28:44], c[44:60] = ip4("10.0.0.5"), ip4(cip)
    struct.pack_into(">HH", c, 60, port, port)
    tag = {0x80: "T", 0x40: "S", 0x20: "I"}.get(prot, "C")
    c[64:106] = e(f"{tag}-{zlib.crc32(f'{job}{cip}{port}'.encode()):020X}", 42)
    init = life
    struct.pack_into(">IIII", c, 108, init, 0, init // 2, 1)
    struct.pack_into(">QQ", c, 124, init * 9000, init * 40000)
    struct.pack_into(">IIII", c, 156, init + conns, 0, init // 2 + conns // 3, 2)
    struct.pack_into(">QQ", c, 172, init * 9000 + bytes_in, init * 40000 + bytes_out)
    tls = ssh = ips = None
    if prot == 0x80:
        t = bytearray(36)
        struct.pack_into(">BBH", t, 0, 2, 0, ver)
        t[4:10] = e(cipher, 6)
        kex = 20 if cipher.startswith("C0") else (1 if cipher.startswith("13") else 2)
        struct.pack_into(">HHHHHHHH", t, 10, enc, 1 if enc in (18, 19) else 4, kex,
                         7, 2, 6, 2, 2048)
        tls = bytes(t)
    elif prot == 0x40:
        s = bytearray(46)
        struct.pack_into(">BBB", s, 0, 2, 0, 2)
        struct.pack_into(">HHHHHHH", s, 4, 2, 0, enc, 6, 3, enc, 6)
        ssh = bytes(s)
    elif prot == 0x20:
        p = bytearray(64)
        p[0], p[1] = 2, 0
        struct.pack_into(">H", p, 40, enc)
        ips = bytes(p)
    head = head119(dt, sid, 12)
    # Triplets: ID, Summary-Common, TLS, SSH, IPSec, Zertifikats-DNs
    return _with_trn(head, 6, [tcpip_id(sid, plex), bytes(c), tls, ssh, ips, None])


def _with_trn(head, ntrip, sections):
    """SMF 119: ab Offset 24 SMF119TRN (Anzahl Triplets) und 2 Byte Reserve, ab 28 die
    Triplets (zuerst TCP/IP-Identifikation)."""
    return build(head, ntrip, sections, extra_after_header=struct.pack(">HH", ntrip, 0))


def zert11(dt, sid, plex, sess, evt, bytes_in, bytes_out, lport):
    job, user, prot, ver, cipher, enc, port, cip = sess
    c = bytearray(128)
    struct.pack_into(">BBBBB", c, 0, evt, prot, 0, 0, 6)
    c[8:16], c[16:24], c[24:32] = e(job, 8), e("STC01234", 8), e(user, 8)
    struct.pack_into(">I", c, 32, hs(dt))
    c[36:40] = packed_date(dt)
    c[48:64], c[64:80] = ip4(cip), ip4("10.0.0.5")
    struct.pack_into(">HHI", c, 80, lport, port, random.randint(1, 1 << 30))
    struct.pack_into(">QQ", c, 88, bytes_in, bytes_out)
    tls = ssh = None
    if prot & 0x80:
        t = bytearray(168)
        struct.pack_into(">HBBB", t, 0, ver, 2, 1, 2)
        t[56:62] = e(cipher, 6)
        kex = 20 if cipher.startswith("C0") else (1 if cipher.startswith("13") else 2)
        struct.pack_into(">HHH", t, 62, enc, 1 if enc in (18, 19) else 4, kex)
        struct.pack_into(">HH", t, 116, 2, 2048)
        tls = bytes(t)
    elif prot & 0x40:
        ssh = bytes([2]) + b"\0" * 47
    head = head119(dt, sid, 11)
    # Triplets: ID, Common, IP-Filter, TLS, SSH, IPSec, Zertifikats-DNs
    return _with_trn(head, 7, [tcpip_id(sid, plex, reason=0x08), bytes(c), None, tls, ssh,
                               None, None])


# --------------------------------------------------------------------------- Ablauf


def spanned(rec, parts=2):
    """Satz in VBS-Segmente teilen (erstes = 1, mittlere = 3, letztes = 2)."""
    body = rec[4:]
    n = len(body) // parts + 1
    chunks = [body[i:i + n] for i in range(0, len(body), n)]
    out = b""
    for i, ch in enumerate(chunks):
        seg = 1 if i == 0 else 2 if i == len(chunks) - 1 else 3
        out += struct.pack(">HBB", len(ch) + 4, seg, 0) + ch
    return out


def generate(end, minutes, rate=1.0, seed=None):
    rnd = random.Random(seed)
    random.seed(seed)
    start = end - timedelta(minutes=minutes)
    recs = []
    jobno = rnd.randint(10000, 60000)
    t = start
    while t < end:  # ein Takt je Minute
        for sid, plex in SYSTEMS:
            # SMF 30: Jobs mit 1-3 Steps
            for _ in range(rnd.choices([0, 1, 2], [3, 5, 2])[0] if rate else 0):
                job, pgm, cls, user, cpu = rnd.choice(JOBS)
                jobno += 1
                jobid = f"JOB{jobno:05d}"
                total = 0.0
                steps = rnd.randint(1, 3)
                jcc, jsti = 0, 0
                for n in range(1, steps + 1):
                    c = max(0.01, rnd.gauss(cpu / steps, cpu / steps / 3))
                    total += c
                    z = c * rnd.choice([0, 0, 0.3, 0.6]) if pgm in ("DSNUTILB", "DFHSIP",
                                                                    "SORT") else 0
                    scc, sti, arc = 0, 0, 0
                    r = rnd.random()
                    if r < 0.03:
                        scc, sti, arc = rnd.choice([0x0C4, 0x0806, 0x0B37, 0x0322]), 0x0200, 4
                    elif r < 0.05:
                        scc, sti = 0x8000 | 4038, 0x0200
                    elif r < 0.15:
                        scc = rnd.choice([4, 8, 12])
                    if scc or sti:
                        jcc, jsti = scc, sti
                    dt = t + timedelta(seconds=rnd.uniform(0, 59))
                    recs.append(smf30(dt, sid, plex, job, jobid, f"STEP{n:02d}", n, pgm, cls,
                                      user, c, z, rnd.uniform(1, 300), 4, scc, sti, arc))
                dt = t + timedelta(seconds=59.5)
                recs.append(smf30(dt, sid, plex, job, jobid, "", steps, pgm, cls, user, total,
                                  0, rnd.uniform(10, 900), 5, jcc, jsti))
            # SMF 80: Logons (RACINIT) und Zugriffe
            for _ in range(rnd.randint(1, 4)):
                user = rnd.choice(USERS)
                q = rnd.choices([0, 1, 6, 9], [85, 10, 3, 2])[0]
                dt = t + timedelta(seconds=rnd.uniform(0, 59))
                recs.append(smf80(dt, sid, 1, q, user if q != 9 else "XUSER9", "SYS1",
                                  rnd.choice(TERMINALS), user, violation=q != 0))
            if rnd.random() < 0.01:  # Kennwort-Raten: viele Fehlversuche eines Benutzers
                for _ in range(rnd.randint(5, 12)):
                    dt = t + timedelta(seconds=rnd.uniform(0, 59))
                    recs.append(smf80(dt, sid, 1, 1, "KSCHULZ", "SYS1", "TCP00666",
                                      "KSCHULZ", violation=True))
            for _ in range(rnd.randint(0, 3)):
                q = rnd.choices([0, 1, 3], [70, 25, 5])[0]
                dt = t + timedelta(seconds=rnd.uniform(0, 59))
                recs.append(smf80(dt, sid, 2, q, rnd.choice(USERS), "SYS1", "", "BATCH",
                                  violation=q == 1,
                                  relocs=[(1, e("PROD.PAYROLL.DATA", 17))]))
        # SMF 119.12 zERT-Summary alle 5 Minuten, SMF 119.11 bei Verbindungsende
        if t.minute % 5 == 0:
            for sid, plex in SYSTEMS:
                for sess in ZERT_SESSIONS:
                    if rnd.random() < 0.8:
                        recs.append(zert12(t + timedelta(seconds=1), sid, plex, sess,
                                           rnd.randint(1, 60), rnd.randint(10**4, 10**7),
                                           rnd.randint(10**4, 10**8), rnd.randint(10, 999)))
        for _ in range(rnd.randint(0, 3)):
            sid, plex = rnd.choice(SYSTEMS)
            dt = t + timedelta(seconds=rnd.uniform(0, 59))
            recs.append(zert11(dt, sid, plex, rnd.choice(ZERT_SESSIONS), rnd.choice([3, 4]),
                               rnd.randint(500, 10**6), rnd.randint(500, 10**7),
                               rnd.randint(1024, 65000)))
        t += timedelta(minutes=1)
    return recs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--end", default="now", help="Ende des Zeitfensters (ISO, UTC) oder now")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--rate", type=float, default=1.0)
    a = ap.parse_args()
    end = (datetime.now(timezone.utc) if a.end == "now"
           else datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc))
    end = end.replace(tzinfo=None, second=0, microsecond=0)
    recs = generate(end, a.minutes, a.rate, a.seed)
    with open(a.out, "wb") as f:
        for i, r in enumerate(recs):
            f.write(spanned(r, 3) if i % 50 == 7 else r)
    print(f"{len(recs)} SMF-Sätze nach {a.out}")


if __name__ == "__main__":
    main()
