#!/bin/bash
# Install Weave Net (historical CNI for this lab) from a checksum-verified manifest.
# Upstream weaveworks/weave was archived in June 2024; v2.8.1 is its last release.
# Kept for the historical Vagrant lab. Consider Calico or Flannel for new work.
# Version, manifest SHA-256 and image tag are pinned in KubernetesClusterSetup/versions.sh.
# Safe to re-run (kubectl apply).
set -euo pipefail

LAB_SETUP_DIR="${LAB_SETUP_DIR:-/home/vagrant/codes/KubernetesClusterSetup}"
# shellcheck source=KubernetesClusterSetup/lab_common.sh
. "$LAB_SETUP_DIR/lab_common.sh" \
  || { echo "ERROR: cannot load $LAB_SETUP_DIR/lab_common.sh (is the synced folder mounted?)" >&2; exit 1; }

MANIFEST_URL="https://github.com/weaveworks/weave/releases/download/v${WEAVE_VERSION}/weave-daemonset-k8s.yaml"
NODE_NAME="$(hostname)"

tmpd="$(mktemp -d)"
trap 'rm -rf "$tmpd"' EXIT

echo "Downloading Weave Net v${WEAVE_VERSION} manifest"
curl -fsSL --retry 3 "$MANIFEST_URL" -o "$tmpd/weave.yaml" \
  || die "could not download $MANIFEST_URL"
echo "${WEAVE_MANIFEST_SHA256}  $tmpd/weave.yaml" | sha256sum -c --quiet - \
  || die "Weave Net manifest checksum mismatch (expected $WEAVE_MANIFEST_SHA256); refusing to apply it"

# The release manifest uses floating `:latest` image tags; pin them to the release tag.
sed -E "s#(weaveworks/weave-(kube|npc)):latest#\1:${WEAVE_IMAGE_TAG}#g" "$tmpd/weave.yaml" > "$tmpd/weave-pinned.yaml"
! grep -q ':latest' "$tmpd/weave-pinned.yaml" \
  || die "Weave Net manifest still references a :latest image after pinning"
[ "$(grep -c "weaveworks/weave-\(kube\|npc\):${WEAVE_IMAGE_TAG}" "$tmpd/weave-pinned.yaml")" = "3" ] \
  || die "expected three pinned Weave Net image references in the manifest"

echo "Installing Weave Net v${WEAVE_VERSION} (images :${WEAVE_IMAGE_TAG})"
kubectl apply -f "$tmpd/weave-pinned.yaml"

echo "Waiting for Weave Net DaemonSet to roll out"
kubectl -n kube-system rollout status daemonset/weave-net --timeout=300s

# Bounded readiness gate (replaces the fixed sleeps that used to follow this step).
kubectl wait --for=condition=Ready "node/$NODE_NAME" --timeout=300s
