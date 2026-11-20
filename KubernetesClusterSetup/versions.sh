#!/bin/bash
# Single source of pinned versions for the historical Vagrant lab (Ubuntu 22.04 "jammy",
# Kubernetes 1.29, Weave Net 2.8.1). Sourced by lab_common.sh; not meant to be executed.
#
# These pins make a fresh `vagrant up` install the same packages every time. They were
# looked up on 2026-11-20 from the public apt indexes (pkgs.k8s.io v1.29, and
# download.docker.com jammy/stable amd64) and from the Weave Net v2.8.1 release asset.
# They have NOT been exercised by a full three-VM run; see KubernetesClusterSetup/README.md.
# Change a pin only together with a full multi-VM test, and keep the comments truthful.
# shellcheck disable=SC2034  # variables are consumed by the scripts that source this file

# Ubuntu release the Docker package versions below are built for.
LAB_UBUNTU_CODENAME="jammy"

# --- Kubernetes (community repository pkgs.k8s.io) -------------------------------------
# 1.29 is the historical lab version and is end-of-life upstream (2025-02-28);
# 1.29.15 is the last patch release in the v1.29 apt repository.
K8S_MINOR="1.29"
K8S_PKG_VERSION="1.29.15-1.1"
# Dependencies of kubeadm/kubelet; the v1.29 repository carries a single version of each.
K8S_CRI_TOOLS_PKG_VERSION="1.29.0-1.1"
K8S_CNI_PKG_VERSION="1.3.0-1.1"
# OpenPGP fingerprint of the pkgs.k8s.io v1.29 Release.key (primary key 234654DA9A296436).
# A fingerprint is pinned instead of a file checksum because the key's expiry is extended
# periodically, which changes the file but not the fingerprint.
K8S_REPO_KEY_FPR="DE15B14486CD377B9E876E1A234654DA9A296436"

# --- Container runtime (Docker's apt repository) --------------------------------------
# containerd 1.7 is pinned on purpose: containerd's RELEASES.md lists v1.7.0+ (not 2.x) for
# Kubernetes 1.26+, and the 2.x default config layout differs from what the lab was written
# for. 1.7.29-1 is the newest 1.7 build in Docker's jammy repository.
CONTAINERD_PKG_VERSION="1.7.29-1~ubuntu.22.04~jammy"
# Docker Engine 29.x requires containerd.io >= 2.1.5, so 28.5.2 (needs containerd.io
# >= 1.7.27) is the newest Engine that installs next to the pinned containerd 1.7.
DOCKER_PKG_VERSION="5:28.5.2-1~ubuntu.22.04~jammy"
DOCKER_BUILDX_PKG_VERSION="0.39.0-1~ubuntu.22.04~jammy"
DOCKER_COMPOSE_PKG_VERSION="5.8.0-1~ubuntu.22.04~jammy"
# Fingerprint documented by Docker for https://download.docker.com/linux/ubuntu/gpg.
DOCKER_REPO_KEY_FPR="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"

# --- CNI: Weave Net (upstream archived 2024-06-20; v2.8.1 is the last release) ---------
WEAVE_VERSION="2.8.1"
# SHA-256 of weave-daemonset-k8s.yaml from the v2.8.1 GitHub release (fetched 2026-11-20).
WEAVE_MANIFEST_SHA256="a2410c91152fdead60084a750d5041749e95350c038a9832e7dff71f85b52c9c"
# That manifest references the floating `:latest` image tags. network_plugin.sh rewrites
# them to this release tag so every run deploys the same images.
WEAVE_IMAGE_TAG="2.8.1"
