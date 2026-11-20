#!/bin/bash
# Install Docker Engine + containerd at pinned versions and configure containerd for kubeadm
# (systemd cgroups). Versions and the Docker apt key fingerprint come from versions.sh.
# Safe to re-run: the original config is backed up once, then regenerated each time.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

LAB_SETUP_DIR="${LAB_SETUP_DIR:-/home/vagrant/codes/KubernetesClusterSetup}"
# shellcheck source=KubernetesClusterSetup/lab_common.sh
. "$LAB_SETUP_DIR/lab_common.sh" \
  || { echo "ERROR: cannot load $LAB_SETUP_DIR/lab_common.sh (is the synced folder mounted?)" >&2; exit 1; }

require_ubuntu_codename

DOCKER_PKGS=(
  "containerd.io=$CONTAINERD_PKG_VERSION"
  "docker-ce=$DOCKER_PKG_VERSION"
  "docker-ce-cli=$DOCKER_PKG_VERSION"
  "docker-buildx-plugin=$DOCKER_BUILDX_PKG_VERSION"
  "docker-compose-plugin=$DOCKER_COMPOSE_PKG_VERSION"
)
check_pinned "${DOCKER_PKGS[@]}"

# Docker's apt signing key, verified by fingerprint, and its apt repository.
sudo apt-get update
sudo apt-get install -y ca-certificates curl gpg
install_apt_key https://download.docker.com/linux/ubuntu/gpg \
  /etc/apt/keyrings/docker.gpg "$DOCKER_REPO_KEY_FPR"
# Older runs stored the key unverified as docker.asc; it is no longer referenced.
sudo rm -f /etc/apt/keyrings/docker.asc

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $LAB_UBUNTU_CODENAME stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get -y update

sudo apt-get -y install --allow-change-held-packages "${DOCKER_PKGS[@]}"
sudo apt-mark hold containerd.io docker-ce docker-ce-cli docker-buildx-plugin docker-compose-plugin

# Group membership takes effect at the next login. Do not run `newgrp` here: in a
# non-interactive provisioning shell it replaces the script's shell.
sudo usermod -aG docker vagrant

# containerd: the containerd.io package ships a config with `disabled_plugins = ["cri"]`,
# which breaks kubeadm. Replace it with the default config and enable SystemdCgroup.
CONFIG_FILE=/etc/containerd/config.toml
BACKUP_FILE="${CONFIG_FILE}.backup"
sudo mkdir -p /etc/containerd
if [ -f "$CONFIG_FILE" ] && [ ! -f "$BACKUP_FILE" ]; then
  sudo cp -p "$CONFIG_FILE" "$BACKUP_FILE"
fi

tmp_config="$(mktemp)"
trap 'rm -f "$tmp_config"' EXIT
containerd config default > "$tmp_config"
# containerd 1.7 prints exactly one runc `SystemdCgroup = false` line; anything else means
# the config layout changed and the substitution below cannot be trusted.
[ "$(grep -c 'SystemdCgroup = false' "$tmp_config" || true)" = "1" ] \
  || die "expected one 'SystemdCgroup = false' line in 'containerd config default' (containerd $CONTAINERD_PKG_VERSION); the config layout changed"
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' "$tmp_config"
grep -q 'SystemdCgroup = true' "$tmp_config" \
  || die 'SystemdCgroup not enabled in generated containerd config'
if grep -q 'disabled_plugins = \[.*"cri".*\]' "$tmp_config"; then
  die 'generated containerd config disables the CRI plugin'
fi

sudo install -m 0644 "$tmp_config" "$CONFIG_FILE"
sudo systemctl restart containerd
wait_for 60 "containerd service active" sudo systemctl is-active --quiet containerd

# Verify the merged config containerd parses from the installed file, not just the sed
# result (`containerd config dump` reads /etc/containerd/config.toml plus any imports).
sudo containerd config dump | grep -q 'SystemdCgroup = true' \
  || die "containerd's parsed config does not have SystemdCgroup = true"
echo "containerd $CONTAINERD_PKG_VERSION active with SystemdCgroup = true"
