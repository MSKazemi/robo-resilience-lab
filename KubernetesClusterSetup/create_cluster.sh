#!/bin/bash
# Bootstrap the control-plane node with kubeadm. Safe to re-run: kubeadm init is skipped
# when the cluster already exists, and every later step is idempotent.
#
# Credential handling (issue #43):
#   * The worker bootstrap token is generated in memory by the Vagrantfile for each Vagrant
#     run and passed in as LAB_BOOTSTRAP_TOKEN. This script registers it with a short TTL
#     (LAB_BOOTSTRAP_TOKEN_TTL, default 1h). It is never written to the synced folder and
#     never printed: kubeadm output is redacted before it reaches the log or the console.
#   * Only the CA public-key hash (not a secret) is handed to the workers through the synced
#     folder, in Vagrant's git-ignored .vagrant/ directory.
#   * The admin kubeconfig is copied only to guest home directories (mode 0600), never to
#     the synced folder.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

LAB_SETUP_DIR="${LAB_SETUP_DIR:-/home/vagrant/codes/KubernetesClusterSetup}"
# shellcheck source=KubernetesClusterSetup/lab_common.sh
. "$LAB_SETUP_DIR/lab_common.sh" \
  || { echo "ERROR: cannot load $LAB_SETUP_DIR/lab_common.sh (is the synced folder mounted?)" >&2; exit 1; }

CONTROL_PLANE_IP="${LAB_CONTROL_PLANE_IP:-192.168.56.10}"
POD_NETWORK_CIDR="10.244.0.0/16"
TOKEN_TTL="${LAB_BOOTSTRAP_TOKEN_TTL:-1h}"
ADMIN_CONF=/etc/kubernetes/admin.conf
NODE_NAME="$(hostname)"
# Written by earlier versions of this script; it held a live join token.
LEGACY_JOIN_FILE=/home/vagrant/codes/join-command.sh

require_bootstrap_token
[[ "$TOKEN_TTL" =~ ^[0-9]+[smh]$ ]] || die "LAB_BOOTSTRAP_TOKEN_TTL must look like 30m or 1h (got '$TOKEN_TTL')"

sudo install -d -m 0700 "$LAB_LOG_DIR"

# --- Remove the legacy host-shared join script ----------------------------------------
if [ -f "$LEGACY_JOIN_FILE" ]; then
  legacy_id="$(grep -oE -- '--token [a-z0-9]{6}\.' "$LEGACY_JOIN_FILE" | head -n1 | sed -E 's/--token ([a-z0-9]{6})\./\1/' || true)"
  if [ -n "$legacy_id" ] && [ -f "$ADMIN_CONF" ]; then
    sudo kubeadm token delete "$legacy_id" > /dev/null 2>&1 || true
    echo "Revoked the bootstrap token (id $legacy_id) from the legacy join-command.sh."
  fi
  rm -f "$LEGACY_JOIN_FILE"
  echo "Removed legacy $LEGACY_JOIN_FILE (it contained a bootstrap token)."
fi

# --- kubeadm init (only once) ---------------------------------------------------------
if [ -f "$ADMIN_CONF" ]; then
  echo "Cluster already initialised ($ADMIN_CONF exists); skipping kubeadm init."
else
  # Preflight checks with actionable messages (kubeadm's own preflight runs as well).
  command -v kubeadm > /dev/null || die "kubeadm is not installed; run KubernetesClusterSetup/kubeadm_kubelet_kubectl.sh first"
  sudo systemctl is-active --quiet containerd || die "containerd is not active; check 'systemctl status containerd' (KubernetesClusterSetup/CRI_DockerEng_containerd.sh)"
  [ -z "$(swapon --show --noheadings)" ] || die "swap is enabled; run KubernetesClusterSetup/provision_all.sh first"
  ipv4_addrs="$(ip -4 -o addr show)"
  grep -qF "inet $CONTROL_PLANE_IP/" <<< "$ipv4_addrs" \
    || die "no network interface has $CONTROL_PLANE_IP; check the private_network setting in the Vagrantfile"

  init_log="$LAB_LOG_DIR/kubeadm-init.log"
  echo "Running kubeadm init (redacted output: $init_log on the control-plane VM)"
  sudo install -m 0600 /dev/null "$init_log"
  # --skip-token-print keeps the token out of kubeadm's own summary; redact() is a second guard.
  if ! sudo timeout 900 kubeadm init \
      --apiserver-advertise-address="$CONTROL_PLANE_IP" \
      --pod-network-cidr="$POD_NETWORK_CIDR" \
      --token "$LAB_BOOTSTRAP_TOKEN" --token-ttl "$TOKEN_TTL" \
      --skip-token-print 2>&1 | redact | sudo tee "$init_log" > /dev/null; then
    echo "kubeadm init failed. Last lines of the redacted log:" >&2
    sudo tail -n 40 "$init_log" >&2 || true
    die "kubeadm init failed; fix the cause, then run 'sudo kubeadm reset -f' on the control plane and 'vagrant provision controlplane'"
  fi
fi

# --- kubeconfig for root (used by the later provisioners) and for the vagrant user -----
# Guest home directories only; never the synced folder.
install_kubeconfig() {
  local dir="$1" owner="$2"
  sudo install -d -m 0700 -o "$owner" -g "$owner" "$dir/.kube"
  sudo install -m 0600 -o "$owner" -g "$owner" "$ADMIN_CONF" "$dir/.kube/config"
}
install_kubeconfig /home/vagrant vagrant
install_kubeconfig /root root
export KUBECONFIG="$ADMIN_CONF"

wait_for 300 "Kubernetes API server /readyz" sudo kubectl --kubeconfig "$ADMIN_CONF" get --raw=/readyz
wait_for 120 "node $NODE_NAME registered" sudo kubectl --kubeconfig "$ADMIN_CONF" get node "$NODE_NAME"

# --- Worker bootstrap token for this Vagrant run --------------------------------------
# Recreate it so its TTL restarts and a re-provision with a new token works. kubeadm
# prints the token on success, so all output is discarded.
token_id="${LAB_BOOTSTRAP_TOKEN%%.*}"
sudo kubeadm token delete "$token_id" > /dev/null 2>&1 || true
sudo kubeadm token create "$LAB_BOOTSTRAP_TOKEN" --ttl "$TOKEN_TTL" \
  --description "robo-resilience-lab worker join (Vagrant run)" > /dev/null 2>&1 \
  || die "could not create the worker bootstrap token (id $token_id); check 'sudo kubeadm token list' on the control plane"
echo "Worker bootstrap token id $token_id registered with TTL $TOKEN_TTL (value not shown)."

# --- Public CA hash for the workers' discovery pinning ---------------------------------
ca_hash="sha256:$(openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt \
  | openssl pkey -pubin -outform der 2>/dev/null \
  | openssl dgst -sha256 -hex | sed 's/^.* //')"
[[ "$ca_hash" =~ ^sha256:[0-9a-f]{64}$ ]] || die "could not compute the cluster CA public-key hash"
mkdir -p "$LAB_HANDOFF_DIR"
printf '%s\n' "$ca_hash" > "$LAB_HANDOFF_DIR/ca-cert-hash.tmp"
mv -f "$LAB_HANDOFF_DIR/ca-cert-hash.tmp" "$LAB_HANDOFF_DIR/ca-cert-hash"
echo "CA public-key hash written for the workers (not a secret): .vagrant/robo-lab/ca-cert-hash"

# --- Lab-only scheduling: allow pods on the control-plane node -------------------------
# Dashboard, metrics-server and Prometheus run on this node.
taint_key="node-role.kubernetes.io/control-plane"
node_taints="$(sudo kubectl --kubeconfig "$ADMIN_CONF" get node "$NODE_NAME" -o jsonpath='{.spec.taints[*].key}')"
if grep -qwF "$taint_key" <<< "$node_taints"; then
  sudo kubectl --kubeconfig "$ADMIN_CONF" taint nodes "$NODE_NAME" "$taint_key:NoSchedule-"
else
  echo "Control-plane NoSchedule taint already removed."
fi

# --- kubectl bash completion (idempotent: appends the line only once) ------------------
sudo apt-get -y install bash-completion
completion_line='source <(kubectl completion bash)'
for rc in /root/.bashrc /home/vagrant/.bashrc; do
  touch "$rc"
  grep -qxF "$completion_line" "$rc" || echo "$completion_line" >> "$rc"
done
chown vagrant:vagrant /home/vagrant/.bashrc
