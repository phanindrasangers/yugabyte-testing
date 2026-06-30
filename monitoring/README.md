# YugabyteDB Monitoring — vCluster-Aware Setup

This directory contains everything needed to monitor YugabyteDB across multiple vClusters and namespaces with a single central Prometheus and two Grafana dashboards that segregate metrics by vCluster, environment, and namespace.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [vCluster Service Naming — The `-x-` Convention](#vcluster-service-naming)
3. [How Prometheus Discovers Services in a vCluster](#how-prometheus-discovers-services)
   - [Approach A: ServiceMonitor (Host-Level)](#approach-a-servicemonitor-host-level)
   - [Approach B: ServiceMonitor (Inside vCluster)](#approach-b-servicemonitor-inside-vcluster)
   - [Approach C: Raw scrape_config](#approach-c-raw-scrape_config)
4. [Label Model](#label-model)
5. [Deploy: Install vClusters](#deploy-install-vclusters)
6. [Deploy: YugabyteDB Helm](#deploy-yugabytedb-helm)
   - [RF3 (3 masters, 3 tservers)](#rf3-3-masters-3-tservers)
   - [Single master + 3 tservers](#single-master--3-tservers)
7. [Deploy: ServiceMonitors (Host)](#deploy-servicemonitors-host)
8. [Deploy: ServiceMonitors (Inside vCluster)](#deploy-servicemonitors-inside-vcluster)
9. [Scaling Masters (RF3 Operations)](#scaling-masters-rf3-operations)
10. [Dashboards](#dashboards)
11. [PrometheusRule Alerts](#prometheusrule-alerts)
12. [Cardinality Control](#cardinality-control)
13. [Higher Env (No vCluster)](#higher-env-no-vcluster)
14. [File Reference](#file-reference)

---

## Architecture Overview

```
HOST CLUSTER (kind / EKS / GKE)
│
├── namespace: monitoring
│     Prometheus (kube-prometheus-stack)
│     Grafana  (dashboards: Fleet + Detail + Platform)
│
├── namespace: cbg-demo   ← vCluster host namespace
│     pod: cbg-demo-0     ← vCluster control-plane
│     pod: yb-master-0-x-cbg-in-x-cbg-demo      ← synced
│     pod: yb-master-0-x-cbg-uae-x-cbg-demo     ← synced
│     pod: yb-master-0-x-cbg-global-x-cbg-demo  ← synced
│     pod: yb-tserver-0-x-cbg-in-x-cbg-demo     ← synced
│     ... (6 YB pods per vCluster)
│     svc: yb-masters-x-cbg-in-x-cbg-demo       ← synced
│     svc: yb-tservers-x-cbg-in-x-cbg-demo      ← synced
│     ... (6 YB services per vCluster)
│
├── namespace: cbg-test   ← vCluster host namespace (same pattern)
│
└── namespace: cbg-dev    ← vCluster host namespace (same pattern)


VIRTUAL CLUSTERS (inside each vCluster)
│
└── vCluster: cbg-demo
      namespace: cbg-in      → yb-master-0, yb-tserver-0 (India)
      namespace: cbg-uae     → yb-master-0, yb-tserver-0 (UAE)
      namespace: cbg-global  → yb-master-0, yb-tserver-0 (Global)
```

### Label model on every scraped series

| Label       | Value (lower env)        | Value (higher env)     |
|-------------|--------------------------|------------------------|
| `job`       | `yb-master` / `yb-tserver` | same                 |
| `vcluster`  | `cbg-demo` / `cbg-test` / `cbg-dev` | *(empty)* |
| `env`       | `cbg-demo` (= vcluster)  | `prod` / `staging`     |
| `namespace` | `cbg-in` / `cbg-uae` / `cbg-global` | `cbg-in-prod` |
| `pod`       | `yb-master-0`            | `yb-master-0`          |

---

## vCluster Service Naming

vCluster syncs every pod and service from inside the virtual cluster to the host namespace. The synced object names follow a strict pattern:

```
<original-name>-x-<original-namespace>-x-<vcluster-name>
```

Examples for a YugabyteDB deployment in namespace `cbg-in` inside vCluster `cbg-demo`:

| Object type | Name inside vCluster | Name on host |
|-------------|----------------------|--------------|
| StatefulSet pod | `yb-master-0` | `yb-master-0-x-cbg-in-x-cbg-demo` |
| StatefulSet pod | `yb-tserver-0` | `yb-tserver-0-x-cbg-in-x-cbg-demo` |
| Headless service | `yb-masters` | `yb-masters-x-cbg-in-x-cbg-demo` |
| Headless service | `yb-tservers` | `yb-tservers-x-cbg-in-x-cbg-demo` |

With 3 vClusters × 3 namespaces each, the host cluster has 18 YB headless services:

```
yb-masters-x-cbg-in-x-cbg-demo
yb-masters-x-cbg-uae-x-cbg-demo
yb-masters-x-cbg-global-x-cbg-demo
yb-masters-x-cbg-in-x-cbg-test
yb-masters-x-cbg-uae-x-cbg-test
yb-masters-x-cbg-global-x-cbg-test
yb-masters-x-cbg-in-x-cbg-dev
yb-masters-x-cbg-uae-x-cbg-dev
yb-masters-x-cbg-global-x-cbg-dev
(+ 9 yb-tservers-x-... services)
```

**Key property:** vCluster preserves the original pod and service *labels* on every synced object. A service labelled `app: yb-master` inside the vCluster has exactly that label on the host. Port names (`http-ui`, `http-ysql-met`) are also preserved. This is what makes label-based service discovery work without any name-based filtering.

**Extra labels added by vCluster on every synced object:**
```
vcluster.loft.sh/namespace: <original-namespace>   (e.g. cbg-in)
vcluster.loft.sh/managed-by: <vcluster-name>       (e.g. cbg-demo)
```
These are an alternative way to read the original namespace without regex.

---

## How Prometheus Discovers Services in a vCluster

There are three approaches. They all produce identical labels so the same dashboards work for all of them.

### Approach A: ServiceMonitor (Host-Level)

**File:** `monitoring/yb-servicemonitors-host.yaml`

This is the recommended approach for central monitoring. Deploy one Prometheus on the host; it discovers synced services in all vCluster host namespaces.

```
HOST PROMETHEUS
    |
    | ServiceMonitor: namespaceSelector = [cbg-demo, cbg-test, cbg-dev]
    |                 selector = {app: yb-master}  ← label on synced service
    |
    ▼
yb-masters-x-cbg-in-x-cbg-demo   (in ns cbg-demo)  ← matched by label, not name
yb-masters-x-cbg-uae-x-cbg-demo  (in ns cbg-demo)  ← same label app: yb-master
yb-masters-x-cbg-in-x-cbg-test   (in ns cbg-test)
... (all 9 master services matched automatically)
```

**Why the `-x-` name does not appear in the ServiceMonitor selector:**
The ServiceMonitor uses `matchLabels: app: yb-master`. Since vCluster preserves original labels, this matches every synced service regardless of its long host-side name. You never write `yb-masters-x-cbg-in-x-cbg-demo` anywhere in the ServiceMonitor.

**The `-x-` name is only used in relabeling,** to extract the original namespace and pod:

```yaml
# From service name: yb-masters-x-cbg-in-x-cbg-demo  ->  namespace=cbg-in
- sourceLabels: [__meta_kubernetes_service_name]
  regex: '(.+?)-x-(.+)-x-(.+)'
  targetLabel: namespace
  replacement: '$2'

# From pod name: yb-master-0-x-cbg-in-x-cbg-demo  ->  pod=yb-master-0
- sourceLabels: [__meta_kubernetes_pod_name]
  regex: '(.+?)-x-(.+)-x-(.+)'
  targetLabel: pod
  replacement: '$1'
```

**Why non-greedy `(.+?)`:** The original name, namespace, and vcluster name all contain hyphens. `(.+)` (greedy) would absorb too much. `(.+?)` stops at the first `-x-` occurrence, correctly isolating the original name.

```
Input:  "yb-master-0-x-cbg-in-x-cbg-demo"
                    ^^^
Greedy (.+) would eat "yb-master-0-x-cbg-in" as group 1 (wrong).
Non-greedy (.+?) stops at first -x-:
  group 1 = "yb-master-0"   group 2 = "cbg-in"   group 3 = "cbg-demo"
```

**Alternative — use the vCluster label** instead of the regex:

```yaml
# Reads the vcluster.loft.sh/namespace annotation directly, no regex needed:
- sourceLabels: [__meta_kubernetes_service_label_vcluster_loft_sh_namespace]
  targetLabel: namespace
```

Both produce the same `namespace` label value. The regex is more portable across vCluster versions.

**Full ServiceMonitor (master) for reference:**

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: yb-master-vcluster-host
  namespace: monitoring
spec:
  namespaceSelector:
    matchNames: [cbg-demo, cbg-test, cbg-dev]   # host ns = vcluster names
  selector:
    matchLabels:
      app: yb-master                             # label preserved by vCluster
  endpoints:
    - port: http-ui                              # port name preserved by vCluster
      path: /prometheus-metrics
      relabelings:
        - sourceLabels: [__meta_kubernetes_namespace]
          targetLabel: vcluster                  # host ns = vcluster name
        - sourceLabels: [__meta_kubernetes_namespace]
          targetLabel: env
        - sourceLabels: [__meta_kubernetes_service_name]
          regex: '(.+?)-x-(.+)-x-(.+)'
          targetLabel: namespace
          replacement: '$2'
        - sourceLabels: [__meta_kubernetes_pod_name]
          regex: '(.+?)-x-(.+)-x-(.+)'
          targetLabel: pod
          replacement: '$1'
```

### Approach B: ServiceMonitor (Inside vCluster)

**File:** `monitoring/yb-servicemonitors-vcluster.yaml`

Deploy a Prometheus Operator **inside** each vCluster. The ServiceMonitor uses standard in-cluster service discovery — no `-x-` names at all, because inside the vCluster everything looks normal.

```
PROMETHEUS inside cbg-demo vCluster
    |
    | ServiceMonitor: namespaceSelector = [cbg-in, cbg-uae, cbg-global]
    |                 selector = {app: yb-master}
    |
    ▼
yb-masters   (in ns cbg-in)     ← plain name, no -x- suffix
yb-masters   (in ns cbg-uae)
yb-masters   (in ns cbg-global)
```

The vcluster and env labels are **stamped as constants** because the ServiceMonitor knows which vCluster it lives in:

```yaml
relabelings:
  - targetLabel: vcluster
    replacement: cbg-demo    # hardcoded — this monitor runs inside cbg-demo
  - targetLabel: env
    replacement: cbg-demo
```

**When to use Approach B:** When each vCluster team owns its own observability stack, or when vCluster network isolation prevents the host Prometheus from reaching pods.

**Apply:**
```bash
vcluster connect cbg-demo -n cbg-demo -- kubectl apply -f monitoring/yb-servicemonitors-vcluster.yaml
vcluster connect cbg-test -n cbg-test -- kubectl apply -f monitoring/yb-servicemonitors-vcluster.yaml
vcluster connect cbg-dev  -n cbg-dev  -- kubectl apply -f monitoring/yb-servicemonitors-vcluster.yaml
```

### Approach C: Raw scrape_config

**File:** `monitoring/prometheus-scrape-config.yaml`

For clusters not using Prometheus Operator (plain `prometheus.yml`). Four sections:

| Section | Topology | vcluster label source |
|---------|----------|----------------------|
| A | Inside-vCluster Prometheus | constant `replacement: cbg-demo` |
| B | Host Prometheus, pod SD | from `__meta_kubernetes_namespace` |
| C | Direct namespace (no vCluster) | omitted (empty label) |
| D | Static targets (VMs, bare metal) | constant or empty string |

**Section B (host-level) — how it identifies services:**

```yaml
- job_name: yb-master-vcluster-host
  kubernetes_sd_configs:
    - role: pod
      namespaces:
        names: [cbg-demo, cbg-test, cbg-dev]   # host namespaces (= vcluster names)
  relabel_configs:
    # Step 1: filter by pod label — same label selector as ServiceMonitor
    - source_labels: [__meta_kubernetes_pod_label_app]
      regex: yb-master
      action: keep
    # Step 2: filter by port name — preserved by vCluster
    - source_labels: [__meta_kubernetes_pod_container_port_name]
      regex: http-ui
      action: keep
    # Step 3: stamp vcluster from host namespace
    - source_labels: [__meta_kubernetes_namespace]
      target_label: vcluster
    # Step 4: extract original pod + namespace from synced name using regex
    - source_labels: [__meta_kubernetes_pod_name]
      regex: '(.+?)-x-(.+)-x-(.+)'
      target_label: pod
      replacement: '$1'
    - source_labels: [__meta_kubernetes_pod_name]
      regex: '(.+?)-x-(.+)-x-(.+)'
      target_label: namespace
      replacement: '$2'
```

**Key point:** neither the ServiceMonitor nor the scrape_config ever filters by the `-x-` service/pod name directly. Both use the `app: yb-master` label for discovery. The `-x-` format only appears in regex relabeling rules that decode the original object identity.

**Load via additionalScrapeConfigs:**
```bash
kubectl -n monitoring create secret generic yb-scrape-configs \
  --from-file=yb-scrape.yaml=monitoring/prometheus-scrape-config.yaml

# In values-monitoring.yaml:
# prometheus:
#   prometheusSpec:
#     additionalScrapeConfigs:
#       name: yb-scrape-configs
#       key: yb-scrape.yaml
```

---

## Label Model

Every scraped time series has these labels regardless of which approach produced it:

```
up{
  job="yb-master",
  vcluster="cbg-demo",   # empty for higher-env (direct namespace)
  env="cbg-demo",        # = vcluster name in lower envs
  namespace="cbg-in",    # original k8s namespace inside vCluster
  pod="yb-master-0"      # original pod name inside vCluster
}
```

Dashboard variables use `vcluster=~"$vcluster"` with `allValue=.*`. This regex matches the empty string, so higher-env series (which have no `vcluster` label) appear when "All" is selected. This is intentional — a single dashboard covers both topologies.

---

## Deploy: Install vClusters

### Prerequisites

```bash
# Install vcluster CLI (no sudo needed)
curl -L -o ~/.local/bin/vcluster \
  "https://github.com/loft-sh/vcluster/releases/download/v0.35.1/vcluster-linux-amd64"
chmod +x ~/.local/bin/vcluster
export PATH=$PATH:~/.local/bin
```

### Create all 3 vClusters

```bash
for vc in cbg-demo cbg-test cbg-dev; do
  vcluster create $vc --namespace $vc \
    --set "controlPlane.statefulSet.resources.requests.cpu=100m" \
    --set "controlPlane.statefulSet.resources.requests.memory=256Mi" \
    --set "controlPlane.statefulSet.resources.limits.cpu=200m" \
    --set "controlPlane.statefulSet.resources.limits.memory=512Mi" \
    --connect=false &
done
wait
vcluster list
```

### Create namespaces in each vCluster

```bash
for vc in cbg-demo cbg-test cbg-dev; do
  for ns in cbg-in cbg-uae cbg-global; do
    vcluster connect $vc --namespace $vc -- kubectl create ns $ns
  done
done
```

---

## Deploy: YugabyteDB Helm

### Shared values file

```yaml
# yb-values.yaml
enableLoadBalancer: false
replicas:
  master: 1       # RF1 for lower envs; set 3 for RF3
  tserver: 1      # set 3 for 3-tserver setup
resource:
  master:
    requests: {cpu: "0.2", memory: 512Mi}
    limits:   {cpu: "0.5", memory: 512Mi}
  tserver:
    requests: {cpu: "0.2", memory: 512Mi}
    limits:   {cpu: "0.5", memory: 512Mi}
storage:
  master:  {count: 1, size: 1Gi}
  tserver: {count: 1, size: 1Gi}
```

### Deploy in all 9 namespace/vCluster combinations

```bash
helm repo add yugabytedb https://charts.yugabyte.com
helm repo update

for vc in cbg-demo cbg-test cbg-dev; do
  for ns in cbg-in cbg-uae cbg-global; do
    vcluster connect $vc --namespace $vc -- \
      helm install yb-$ns yugabytedb/yugabyte \
        -n $ns --version 2024.1.6 --values yb-values.yaml &
  done
  wait
done
```

### RF3 (3 masters, 3 tservers) — production-grade

```yaml
# yb-values-rf3.yaml
enableLoadBalancer: false
replicas:
  master: 3
  tserver: 3
resource:
  master:
    requests: {cpu: "1", memory: 2Gi}
    limits:   {cpu: "2", memory: 4Gi}
  tserver:
    requests: {cpu: "2", memory: 4Gi}
    limits:   {cpu: "4", memory: 8Gi}
storage:
  master:  {count: 1, size: 10Gi}
  tserver: {count: 2, size: 100Gi}
```

```bash
helm install yb-cbg-in yugabytedb/yugabyte \
  -n cbg-in --values yb-values-rf3.yaml
```

### Single master + 3 tservers

For dev/test when you want write throughput without the RF3 master overhead:

```yaml
replicas:
  master: 1
  tserver: 3
```

The fleet dashboard counts masters and tservers separately, so this topology is clearly visible.

---

## Deploy: ServiceMonitors (Host)

The host-level approach is what is deployed in this environment. One `apply` covers all 3 vClusters and all their namespaces automatically. When you add a new namespace inside a vCluster, the synced service carries the original `app: yb-master` label and is auto-discovered without any ServiceMonitor change.

```bash
kubectl apply -f monitoring/yb-servicemonitors-host.yaml
```

**Verify targets are up:**
```bash
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090 &
# Open http://localhost:9090/targets  filter: "yb"
```

---

## Deploy: ServiceMonitors (Inside vCluster)

```bash
for vc in cbg-demo cbg-test cbg-dev; do
  vcluster connect $vc --namespace $vc -- \
    kubectl apply -f monitoring/yb-servicemonitors-vcluster.yaml
done
```

---

## Scaling Masters (RF3 Operations)

Never scale all masters simultaneously. YugabyteDB requires 2 of 3 masters for quorum.

### Scale up: 1 master → 3 masters

```bash
# 1. Helm upgrade to 3 replicas
vcluster connect cbg-demo -n cbg-demo -- \
  helm upgrade yb-cbg-in yugabytedb/yugabyte -n cbg-in \
  --reuse-values --set replicas.master=3

# 2. Wait for pods
vcluster connect cbg-demo -n cbg-demo -- \
  kubectl rollout status sts/yb-master -n cbg-in

# 3. Register new masters in quorum
vcluster connect cbg-demo -n cbg-demo -- \
  kubectl exec -n cbg-in yb-master-0 -- \
    yb-admin -master_addresses yb-masters.cbg-in.svc:7100 \
    change_master_config ADD_SERVER <master-1-ip> 7100

# Repeat ADD_SERVER for master-2
```

### Scale down: 3 masters → 1 master

Always REMOVE_SERVER before terminating a pod.

```bash
# 1. Remove from quorum first
vcluster connect cbg-demo -n cbg-demo -- \
  kubectl exec -n cbg-in yb-master-0 -- \
    yb-admin -master_addresses yb-masters.cbg-in.svc:7100 \
    change_master_config REMOVE_SERVER <master-2-ip> 7100

# Repeat for master-1

# 2. Scale down
vcluster connect cbg-demo -n cbg-demo -- \
  helm upgrade yb-cbg-in yugabytedb/yugabyte -n cbg-in \
  --reuse-values --set replicas.master=1
```

**Never do this:** `kubectl scale sts/yb-master --replicas=1` without first removing from quorum. The surviving master will wait indefinitely for missing peers.

---

## Dashboards

Access Grafana:
```bash
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
# http://localhost:3000  (admin / admin)
```

### Fleet Overview

Covers all vClusters, environments, and namespaces on one page.

| Panel | What it shows |
|-------|---------------|
| vClusters | Count of distinct vcluster label values (non-empty) |
| Environments | Count of distinct env values |
| Masters up | Total masters scraping successfully |
| TServers up | Total tservers scraping successfully |
| Dead TServers | Sum of `num_tablet_servers_dead` from masters |
| Fleet table | One row per vcluster/env/namespace — masters up, tservers up, SST size |
| WAL bytes/s | `rate(log_bytes_logged[5m])` summed across fleet |
| SST size | `sum(rocksdb_current_version_sst_files_size)` across fleet |

Variables cascade: selecting `cbg-demo` in the vcluster dropdown narrows env to only that vCluster's environments.

### vCluster / Environment Detail

Single-vCluster drill-down with pod-level breakdown.

Variables: `vcluster` (single-select), `env`, `namespace`, `pod`.

Key panels: master quorum health, YSQL SELECT latency, write back-pressure, RocksDB block cache hit ratio, SST size per pod, tserver RPC latency.

### Regenerate dashboards

```bash
cd monitoring
python3 generate_dashboards.py
helm upgrade grafana-dashboards charts/grafana-dashboards \
  -n monitoring --reuse-values
```

---

## PrometheusRule Alerts

### Apply directly

```bash
kubectl apply -f monitoring/prometheus-rules.yaml -n monitoring
```

If kube-prometheus-stack uses a `ruleSelector`, uncomment the label in the file:
```yaml
metadata:
  labels:
    release: monitoring
```

### Alert reference

| Alert | Severity | For | Condition |
|-------|----------|-----|-----------|
| `YBMasterDown` | critical | 2m | `up{job="yb-master"} == 0` for any pod |
| `YBMasterQuorumAtRisk` | critical | 5m | fewer than 3 masters up when 2+ known |
| `YBTServerDown` | critical | 2m | `up{job="yb-tserver"} == 0` for any pod |
| `YBTServerReportedDead` | warning | 5m | `num_tablet_servers_dead > 0` |
| `YBWriteBackPressure` | warning | 5m | `rate(majority_sst_files_rejections[5m]) > 0` |
| `YBHighYSQLSelectLatency` | warning | 5m | avg YSQL SELECT > 100ms |
| `YBHighReadLatency` | warning | 5m | tserver read RPC avg > 100ms |
| `YBHighWriteLatency` | warning | 5m | tserver write RPC avg > 100ms |
| `YBLowBlockCacheHitRatio` | info | 15m | RocksDB block cache hit ratio < 80% |
| `YBHighLogErrorRate` | warning | 5m | `glog_error_messages > 1/s` |

All alerts group by `vcluster`, `env`, `namespace` for per-environment Alertmanager routing.

---

## Cardinality Control

Every ServiceMonitor and scrape_config endpoint uses this `metricRelabelings` keep rule:

```yaml
metricRelabelings:
  - sourceLabels: [metric_type, __name__]
    separator: ";"
    regex: "(server|cluster|);.*|tablet;(rocksdb_current_version_sst_files_size|rocksdb_block_cache_hit|rocksdb_block_cache_miss|log_bytes_logged|majority_sst_files_rejections)"
    action: keep
```

A YB node exposes ~4,000 metrics per scrape. This rule keeps:
- All metrics where `metric_type` is `server`, `cluster`, or absent (the most important ones)
- 5 specific tablet-scoped metrics needed by dashboards and alerts

Everything else (per-table rollups, per-tablet details) is dropped, reducing storage by ~90%.

To add a metric: extend the `|tablet;(...)` alternation group.

---

## Higher Env (No vCluster)

In production or staging, YugabyteDB typically runs directly in a Kubernetes namespace with no vCluster layer. There is no `-x-` service renaming, no vCluster control-plane, and no `vcluster` label on scraped metrics. Two separate Grafana dashboards are provided for this topology.

### Label model (higher env)

| Label       | Value                                  |
|-------------|----------------------------------------|
| `job`       | `yb-master` / `yb-tserver`             |
| `vcluster`  | *(absent — no label)*                  |
| `env`       | `prod` / `staging` (constant, set in relabeling) |
| `namespace` | `cbg-in-prod` / `cbg-uae-prod` / etc. |
| `pod`       | `yb-master-0`                          |

### ServiceMonitor (higher env)

No vCluster selector is needed. The ServiceMonitor looks exactly like a standard Kubernetes ServiceMonitor — the only difference is that the relabeling stamps `env` but omits `vcluster`:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: yb-master-prod
  namespace: monitoring
spec:
  namespaceSelector:
    matchNames: [cbg-in-prod, cbg-uae-prod, cbg-global-prod]
  selector:
    matchLabels:
      app: yb-master
  endpoints:
    - port: http-ui
      path: /prometheus-metrics
      relabelings:
        - sourceLabels: [__meta_kubernetes_namespace]
          targetLabel: namespace
        - sourceLabels: [__meta_kubernetes_pod_name]
          targetLabel: pod
        - targetLabel: env
          replacement: prod          # stamp env; do NOT add vcluster
      metricRelabelings:
        - sourceLabels: [metric_type, __name__]
          separator: ";"
          regex: "(server|cluster|);.*|tablet;(rocksdb_current_version_sst_files_size|rocksdb_block_cache_hit|rocksdb_block_cache_miss|log_bytes_logged|majority_sst_files_rejections)"
          action: keep
```

Apply it:

```bash
kubectl apply -f monitoring/yb-servicemonitors.yaml -n monitoring
```

### Raw scrape_config (higher env)

See **Section C** in `monitoring/prometheus-scrape-config.yaml`. Uncomment and replace the namespace list and `env` constant with your values.

### Why `vcluster` is omitted — and why that's correct

The fleet dashboards (`yb-fleet-overview`, `yb-env-detail`) have a `vcluster` template variable with `allValue=.*`. The regex `.*` matches the empty string, so series that carry no `vcluster` label also appear when "All" is selected. Higher-env namespaces show up in the fleet table alongside vCluster-based ones.

However, using the fleet dashboards for production is awkward: the `vcluster` dropdown is irrelevant, and panel titles mention "vCluster / Environment" rather than just namespaces. The dedicated namespace dashboards below give a cleaner view.

### Dedicated dashboards for direct-namespace environments

Two dashboards are provided under **YugabyteDB/** that have no `vcluster` variable at all:

#### `yb-ns-overview` — Namespace Overview (Direct / Higher Env)

Variables: `Environment` (multi-select) → `Namespace` (multi-select, cascades from env).

Panels:
- Stats row: Environments, Namespaces, Masters Up, Masters Down, TServers Up, TServers Down
- YSQL Ops/sec grouped by `env/namespace`
- TServer Read+Write Ops/sec grouped by `env/namespace`
- Avg YSQL Select latency by `env/namespace`
- Log error rate by `env/namespace`
- Table: one row per `env+namespace` with Masters Up, TServers Up, Live TServers, Dead TServers (dead column turns red)

#### `yb-ns-detail` — Namespace Detail (Direct / Higher Env)

Variables: `Environment` (single-select) → `Namespace` (multi) → `Pod` (multi).

Panels: identical to `yb-env-detail` (Masters section, TServers resources, TServers read/write, YSQL, Storage/RocksDB) but all PromQL uses `env="$env",namespace=~"$namespace"` with no `vcluster` selector.

#### Deploy / reload

The dashboards are shipped as ConfigMaps via the `grafana-dashboards` Helm chart. After any JSON change:

```bash
helm upgrade --install grafana-dashboards \
  monitoring/charts/grafana-dashboards \
  -n monitoring \
  -f monitoring/charts/grafana-dashboards/values.yaml
```

Grafana's sidecar detects the ConfigMap labels and loads the dashboards automatically — no restart needed.

#### Verify in Grafana

```bash
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
```

Open `http://localhost:3000`, navigate to **YugabyteDB/** folder. You should see four dashboards:

| Dashboard | UID | Use for |
|-----------|-----|---------|
| Fleet Overview (All vClusters / Environments) | `yb-fleet-overview` | Cross-vCluster fleet view |
| vCluster / Environment Detail | `yb-env-detail` | Drill-down into one vCluster+env |
| Namespace Overview (Direct / Higher Env) | `yb-ns-overview` | Multi-env direct-namespace overview |
| Namespace Detail (Direct / Higher Env) | `yb-ns-detail` | Pod-level detail for direct namespaces |

### Topology comparison

| Characteristic | Lower env (vCluster) | Higher env (direct namespace) |
|----------------|----------------------|-------------------------------|
| YB service names | `yb-masters-x-cbg-in-x-cbg-demo` | `yb-masters` |
| `vcluster` label | `cbg-demo` / `cbg-test` / `cbg-dev` | *(absent)* |
| ServiceMonitor type | Host-level (Approach A) | Standard Kubernetes |
| `-x-` relabeling needed | Yes — to extract original namespace/pod | No |
| Dashboard to use | `yb-fleet-overview` + `yb-env-detail` | `yb-ns-overview` + `yb-ns-detail` |

---

## File Reference

```
monitoring/
├── README.md                            this file
├── generate_dashboards.py               builds fleet + detail + platform JSON
├── prometheus-rules.yaml                PrometheusRule CRD (10 alerts, 2 groups)
├── prometheus-scrape-config.yaml        raw scrape_configs (4 sections A-D)
├── yb-servicemonitors-host.yaml         host-level ServiceMonitors (Approach A, all vClusters)
├── yb-servicemonitors-vcluster.yaml     inside-vCluster ServiceMonitors (Approach B)
├── yb-servicemonitors.yaml              standard ServiceMonitors for direct-namespace envs
├── values-monitoring.yaml               kube-prometheus-stack Helm values
└── charts/
    └── grafana-dashboards/
        ├── Chart.yaml
        ├── values.yaml                  PrometheusRules stanza
        ├── templates/
        │   ├── configmap.yaml
        │   └── prometheusrules.yaml
        └── files/
            └── dashboards/
                ├── YugabyteDB/
                │   ├── yb-fleet-overview.json   vCluster fleet view (all envs)
                │   ├── yb-env-detail.json        vCluster per-env drill-down
                │   ├── yb-ns-overview.json       direct namespace overview (higher env)
                │   └── yb-ns-detail.json         direct namespace drill-down (higher env)
                └── Platform/
                    └── prometheus-targets.json
```
