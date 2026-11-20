# Monitoring System

## Table of Contents

- [Monitoring System](#monitoring-system)
  - [Table of Contents](#table-of-contents)
  - [Overview](#overview)
  - [Key Components of the Monitoring System](#key-components-of-the-monitoring-system)
    - [Metrics Server](#metrics-server)
    - [Headlamp web UI](#headlamp-web-ui)
      - [Install Headlamp](#install-headlamp)
      - [Open Headlamp with a port-forward](#open-headlamp-with-a-port-forward)
      - [Log in with a short-lived read-only token](#log-in-with-a-short-lived-read-only-token)
      - [Revoke the old cluster-admin token](#revoke-the-old-cluster-admin-token)
      - [Smoke check after deployment](#smoke-check-after-deployment)
    - [Kubernetes Dashboard (legacy, deprecated)](#kubernetes-dashboard-legacy-deprecated)
      - [Dashboard Access and Security](#dashboard-access-and-security)
    - [Prometheus and Grafana](#prometheus-and-grafana)
      - [Helm Installation](#helm-installation)
      - [Prometheus and Grafana Setup](#prometheus-and-grafana-setup)
        - [Add the Prometheus Helm Chart Repository](#add-the-prometheus-helm-chart-repository)
        - [Install Prometheus and Grafana Helm Chart](#install-prometheus-and-grafana-helm-chart)
        - [Persistent Volume Configuration](#persistent-volume-configuration)
        - [Verifying the Installation](#verifying-the-installation)
        - [Access Prometheus](#access-prometheus)
      - [Grafana](#grafana)
  - [Verification status](#verification-status)
  - [Access URLs and Credentials](#access-urls-and-credentials)


## Overview

This project uses Vagrant alongside bash scripts to automate the creation and configuration of virtual machines (VMs) in VirtualBox. This setup includes a Kubernetes cluster with a comprehensive monitoring system. The included `Vagrantfile` and various scripts handle the entire setup process, negating the need for manual configuration commands.

## Key Components of the Monitoring System

### Metrics Server

The Metrics Server is a scalable, efficient source of container resource metrics for Kubernetes built-in autoscaling pipelines. The Metrics Server plays a crucial role in Kubernetes monitoring and management systems due to its ability to provide core metrics about clusters, which include metrics on CPU and memory usage for nodes and pods. The Metrics Server is installed using a direct `kubectl apply -f` command, fetching the latest components from the official GitHub repository.

```shell
vagrant ssh controlplane
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

or

```shell
vagrant ssh controlplane
kubectl apply -f https://raw.githubusercontent.com/techiescamp/kubeadm-scripts/main/manifests/metrics-server.yaml
```

### Headlamp web UI

[Headlamp](https://github.com/kubernetes-sigs/headlamp) (a Kubernetes SIG UI project) replaces the archived Kubernetes Dashboard as the lab's web UI ([issue #4](https://github.com/MSKazemi/robo-resilience-lab/issues/4)).

> **Not deployed yet.** The Headlamp manifests have only been checked statically (`kubeconform` strict against the Kubernetes 1.29 schemas, see [Verification status](#verification-status)). Nobody has started them on the Vagrant cluster or on kind, opened the UI, or logged in. Treat the steps below as the intended procedure until a run is recorded.

What is installed, from [`headlamp/headlamp.yaml`](./headlamp/headlamp.yaml) and [`headlamp/viewer-rbac.yaml`](./headlamp/viewer-rbac.yaml):

| Item | Setting | Why |
|---|---|---|
| Version | Headlamp **v0.46.0** (latest stable release on 2026-11-20; matches Helm chart `headlamp` 0.46.0) | Reproducible |
| Image | `ghcr.io/headlamp-k8s/headlamp:v0.46.0@sha256:8a0ef975…8b75` (digest of the multi-arch index) | A moved tag cannot change what runs |
| Exposure | `Service` type **ClusterIP**, port 80 → container port 4466 | Not reachable from the lab network; use `kubectl port-forward` |
| Pod identity | ServiceAccount `headlamp` with **no** RoleBinding or ClusterRoleBinding | The upstream chart binds `cluster-admin` by default; this lab does not |
| Login identity | ServiceAccount `headlamp-viewer`, bound to the built-in `view` ClusterRole plus a small read-only ClusterRole for nodes, PersistentVolumes and StorageClasses | Read-only. `view` cannot read Secrets, Roles or RoleBindings, and cannot change anything |
| Tokens | **No** token Secret. Tokens come from `kubectl create token --duration` | Short-lived, never written to disk by the lab |
| Pod hardening | Namespace enforces the `restricted` Pod Security Standard; non-root user 100:101, read-only root filesystem, all capabilities dropped, `RuntimeDefault` seccomp, CPU/memory limits | Least privilege for the pod itself |
| Session | `-session-ttl=3600` (1 hour; the chart default is 24 hours) | Matches the short token lifetime |

Headlamp runs in in-cluster mode: the UI asks each user for a token and sends the API requests with it, so the token decides what a user can see. The pod still mounts its own (unprivileged) service-account token, because in-cluster mode reads the API server address and CA bundle from that mount.

The manifests follow the upstream Helm chart template (`charts/headlamp/templates/deployment.yaml` at tag `headlamp-helm-0.46.0`) for arguments, port, probes and user ID. They are plain YAML instead of a Helm release so that the repository's static validators can check them and so that no chart default (such as the `cluster-admin` binding) is pulled in unseen.

#### Install Headlamp

`monitoring.sh` applies both files during Vagrant provisioning. To apply them by hand on the control plane:

```shell
vagrant ssh controlplane
kubectl apply -f /home/vagrant/codes/MonitoringSystem/headlamp/headlamp.yaml
kubectl apply -f /home/vagrant/codes/MonitoringSystem/headlamp/viewer-rbac.yaml
kubectl -n headlamp rollout status deployment/headlamp --timeout=5m
```

To remove it, run `kubectl delete -f` on the same two files.

#### Open Headlamp with a port-forward

`kubectl port-forward` listens on `127.0.0.1` by default. Keep it that way: do **not** add `--address 0.0.0.0`, and do not change the Service to `NodePort` or `LoadBalancer`.

On the Vagrant lab, `kubectl` runs inside the `controlplane` VM, so carry the port to the host with an SSH tunnel:

```shell
# On the host: open a shell on the control plane and forward host port 8080 to it
vagrant ssh controlplane -- -L 8080:127.0.0.1:8080

# Inside that VM shell
kubectl -n headlamp port-forward svc/headlamp 8080:80
```

Then browse to <http://127.0.0.1:8080> on the host. If `kubectl` runs on your own machine (for example against a kind cluster), only the `port-forward` line is needed.

#### Log in with a short-lived read-only token

Create a token when you need it and paste it into the Headlamp login prompt. Run this in a second shell on the control plane:

```shell
kubectl -n headlamp create token headlamp-viewer --duration=30m
```

- The token is printed to your terminal only. **Do not** redirect it to a file, commit it, or paste it into an issue or chat.
- It is a bound token (TokenRequest API) and expires after the requested duration. The API server may cap the duration (`--service-account-max-token-expiration`). Create a new one when it expires.
- Deleting the `headlamp-viewer` ServiceAccount (`kubectl delete -f .../viewer-rbac.yaml`) invalidates every token issued for it.
- Pages for objects the `view` role does not cover (for example Secrets) show a "forbidden" message. That is expected.

There is no admin login for the UI. Do administrative work with `kubectl` on the control plane. If you really need a write-capable UI identity for a short experiment, create a separate ServiceAccount bound to the built-in `edit` ClusterRole through a `RoleBinding` in one namespace, use a short `--duration`, and delete it afterwards. Do not bind `cluster-admin`.

#### Revoke the old cluster-admin token

Clusters provisioned by earlier versions of `monitoring.sh` have an `admin-user` ServiceAccount bound to `cluster-admin`, a long-lived token Secret, and a copy of that token in `/home/vagrant/dashboard-token`. `monitoring.sh` now prints a notice if that file still exists. To revoke it:

```shell
vagrant ssh controlplane
kubectl delete -f /home/vagrant/codes/MonitoringSystem/admin-user.yaml
rm /home/vagrant/dashboard-token
```

#### Smoke check after deployment

These are the checks to run and record once the manifests are deployed. **None of them has been run yet.**

```shell
kubectl -n headlamp get deployment,pods,service
kubectl -n headlamp get service headlamp -o jsonpath='{.spec.type}{"\n"}'   # expect ClusterIP
# With the port-forward from above running:
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/        # expect 200
# Least privilege of the login identity (expect yes, then no, no, no):
kubectl auth can-i list pods -A --as=system:serviceaccount:headlamp:headlamp-viewer
kubectl auth can-i get secrets -A --as=system:serviceaccount:headlamp:headlamp-viewer
kubectl auth can-i delete pods -A --as=system:serviceaccount:headlamp:headlamp-viewer
kubectl auth can-i '*' '*' --as=system:serviceaccount:headlamp:headlamp-viewer
# The pod's own identity should have no rights (expect no):
kubectl auth can-i list pods -A --as=system:serviceaccount:headlamp:headlamp
```

Then log in with a token from the previous step and confirm that the workloads page lists the lab pods.

### Kubernetes Dashboard (legacy, deprecated)

> **Deprecated.** The upstream [Kubernetes Dashboard](https://github.com/kubernetes/dashboard) repository is archived, and the `v2.7.0` manifest used here is old. Use [Headlamp](#headlamp-web-ui) instead. This section is kept as a record of the original lab setup.

`monitoring.sh` no longer installs the Dashboard by default, no longer applies `admin-user.yaml` and no longer writes a token file. It installs the `v2.7.0` manifest only when you opt in:

```shell
vagrant ssh controlplane
sudo INSTALL_LEGACY_DASHBOARD=1 bash /home/vagrant/codes/MonitoringSystem/monitoring.sh
# or apply only the Dashboard manifest:
kubectl apply -f https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml
```

Upstream documentation: https://kubernetes.io/docs/tasks/access-application-cluster/web-ui-dashboard/

#### Dashboard Access and Security

Originally an `admin-user` ServiceAccount bound to `cluster-admin` (`admin-user.yaml`) was created and its long-lived token was saved to `/home/vagrant/dashboard-token`. Both are now legacy: the files are kept for reference and so that old installs can be [revoked](#revoke-the-old-cluster-admin-token). If you use the legacy Dashboard, log in with the same short-lived read-only token as Headlamp and reach it through a port-forward (the v2.7.0 manifest creates a `ClusterIP` Service; the old NodePort `30664` URL was never set by the repository's scripts):

```shell
kubectl -n kubernetes-dashboard port-forward svc/kubernetes-dashboard 8443:443
kubectl -n headlamp create token headlamp-viewer --duration=30m
```

> **Security warning:** do not apply `admin-user.yaml`; its token gives full control of the cluster. `admin-user-readonly.yaml` (built-in `view` role) is superseded by `headlamp/viewer-rbac.yaml`, which grants the same `view` role (plus read access to nodes, PersistentVolumes and StorageClasses) without a long-lived token Secret.

### Prometheus and Grafana

#### Helm Installation

Helm, the package manager for Kubernetes, is installed using a script that downloads and configures the necessary repositories and packages. This tool is crucial for managing Kubernetes applications, including Prometheus and Grafana.

```shell
vagrant ssh controlplane
curl https://baltocdn.com/helm/signing.asc | gpg --dearmor | sudo tee /usr/share/keyrings/helm.gpg > /dev/null
sudo apt-get install apt-transport-https --yes
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/helm.gpg] https://baltocdn.com/helm/stable/debian/ all main" | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list
sudo apt-get update
sudo apt-get install helm
```

#### Prometheus and Grafana Setup

Prometheus, a powerful monitoring and alerting toolkit, is integrated into the system using Helm. The script includes commands to add the Prometheus Helm chart repository, update it, and install Prometheus with custom configurations (`--values /home/vagrant/codes/MonitoringSystem/kube-prometheus-stack-values.yaml`) to adjust its default settings, like changing the service type to `NodePort` to allow external access.

Configuration changes:

- Grafana:
  - service type: from `ClusterIP` to `NodePort`
  - `PersistentVolumeClaim`

- Prometheus:
  - service type: from `ClusterIP` to `NodePort` (`nodePort: 30090`)
  - `PersistentVolumeClaim`: `storageSpec.volumeClaimTemplate` (10Gi, `local-storage`). The chart silently ignores a `volumeClaimTemplate` placed as a sibling of `storageSpec`; this was broken before and is fixed in the values file (checked with `helm template`, see Verification status below).
  - Retention: how long to retain metrics, `retention: 1y`, capped by `retentionSize: "8GB"` so the 10Gi volume cannot fill up.

- Alertmanager: the same `storage.volumeClaimTemplate` nesting is fixed (`retention: 1200h`).


##### Add the Prometheus Helm Chart Repository

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
```

##### Install Prometheus and Grafana Helm Chart


```bash
# Creating Persistent Volume for Prometheus and Grafana
kubectl apply -f /home/vagrant/codes/MonitoringSystem/pv.yaml

# Creating namespace for Prometheus
kubectl create namespace prometheus

# Installing Prometheus and Grafana using Helm
helm install prometheus prometheus-community/kube-prometheus-stack -n prometheus --values /home/vagrant/codes/MonitoringSystem/kube-prometheus-stack-values.yaml
``` 
##### Persistent Volume Configuration

The script also sets up necessary persistent volumes for Prometheus and Grafana, ensuring data persistence across pod restarts and deletions. This is crucial for maintaining historical data for analysis.

##### Verifying the Installation
```bash
kubectl get all -n prometheus
```


##### Access Prometheus

Prometheus is exposed as a NodePort on port `30090` (from the values file: `prometheus.service.nodePort`), i.e. `http://<node-ip>:30090`. **Prometheus has no login.** Anyone who can reach that port can query metrics and the API. Prefer a port-forward on untrusted networks:

```bash
kubectl -n prometheus port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090
curl -s http://127.0.0.1:9090/-/ready
```

Below are screenshots of the Prometheus graph and rules pages from the original lab run.

![Prometheus graph of the node:node_cpu_utilization:ratio_rate5m query over 15 minutes, with node CPU utilization rising to about 0.02](../images/p1.png)
![Prometheus Rules page listing the Alertmanager alerting rules, each in the OK state](../images/p2.png)

#### Grafana

Grafana is used for visualization and analytics of metrics gathered from Prometheus. It is installed and configured similarly to Prometheus, using Helm. The script sets up persistent volumes required for storing data and ensures that Grafana is accessible via a NodePort.

Grafana is exposed as a NodePort on port `30000` (from the values file: `grafana.service.nodePort`), i.e. `http://<node-ip>:30000`. Login is `admin` / `prom-operator` **unless you override it**. That password is a well-known default published in this README and in the values file, and the NodePort reaches every node IP. Before exposing the lab, set your own password at install time (`--set grafana.adminPassword='<strong-password>'`) or use a port-forward:

```bash
kubectl -n prometheus port-forward svc/prometheus-grafana 3000:80
curl -s http://127.0.0.1:3000/api/health
```

The Grafana screenshots below show node metrics, per-pod network traffic, and Kubernetes API server health from the original lab run.

![Grafana Node Exporter / Nodes dashboard for instance 192.168.56.10:9100 with CPU, load average, memory, disk and network panels](../images/g1.png)
![Grafana Kubernetes / Networking / Pod dashboard for the ros pod ending 9wdkl, showing receive and transmit bandwidth](../images/g2.png)
![Grafana Kubernetes / Networking / Pod dashboard for the ros pod ending vd8rs, showing receive and transmit bandwidth](../images/g3.png)
![Grafana Kubernetes / Networking / Pod dashboard for the ros pod ending x4hcb, showing receive and transmit bandwidth](../images/g4.png)
![Grafana Kubernetes / Networking / Pod dashboard for the weave-net pod, showing receive and transmit bandwidth](../images/g5.png)
![Grafana Kubernetes / Networking / Pod dashboard for the etcd-controlplane pod, showing receive and transmit bandwidth](../images/g6.png)
![Grafana Kubernetes / Networking / Pod dashboard for the ros pod ending vd8rs with the Bandwidth, Packets and Errors sections expanded](../images/g7.png)
![Grafana Kubernetes / API server dashboard with availability, request, work queue, memory, CPU and goroutine panels](../images/g8.png)

## Verification status

**Headlamp manifests (this change, issue #4):** static checks only. `headlamp/headlamp.yaml` and `headlamp/viewer-rbac.yaml` pass `python3 scripts/validate-k8s-manifests.py` (kubeconform v0.8.0, strict, Kubernetes 1.29 schemas) and `tests/test_manifests.py` (pinned image, resource limits, no privileged or host-namespace settings). `monitoring.sh` passes `bash -n`. They have **not** been deployed to any cluster, the UI has not been opened, no login was tried and none of the [smoke checks](#smoke-check-after-deployment) has been run. The Headlamp version and image digest were read from the GitHub releases of `kubernetes-sigs/headlamp` and the `ghcr.io` registry on 2026-11-20.

**kube-prometheus-stack chart (earlier run).** What was actually run (Helm v4.3.1, kubeconform v0.8.0, kind v0.33.0 with Kubernetes 1.37 node image, chart `prometheus-community/kube-prometheus-stack` **92.16.1**, app v0.94.2):

- `helm template` of the chart with `kube-prometheus-stack-values.yaml` succeeds. The rendered Prometheus and Alertmanager objects now carry the 10Gi `local-storage` claim template (before the fix they had no storage at all).
- `kubeconform -strict` on the rendered output: 78 valid, 0 invalid; 50 CRD-kinds skipped (no schema) with `-ignore-missing-schemas`.
- `pv.yaml`, `pvc.yaml`, `admin-user.yaml` and `admin-user-readonly.yaml` pass `kubeconform -strict` and a server-side `kubectl apply --dry-run=server` on kind. `pv.yaml`, `admin-user.yaml` and `admin-user-readonly.yaml` were also applied to a throwaway kind cluster. RBAC was checked with `kubectl auth can-i`: `admin-user` can read Secrets and delete pods in `kube-system`; `dashboard-viewer` can list pods but cannot read Secrets or delete pods.
- The chart install into kind was **not** completed. The Prometheus/Grafana pods never started, because `kube-proxy` on the test node could not start (`fsnotify watcher init: too many open files`: the host's per-user inotify instance limit was exhausted by other clusters). Live Prometheus/Grafana health checks (`/-/ready`, `/api/health`) therefore have **not** been run.
- Fixed during testing: the pinned admission certgen image (an ingress-nginx image from 2022) rejects the `--secret-type` flag that chart 92.x passes, so the install hung at the admission hook. The chart's own image (`jkroepke/kube-webhook-certgen:1.11.0`) is now pinned in the values file.
- Not verified on the lab's Kubernetes 1.29 cluster. The chart declares `kubeVersion >=1.25.0-0`.

`monitoring.sh` now pins the chart to `--version 92.16.1`, the version checked above (override with `KPS_CHART_VERSION=<version>`). The newest release on 2026-11-20 is 93.4.1, a new major version; it is not pinned because the values file has not been re-checked against it. The pinned `monitoring.sh` itself has not been re-run.

## Access URLs and Credentials

- **Headlamp**: <http://127.0.0.1:8080> on the host, through `kubectl port-forward` and an SSH tunnel (ClusterIP only, see [Open Headlamp](#open-headlamp-with-a-port-forward)). Login: a short-lived token from `kubectl -n headlamp create token headlamp-viewer --duration=30m`; no token is stored.
- **Kubernetes Dashboard (legacy, deprecated)**: not installed by default; see the [legacy section](#kubernetes-dashboard-legacy-deprecated).
- **Prometheus**: `http://<node-ip>:30090` (NodePort from the values file; no login, use port-forward on untrusted networks)
- **Grafana**: `http://<node-ip>:30000` (default login `admin` / `prom-operator` unless overridden; change it)

Using the values in `kube-prometheus-stack-values.yaml` the lab's node IP is `192.168.56.10` (the controlplane VM), so the lab URLs are `http://192.168.56.10:30090` and `http://192.168.56.10:30000`.

For more detailed information on the monitoring setup and modifications, refer to the `./MonitoringSystem/monitoring.sh` in the repository.