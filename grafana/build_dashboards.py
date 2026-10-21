#!/usr/bin/env python3
"""Erzeugt die Grafana-Dashboards in grafana/dashboards/ (JSON). Nach Änderungen:

    python3 grafana/build_dashboards.py

Datenquellen: Prometheus (uid prometheus, RMF über OpenTelemetry) und Loki (uid loki,
SMF-Sätze als OpenTelemetry-Logs aus smf/smf_otel.py).
"""

import json
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboards")
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}

GREEN, ORANGE, RED, BLUE = "green", "orange", "red", "blue"


# Ports (Labelwerte wie "1414") nicht als Zahl mit SI-Präfix anzeigen
PORT_AS_TEXT = [{"matcher": {"id": "byRegexp", "options": "(?i).*port.*"},
                 "properties": [{"id": "unit", "value": "none"},
                                {"id": "decimals", "value": 0}]}]


class Board:
    def __init__(self, uid, title, tags, refresh="30s", time_from="now-6h"):
        self.uid, self.title, self.tags = uid, title, tags
        self.refresh, self.time_from = refresh, time_from
        self.panels, self.vars, self.y, self.id = [], [], 0, 0
        self.links = []

    # ---------------------------------------------------------------- Bausteine
    def _next(self):
        self.id += 1
        return self.id

    def row(self, title):
        self.end_row()
        self.panels.append({"id": self._next(), "type": "row", "title": title,
                            "collapsed": False, "gridPos": {"x": 0, "y": self.y, "w": 24,
                                                            "h": 1}})
        self.y += 1
        self.x = 0
        self.rowh = 0

    def _place(self, w, h):
        if not hasattr(self, "x") or self.x + w > 24:
            self.x, self.y = 0, self.y + getattr(self, "rowh", 0)
            self.rowh = 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.rowh = max(self.rowh, h)
        return pos

    def end_row(self):
        self.y += getattr(self, "rowh", 0)
        self.x, self.rowh = 0, 0

    @staticmethod
    def _targets(ds, exprs, legends=None, instant=False):
        if isinstance(exprs, str):
            exprs = [exprs]
        legends = legends or [""] * len(exprs)
        if isinstance(legends, str):
            legends = [legends]
        out = []
        for i, (e, l) in enumerate(zip(exprs, legends)):
            t = {"refId": chr(65 + i), "expr": e, "legendFormat": l, "datasource": ds}
            if instant:
                t["instant"] = True
                if ds is LOKI:
                    t["queryType"] = "instant"
                else:
                    t["range"] = False
                if ds is PROM:
                    t["format"] = "table"
            out.append(t)
        return out

    def ts(self, title, ds, exprs, legends=None, unit="short", w=12, h=8, stack=False,
           bars=False, maxv=None, desc="", overrides=None):
        p = {"id": self._next(), "type": "timeseries", "title": title, "description": desc,
             "datasource": ds, "gridPos": self._place(w, h),
             "targets": self._targets(ds, exprs, legends),
             "fieldConfig": {"defaults": {"unit": unit, "min": 0, "custom": {
                 "lineWidth": 1 if bars else 2, "fillOpacity": 80 if bars else 12,
                 "drawStyle": "bars" if bars else "line", "showPoints": "never",
                 "stacking": {"mode": "normal" if stack else "none"}}},
                 "overrides": overrides or []},
             "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                         "tooltip": {"mode": "multi", "sort": "desc"}}}
        if maxv is not None:
            p["fieldConfig"]["defaults"]["max"] = maxv
        self.panels.append(p)

    def stat(self, title, ds, expr, unit="short", w=4, h=4, steps=None, desc="",
             decimals=None, color_mode="background", instant=True):
        p = {"id": self._next(), "type": "stat", "title": title, "description": desc,
             "datasource": ds, "gridPos": self._place(w, h),
             "targets": self._targets(ds, expr, instant=instant and ds is LOKI),
             "fieldConfig": {"defaults": {
                 "unit": unit, "noValue": "0",
                 "thresholds": {"mode": "absolute",
                                "steps": steps or [{"color": BLUE, "value": None}]}},
                 "overrides": []},
             "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": color_mode,
                         "graphMode": "none" if ds is LOKI else "area",
                         "textMode": "value"}}
        if decimals is not None:
            p["fieldConfig"]["defaults"]["decimals"] = decimals
        self.panels.append(p)

    def bargauge(self, title, ds, expr, legend, unit="short", w=8, h=8, desc="",
                 steps=None):
        self.panels.append({
            "id": self._next(), "type": "bargauge", "title": title, "description": desc,
            "datasource": ds, "gridPos": self._place(w, h),
            "targets": [dict(self._targets(ds, expr, legend, instant=True)[0],
                             format="time_series")],
            "transformations": ([{"id": "sortBy", "options": {
                "sort": [{"field": "Value #A", "desc": True}]}}] if ds is LOKI else []),
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "thresholds": {
                "mode": "absolute", "steps": steps or [{"color": BLUE, "value": None}]}},
                "overrides": PORT_AS_TEXT},
            "options": {"orientation": "horizontal", "displayMode": "gradient",
                        "showUnfilled": True, "valueMode": "color",
                        # Loki-Momentabfragen kommen als Tabelle (eine Zeile je Wert)
                        "reduceOptions": {"calcs": ["lastNotNull"], "values": ds is LOKI},
                        "minVizHeight": 10,
                        "maxVizHeight": 24, "sizing": "manual"}})

    def pie(self, title, ds, expr, legend, w=6, h=8, desc="", unit="short"):
        self.panels.append({
            "id": self._next(), "type": "piechart", "title": title, "description": desc,
            "datasource": ds, "gridPos": self._place(w, h),
            "targets": [dict(self._targets(ds, expr, legend, instant=True)[0],
                             format="time_series")],
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "values": ds is LOKI},
                        "pieType": "donut",
                        "legend": {"displayMode": "table", "placement": "right",
                                   "values": ["value", "percent"]},
                        "tooltip": {"mode": "single"}}})

    def table(self, title, ds, expr, columns, w=12, h=8, desc="", unit="short",
              sort_col="Value"):
        """Instant-Abfrage als Tabelle: columns = {Label: Anzeigename}."""
        rename = {k: v for k, v in columns.items()}
        rename["Value"] = rename.get("Value", "Wert")
        rename["Value #A"] = rename["Value"]  # so benennt Grafana die Loki-Wertspalte
        order = {k: i for i, k in enumerate(list(columns) + ["Value", "Value #A"])}
        self.panels.append({
            "id": self._next(), "type": "table", "title": title, "description": desc,
            "datasource": ds, "gridPos": self._place(w, h),
            "targets": [dict(self._targets(ds, expr, instant=True)[0], format="table")],
            "transformations": ([{"id": "labelsToFields", "options": {"mode": "columns"}}]
                                if ds is LOKI else []) + [
                {"id": "merge", "options": {}},
                {"id": "organize", "options": {
                    "excludeByName": {"Time": True, "__name__": True, "job": True,
                                      "instance": True, "service_name": True,
                                      "Line": True, "id": True, "tsNs": True,
                                      "labels": True},
                    "indexByName": order, "renameByName": rename}},
                {"id": "sortBy", "options": {"sort": [{"field": rename["Value"],
                                                       "desc": True}]}}],
            "fieldConfig": {"defaults": {"unit": unit, "custom": {"align": "auto"}},
                            "overrides": PORT_AS_TEXT + [{"matcher": {"id": "byName",
                                                       "options": rename["Value"]},
                                           "properties": [{"id": "custom.cellOptions",
                                                           "value": {"type": "color-background",
                                                                     "mode": "gradient"}},
                                                          {"id": "color", "value": {
                                                              "mode": "continuous-BlYlRd"}}]}]},
            "options": {"showHeader": True, "cellHeight": "sm"}})

    def logs(self, title, expr, w=24, h=10, desc=""):
        self.panels.append({
            "id": self._next(), "type": "logs", "title": title, "description": desc,
            "datasource": LOKI, "gridPos": self._place(w, h),
            "targets": [{"refId": "A", "expr": expr, "datasource": LOKI,
                         "queryType": "range"}],
            "options": {"showTime": True, "wrapLogMessage": False, "sortOrder": "Descending",
                        "enableLogDetails": True, "dedupStrategy": "none",
                        "prettifyLogMessage": False}})

    def text(self, md, w=24, h=3):
        self.panels.append({"id": self._next(), "type": "text", "title": "",
                            "gridPos": self._place(w, h),
                            "options": {"mode": "markdown", "content": md}})

    def var_textbox(self, name, label, default=".*"):
        self.vars.append({"name": name, "label": label, "type": "textbox",
                          "query": default, "current": {"text": default, "value": default},
                          "hide": 0})

    def var_query(self, name, label, query, ds=PROM):
        self.vars.append({"name": name, "label": label, "type": "query", "datasource": ds,
                          "query": {"query": query, "refId": name}, "definition": query,
                          "includeAll": True, "multi": True, "allValue": ".*",
                          "current": {"text": "All", "value": "$__all"}, "refresh": 2})

    def save(self):
        d = {"uid": self.uid, "title": self.title, "tags": self.tags, "timezone": "browser",
             "schemaVersion": 41, "version": 1, "refresh": self.refresh, "editable": True,
             "time": {"from": self.time_from, "to": "now"},
             "templating": {"list": self.vars}, "panels": self.panels,
             "links": [{"title": "z/OS", "type": "dashboards", "tags": ["z/OS"],
                        "asDropdown": True, "includeVars": False, "keepTime": True}]}
        path = os.path.join(OUT, f"{self.uid}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print(path, len(self.panels), "Panels")


# ------------------------------------------------------------------ Abfragebausteine

SP = 'zos_sysplex_name=~"$sysplex"'
M3 = 'sysplex=~"$sysplex"'


def smf(typ, *filters):
    """LogQL-Selektor für SMF-Typ mit Filtern auf strukturierte Metadaten."""
    f = "".join(f" | {x}" for x in filters)
    return f'{{service_name="zos-smf-{typ}"}} | zos_smf_id=~"$system"{f}'


def cnt(sel, by=None, rng="$__auto"):
    inner = f"count_over_time({sel} [{rng}])"
    return f"sum by ({by}) ({inner})" if by else f"sum({inner})"


def unwrap(sel, field, by=None, rng="$__auto", fn="sum_over_time", agg="sum"):
    inner = f'{fn}({sel} | unwrap {field} | __error__="" [{rng}])'
    return f"{agg} by ({by}) ({inner})" if by else f"{agg}({inner})"


# ------------------------------------------------------------------ Dashboards


def overview():
    b = Board("zos-rmf-otel", "z/OS Übersicht (OpenTelemetry)", ["z/OS", "RMF", "SMF",
                                                                   "OpenTelemetry"],
              refresh="30s", time_from="now-1h")
    b.var_query("sysplex", "Sysplex", "label_values(mvs_rmf_m3_mintime, sysplex)")
    b.var_textbox("system", "SMF-System (Regex)")
    b.row("RMF Monitor III (Agent und /metrics/m3)")
    ok = [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 75},
          {"color": RED, "value": 90}]
    b.stat("CPU (Mittel)", PROM, f"avg(rmf_cpu_utilization_percent{{{SP}}})", "percent",
           steps=ok, decimals=1)
    b.stat("Aktive Benutzer", PROM, f"sum(rmf_users_active{{{SP}}})", decimals=0)
    b.stat("Transaktionen/s", PROM, f"sum(rmf_transaction_rate_per_second{{{SP}}})",
           "reqps", decimals=1)
    b.stat("I/O-Rate", PROM, f"sum(rmf_io_rate_per_second{{{SP}}})", "iops", decimals=0)
    b.stat("Delay (Mittel)", PROM, f"avg(rmf_delay_percent{{{SP}}})", "percent",
           steps=[{"color": GREEN, "value": None}, {"color": ORANGE, "value": 20},
                  {"color": RED, "value": 40}], decimals=1)
    b.stat("MSU/h (Summe LPARs)", PROM,
           'sum(avg by (lpar_name) (mvs_lpar_actual_msu_h{line_type="CP"}))', decimals=0)
    b.ts("CPU-Auslastung (CP) je System", PROM, f"rmf_cpu_utilization_percent{{{SP}}}",
         "{{zos_system_name}}", "percent", w=8, maxv=100)
    b.ts("Workflow / Delay je System", PROM,
         [f"rmf_workflow_percent{{{SP}}}", f"rmf_delay_percent{{{SP}}}"],
         ["Workflow {{zos_system_name}}", "Delay {{zos_system_name}}"], "percent", w=8)
    b.ts("Transaktionen je System", PROM, f"rmf_transaction_rate_per_second{{{SP}}}",
         "{{zos_system_name}}", "reqps", w=8, stack=True)
    b.end_row()
    b.row("SMF (Jobs, RACF, zERT) – Details in den SMF-Dashboards")
    b.stat("Jobs beendet", LOKI, cnt(smf(30, 'smf_record_subtype="5"'), rng="$__range"),
           steps=[{"color": BLUE, "value": None}])
    b.stat("Abends", LOKI, cnt(smf(30, 'smf_record_subtype="4"',
                                   'smf30_completion_status="abend"'), rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": RED, "value": 1}])
    b.stat("CPU-Sekunden (Jobs)", LOKI, unwrap(smf(30, 'smf_record_subtype="5"'),
                                               "smf30_cpu_total_seconds", rng="$__range"),
           "s", decimals=0)
    b.stat("Fehlgeschlagene Logons", LOKI,
           cnt(smf(80, 'racf_event="JOBINIT"', 'racf_result="failure"'), rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1},
                  {"color": RED, "value": 20}])
    b.stat("Ungeschützte Verbindungen", LOKI,
           unwrap(smf(119, 'zert_record="summary"', 'zert_protection="unprotected"'),
                  "zert_connections", rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": RED, "value": 1}])
    b.stat("Schwach geschützte Verbindungen", LOKI,
           unwrap(smf(119, 'zert_record="summary"', 'zert_protection="weak"'),
                  "zert_connections", rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}])
    b.ts("SMF-Sätze je Typ", LOKI, 'sum by (service_name) (count_over_time('
         '{service_name=~"zos-smf-.*"} | zos_smf_id=~"$system" [$__auto]))',
         "{{service_name}}", w=8, bars=True, stack=True)
    b.ts("Logons: erfolgreich / fehlgeschlagen", LOKI,
         cnt(smf(80, 'racf_event="JOBINIT"'), "racf_result"), "{{racf_result}}", w=8,
         bars=True, stack=True,
         overrides=[{"matcher": {"id": "byName", "options": "failure"},
                     "properties": [{"id": "color", "value": {"mode": "fixed",
                                                              "fixedColor": "red"}}]},
                    {"matcher": {"id": "byName", "options": "success"},
                     "properties": [{"id": "color", "value": {"mode": "fixed",
                                                              "fixedColor": "green"}}]}])
    b.ts("Verbindungen nach Schutz (zERT)", LOKI,
         unwrap(smf(119, 'zert_record="summary"'), "zert_connections", "zert_protection"),
         "{{zert_protection}}", w=8, bars=True, stack=True, overrides=PROT_COLORS)
    b.save()


PROT_COLORS = [{"matcher": {"id": "byName", "options": n},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
               for n, c in (("strong", "green"), ("weak", "orange"),
                            ("unprotected", "red"))]


def rmf_detail():
    b = Board("zos-rmf-detail", "z/OS RMF Detail", ["z/OS", "RMF", "OpenTelemetry"],
              refresh="1m", time_from="now-3h")
    b.var_query("sysplex", "Sysplex", "label_values(mvs_rmf_m3_mintime, sysplex)")
    b.row("Workload: Using und Delay je Ursache (Agent, /gpm/perform)")
    b.ts("Using je System", PROM,
         [f"rmf_using_processor_percent{{{SP}}}", f"rmf_using_io_percent{{{SP}}}"],
         ["CPU {{zos_system_name}}", "I/O {{zos_system_name}}"], "percent", w=8)
    b.ts("Delay nach Ursache (Sysplex-Mittel)", PROM,
         [f"avg(rmf_delay_processor_percent{{{SP}}})", f"avg(rmf_delay_io_percent{{{SP}}})",
          f"avg(rmf_delay_storage_percent{{{SP}}})",
          f"avg(rmf_delay_enqueue_percent{{{SP}}})",
          f"avg(rmf_delay_operator_percent{{{SP}}})"],
         ["Prozessor", "I/O", "Speicher", "Enqueue", "Operator"], "percent", w=8, stack=True)
    b.ts("Workflow je Ursache", PROM,
         [f"rmf_workflow_processor_percent{{{SP}}}", f"rmf_workflow_io_percent{{{SP}}}"],
         ["CPU {{zos_system_name}}", "I/O {{zos_system_name}}"], "percent", w=8, maxv=100)
    b.ts("Delay je System und Ursache", PROM,
         [f"rmf_delay_processor_percent{{{SP}}}", f"rmf_delay_io_percent{{{SP}}}",
          f"rmf_delay_storage_percent{{{SP}}}", f"rmf_delay_enqueue_percent{{{SP}}}"],
         ["{{zos_system_name}} CPU", "{{zos_system_name}} I/O",
          "{{zos_system_name}} Speicher", "{{zos_system_name}} Enqueue"], "percent", w=12)
    b.ts("Benutzer und Transaktionen", PROM,
         [f"rmf_users_active{{{SP}}}", f"rmf_transaction_rate_per_second{{{SP}}}"],
         ["aktiv {{zos_system_name}}", "Trx/s {{zos_system_name}}"], w=12)
    b.end_row()
    b.row("Speicher")
    b.ts("Common Storage (Agent)", PROM,
         [f"rmf_storage_csa_utilization_percent{{{SP}}}",
          f"rmf_storage_ecsa_utilization_percent{{{SP}}}",
          f"rmf_storage_sqa_utilization_percent{{{SP}}}",
          f"rmf_storage_esqa_utilization_percent{{{SP}}}"],
         ["CSA {{zos_system_name}}", "ECSA {{zos_system_name}}", "SQA {{zos_system_name}}",
          "ESQA {{zos_system_name}}"], "percent", w=8, maxv=100)
    b.ts("Realer Speicher belegt (/metrics/m3)", PROM,
         f"1 - mvs_system_storage_frames_available_total{{{M3}}} / "
         f"mvs_system_storage_real_storage_frames{{{M3}}}", "{{sysname}}", "percentunit",
         w=8, maxv=1)
    b.ts("Unreferenced Interval Count (UIC)", PROM, f"rmf_storage_uic_seconds{{{SP}}}",
         "{{zos_system_name}}", "s", w=8,
         desc="Niedrige Werte (< einige hundert Sekunden) bedeuten Speicherdruck.")
    b.end_row()
    b.row("Jobs und Dienstklassen (/metrics/m3)")
    b.bargauge("Top 15 Jobs nach CPU (EAppl %)", PROM,
               f"topk(15, mvs_job_total_eappl_percent{{{M3}}})", "{{sysname}}.{{job_name}}",
               "percent", w=8, h=10)
    b.bargauge("Top 15 Jobs nach zIIP (EAppl %)", PROM,
               f"topk(15, mvs_job_iip_eappl_percent{{{M3}}})", "{{sysname}}.{{job_name}}",
               "percent", w=8, h=10)
    b.bargauge("Top 15 Jobs nach Working Set", PROM,
               f"topk(15, mvs_job_working_set_frames_total{{{M3}}} * 4096)",
               "{{sysname}}.{{job_name}}", "bytes", w=8, h=10)
    b.ts("WLM-Delay je Dienstklasse", PROM,
         f'avg by (group_name) (mvs_wlm_group_delay_percent{{group_type="W",{M3}}})',
         "{{group_name}}", "percent", w=12)
    b.ts("WLM-Delay nach Ursache (alle Dienstklassen)", PROM,
         [f'avg(mvs_wlm_group_delay_{d}_percent{{group_type="W",{M3}}})'
          for d in ("proc", "dev", "stor", "enq", "jes", "hsm", "xcf", "oper", "subs")],
         ["Prozessor", "Gerät", "Speicher", "Enqueue", "JES", "HSM", "XCF", "Operator",
          "Subsystem"], "percent", w=12, stack=True)
    b.end_row()
    b.row("Coupling Facility und Kapazität (/metrics/m3)")
    b.ts("CF-Prozessor", PROM, f"mvs_cf_processor_utilization_percent{{{M3}}}", "{{name}}",
         "percent", w=6, maxv=100)
    b.ts("CF-Anfragen/s", PROM, f"mvs_cf_request_rate{{{M3}}}", "{{name}}", "reqps", w=6)
    b.ts("CF-Speicher belegt", PROM, f"mvs_cf_storage_utilized_percent{{{M3}}}", "{{name}}",
         "percent", w=6, maxv=100)
    b.ts("MSU/h je LPAR", PROM, 'avg by (lpar_name) (mvs_lpar_actual_msu_h{line_type="CP"})',
         "{{lpar_name}}", w=6, stack=True)
    b.ts("CPC-Auslastung (MSU / Kapazität)", PROM,
         'sum by (cpc_id) (avg by (cpc_id, lpar_name) (mvs_lpar_actual_msu_h{line_type="CP"}))'
         ' / on (cpc_id) avg by (cpc_id) (mvs_cpc_capacity_msu_h)', "{{cpc_id}}",
         "percentunit", w=12, maxv=1)
    b.save()


def jobs():
    b = Board("zos-smf-jobs", "z/OS Jobs (SMF 30)", ["z/OS", "SMF", "OpenTelemetry"],
              time_from="now-6h")
    b.var_textbox("system", "System (Regex)")
    b.var_textbox("jobname", "Jobname (Regex)")
    J = 'zos_job_name=~"$jobname"'
    job, step = smf(30, 'smf_record_subtype="5"', J), smf(30, 'smf_record_subtype="4"', J)
    b.row("Überblick")
    b.stat("Jobs beendet", LOKI, cnt(job, rng="$__range"))
    b.stat("Steps beendet", LOKI, cnt(step, rng="$__range"))
    b.stat("Abends (Steps)", LOKI, cnt(f'{step} | smf30_completion_status="abend"',
                                       rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": RED, "value": 1}])
    b.stat("Steps mit RC > 0", LOKI, cnt(f'{step} | smf30_completion_status="rc"',
                                         rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}])
    b.stat("CPU-Sekunden", LOKI, unwrap(job, "smf30_cpu_total_seconds", rng="$__range"),
           "s", decimals=0)
    b.stat("davon zIIP-Sekunden", LOKI, unwrap(step, "smf30_cpu_ziip_seconds",
                                               rng="$__range"), "s", decimals=0)
    b.ts("Beendete Jobs je System", LOKI, cnt(job, "zos_smf_id"), "{{zos_smf_id}}", w=8,
         bars=True, stack=True)
    b.ts("CPU-Sekunden je System", LOKI, unwrap(job, "smf30_cpu_total_seconds", "zos_smf_id"),
         "{{zos_smf_id}}", "s", w=8, bars=True, stack=True)
    b.ts("Step-Ergebnis", LOKI, cnt(step, "smf30_completion_status"),
         "{{smf30_completion_status}}", w=8, bars=True, stack=True,
         overrides=[{"matcher": {"id": "byName", "options": n},
                     "properties": [{"id": "color", "value": {"mode": "fixed",
                                                              "fixedColor": c}}]}
                    for n, c in (("ok", "green"), ("rc", "yellow"), ("abend", "red"),
                                 ("flushed", "purple"))])
    b.end_row()
    b.row("Verbraucher")
    b.bargauge("Top 15 Jobs nach CPU", LOKI,
               f"topk(15, {unwrap(job, 'smf30_cpu_total_seconds', 'zos_job_name', '$__range')})",
               "{{zos_job_name}}", "s", w=8, h=10)
    b.bargauge("Top 15 Benutzer nach CPU", LOKI,
               f"topk(15, {unwrap(job, 'smf30_cpu_total_seconds', 'zos_user_id', '$__range')})",
               "{{zos_user_id}}", "s", w=8, h=10)
    b.bargauge("Top 15 Programme nach CPU (Steps)", LOKI,
               f"topk(15, {unwrap(step, 'smf30_cpu_total_seconds', 'zos_program_name', '$__range')})",
               "{{zos_program_name}}", "s", w=8, h=10)
    b.ts("CPU je Job (Top 10)", LOKI,
         f"topk(10, {unwrap(job, 'smf30_cpu_total_seconds', 'zos_job_name')})",
         "{{zos_job_name}}", "s", w=12, bars=True, stack=True)
    b.ts("Laufzeit je Job (Mittel, Top 10)", LOKI,
         f"topk(10, {unwrap(job, 'smf30_elapsed_seconds', 'zos_job_name', fn='avg_over_time', agg='max')})",
         "{{zos_job_name}}", "s", w=12, bars=True)
    b.pie("Jobs je Klasse", LOKI, cnt(job, "zos_job_class", "$__range"), "{{zos_job_class}}",
          w=6)
    b.pie("Completion-Codes (ohne RC0000)", LOKI,
          cnt(f'{step} | smf30_completion_code!="RC0000"', "smf30_completion_code",
              "$__range"), "{{smf30_completion_code}}", w=6)
    b.table("Abends nach Job und Code", LOKI,
            cnt(f'{step} | smf30_completion_status="abend"',
                "zos_job_name, zos_step_name, zos_program_name, smf30_completion_code",
                "$__range"),
            {"zos_job_name": "Job", "zos_step_name": "Step", "zos_program_name": "Programm",
             "smf30_completion_code": "Code", "Value": "Anzahl"}, w=12)
    b.end_row()
    b.row("Einzelsätze")
    b.logs("Abends und Steps mit RC > 0",
           f'{step} | smf30_completion_status=~"abend|rc|flushed"', h=9)
    b.logs("Job-Ende (SMF 30.5)", job, h=9)
    b.save()


def racf():
    b = Board("zos-smf-racf", "z/OS Sicherheit (RACF, SMF 80)", ["z/OS", "SMF", "RACF",
                                                                 "OpenTelemetry"],
              time_from="now-6h")
    b.var_textbox("system", "System (Regex)")
    b.var_textbox("user", "Benutzer (Regex)")
    U = 'racf_user=~"$user"'
    logon = smf(80, 'racf_event="JOBINIT"', U)
    fail = f'{logon} | racf_result="failure"'
    fail_term = fail + ' | racf_terminal!=""'
    b.row("Anmeldungen (RACINIT: TSO-Logon, Jobstart, Anwendungen)")
    b.stat("Erfolgreiche Logons", LOKI, cnt(f'{logon} | racf_event_qualifier="0"',
                                            rng="$__range"),
           steps=[{"color": GREEN, "value": None}])
    b.stat("Fehlgeschlagene Logons", LOKI, cnt(fail, rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1},
                  {"color": RED, "value": 20}])
    b.stat("Falsches Kennwort", LOKI, cnt(f'{logon} | racf_event_qualifier="1"',
                                          rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}])
    b.stat("Gesperrte Benutzer", LOKI, cnt(f'{logon} | racf_event_qualifier=~"6|7"',
                                           rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": RED, "value": 1}])
    b.stat("Unbekannte Benutzer", LOKI, cnt(f'{logon} | racf_event_qualifier="9"',
                                            rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}])
    b.stat("Zugriffsverletzungen", LOKI,
           cnt(smf(80, 'racf_event="ACCESS"', 'racf_result="failure"', U), rng="$__range"),
           steps=[{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}])
    b.ts("Logons: erfolgreich / fehlgeschlagen", LOKI, cnt(logon, "racf_result"),
         "{{racf_result}}", w=12, bars=True, stack=True,
         overrides=[{"matcher": {"id": "byName", "options": n},
                     "properties": [{"id": "color", "value": {"mode": "fixed",
                                                              "fixedColor": c}}]}
                    for n, c in (("success", "green"), ("failure", "red"))])
    b.ts("Fehlversuche je Grund", LOKI, cnt(fail, "racf_reason"), "{{racf_reason}}", w=12,
         bars=True, stack=True)
    b.end_row()
    b.row("Auffälligkeiten")
    b.ts("Kennwort-Raten? Benutzer mit ≥ 5 Fehlversuchen in 10 Minuten", LOKI,
         f"sum by (racf_user, racf_terminal) (count_over_time({fail} [10m])) >= 5",
         "{{racf_user}} @ {{racf_terminal}}", w=12, bars=True,
         desc="Gleitendes 10-Minuten-Fenster: Balken nur, wenn ein Benutzer an einem "
              "Terminal mindestens 5 Fehlversuche hatte.")
    b.table("Fehlversuche je Benutzer, Terminal und System", LOKI,
            cnt(fail, "racf_user, racf_terminal, zos_smf_id, racf_reason", "$__range"),
            {"racf_user": "Benutzer", "racf_terminal": "Terminal", "zos_smf_id": "System",
             "racf_reason": "Grund", "Value": "Fehlversuche"}, w=12)
    b.bargauge("Fehlversuche je Benutzer", LOKI,
               f"topk(15, {cnt(fail, 'racf_user', '$__range')})", "{{racf_user}}", w=6, h=8,
               steps=[{"color": ORANGE, "value": None}, {"color": RED, "value": 10}])
    b.bargauge("Fehlversuche je Terminal", LOKI,
               f"topk(15, {cnt(fail_term, 'racf_terminal', '$__range')})", "{{racf_terminal}}",
               w=6, h=8, steps=[{"color": ORANGE, "value": None}, {"color": RED, "value": 10}])
    b.pie("Logons je Benutzer", LOKI, cnt(f'{logon} | racf_result="success"', "racf_user",
                                          "$__range"), "{{racf_user}}", w=8)
    viol = smf(80, 'racf_event="ACCESS"', 'racf_result="failure"', U)
    b.bargauge("Zugriffsverletzungen je Benutzer", LOKI,
               f"topk(15, {cnt(viol, 'racf_user', '$__range')})", "{{racf_user}}", w=8, h=8)
    b.pie("RACF-Ereignisse", LOKI, cnt(smf(80, U), "racf_event", "$__range"),
          "{{racf_event}}", w=8)
    b.end_row()
    b.row("Einzelsätze")
    b.logs("Fehlgeschlagene Logons", fail, h=9)
    b.logs("Zugriffsverletzungen und Befehle", smf(80, 'racf_event!="JOBINIT"', U), h=9)
    b.save()


def zert():
    b = Board("zos-smf-zert", "z/OS Verschlüsselung (zERT, SMF 119)",
              ["z/OS", "SMF", "zERT", "TCP/IP", "OpenTelemetry"], time_from="now-6h")
    b.var_textbox("system", "System (Regex)")
    b.var_textbox("jobname", "Jobname (Regex)")
    S = smf(119, 'zert_record="summary"', 'zos_job_name=~"$jobname"')
    C = smf(119, 'zert_record="connection"', 'zos_job_name=~"$jobname"')
    conns = lambda by=None, rng="$__auto", sel=S: unwrap(sel, "zert_connections", by, rng)
    b.text("Grundlage: **zERT-Summary (SMF 119.12)** je Sicherheitssitzung und Intervall "
           "(Verbindungen = neue im Intervall) und **Verbindungsdetail (SMF 119.11)**. "
           "Einstufung *strong / weak / unprotected*: *weak* = SSLv2/v3, TLS 1.0/1.1 oder "
           "schwache Verschlüsselung (DES, 3DES, RC2, RC4, keine).", h=2)
    b.row("Schutz der Verbindungen")
    b.stat("Verbindungen", LOKI, conns(rng="$__range"))
    protected = S + ' | zert_protection!="unprotected"'
    b.stat("Anteil geschützt", LOKI,
           f'{conns(None, "$__range", protected)} / {conns(rng="$__range")}', "percentunit",
           steps=[{"color": RED, "value": None}, {"color": ORANGE, "value": 0.9},
                  {"color": GREEN, "value": 0.99}], decimals=1)
    b.stat("Ungeschützt", LOKI, conns(rng="$__range",
                                      sel=S + ' | zert_protection="unprotected"'),
           steps=[{"color": GREEN, "value": None}, {"color": RED, "value": 1}])
    b.stat("Schwach geschützt", LOKI, conns(rng="$__range",
                                            sel=S + ' | zert_protection="weak"'),
           steps=[{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}])
    b.stat("TLS 1.3", LOKI, conns(rng="$__range", sel=S + ' | zert_tls_version="TLSv1.3"'),
           steps=[{"color": GREEN, "value": None}])
    b.stat("Datenvolumen", LOKI,
           f'{unwrap(S, "zert_bytes_in", rng="$__range")} + '
           f'{unwrap(S, "zert_bytes_out", rng="$__range")}', "bytes", decimals=1)
    b.ts("Verbindungen nach Schutz", LOKI, conns("zert_protection"), "{{zert_protection}}",
         w=12, bars=True, stack=True, overrides=PROT_COLORS)
    b.ts("Verbindungen nach Protokoll", LOKI, conns("zert_protocol"), "{{zert_protocol}}",
         w=12, bars=True, stack=True)
    b.end_row()
    b.row("TLS im Detail")
    b.pie("TLS-Versionen", LOKI, conns("zert_tls_version", "$__range",
                                       S + ' | zert_protocol="TLS"'), "{{zert_tls_version}}",
          w=6)
    b.pie("Cipher-Suites", LOKI, conns("zert_tls_cipher", "$__range",
                                       S + ' | zert_protocol="TLS"'), "{{zert_tls_cipher}}",
          w=6)
    b.pie("Verschlüsselung", LOKI, conns("zert_tls_encryption", "$__range",
                                         S + ' | zert_protocol="TLS"'),
          "{{zert_tls_encryption}}", w=6)
    b.pie("Schlüsselaustausch", LOKI, conns("zert_tls_key_exchange", "$__range",
                                            S + ' | zert_protocol="TLS"'),
          "{{zert_tls_key_exchange}}", w=6)
    b.end_row()
    b.row("Handlungsbedarf")
    b.table("Ungeschützte Verbindungen (Job, Port, Client)", LOKI,
            conns("zos_job_name, server_port, client_address, network_transport", "$__range",
                  S + ' | zert_protection="unprotected"'),
            {"zos_job_name": "Job", "server_port": "Port", "client_address": "Client",
             "network_transport": "Protokoll", "Value": "Verbindungen"}, w=12)
    b.table("Schwach geschützte Verbindungen", LOKI,
            conns("zos_job_name, server_port, zert_tls_version, zert_tls_cipher", "$__range",
                  S + ' | zert_protection="weak"'),
            {"zos_job_name": "Job", "server_port": "Port", "zert_tls_version": "Version",
             "zert_tls_cipher": "Cipher", "Value": "Verbindungen"}, w=12)
    b.bargauge("Verbindungen je Job", LOKI,
               f'topk(15, {conns("zos_job_name", "$__range")})', "{{zos_job_name}}", w=8,
               h=8)
    b.bargauge("Verbindungen je Server-Port", LOKI,
               f'topk(15, {conns("server_port", "$__range")})', "{{server_port}}", w=8, h=8)
    b.ts("Kurze Verbindungen (< 10 s) je Job", LOKI,
         unwrap(S, "zert_connections_short", "zos_job_name"), "{{zos_job_name}}", w=8,
         bars=True, stack=True)
    b.end_row()
    b.row("Einzelsätze")
    b.logs("Ungeschützte und schwache Sitzungen (Summary)",
           S + ' | zert_protection=~"unprotected|weak"', h=9)
    b.logs("Verbindungsende (SMF 119.11)", C, h=9)
    b.save()


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    overview()
    rmf_detail()
    jobs()
    racf()
    zert()
