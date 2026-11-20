#!/bin/bash
# Join this VM to the lab cluster as a worker. Safe to re-run: a node that already has
# /etc/kubernetes/kubelet.conf is left alone.
#
# Inputs (passed by the Vagrantfile, never stored in the synced folder):
#   LAB_BOOTSTRAP_TOKEN   bootstrap token of the current Vagrant run (secret, not printed)
#   LAB_CONTROL_PLANE_IP  API server address (default 192.168.56.10)
# The CA public-key hash comes from .vagrant/robo-lab/ca-cert-hash, written by
# create_cluster.sh. It pins the control plane's identity and is not a secret.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

LAB_SETUP_DIR="${LAB_SETUP_DIR:-/home/vagrant/codes/KubernetesClusterSetup}"
# shellcheck source=KubernetesClusterSetup/lab_common.sh
. "$LAB_SETUP_DIR/lab_common.sh" \
  || { echo "ERROR: cannot load $LAB_SETUP_DIR/lab_common.sh (is the synced folder mounted?)" >&2; exit 1; }

CONTROL_PLANE_IP="${LAB_CONTROL_PLANE_IP:-192.168.56.10}"
API_ENDPOINT="$CONTROL_PLANE_IP:6443"
KUBELET_CONF=/etc/kubernetes/kubelet.conf
CA_HASH_FILE="$LAB_HANDOFF_DIR/ca-cert-hash"
NODE_NAME="$(hostname)"

if [ -f "$KUBELET_CONF" ]; then
  echo "Node $NODE_NAME already joined ($KUBELET_CONF exists); skipping kubeadm join."
  exit 0
fi

require_bootstrap_token
command -v kubeadm > /dev/null || die "kubeadm is not installed; run KubernetesClusterSetup/kubeadm_kubelet_kubectl.sh first"
sudo systemctl is-active --quiet containerd || die "containerd is not active; check 'systemctl status containerd'"
[ -z "$(swapon --show --noheadings)" ] || die "swap is enabled; run KubernetesClusterSetup/provision_all.sh first"

[ -r "$CA_HASH_FILE" ] \
  || die "$CA_HASH_FILE is missing. Provision the control plane first: 'vagrant up controlplane' (or 'vagrant provision controlplane')."
ca_hash="$(tr -d '[:space:]' < "$CA_HASH_FILE")"
[[ "$ca_hash" =~ ^sha256:[0-9a-f]{64}$ ]] || die "$CA_HASH_FILE does not contain a sha256:<64 hex> CA hash"

# /readyz is readable anonymously in a default kubeadm cluster; -k because trust is
# established by the pinned CA hash during the join itself, not by this probe.
wait_for 300 "API server https://$API_ENDPOINT/readyz reachable" \
  curl -fsk --max-time 5 "https://$API_ENDPOINT/readyz"

sudo install -d -m 0700 "$LAB_LOG_DIR"
join_log="$LAB_LOG_DIR/kubeadm-join.log"
sudo install -m 0600 /dev/null "$join_log"
echo "Running kubeadm join against $API_ENDPOINT (redacted output: $join_log on this VM)"
if ! sudo timeout 600 kubeadm join "$API_ENDPOINT" \
    --token "$LAB_BOOTSTRAP_TOKEN" \
    --discovery-token-ca-cert-hash "$ca_hash" 2>&1 | redact | sudo tee "$join_log" > /dev/null; then
  echo "kubeadm join failed. Last lines of the redacted log:" >&2
  sudo tail -n 40 "$join_log" >&2 || true
  cat >&2 <<'MSG'
Common cause: the bootstrap token is created per Vagrant run and expires after
LAB_BOOTSTRAP_TOKEN_TTL (default 1h). If the control plane was provisioned by a different
'vagrant' command than this worker, re-run both in ONE command so they share a token:
  vagrant provision controlplane <this-worker>
If a partial join left files behind, run 'sudo kubeadm reset -f' on this worker first.
MSG
  die "kubeadm join failed for $NODE_NAME"
fi

wait_for 120 "kubelet service active" sudo systemctl is-active --quiet kubelet
# The kubelet's own credentials may read its Node object (Node authorizer).
wait_for 300 "node $NODE_NAME Ready" bash -c \
  "[ \"\$(kubectl --kubeconfig $KUBELET_CONF get node $NODE_NAME -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}')\" = True ]"
echo "Node $NODE_NAME joined and Ready. Check the whole cluster with: vagrant ssh controlplane -c 'kubectl get nodes -o wide'"
