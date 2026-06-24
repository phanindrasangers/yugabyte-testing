# YugabyteDB 2024.1.2 — master scale-up failure (UUID mismatch)

## Symptom
Deployed the helm chart with `replicas.master: 1`, ran fine for weeks, later set
`replicas.master: 3` + `replicas.tserver: 3` and `helm upgrade`. The new master pods
fail; logs show universe UUID mismatch and the cluster loses its leader:

```
Failed CheckUniverseUuidMatchFromTserver check: Invalid argument:
Received wrong universe_uuid <new>, expected <original>
...
Could not locate the leader master: no leader found
```

## Root cause
The chart derives `--master_addresses` and `--replication_factor` directly from
`replicas.master` (`templates/_helpers.tpl`). The master Raft quorum is fixed at the
first bootstrap. A `helm upgrade` that bumps the replica count only:

1. restarts `yb-master-0` with a 3-address list and `--replication_factor=3`, and
2. starts `yb-master-1` / `yb-master-2` on **fresh empty volumes** with new UUIDs.

It never runs the Raft membership change (`change_master_config ADD_SERVER`). The two
empty masters race, win a leader election, and overwrite the original master's log,
which is why the universe UUID no longer matches and data becomes unreachable.

Note: `kubectl get pods` shows the masters as `Running 3/3` the whole time — k8s only
checks the process is alive, not that the master is a healthy quorum member. Look at
the master logs and `yb-admin list_all_masters`, not pod status.

## Will scaling back to 1 fix it?
No, not reliably. Once the empty masters win an election they truncate
`yb-master-0`'s Raft log. Scaling `replicas.master` back to 1 deletes the extra pods
and rewrites the gflag, but does not rewind `yb-master-0`'s consensus metadata to a
clean single-member config. If the original master was already demoted/overwritten,
the only safe recovery is **restore from backup** into a fresh universe.

## The fix

### Prevention (do this)
Deploy at the **final master count from day one**. The master count must equal your
target replication factor (RF=3 → 3 masters). Do not grow masters later by bumping
the replica count.

```bash
helm install yb-demo ./yugabyte -n yb-demo --create-namespace \
  --set replicas.master=3 --set replicas.tserver=3 -f values-repro.yaml
```

### Adding masters later, in place (1 -> 3) — verified runbook

The key rule: **add ONE master at a time**, and run `change_master_config ADD_SERVER`
after each before starting the next. A single empty master can never win an election
against the real quorum, so the original universe is preserved. The danger is only
when two empty masters start together (a plain `helm upgrade 1->3`) — they vote for
each other, form a competing empty quorum, and corrupt the original.

So grow the StatefulSet one replica per step:

```bash
DOM=<namespace>.svc.cluster.local
M0=yb-master-0.yb-masters.$DOM:7100
M1=yb-master-1.yb-masters.$DOM:7100

# --- 1 -> 2 ---
helm upgrade yb-demo ./yugabyte -n <ns> -f values-repro.yaml \
  --set replicas.master=2 --set replicas.totalMasters=2
# wait until yb-master-1 is Running and yb-master-0 is LEADER again, then:
kubectl exec -n <ns> yb-master-0 -c yb-master -- \
  yb-admin -master_addresses $M0 change_master_config ADD_SERVER yb-master-1.yb-masters.$DOM 7100
kubectl exec -n <ns> yb-master-0 -c yb-master -- \
  yb-admin -master_addresses $M0,$M1 list_all_masters     # expect 2 ALIVE

# --- 2 -> 3 ---
helm upgrade yb-demo ./yugabyte -n <ns> -f values-repro.yaml \
  --set replicas.master=3 --set replicas.totalMasters=3
# wait until yb-master-2 is Running and a LEADER exists, then:
kubectl exec -n <ns> yb-master-0 -c yb-master -- \
  yb-admin -master_addresses $M0,$M1 change_master_config ADD_SERVER yb-master-2.yb-masters.$DOM 7100
kubectl exec -n <ns> yb-master-0 -c yb-master -- \
  yb-admin -master_addresses $M0,$M1,$M2 list_all_masters  # expect 3 ALIVE, 1 LEADER
```

Each `ADD_SERVER` makes the leader remote-bootstrap the sys-catalog onto the new
master, which joins as FOLLOWER in the **same** universe — no UUID mismatch. After the
last add, the rendered `--master_addresses` / `--replication_factor=3` gflag matches
the real quorum, so the chart and the cluster are back in sync. Verified end to end:
data (5000 rows) intact, zero mismatch errors.

If the universe is ALREADY broken (empty masters won an election), this does not apply
— restore from backup into a fresh universe instead.

### Scaling masters back down (3 -> 1) safely

First, the caveat: **the master count should equal your replication factor.** A 3-master
universe stores its sys-catalog at RF=3; dropping to 1 master means dropping the
sys-catalog to RF=1, so you lose master fault tolerance (a single master pod failure
then takes the control plane down). Only do this on a test cluster, or when you are
deliberately lowering RF. It does not affect tablet (data) RF, which is separate.

The procedure is the exact mirror of adding: **remove ONE master at a time from the
Raft quorum with `change_master_config REMOVE_SERVER`, and do it BEFORE scaling the
StatefulSet down.** Two rules:

1. Never remove the **leader** — only remove followers. If the master you want to keep
   (`yb-master-0`) is not the leader, step the current leader down first, or just
   remove the other two and let the survivor be elected.
2. Remove the **highest ordinal first** (`yb-master-2`, then `yb-master-1`), because
   that is the order Kubernetes deletes pods when you shrink the StatefulSet — so the
   Raft config and the surviving pod stay in agreement.

If you scale the StatefulSet down first, you delete pods that are still voting members
of the quorum, which can lose quorum and strand the universe. Remove from Raft first,
scale the pods second.

```bash
DOM=<namespace>.svc.cluster.local
M0=yb-master-0.yb-masters.$DOM:7100
M1=yb-master-1.yb-masters.$DOM:7100
M2=yb-master-2.yb-masters.$DOM:7100

# confirm yb-master-0 is LEADER; if not, pick the leader as the one to keep
kubectl exec -n <ns> yb-master-0 -c yb-master -- \
  yb-admin -master_addresses $M0,$M1,$M2 list_all_masters

# --- 3 -> 2 ---  (remove the follower yb-master-2 from the quorum first)
kubectl exec -n <ns> yb-master-0 -c yb-master -- \
  yb-admin -master_addresses $M0,$M1,$M2 change_master_config REMOVE_SERVER yb-master-2.yb-masters.$DOM 7100
kubectl exec -n <ns> yb-master-0 -c yb-master -- \
  yb-admin -master_addresses $M0,$M1 list_all_masters     # expect 2 ALIVE
# now shrink the StatefulSet so the gflag list matches the quorum
helm upgrade yb-demo ./yugabyte -n <ns> -f values-repro.yaml \
  --set replicas.master=2 --set replicas.totalMasters=2

# --- 2 -> 1 ---
kubectl exec -n <ns> yb-master-0 -c yb-master -- \
  yb-admin -master_addresses $M0,$M1 change_master_config REMOVE_SERVER yb-master-1.yb-masters.$DOM 7100
kubectl exec -n <ns> yb-master-0 -c yb-master -- \
  yb-admin -master_addresses $M0 list_all_masters         # expect 1 ALIVE, LEADER
helm upgrade yb-demo ./yugabyte -n <ns> -f values-repro.yaml \
  --set replicas.master=1 --set replicas.totalMasters=1

# reclaim the orphaned master volumes (StatefulSet leaves PVCs behind on scale-down)
kubectl delete pvc -n <ns> datadir0-yb-master-2 datadir0-yb-master-1
```

After each `REMOVE_SERVER` the leader commits a Raft config change that drops the
member cleanly, so the universe UUID and sys-catalog are preserved — no mismatch. This
is a safe, supported shrink because you are reducing membership through consensus, not
by deleting live voters. Adjust the PVC names to match your `storage.master.count` and
storageClass; check them with `kubectl get pvc -n <ns> -l app=yb-master`.

This is different from "scaling back to 1 to recover a broken cluster" (see above) —
that does not work, because once empty masters have corrupted the quorum there is no
healthy consensus left to do an orderly `REMOVE_SERVER` against.

### Alternative: backup + restore
Take a `ysql_dump` / distributed snapshot, deploy a new universe at the desired master
count, restore into it. Use this when you can take a maintenance window or the cluster
is already corrupted.

Scale tservers freely — that is safe and supported. The constraint is masters only.

## Reproducing locally (kind)
```bash
helm repo add yugabytedb https://charts.yugabyte.com && helm repo update
helm pull yugabytedb/yugabyte --version 2024.1.2 --untar
kubectl create ns yb-demo

# 1) install RF=1 and load data
helm install yb-demo ./yugabyte -n yb-demo -f values-repro.yaml \
  --set replicas.master=1 --set replicas.tserver=1 --set replicas.totalMasters=1 --wait
kubectl exec -n yb-demo yb-tserver-0 -c yb-tserver -- \
  /home/yugabyte/bin/ysqlsh -h yb-tserver-0.yb-tservers.yb-demo -d yugabyte \
  -c "CREATE TABLE accounts(id int primary key, name text);
      INSERT INTO accounts SELECT g,'u'||g FROM generate_series(1,5000) g;"

# 2) break it: scale masters 1 -> 3
helm upgrade yb-demo ./yugabyte -n yb-demo -f values-repro.yaml   # values has master:3

# 3) observe the failure
kubectl logs yb-master-2 -n yb-demo -c yb-master | grep -i universe_uuid
kubectl exec -n yb-demo yb-master-0 -c yb-master -- \
  yb-admin -master_addresses yb-master-0.yb-masters.yb-demo.svc.cluster.local:7100 list_all_masters
```

`values-repro.yaml` shrinks CPU/memory/storage so 3+3 pods fit on a single kind node.
