#!/usr/bin/env python3
"""
Generate two Grafana dashboards for monitoring YugabyteDB masters and tservers
across N environments and N namespaces.

These dashboards query the RAW metric names that YugabyteDB exposes on its
/prometheus-metrics endpoints (master :7000, tserver :9000, ysql :13000), as
scraped by a standard Prometheus via ServiceMonitor. They do NOT use the
YugabyteDB Anywhere model (rpc_latency_count{saved_name=...}); see monitoring/README.md.

Label model assumed (added by the Prometheus ServiceMonitor scrape):
  env        -> Prometheus external label, one value per environment/cluster
  namespace  -> k8s namespace the YB pod runs in
  pod        -> pod name (yb-master-N / yb-tserver-N)
  job        -> "yb-master" or "yb-tserver" (set via ServiceMonitor relabeling)

Two dashboards:
  1. yb-fleet-overview.json   -> roll-up across all envs/namespaces (multi-select)
  2. yb-env-detail.json       -> deep dive into one env, filter by namespace/pod
"""
import json
import os

PROM = {"type": "prometheus", "uid": "${datasource}"}
OUT = os.path.join(os.path.dirname(__file__), "dashboards")

# ---------------------------------------------------------------- helpers ----

def target(expr, legend="", instant=False, ref="A"):
    return {
        "datasource": PROM,
        "expr": expr,
        "legendFormat": legend,
        "instant": instant,
        "range": not instant,
        "refId": ref,
    }


def targets(*specs):
    out = []
    for i, (expr, legend) in enumerate(specs):
        out.append(target(expr, legend, ref=chr(ord("A") + i)))
    return out


def gridpos(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def stat(title, expr, unit="none", grid=None, thresholds=None, color_mode="value",
         legend=""):
    steps = thresholds or [{"color": "green", "value": None}]
    return {
        "type": "stat",
        "title": title,
        "datasource": PROM,
        "gridPos": grid,
        "targets": [target(expr, legend, instant=True)],
        "options": {
            "colorMode": color_mode,
            "graphMode": "area",
            "justifyMode": "auto",
            "textMode": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "fieldConfig": {
            "defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}},
            "overrides": [],
        },
    }


def timeseries(title, tgts, unit="short", grid=None, stack=False, fill=10,
               desc="", legend_table=False):
    return {
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "gridPos": grid,
        "targets": tgts,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 1,
                    "fillOpacity": fill,
                    "showPoints": "never",
                    "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                    "axisLabel": "",
                },
                "color": {"mode": "palette-classic"},
            },
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "table" if legend_table else "list",
                "placement": "bottom",
                "calcs": ["lastNotNull", "max"] if legend_table else [],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def table(title, tgts, grid=None, desc="", overrides=None):
    return {
        "type": "table",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "gridPos": grid,
        "targets": tgts,
        "transformations": [
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True, "__name__": True, "job": True,
                                       "metric_id": True, "metric_type": True,
                                       "instance": True, "service": True,
                                       "container": True, "endpoint": True,
                                       "exported_instance": True, "cluster": True,
                                       "prometheus": True},
                    "renameByName": {},
                },
            },
        ],
        "fieldConfig": {"defaults": {"custom": {"align": "auto"}},
                        "overrides": overrides or []},
        "options": {"showHeader": True, "cellHeight": "sm",
                     "footer": {"show": False}},
    }


def row(title, y):
    return {"type": "row", "title": title, "collapsed": False,
            "gridPos": gridpos(0, y, 24, 1), "panels": []}


def text_panel(title, content, grid):
    return {"type": "text", "title": title, "datasource": None, "gridPos": grid,
            "options": {"mode": "markdown", "content": content}}


# ------------------------------------------------------------- variables ----

def var_datasource():
    return {
        "type": "datasource", "name": "datasource", "label": "Data source",
        "query": "prometheus", "refresh": 1, "hide": 0, "current": {}, "regex": "",
        "multi": False, "includeAll": False,
    }


def var_query(name, label, query, multi=True, all_=True, hide=0):
    v = {
        "type": "query", "name": name, "label": label,
        "datasource": PROM,
        "query": query, "definition": query,
        "refresh": 2, "sort": 1, "hide": hide,
        "multi": multi, "includeAll": all_, "current": {},
        "regex": "",
    }
    if all_:
        v["allValue"] = ".*"
    return v


# filter fragments reused in exprs
F_ENV = 'env=~"$env"'
F_NS = 'namespace=~"$namespace"'
NSEL = '{%s,%s}' % (F_ENV, F_NS)            # env + namespace
MAST = '{job="yb-master",%s,%s}' % (F_ENV, F_NS)
TSRV = '{job="yb-tserver",%s,%s}' % (F_ENV, F_NS)


def base_dashboard(uid, title, variables, tags, refresh="30s", time_from="now-6h"):
    return {
        "uid": uid,
        "title": title,
        "tags": tags,
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "graphTooltip": 1,
        "refresh": refresh,
        "time": {"from": time_from, "to": "now"},
        "timezone": "",
        "templating": {"list": variables},
        "annotations": {"list": [{
            "builtIn": 1, "type": "dashboard", "hide": True,
            "datasource": {"type": "grafana", "uid": "-- Grafana --"},
            "name": "Annotations & Alerts",
            "iconColor": "rgba(0, 211, 255, 1)",
        }]},
        "panels": [],
    }


def layout(dash, items):
    """items: list of (panel_fn_result_without_grid, w, h) or ('row', title).
    Lays out left-to-right wrapping at width 24."""
    x = 0
    y = 0
    rowh = 0
    panels = dash["panels"]
    for it in items:
        if it[0] == "row":
            if x:
                y += rowh
                x = 0
                rowh = 0
            panels.append(row(it[1], y))
            y += 1
            continue
        panel, w, h = it
        if x + w > 24:
            y += rowh
            x = 0
            rowh = 0
        panel["gridPos"] = gridpos(x, y, w, h)
        panel["id"] = len(panels) + 1
        panels.append(panel)
        x += w
        rowh = max(rowh, h)


# ============================================================ FLEET DASH ====

def build_fleet():
    variables = [
        var_datasource(),
        var_query("env", "Environment",
                  "label_values(server_uptime_ms, env)", multi=True, all_=True),
        var_query("namespace", "Namespace",
                  'label_values(server_uptime_ms{env=~"$env"}, namespace)',
                  multi=True, all_=True),
    ]
    d = base_dashboard("yb-fleet-overview",
                       "YugabyteDB · Fleet Overview (All Environments)",
                       variables, ["yugabytedb", "fleet"], refresh="30s")

    live = 'sum(num_tablet_servers_live{%s,%s})' % (F_ENV, F_NS)
    dead = 'sum(num_tablet_servers_dead{%s,%s})' % (F_ENV, F_NS)

    items = [
        ("row", "Fleet health"),
        (stat("Environments", "count(count by (env)(server_uptime_ms{%s}))" % F_ENV,
              "none"), 3, 4),
        (stat("Namespaces with YB",
              "count(count by (env,namespace)(server_uptime_ms%s))" % NSEL, "none"),
         3, 4),
        # up is deduped per pod (max by pod) because tservers are scraped on two
        # endpoints (:9000 and :13000); a plain sum(up) would double-count them.
        (stat("Masters Up", "sum(max by (env,namespace,pod)(up%s))" % MAST, "none",
              color_mode="value"), 3, 4),
        (stat("Masters Down", "sum(max by (env,namespace,pod)(up%s) == bool 0)" % MAST, "none",
              thresholds=[{"color": "green", "value": None},
                          {"color": "red", "value": 1}]), 3, 4),
        (stat("TServers Up", "sum(max by (env,namespace,pod)(up%s))" % TSRV, "none"), 3, 4),
        (stat("TServers Down", "sum(max by (env,namespace,pod)(up%s) == bool 0)" % TSRV, "none",
              thresholds=[{"color": "green", "value": None},
                          {"color": "red", "value": 1}]), 3, 4),
        (stat("Live TServers (cluster view)", live, "none"), 3, 4),
        (stat("Dead TServers (cluster view)", dead, "none",
              thresholds=[{"color": "green", "value": None},
                          {"color": "red", "value": 1}]), 3, 4),

        ("row", "Throughput across environments"),
        (timeseries("YSQL Ops/sec by environment", targets(
            ("sum by (env) (rate(handler_latency_yb_ysqlserver_SQLProcessor_SelectStmt_count%s[5m]) "
             "+ rate(handler_latency_yb_ysqlserver_SQLProcessor_InsertStmt_count%s[5m]) "
             "+ rate(handler_latency_yb_ysqlserver_SQLProcessor_UpdateStmt_count%s[5m]) "
             "+ rate(handler_latency_yb_ysqlserver_SQLProcessor_DeleteStmt_count%s[5m]) "
             "+ rate(handler_latency_yb_ysqlserver_SQLProcessor_OtherStmts_count%s[5m]))"
             % (NSEL, NSEL, NSEL, NSEL, NSEL), "{{env}}")),
            unit="ops", legend_table=True), 12, 8),
        (timeseries("TServer Read+Write Ops/sec by environment", targets(
            ("sum by (env) (rate(handler_latency_yb_tserver_TabletServerService_Read_count%s[5m]) "
             "+ rate(handler_latency_yb_tserver_TabletServerService_Write_count%s[5m]))"
             % (NSEL, NSEL), "{{env}}")),
            unit="ops", legend_table=True), 12, 8),
        (timeseries("Avg YSQL Select latency by environment", targets(
            ("sum by (env)(rate(handler_latency_yb_ysqlserver_SQLProcessor_SelectStmt_sum%s[5m])) "
             "/ clamp_min(sum by (env)(rate(handler_latency_yb_ysqlserver_SQLProcessor_SelectStmt_count%s[5m])),1)"
             % (NSEL, NSEL), "{{env}}")), unit="µs", legend_table=True), 12, 8),
        (timeseries("Log error rate by environment (master+tserver)", targets(
            ("sum by (env)(rate(glog_error_messages{%s,%s}[5m]))" % (F_ENV, F_NS),
             "{{env}}")), unit="cps", legend_table=True), 12, 8),

        ("row", "Per environment / namespace status"),
        (table("YugabyteDB instances by env / namespace", [
            target('sum by (env,namespace)(max by (env,namespace,pod)(up{job="yb-master",%s,%s}))' % (F_ENV, F_NS),
                   "Masters Up", instant=True, ref="A"),
            target('sum by (env,namespace)(max by (env,namespace,pod)(up{job="yb-tserver",%s,%s}))' % (F_ENV, F_NS),
                   "TServers Up", instant=True, ref="B"),
            target('sum by (env,namespace)(num_tablet_servers_live%s)' % NSEL,
                   "Live TServers", instant=True, ref="C"),
            target('sum by (env,namespace)(num_tablet_servers_dead%s)' % NSEL,
                   "Dead TServers", instant=True, ref="D"),
        ], desc="One row per environment+namespace running YugabyteDB.",
            overrides=[{
                "matcher": {"id": "byName", "options": "Dead TServers"},
                "properties": [{"id": "custom.cellOptions",
                                "value": {"type": "color-background"}},
                               {"id": "thresholds", "value": {"mode": "absolute",
                                "steps": [{"color": "green", "value": None},
                                          {"color": "red", "value": 1}]}}],
            }]), 24, 9),
    ]
    layout(d, items)
    return d


# ========================================================= PER-ENV DASH ====

def build_env_detail():
    variables = [
        var_datasource(),
        var_query("env", "Environment",
                  "label_values(server_uptime_ms, env)", multi=False, all_=False),
        var_query("namespace", "Namespace",
                  'label_values(server_uptime_ms{env=~"$env"}, namespace)',
                  multi=True, all_=True),
        var_query("pod", "Pod",
                  'label_values(server_uptime_ms{env=~"$env",namespace=~"$namespace"}, pod)',
                  multi=True, all_=True),
    ]
    d = base_dashboard("yb-env-detail",
                       "YugabyteDB · Environment Detail",
                       variables, ["yugabytedb", "detail"], refresh="30s")

    POD = 'pod=~"$pod"'
    M = '{job="yb-master",%s,%s,%s}' % (F_ENV, F_NS, POD)
    T = '{job="yb-tserver",%s,%s,%s}' % (F_ENV, F_NS, POD)
    C = '{%s,%s}' % (F_ENV, F_NS)   # cluster-level (num_tablet_servers_*)

    items = [
        ("row", "Cluster health"),
        # dedupe up per pod: tservers expose two scrape endpoints (:9000, :13000)
        (stat("Masters Up", "sum(max by (env,namespace,pod)(up%s))" % M, "none"), 4, 4),
        (stat("TServers Up", "sum(max by (env,namespace,pod)(up%s))" % T, "none"), 4, 4),
        (stat("Live TServers", "sum(num_tablet_servers_live%s)" % C, "none"), 4, 4),
        (stat("Dead TServers", "sum(num_tablet_servers_dead%s)" % C, "none",
              thresholds=[{"color": "green", "value": None},
                          {"color": "red", "value": 1}]), 4, 4),
        (stat("Min server uptime", "min(server_uptime_ms%s)/1000" %
              ('{%s,%s,%s}' % (F_ENV, F_NS, POD)), "s"), 4, 4),
        (timeseries("Live vs dead tservers", targets(
            ("sum(num_tablet_servers_live%s)" % C, "live"),
            ("sum(num_tablet_servers_dead%s)" % C, "dead")), unit="none"), 4, 4),

        ("row", "Masters"),
        (timeseries("Master CPU (cores)", targets(
            ("(rate(cpu_utime%s[5m]) + rate(cpu_stime%s[5m]))/1000" % (M, M),
             "{{pod}}")), unit="none", desc="cpu_utime/stime are milliseconds; /1000 ~ cores"), 8, 7),
        (timeseries("Master heap size", targets(
            ("generic_heap_size%s" % M, "{{pod}}")), unit="bytes"), 8, 7),
        (timeseries("Master inbound RPC/sec", targets(
            ("rate(rpc_inbound_calls_created%s[5m])" % M, "{{pod}}")), unit="ops"), 8, 7),
        (timeseries("Master log warn/error rate", targets(
            ("rate(glog_warning_messages%s[5m])" % M, "{{pod}} warn"),
            ("rate(glog_error_messages%s[5m])" % M, "{{pod}} error")), unit="cps"), 12, 7),
        (timeseries("Master threads running", targets(
            ("sum by (pod)(threads_running%s)" % M, "{{pod}}")), unit="short"), 12, 7),

        ("row", "TServers — resources"),
        (timeseries("TServer CPU (cores)", targets(
            ("(rate(cpu_utime%s[5m]) + rate(cpu_stime%s[5m]))/1000" % (T, T),
             "{{pod}}")), unit="none"), 8, 7),
        (timeseries("TServer heap size", targets(
            ("generic_heap_size%s" % T, "{{pod}}")), unit="bytes"), 8, 7),
        (timeseries("TServer inbound RPC/sec", targets(
            ("rate(rpc_inbound_calls_created%s[5m])" % T, "{{pod}}")), unit="ops"), 8, 7),

        ("row", "TServers — read / write"),
        (timeseries("Read Ops/sec by tserver", targets(
            ("sum by (pod)(rate(handler_latency_yb_tserver_TabletServerService_Read_count%s[5m]))"
             % T, "{{pod}}")), unit="ops"), 12, 7),
        (timeseries("Write Ops/sec by tserver", targets(
            ("sum by (pod)(rate(handler_latency_yb_tserver_TabletServerService_Write_count%s[5m]))"
             % T, "{{pod}}")), unit="ops"), 12, 7),
        (timeseries("Read latency avg by tserver", targets(
            ("sum by (pod)(rate(handler_latency_yb_tserver_TabletServerService_Read_sum%s[5m])) "
             "/ clamp_min(sum by (pod)(rate(handler_latency_yb_tserver_TabletServerService_Read_count%s[5m])),1)"
             % (T, T), "{{pod}}")), unit="µs"), 12, 7),
        (timeseries("Write latency avg by tserver", targets(
            ("sum by (pod)(rate(handler_latency_yb_tserver_TabletServerService_Write_sum%s[5m])) "
             "/ clamp_min(sum by (pod)(rate(handler_latency_yb_tserver_TabletServerService_Write_count%s[5m])),1)"
             % (T, T), "{{pod}}")), unit="µs"), 12, 7),

        ("row", "YSQL"),
        (timeseries("YSQL Ops/sec by statement", targets(
            ("sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_SelectStmt_count%s[5m]))" % C, "select"),
            ("sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_InsertStmt_count%s[5m]))" % C, "insert"),
            ("sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_UpdateStmt_count%s[5m]))" % C, "update"),
            ("sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_DeleteStmt_count%s[5m]))" % C, "delete"),
            ("sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_OtherStmts_count%s[5m]))" % C, "other"),
        ), unit="ops", stack=True), 8, 7),
        (timeseries("YSQL avg latency by statement", targets(
            ("sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_SelectStmt_sum%s[5m]))/clamp_min(sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_SelectStmt_count%s[5m])),1)" % (C, C), "select"),
            ("sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_InsertStmt_sum%s[5m]))/clamp_min(sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_InsertStmt_count%s[5m])),1)" % (C, C), "insert"),
            ("sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_UpdateStmt_sum%s[5m]))/clamp_min(sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_UpdateStmt_count%s[5m])),1)" % (C, C), "update"),
            ("sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_DeleteStmt_sum%s[5m]))/clamp_min(sum(rate(handler_latency_yb_ysqlserver_SQLProcessor_DeleteStmt_count%s[5m])),1)" % (C, C), "delete"),
        ), unit="µs"), 8, 7),
        (timeseries("YSQL active connections", targets(
            ("sum by (pod)(yb_ysqlserver_active_connection_total%s)" % T, "{{pod}}")),
            unit="none"), 8, 7),

        ("row", "Storage (RocksDB / WAL)"),
        (timeseries("SST files size by tserver", targets(
            ("sum by (pod)(rocksdb_current_version_sst_files_size%s)" % T, "{{pod}}")),
            unit="bytes"), 8, 7),
        (timeseries("Block cache hit ratio", targets(
            ("sum(rate(rocksdb_block_cache_hit%s[5m])) / clamp_min(sum(rate(rocksdb_block_cache_hit%s[5m])) + sum(rate(rocksdb_block_cache_miss%s[5m])),1)"
             % (T, T, T), "hit ratio")), unit="percentunit"), 8, 7),
        (timeseries("WAL bytes/sec by tserver", targets(
            ("sum by (pod)(rate(log_bytes_logged%s[5m]))" % T, "{{pod}}")),
            unit="Bps"), 8, 7),
        (timeseries("Majority SST file rejections/sec (write back-pressure)", targets(
            ("sum by (pod)(rate(majority_sst_files_rejections%s[5m]))" % T, "{{pod}}")),
            unit="cps", desc="Sustained >0 means tablets are throttling writes"), 24, 6),
    ]
    layout(d, items)
    return d


# ====================================================== PLATFORM DASH ====
# Component-agnostic dashboard, proves the chart is not YugabyteDB-specific.
# Uses metrics every Prometheus scrape has: up, scrape_duration_seconds, and
# Prometheus' own TSDB metrics.

def build_platform():
    variables = [
        var_datasource(),
        var_query("job", "Job", "label_values(up, job)", multi=True, all_=True),
    ]
    d = base_dashboard("platform-prometheus-targets",
                       "Platform · Prometheus Targets & Scrape Health",
                       variables, ["platform", "prometheus"], refresh="30s")
    J = '{job=~"$job"}'
    items = [
        ("row", "Scrape targets"),
        (stat("Targets Up", "sum(up%s)" % J, "none"), 4, 4),
        (stat("Targets Down", "sum(up%s == bool 0)" % J, "none",
              thresholds=[{"color": "green", "value": None},
                          {"color": "red", "value": 1}]), 4, 4),
        (stat("Jobs", "count(count by (job)(up%s))" % J, "none"), 4, 4),
        (stat("Max scrape duration", "max(scrape_duration_seconds%s)" % J, "s"), 4, 4),
        (timeseries("Targets up by job", targets(
            ("sum by (job)(up%s)" % J, "{{job}}")), unit="none", legend_table=True), 12, 8),
        (timeseries("Scrape duration by job (avg)", targets(
            ("avg by (job)(scrape_duration_seconds%s)" % J, "{{job}}")), unit="s",
            legend_table=True), 12, 8),
        ("row", "Prometheus server"),
        (timeseries("TSDB head series", targets(
            ("prometheus_tsdb_head_series", "series")), unit="short"), 8, 7),
        (timeseries("Samples ingested/sec", targets(
            ("rate(prometheus_tsdb_head_samples_appended_total[5m])", "samples/s")),
            unit="cps"), 8, 7),
        (timeseries("Prometheus memory (RSS)", targets(
            ("process_resident_memory_bytes{job=~\".*prometheus.*\"}", "{{pod}}")),
            unit="bytes"), 8, 7),
    ]
    layout(d, items)
    return d


# ----------------------------------------------------------------- main ----

def wrap_for_provisioning(dash):
    # The Grafana sidecar imports the file as-is; keep the bare dashboard model.
    return dash


# Publish into the generic grafana-dashboards chart, organised by component folder.
CHART_ROOT = os.path.join(os.path.dirname(__file__), "charts",
                          "grafana-dashboards", "files", "dashboards")


def write_dashboard(d, name, folder):
    """Write JSON to monitoring/dashboards/ (flat) and into the chart under <folder>/."""
    blob = json.dumps(d, indent=2)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name + ".json"), "w") as f:
        f.write(blob)
    chart_dir = os.path.join(CHART_ROOT, folder)
    os.makedirs(chart_dir, exist_ok=True)
    with open(os.path.join(chart_dir, name + ".json"), "w") as f:
        f.write(blob)
    print("wrote [%s] %s  panels: %d" %
          (folder, name, len([p for p in d["panels"] if p.get("type") != "row"])))


def main():
    write_dashboard(build_fleet(), "yb-fleet-overview", "YugabyteDB")
    write_dashboard(build_env_detail(), "yb-env-detail", "YugabyteDB")
    write_dashboard(build_platform(), "prometheus-targets", "Platform")


if __name__ == "__main__":
    main()
