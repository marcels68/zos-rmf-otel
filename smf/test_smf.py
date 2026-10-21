#!/usr/bin/env python3
"""Tests für smf_otel.py (Leser/Parser) und smf_gen.py (Testdaten).

    cd smf && python3 -m unittest -v test_smf.py

Die Tests "Handsatz" bauen Sätze direkt nach den Offsets der IBM-Tabellen (pacsys.com),
unabhängig vom Generator, damit ein Fehler im Generator den Parser nicht "bestätigt".
"""

import io
import os
import struct
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smf_gen as gen  # noqa: E402
import smf_otel as smf  # noqa: E402

UTC = timezone.utc
T = datetime(2026, 10, 21, 13, 45, 30, 250000)


def parse_one(rec, tz=UTC):
    out = list(smf.read_records(io.BytesIO(rec)))
    assert len(out) == 1, len(out)
    return smf.parse(out[0], tz)


def e(s, n):
    return s.ljust(n).encode("cp037")


class HeaderTest(unittest.TestCase):
    def test_date_time(self):
        rec = gen.with_rdw(gen.header(80, T, "SYSA") + b"\0" * 100)
        h = smf.header(rec, UTC)
        self.assertEqual(h["type"], 80)
        self.assertEqual(h["sid"], "SYSA")
        self.assertEqual(h["time"], T.replace(microsecond=250000, tzinfo=UTC))
        self.assertNotIn("subtype", h)

    def test_packed_date(self):
        self.assertEqual(gen.packed_date(T).hex().upper(), "0126294F")  # 21.10.2026 = Tag 294
        self.assertEqual(smf.smf_date(bytes.fromhex("0099365F"), 0), (1999, 365))

    def test_timezone(self):
        rec = gen.with_rdw(gen.header(80, T, "SYSA") + b"\0" * 100)
        h = smf.header(rec, smf.get_tz("+02:00"))
        self.assertEqual(h["time"].astimezone(UTC).hour, 11)


class ReaderTest(unittest.TestCase):
    def test_spanned(self):
        rec = gen.smf80(T, "SYSA", 1, 0, "JMEIER", "SYS1", "TCP00012", "JMEIER")
        data = gen.spanned(rec, 3) + rec
        out = list(smf.read_records(io.BytesIO(data)))
        self.assertEqual(out, [rec, rec])

    def test_bdw(self):
        rec = gen.smf80(T, "SYSA", 1, 0, "JMEIER", "SYS1", "", "JMEIER")
        block = rec + rec
        data = struct.pack(">HH", len(block) + 4, 0) + block
        self.assertEqual(len(list(smf.read_records(io.BytesIO(data), bdw=True))), 2)

    def test_bad_rdw(self):
        with self.assertRaises(ValueError):
            list(smf.read_records(io.BytesIO(b"\x00\x02\x00\x00")))


class Smf30Test(unittest.TestCase):
    def test_completion(self):
        self.assertEqual(smf.completion(0x00C4, 0x0200), ("abend", "S0C4"))
        self.assertEqual(smf.completion(0x8000 | 4038, 0x0200), ("abend", "U4038"))
        self.assertEqual(smf.completion(8, 0), ("rc", "RC0008"))
        self.assertEqual(smf.completion(0, 0), ("ok", "RC0000"))
        self.assertEqual(smf.completion(0, 0x0100), ("flushed", "FLUSH"))

    def test_step_end(self):
        rec = gen.smf30(T, "SYSA", "PLEX1", "PAYROLL1", "JOB01234", "STEP02", 2, "PAYCALC",
                        "A", "BATCH1", 12.5, 3.25, 90, 4, scc=0x0806, sti=0x0200, arc=4)
        h, a, body = parse_one(rec)
        self.assertEqual(h["subtype"], 4)
        self.assertEqual(a["zos.job.name"], "PAYROLL1")
        self.assertEqual(a["zos.job.id"], "JOB01234")
        self.assertEqual(a["zos.step.name"], "STEP02")
        self.assertEqual(a["zos.program.name"], "PAYCALC")
        self.assertEqual(a["zos.user.id"], "BATCH1")
        self.assertEqual(a["zos.system.name"], "SYSA")
        self.assertEqual(a["zos.sysplex.name"], "PLEX1")
        self.assertEqual(a["smf30.completion.code"], "S806")
        self.assertEqual(a["smf30.abend.reason"], "00000004")
        self.assertAlmostEqual(a["smf30.cpu.total_seconds"], 12.5, places=1)
        self.assertAlmostEqual(a["smf30.cpu.ziip_seconds"], 3.25, places=2)
        self.assertAlmostEqual(a["smf30.elapsed_seconds"], 90, delta=0.02)
        self.assertTrue(body.startswith("SYSA SMF30.4 STEP PAYROLL1"))

    def test_handsatz_offsets(self):
        """SMF 30 nach IBM-Tabelle: Triplets ab 24 (SOF, IOF, UOF, TOF, COF ...)."""
        rec = bytearray(600)
        rec[4:24] = gen.header(30, T, "SYSB", 5)
        ident, comp, cpu = 200, 460, 480
        struct.pack_into(">IHH", rec, 32, ident, 250, 1)    # SMF30IOF/ILN/ION
        struct.pack_into(">IHH", rec, 48, comp, 8, 1)       # SMF30TOF
        struct.pack_into(">IHH", rec, 56, cpu, 12, 1)       # SMF30COF (alte kurze Länge)
        rec[ident:ident + 8] = e("NIGHTLY", 8)              # SMF30JBN
        rec[ident + 32:ident + 40] = e("JOB00042", 8)       # SMF30JNM
        rec[ident + 108:ident + 116] = e("OPER01", 8)       # SMF30RUD
        struct.pack_into(">HH", rec, comp, 12, 0)           # SMF30SCC = RC 12
        struct.pack_into(">II", rec, cpu + 4, 1234, 66)     # SMF30CPT, SMF30CPS (1/100 s)
        struct.pack_into(">HH", rec, 0, len(rec), 0)
        h, a, _ = smf.parse(bytes(rec), UTC)
        self.assertEqual((a["zos.job.name"], a["zos.job.id"], a["zos.user.id"]),
                         ("NIGHTLY", "JOB00042", "OPER01"))
        self.assertEqual(a["smf30.completion.code"], "RC0012")
        self.assertEqual(a["smf30.cpu.total_seconds"], 13.0)
        self.assertNotIn("smf30.cpu.ziip_seconds", a)  # kurzer Prozessorabschnitt

    def test_other_subtypes_skipped(self):
        rec = gen.smf30(T, "SYSA", "PLEX1", "X", "JOB1", "S", 1, "P", "A", "U", 1, 0, 1, 2)
        self.assertIsNone(parse_one(rec))


class Smf80Test(unittest.TestCase):
    def test_logon_failure(self):
        rec = gen.smf80(T, "SYSA", 1, 1, "KSCHULZ", "SYS1", "TCP00666", "KSCHULZ",
                        violation=True)
        _, a, body = parse_one(rec)
        self.assertEqual(a["racf.event"], "JOBINIT")
        self.assertEqual(a["racf.result"], "failure")
        self.assertEqual(a["racf.reason"], "ungültiges Kennwort")
        self.assertEqual(a["racf.user"], "KSCHULZ")
        self.assertEqual(a["racf.terminal"], "TCP00666")
        self.assertTrue(a["racf.violation"])
        self.assertIn("failure user KSCHULZ", body)

    def test_handsatz_offsets(self):
        """SMF 80 nach IBM-Tabelle: DES 18, EVT 20, EVQ 21, USR 22, GRP 30, REL 38,
        CNT 40, TRM 46, JBN 54; Relocate: Typ, Länge, Daten."""
        rec = bytearray(120)
        rec[4:18] = gen.header(80, T, "SYSC")
        struct.pack_into(">HBB", rec, 18, 0x8000, 2, 1)
        rec[22:30], rec[30:38] = e("WEBSRV", 8), e("WEBGRP", 8)
        struct.pack_into(">HH", rec, 38, 100, 1)
        rec[46:54], rec[54:62] = e("LU000A1", 8), e("WEBJOB", 8)
        rec[100:102] = bytes([1, 9])
        rec[102:111] = e("PROD.DATA", 9)
        struct.pack_into(">HH", rec, 0, 111, 0)
        _, a, _ = smf.parse(bytes(rec[:111]), UTC)
        self.assertEqual(a["racf.event"], "ACCESS")
        self.assertEqual(a["racf.result"], "failure")
        self.assertEqual(a["racf.reason"], "keine Berechtigung")
        self.assertEqual((a["racf.user"], a["racf.group"]), ("WEBSRV", "WEBGRP"))
        self.assertEqual((a["racf.terminal"], a["zos.job.name"]), ("LU000A1", "WEBJOB"))
        self.assertEqual(a["racf.reloc.1"], "PROD.DATA")


class Smf119Test(unittest.TestCase):
    def test_summary_tls(self):
        sess = ("TN3270", "TCPIP", 0x80, 0x0303, "C02F", 18, 992, "10.1.2.31")
        rec = gen.zert12(T, "SYSA", "PLEX1", sess, 40, 1000, 2000, 100)
        _, a, body = parse_one(rec)
        self.assertEqual(a["zert.record"], "summary")
        self.assertEqual(a["zert.protocol"], "TLS")
        self.assertEqual(a["zert.tls.version"], "TLSv1.2")
        self.assertEqual(a["zert.tls.cipher"], "ECDHE-RSA-AES128-GCM-SHA256")
        self.assertEqual(a["zert.tls.encryption"], "AES GCM 128")
        self.assertEqual(a["zert.tls.key_exchange"], "ECDHE_RSA")
        self.assertEqual(a["zert.tls.server_key"], "RSA 2048")
        self.assertEqual(a["zert.protection"], "strong")
        self.assertEqual((a["zert.connections"], a["zert.bytes.in"], a["zert.bytes.out"]),
                         (40, 1000, 2000))
        self.assertEqual((a["server.port"], a["client.address"]), (992, "10.1.2.31"))
        self.assertEqual(a["zos.tcpip.stack"], "TCPIP")
        self.assertEqual(a["zert.interval_seconds"], 300.0)

    def test_summary_weak_and_unprotected(self):
        weak = ("FTPD1", "FTPD", 0x80, 0x0301, "000A", 4, 21, "192.168.7.20")
        _, a, _ = parse_one(gen.zert12(T, "SYSA", "PLEX1", weak, 1, 1, 1, 1))
        self.assertEqual((a["zert.tls.version"], a["zert.protection"]), ("TLSv1.0", "weak"))
        none = ("MQCHIN", "MQPROD", 0x00, 0, "", 0, 1414, "10.2.0.90")
        _, a, _ = parse_one(gen.zert12(T, "SYSA", "PLEX1", none, 1, 1, 1, 1))
        self.assertEqual((a["zert.protocol"], a["zert.protection"]), ("none", "unprotected"))

    def test_handsatz_summary_common(self):
        """SMF 119.12 nach IBM-Tabelle: TRN 24, ID-Triplet 28, Common-Triplet 36;
        Common: EventType 8, SecProtos 10, Jobname 11, IPProto 27, SrvIP 28, CltIP 44,
        Port 60/62, Zähler 108.../156..."""
        rec = bytearray(400)
        rec[4:24] = gen.header(119, T, "SYSA", 12, ssi="TCP")
        struct.pack_into(">HH", rec, 24, 6, 0)
        struct.pack_into(">IHH", rec, 28, 100, 64, 1)
        struct.pack_into(">IHH", rec, 36, 170, 204, 1)
        rec[100 + 16:100 + 24] = e("TCPIP2", 8)
        c = 170
        struct.pack_into(">BBB", rec, c + 8, 1, 0x40, 0x00)
        rec[c + 11:c + 19] = e("CICSPRD1", 8)
        rec[c + 27] = 6
        rec[c + 28:c + 32], rec[c + 44:c + 48] = bytes([10, 0, 0, 1]), bytes([10, 9, 8, 7])
        struct.pack_into(">HH", rec, c + 60, 3000, 3000)
        struct.pack_into(">I", rec, c + 108, 10)
        struct.pack_into(">I", rec, c + 156, 25)
        struct.pack_into(">HH", rec, 0, len(rec), 0)
        _, a, _ = smf.parse(bytes(rec), UTC)
        self.assertEqual((a["zos.job.name"], a["zos.tcpip.stack"]), ("CICSPRD1", "TCPIP2"))
        self.assertEqual((a["server.address"], a["client.address"]), ("10.0.0.1", "10.9.8.7"))
        self.assertEqual((a["server.port"], a["zert.connections"]), (3000, 15))
        self.assertEqual(a["zert.protection"], "unprotected")

    def test_connection_detail(self):
        sess = ("TN3270", "TCPIP", 0x80, 0x0304, "1302", 19, 992, "10.1.2.44")
        _, a, body = parse_one(gen.zert11(T, "SYSB", "PLEX1", sess, 3, 500, 600, 40000))
        self.assertEqual(a["zert.record"], "connection")
        self.assertEqual(a["zert.event"], "Verbindungsende")
        self.assertEqual(a["zert.tls.version"], "TLSv1.3")
        self.assertEqual(a["zert.tls.cipher"], "TLS_AES_256_GCM_SHA384")
        self.assertEqual((a["network.local.port"], a["network.peer.port"]), (992, 40000))
        self.assertEqual(a["network.peer.address"], "10.1.2.44")
        self.assertEqual(a["zert.protection"], "strong")


class OtlpTest(unittest.TestCase):
    def test_payload(self):
        recs = gen.generate(datetime(2026, 10, 21, 12, 0), 10, seed=7)
        events = [r for r in (smf.parse(x, UTC) for x in recs) if r]
        self.assertGreater(len(events), 20)
        p = smf.to_otlp(events)
        names = {r["resource"]["attributes"][0]["value"]["stringValue"]
                 for r in p["resourceLogs"]}
        self.assertEqual(names, {"zos-smf-30", "zos-smf-80", "zos-smf-119"})
        rec = p["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertIn("timeUnixNano", rec)
        self.assertTrue(rec["body"]["stringValue"].split()[0] in ("SYSA", "SYSB"))

    def test_cli_print(self):
        import contextlib
        import tempfile
        recs = gen.generate(datetime(2026, 10, 21, 12, 0), 5, seed=3)
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            for i, r in enumerate(recs):
                f.write(gen.spanned(r, 3) if i % 5 == 0 else r)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = smf.main([f.name, "--print"])
        os.unlink(f.name)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.getvalue().splitlines()), len(recs))
        self.assertNotIn("fehlerhaft", err.getvalue())


if __name__ == "__main__":
    unittest.main()
