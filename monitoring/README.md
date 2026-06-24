# Central Grafana monitoring for YugabyteDB (N envs / N namespaces)

This deploys Prometheus + Grafana and gives you two dashboards to watch YugabyteDB
masters and tservers across every environment and namespace from one place, with
`env`, `namespace`, and `pod` dropdowns.

The dashboards query the **raw** metric names that YugabyteDB exposes on its
`/prometheus-metrics` endpoints, scraped by a normal Prometheus through a
ServiceMonitor. They are not the YugabyteDB Anywhere model (see "Why not the
official dashboard" below).

## What you get

| File | Purpose |
|------|---------|
| `dashboards/yb-fleet-overview.json` | Roll-up across all envs/namespaces. Multi-select `env` and `namespace`. Masters/tservers up, live/dead tservers, ops and latency by environment, and a status table with one row per env+namespace. Start here. |
| `dashboards/yb-env-detail.json` | Deep dive into one environment. Single-select `env`, multi-select `namespace` and `pod`. Master and tserver CPU/heap/RPC, read/write ops and latency, YSQL ops/latency/connections, and RocksDB/WAL storage. |
| `generate_dashboards.py` | Regenerates both JSON files. Edit panels/queries here, not in the JSON. |
| `values-monitoring.yaml` | Lean kube-prometheus-stack values (Prometheus + Grafana, k8s control-plane scrapers off). |
| `yb-servicemonitors.yaml` | Tells Prometheus how to scrape YugabyteDB master/tserver, sets `job`/`env`/`cluster` labels, and controls metric cardinality. |
| `yb-servicemonitors-multienv.yaml` | Same, but stamps distinct `env` labels (prod, staging) per namespace to simulate several environments from one Prometheus. |
| `prometheus-scrape-config.yaml` | The same scrape as a raw `scrape_configs` block, for plain Prometheus without the Operator. Produces identical labels, so the dashboards work unchanged. Includes a non-Kubernetes (static_configs) variant. |

## Deploy (single env, what is running now)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && helm repo update
kubectl create namespace monitoring

# 1) Prometheus + Grafana
helm install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring -f monitoring/values-monitoring.yaml

# 2) tell Prometheus how to scrape YugabyteDB
kubectl apply -f monitoring/yb-servicemonitors.yaml

# 3) load the two dashboards (Grafana sidecar imports any configmap labelled grafana_dashboard=1)
kubectl create configmap yb-fleet-overview -n monitoring \
  --from-file=monitoring/dashboards/yb-fleet-overview.json
kubectl create configmap yb-env-detail -n monitoring \
  --from-file=monitoring/dashboards/yb-env-detail.json
kubectl label configmap yb-fleet-overview yb-env-detail -n monitoring grafana_dashboard=1
```

Open Grafana:

```bash
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
# http://localhost:3000  user: admin  pass: admin  (change for real use)
```

The dashboards land under Dashboards, tagged `yugabytedb`.

## Label model the dashboards rely on

Every YugabyteDB series carries these labels after the scrape:

| Label | Source | Used for |
|-------|--------|----------|
| `env` | stamped by ServiceMonitor relabeling (`replacement: dev`) | environment dropdown |
| `cluster` | stamped by ServiceMonitor relabeling | secondary identity |
| `namespace` | k8s service discovery | namespace dropdown |
| `pod` | k8s service discovery | pod dropdown / per-pod series |
| `job` | ServiceMonitor relabeling (`yb-master` / `yb-tserver`) | master vs tserver split |

Note on `env`: Prometheus `externalLabels` are only attached on remote_write and
federation, not on locally stored series, so they do **not** show up when Grafana
queries that same Prometheus directly. That is why `env` and `cluster` are stamped
through ServiceMonitor `relabelings` as well. Set them per environment.

## Scaling to N environments and N namespaces

**More namespaces in the same cluster.** Add them to `namespaceSelector.matchNames`
in `yb-servicemonitors.yaml` (or switch to a label selector so any namespace tagged,
say, `yugabyte=true` is picked up automatically), then re-apply. The `namespace`
dropdown fills in on its own from `label_values(server_uptime_ms, namespace)`.

**More environments.** Each environment is a separate Kubernetes cluster, so run one
monitoring stack per environment and set its identity in two places that must agree:
`prometheus.prometheusSpec.externalLabels.env` in `values-monitoring.yaml`, and the
`env`/`cluster` `replacement` values in `yb-servicemonitors.yaml`. Then centralize with
one of:

1. **One central Prometheus / Thanos / Grafana Mimir.** Each env's Prometheus
   `remote_write`s to it. The central store sees every env via the `env` label. Point
   both dashboards at that single data source and the `env` dropdown lists everything.
   This is the recommended central model.
2. **Grafana data source per environment.** Add each env's Prometheus as its own
   Grafana data source. The `datasource` dropdown at the top of each dashboard selects
   the environment. Simple, no central store, but the fleet dashboard then shows one
   env at a time.

Both dashboards expose a `datasource` variable, so either model works without editing
the JSON.

## Cardinality control

`yb-servicemonitors.yaml` keeps all `server` and `cluster` metrics plus a short
allowlist of tablet-scoped storage metrics (`rocksdb_current_version_sst_files_size`,
`rocksdb_block_cache_hit`/`miss`, `log_bytes_logged`, `majority_sst_files_rejections`).
It drops per-table rollups and all other per-tablet series, which otherwise grow with
table and tablet count and dominate the time series database on large clusters. Add
metric names to the allowlist regex if you need more tablet-level detail, and budget
for the extra cardinality.

## Why not the official YugabyteDB dashboard

The dashboard at `cloud/grafana/YugabyteDB.json` in the yugabyte-db repo is built for
**YugabyteDB Anywhere**, whose collector rewrites metrics into
`rpc_latency_count{saved_name="...", node_prefix="...", export_type="master_export"}`.
A plain Prometheus scraping the helm chart pods sees the raw names instead, for example
`handler_latency_yb_tserver_TabletServerService_Read_count` with `exported_instance`
and the k8s `namespace`/`pod` labels. The official JSON therefore renders empty against
a ServiceMonitor scrape. These dashboards use the raw names directly and add the
multi-env templating, so they work with the standard helm + Prometheus Operator setup.

## A note on units

`cpu_utime` / `cpu_stime` are cumulative milliseconds, so the CPU panels use
`(rate(cpu_utime[5m]) + rate(cpu_stime[5m]))/1000` and label the result as cores.
`handler_latency_*_sum` is in microseconds, so average-latency panels use the µs unit.
```
