#!/bin/bash
# Install metrics-server, the Headlamp web UI (read-only login identity),
# Helm and a pinned kube-prometheus-stack chart on the Vagrant control plane.
# Safe to re-run (apply / upgrade --install).
#
# Security defaults (issue #4):
#   * Headlamp replaces the archived Kubernetes Dashboard. Its Service is
#     ClusterIP only; reach it with `kubectl port-forward`.
#   * No cluster-admin account is created and NO token is written to a file or
#     printed. Log in with a short-lived token created on demand, see
#     MonitoringSystem/README.md ("Headlamp web UI").
#   * The legacy Dashboard v2.7.0 manifest is installed only when
#     INSTALL_LEGACY_DASHBOARD=1 is set; it no longer creates admin-user.
#
# Static checks only: this script has not been run end to end since the
# Headlamp change. See "Verification status" in MonitoringSystem/README.md.
set -euo pipefail

MONITORING_DIR=/home/vagrant/codes/MonitoringSystem
# kube-prometheus-stack chart version. 92.16.1 is the version the values file was
# checked against with `helm template` (see the README). The newest release on
# 2026-11-20 is 93.4.1, a new major version; it is NOT pinned here because the
# values file has not been re-checked against it.
KPS_CHART_VERSION="${KPS_CHART_VERSION:-92.16.1}"

echo "Installing monitoring tools"

# Install Metrics Server
# NOTE: `latest` is unpinned; pin a version compatible with the cluster minor when known.
echo "Installing Metrics Server"
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

# Install Headlamp (pinned image digest, ClusterIP Service, no cluster-admin)
echo "Installing Headlamp web UI"
kubectl apply -f "$MONITORING_DIR/headlamp/headlamp.yaml"
kubectl apply -f "$MONITORING_DIR/headlamp/viewer-rbac.yaml"
if ! kubectl -n headlamp rollout status deployment/headlamp --timeout=5m; then
  echo "WARNING: the Headlamp deployment did not become ready; check 'kubectl -n headlamp describe pod'" >&2
fi

# Legacy, opt-in only: the upstream Kubernetes Dashboard project is archived.
if [ "${INSTALL_LEGACY_DASHBOARD:-0}" = "1" ]; then
  echo "INSTALL_LEGACY_DASHBOARD=1: installing the LEGACY Kubernetes Dashboard v2.7.0 (archived upstream)"
  kubectl apply -f https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml
fi

# Earlier versions of this script stored a cluster-admin token here. Do not
# delete it silently; tell the operator how to revoke it.
if [ -e /home/vagrant/dashboard-token ]; then
  echo "NOTICE: /home/vagrant/dashboard-token from an earlier run still exists and holds a cluster-admin token." >&2
  echo "        Revoke and remove it: kubectl delete -f $MONITORING_DIR/admin-user.yaml && rm /home/vagrant/dashboard-token" >&2
fi

# Install Helm (signing key and apt source as documented by Helm)
echo "Installing Helm"
curl -fsSL https://baltocdn.com/helm/signing.asc | gpg --batch --yes --dearmor | sudo tee /usr/share/keyrings/helm.gpg > /dev/null
sudo apt-get install -y apt-transport-https
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/helm.gpg] https://baltocdn.com/helm/stable/debian/ all main" | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list > /dev/null
sudo apt-get update
sudo apt-get install -y helm

# Persistent Volumes for Prometheus and Grafana
echo "Creating Persistent Volume for Prometheus and Grafana"
kubectl apply -f "$MONITORING_DIR/pv.yaml"

# Install Prometheus
echo "Prometheus"
echo "Adding Prometheus Helm repository"
if ! helm repo list 2>/dev/null | grep -q '^prometheus-community[[:space:]]'; then
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
fi
helm repo update

# Host directories backing the local PVs. Group 2000 matches the Prometheus pod's
# fsGroup in kube-prometheus-stack-values.yaml; mode 0775 instead of the earlier
# world-writable 777. Grafana's fsGroup is not covered here; verify on a real cluster.
for i in {1..5}; do
  sudo mkdir -p "/mnt/data$i"
  sudo chgrp 2000 "/mnt/data$i"
  sudo chmod 0775 "/mnt/data$i"
done

echo "Creating namespace for Prometheus"
kubectl create namespace prometheus --dry-run=client -o yaml | kubectl apply -f -

echo "Installing Prometheus (kube-prometheus-stack chart $KPS_CHART_VERSION)"
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --version "$KPS_CHART_VERSION" \
  -n prometheus \
  --values "$MONITORING_DIR/kube-prometheus-stack-values.yaml" \
  --wait --timeout 15m

cat <<'EOF'
Monitoring tools installed. Open Headlamp (no token is stored anywhere):
  1. On the host:  vagrant ssh controlplane -- -L 8080:127.0.0.1:8080
  2. In that VM shell:  kubectl -n headlamp port-forward svc/headlamp 8080:80
  3. In a second VM shell:  kubectl -n headlamp create token headlamp-viewer --duration=30m
  4. Browse http://127.0.0.1:8080 on the host and paste the token at the login prompt.
EOF
