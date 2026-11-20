#!/bin/bash
# Base system preparation for every node: packages, swap off, kernel modules and sysctl.
# Safe to re-run.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

LAB_SETUP_DIR="${LAB_SETUP_DIR:-/home/vagrant/codes/KubernetesClusterSetup}"
# shellcheck source=KubernetesClusterSetup/lab_common.sh
. "$LAB_SETUP_DIR/lab_common.sh" \
  || { echo "ERROR: cannot load $LAB_SETUP_DIR/lab_common.sh (is the synced folder mounted?)" >&2; exit 1; }

# Preflight: the pinned package versions in versions.sh are built for one Ubuntu release.
require_ubuntu_codename

# Install system packages using apt
sudo apt-get update
sudo apt-get install -y git vim curl bash-completion
# sudo apt-get -y upgrade

# Disable swap now, and keep it off after reboot (cron @reboot; appended only once).
sudo swapoff -a
[ -z "$(swapon --show --noheadings)" ] || die "swap is still enabled after 'swapoff -a'; kubelet requires swap off"
swap_cron='@reboot /sbin/swapoff -a'
if ! crontab -l 2>/dev/null | grep -qxF "$swap_cron"; then
  (crontab -l 2>/dev/null || true; echo "$swap_cron") | crontab -
fi

sudo install -d -m 0755 /etc/modules-load.d /etc/sysctl.d
cat <<EOF | sudo tee /etc/modules-load.d/k8s.conf > /dev/null
overlay
br_netfilter
EOF

sudo modprobe overlay
sudo modprobe br_netfilter

# sysctl params required by setup, params persist across reboots
cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf > /dev/null
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF

# Apply sysctl params without reboot
sudo sysctl --system

sysctl net.bridge.bridge-nf-call-iptables net.bridge.bridge-nf-call-ip6tables net.ipv4.ip_forward
