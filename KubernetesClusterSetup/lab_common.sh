#!/bin/bash
# Shared helpers for the Vagrant lab provisioning scripts. Sourced, not executed.
# Loads the pinned versions from versions.sh in the same directory.
#
# The provisioning scripts are uploaded alone to the guest by Vagrant, so they locate this
# file through the synced folder: LAB_SETUP_DIR (default /home/vagrant/codes/KubernetesClusterSetup).

LAB_SETUP_DIR="${LAB_SETUP_DIR:-/home/vagrant/codes/KubernetesClusterSetup}"
# shellcheck source=KubernetesClusterSetup/versions.sh
. "$LAB_SETUP_DIR/versions.sh"

# Guest-only directory for provisioning logs (root-only; never inside the synced folder).
LAB_LOG_DIR="/var/log/robo-lab"
# Hand-off directory inside the synced folder for NON-secret data only (the public CA
# certificate hash). `.vagrant/` is Vagrant's own state directory and is git-ignored.
LAB_HANDOFF_DIR="/home/vagrant/codes/.vagrant/robo-lab"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

# Replace bootstrap tokens and certificate keys with placeholders so command output can be
# logged or shown on failure without leaking credentials.
redact() {
  sed -E \
    -e 's/[a-z0-9]{6}\.[a-z0-9]{16}/<redacted-token>/g' \
    -e 's/(--certificate-key[ =])[0-9a-fA-F]+/\1<redacted>/g' \
    -e 's/(token: )[A-Za-z0-9._-]{20,}/\1<redacted>/g'
}

# Validate a kubeadm bootstrap token ("abcdef.0123456789abcdef") without printing it.
require_bootstrap_token() {
  [ -n "${LAB_BOOTSTRAP_TOKEN:-}" ] \
    || die "LAB_BOOTSTRAP_TOKEN is not set. The Vagrantfile passes it to this script; for a manual run, generate one with 'kubeadm token generate'."
  [[ "$LAB_BOOTSTRAP_TOKEN" =~ ^[a-z0-9]{6}\.[a-z0-9]{16}$ ]] \
    || die "LAB_BOOTSTRAP_TOKEN does not have the kubeadm format [a-z0-9]{6}.[a-z0-9]{16} (value not shown)."
}

# wait_for SECONDS DESCRIPTION COMMAND...  - retry COMMAND every 5 s until it succeeds
# or SECONDS have passed; then fail with DESCRIPTION.
wait_for() {
  local timeout="$1" what="$2" waited=0
  shift 2
  echo "Waiting up to ${timeout}s for: $what"
  until "$@" > /dev/null 2>&1; do
    if (( waited >= timeout )); then
      die "timed out after ${timeout}s waiting for: $what"
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done
  echo "Ready: $what (after ~${waited}s)"
}

require_ubuntu_codename() {
  local codename
  # shellcheck disable=SC1091
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
  [ "$codename" = "$LAB_UBUNTU_CODENAME" ] \
    || die "this lab pins packages built for Ubuntu '$LAB_UBUNTU_CODENAME', but the guest is '${codename:-unknown}'. Use the default box (LAB_BOX unset) or update KubernetesClusterSetup/versions.sh."
}

# install_apt_key URL DEST FINGERPRINT - download an apt signing key, check that it holds
# exactly one primary key with the expected fingerprint, and install it dearmored at DEST.
install_apt_key() {
  local url="$1" dest="$2" want="$3" tmpd pubs got
  tmpd="$(mktemp -d)"
  mkdir -m 0700 "$tmpd/gnupg"
  if ! curl -fsSL --retry 3 "$url" -o "$tmpd/key"; then
    rm -rf "$tmpd"
    die "could not download the apt signing key from $url"
  fi
  pubs="$(GNUPGHOME="$tmpd/gnupg" gpg --batch --show-keys --with-colons "$tmpd/key" | grep -c '^pub:' || true)"
  got="$(GNUPGHOME="$tmpd/gnupg" gpg --batch --show-keys --with-colons --with-fingerprint "$tmpd/key" \
    | awk -F: '/^pub:/{p=1; next} p && /^fpr:/{print $10; exit}')"
  if [ "$pubs" != "1" ] || [ "$got" != "$want" ]; then
    rm -rf "$tmpd"
    die "apt key from $url has fingerprint '${got:-none}' (primary keys: ${pubs:-0}); expected $want. Refusing to trust it."
  fi
  GNUPGHOME="$tmpd/gnupg" gpg --batch --yes --dearmor -o "$tmpd/key.gpg" "$tmpd/key"
  sudo install -D -m 0644 "$tmpd/key.gpg" "$dest"
  rm -rf "$tmpd"
  echo "Installed apt key $want -> $dest"
}

# check_pinned PKG=VERSION... - fail if a package is already installed at a different
# version (re-provisioning a VM built with older, unpinned scripts). Downgrading a container
# runtime or kubelet in place is not something this lab supports.
check_pinned() {
  local spec pkg want have
  for spec in "$@"; do
    pkg="${spec%%=*}"
    want="${spec#*=}"
    have="$(dpkg-query -W -f='${Status} ${Version}' "$pkg" 2>/dev/null | awk '$3=="installed"{print $4}' || true)"
    if [ -n "$have" ] && [ "$have" != "$want" ]; then
      die "$pkg $have is installed but the lab pins $want (KubernetesClusterSetup/versions.sh). Recreate this VM ('vagrant destroy <name>' then 'vagrant up <name>') instead of changing it in place."
    fi
  done
}
