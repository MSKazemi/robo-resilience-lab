#!/bin/bash
# Install kubeadm, kubelet and kubectl from the community pkgs.k8s.io apt repository at the
# exact version pinned in versions.sh (K8S_MINOR / K8S_PKG_VERSION), with the repository key
# verified by fingerprint. Safe to re-run.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

LAB_SETUP_DIR="${LAB_SETUP_DIR:-/home/vagrant/codes/KubernetesClusterSetup}"
# shellcheck source=KubernetesClusterSetup/lab_common.sh
. "$LAB_SETUP_DIR/lab_common.sh" \
  || { echo "ERROR: cannot load $LAB_SETUP_DIR/lab_common.sh (is the synced folder mounted?)" >&2; exit 1; }

K8S_PKGS=(
  "kubelet=$K8S_PKG_VERSION"
  "kubeadm=$K8S_PKG_VERSION"
  "kubectl=$K8S_PKG_VERSION"
  "cri-tools=$K8S_CRI_TOOLS_PKG_VERSION"
  "kubernetes-cni=$K8S_CNI_PKG_VERSION"
)
check_pinned "${K8S_PKGS[@]}"

sudo apt-get update
# apt-transport-https may be a dummy package on newer releases; harmless if so.
sudo apt-get install -y apt-transport-https ca-certificates curl gpg

install_apt_key "https://pkgs.k8s.io/core:/stable:/v${K8S_MINOR}/deb/Release.key" \
  /etc/apt/keyrings/kubernetes-apt-keyring.gpg "$K8S_REPO_KEY_FPR"

# Overwrites any existing configuration in /etc/apt/sources.list.d/kubernetes.list
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v${K8S_MINOR}/deb/ /" \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list > /dev/null

sudo apt-get update
sudo apt-get install -y --allow-change-held-packages "${K8S_PKGS[@]}"
sudo apt-mark hold kubelet kubeadm kubectl cri-tools kubernetes-cni

sudo systemctl enable --now kubelet
echo "kubeadm/kubelet/kubectl $K8S_PKG_VERSION installed and held"
