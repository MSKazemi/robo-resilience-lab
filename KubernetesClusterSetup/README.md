# Deploying a Kubernetes Cluster Using Vagrant and VirtualBox

## Table of Contents

- [Deploying a Kubernetes Cluster Using Vagrant and VirtualBox](#deploying-a-kubernetes-cluster-using-vagrant-and-virtualbox)
  - [Table of Contents](#table-of-contents)
  - [Overview](#overview)
  - [Prerequisites](#prerequisites)
    - [Installation of VirtualBox](#installation-of-virtualbox)
    - [Installation of Vagrant](#installation-of-vagrant)
  - [Creating VMs and Setting up the Kubernetes Cluster](#creating-vms-and-setting-up-the-kubernetes-cluster)
    - [Clsuter Verification](#clsuter-verification)
  - [Pinned Versions, Credentials and Re-provisioning](#pinned-versions-credentials-and-re-provisioning)
    - [Pinned versions](#pinned-versions)
    - [Worker join credential](#worker-join-credential)
    - [Re-running provisioning](#re-running-provisioning)
    - [Troubleshooting](#troubleshooting)
    - [Static checks and manual three-node verification](#static-checks-and-manual-three-node-verification)
  - [Cluster VM's Lifecycle Commands](#cluster-vms-lifecycle-commands)
    - [Shutdown the Cluster](#shutdown-the-cluster)
    - [Start the Cluster](#start-the-cluster)
    - [Destroy the Cluster](#destroy-the-cluster)
  - [Some Technical Details](#some-technical-details)
    - [Vagrantfile](#vagrantfile)
    - [Kubeadm](#kubeadm)
    - [Kubectl](#kubectl)
    - [Kubelet](#kubelet)
    - [Container Network Interface Plugin](#container-network-interface-plugin)

## Overview

This guide will walk you through the process of setting up a Kubernetes cluster with one control plane node and two worker nodes using Ubuntu 22.04 as the host operating system. This setup leverages Vagrant and VirtualBox to create and configure virtual machines automatically.

## Prerequisites

Before beginning, ensure you have VirtualBox and Vagrant installed on your machine. The host does not have to be Ubuntu: see the [Windows and macOS guide](../docs/WINDOWS-AND-MAC.md) for Windows, Intel Mac and Apple Silicon (not supported by this lab's VirtualBox box) notes.

### Installation of VirtualBox

To install VirtualBox, run the following commands:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install virtualbox -y
```

Alternatively, you can download and install VirtualBox from the following link:
- https://www.virtualbox.org/wiki/Linux_Downloads
- https://download.virtualbox.org/virtualbox/7.0.14/virtualbox-7.0_7.0.14-161095~Ubuntu~jammy_amd64.deb

### Installation of Vagrant

Install Vagrant with these commands:

```bash
sudo apt update && sudo apt upgrade -y
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install vagrant
```

Verify the installation:

```bash
vagrant --version
```

## Creating VMs and Setting up the Kubernetes Cluster

This project uses a Vagrantfile to automatically create and configure three VMs:

1. `controlplane`: This is the control plane node of the Kubernetes cluster. It manages the state of the cluster, such as which nodes are part of the cluster, what applications are running, and their desired state.
2. `worker1` and `worker2`: These are the worker nodes where the applications will be deployed.

To create the VMs and set up the Kubernetes cluster, clone the repository and execute the following:

```bash
git clone git@github.com:MSKazemi/robo-resilience-lab.git
cd robo-resilience-lab
vagrant up | tee vagrant.log
```

The `vagrant up` command will read the `Vagrantfile` in your current directory and create the VMs as specified. It will also execute the provisioning scripts defined in the `Vagrantfile` to install necessary packages, set up the Kubernetes cluster, and join the worker nodes to the cluster. So it is not necessary to run any thing else. And following is the explanation of the `Vagrantfile` and the `kubeadm` tool. The command `vagrant up | tee vagrant.log` also keeps a log of the provisioning output (`vagrant.log` is git-ignored). The scripts are written so that this log does not contain the worker join token or the dashboard token: `MonitoringSystem/monitoring.sh` saves the dashboard token to `/home/vagrant/dashboard-token` (mode 0600) on the control-plane VM; read it with `vagrant ssh controlplane -c 'cat ~/dashboard-token'`. Still review a log before sharing it.

### Clsuter Verification
  
After `vagrant up`, you should verify that all nodes and pods in your Kubernetes cluster are running correctly:

1. Check the status of all nodes in the cluster:

    ```sh
    kubectl get nodes -o wide
    ```

    All nodes should be in the `Ready` state.

2. Check the status of all pods in the cluster:

    ```sh
    kubectl get pods -A -o wide
    ```

    All pods should be in the `Running` state, and there should be no errors.

`kubectl` is configured for the `vagrant` user on the control-plane VM only, so run these checks there, for example `vagrant ssh controlplane -c 'kubectl get nodes -o wide'`.

## Pinned Versions, Credentials and Re-provisioning

> **Verification status:** these provisioning changes were reviewed statically, and the scripts were run twice in an Ubuntu 22.04 container with `kubeadm`, `kubectl`, `systemctl` and the kernel/swap commands replaced by stubs. In that container the pinned apt packages were really installed and the repository keys really checked, but no cluster was created. **No `vagrant up` or three-VM run has been performed for these changes**, so they are unverified at runtime on VirtualBox.

### Pinned versions

[`versions.sh`](versions.sh) is the single place where versions are set. The provisioning scripts load it through [`lab_common.sh`](lab_common.sh) from the synced folder (`/home/vagrant/codes/KubernetesClusterSetup`).

| Component | Pin | Reason for this version |
|---|---|---|
| Ubuntu guest | `jammy` (22.04), checked before anything is installed | The Docker package versions below are built for it |
| kubeadm / kubelet / kubectl | `1.29.15-1.1` from the `pkgs.k8s.io` `v1.29` repository | Historical lab version (end-of-life upstream); last 1.29 patch release |
| cri-tools / kubernetes-cni | `1.29.0-1.1` / `1.3.0-1.1` | The only versions in the `v1.29` repository |
| containerd.io | `1.7.29-1` | containerd's release notes list 1.7 (not 2.x) for Kubernetes 1.26+; newest 1.7 build in Docker's repository |
| Docker Engine (`docker-ce`, `docker-ce-cli`) | `28.5.2` | Docker 29.x requires containerd 2.x |
| Weave Net | `v2.8.1` manifest, SHA-256 checked; image tags rewritten from `:latest` to `:2.8.1` | Last release of the archived project |

The apt signing keys of `pkgs.k8s.io` and Docker are checked against pinned OpenPGP **fingerprints** before they are trusted (a fingerprint survives the periodic key-expiry extensions that change the key file). All pinned packages are put on `apt-mark hold`.

If a VM already has a *different* version of a pinned package (for example one provisioned by an older, unpinned version of these scripts), provisioning stops with a message instead of downgrading a container runtime or kubelet in place. Recreate that VM: `vagrant destroy <name>`, then `vagrant up <name>`.

### Worker join credential

Earlier versions wrote a live `kubeadm join` command, including a bootstrap token valid for 24 hours, to `join-command.sh` in the repository folder shared with the host. Now:

- The `Vagrantfile` generates a new random bootstrap token **in memory** each time Vagrant loads it. The token is passed only as an environment variable to `create_cluster.sh` (control plane) and [`join_worker.sh`](join_worker.sh) (workers), and is marked sensitive so that Vagrant hides it from its output and logs (`config.vagrant.sensitive` and the shell provisioner's `sensitive` option).
- The control plane registers that token with a short lifetime, `LAB_BOOTSTRAP_TOKEN_TTL` (default `1h`; for example `LAB_BOOTSTRAP_TOKEN_TTL=30m vagrant up`). It then expires on its own. To revoke it earlier, run `vagrant ssh controlplane -c 'sudo kubeadm token list'` and `sudo kubeadm token delete <token-id>` on the control plane.
- `kubeadm init` and `kubeadm join` output goes through a redaction filter (bootstrap tokens and certificate keys become placeholders) into root-only logs **inside the VMs**: `/var/log/robo-lab/kubeadm-init.log` and `/var/log/robo-lab/kubeadm-join.log`.
- The only file handed to the workers through the synced folder is the cluster CA's public-key hash, `.vagrant/robo-lab/ca-cert-hash`, inside Vagrant's own git-ignored directory. It is not a secret; it lets the workers check that they are joining the right control plane.
- The admin kubeconfig is copied only to `/home/vagrant/.kube/config` and `/root/.kube/config` inside the control-plane VM (mode 0600), not to the synced folder.
- A `join-command.sh` left over from an older run is deleted, and its token is revoked when the cluster already exists.

Inside a guest, root can still see the token while a provisioner runs, because it is part of that provisioner's environment. This is acceptable for a single-user teaching lab, and the short lifetime limits the exposure.

### Re-running provisioning

- `vagrant provision` (or `vagrant up --provision`) skips `kubeadm init` when `/etc/kubernetes/admin.conf` exists and skips `kubeadm join` on a worker that already has `/etc/kubernetes/kubelet.conf`. The `/etc/hosts` entries, the swap cron entry, the taint removal and the bash-completion line are added only once.
- Because a token exists only for one Vagrant command, a worker that has **not** joined yet must be provisioned in the same command as the control plane, for example `vagrant provision controlplane worker1`. Vagrant provisions the machines in the order controlplane, worker1, worker2.
- The fixed `sleep 20` delays were replaced by bounded readiness checks that stop with a message: API server `/readyz` (300 s), control-plane node registered (120 s), Weave Net rollout (300 s) and control-plane node `Ready` (300 s); on each worker, API server reachable (300 s), `kubeadm join` (600 s) and worker node `Ready` (300 s).

### Troubleshooting

| Symptom | What to do |
|---|---|
| `ERROR: cannot load .../lab_common.sh (is the synced folder mounted?)` | The VirtualBox shared folder is not mounted at `/home/vagrant/codes`. Check the synced-folder and Guest Additions messages at the start of `vagrant up`. |
| `... is installed but the lab pins ...` | The VM has a different package version. Recreate it (`vagrant destroy <name>`, then `vagrant up <name>`). |
| `apt key from ... has fingerprint ...` | The downloaded repository key is not the pinned one. Do not bypass this check: look at the network or proxy, and change the fingerprint in `versions.sh` only after the vendor's documentation confirms a key rotation. |
| `Weave Net manifest checksum mismatch` | The release asset changed or the download was altered. Do not apply it. |
| `kubeadm join failed` on a worker | Usually the token belongs to another Vagrant command or has expired: run `vagrant provision controlplane <worker>`. If a partial join left files behind, run `sudo kubeadm reset -f` on the worker first. The redacted log is `/var/log/robo-lab/kubeadm-join.log` on that worker. |
| `kubeadm init failed` | Read `/var/log/robo-lab/kubeadm-init.log` on the control plane, fix the cause, run `sudo kubeadm reset -f`, then `vagrant provision controlplane`. |
| `timed out ... waiting for: node <name> Ready` | Check the CNI pods: `vagrant ssh controlplane -c 'kubectl -n kube-system get pods -o wide'`. |

### Static checks and manual three-node verification

Checks that need no VM (run from the repository root):

```bash
bash scripts/check-shell.sh   # bash -n on every tracked shell script
ruby -c Vagrantfile           # Vagrantfile syntax, if Ruby is installed
```

Manual three-node verification, on a host with enough capacity (about 30 GB RAM and 18 vCPUs with the default profile):

```bash
vagrant up 2>&1 | tee vagrant.log                          # 1. clean boot
vagrant ssh controlplane -c 'kubectl get nodes -o wide'    #    expect controlplane, worker1 and worker2 Ready
vagrant provision 2>&1 | tee vagrant-reprovision.log       # 2. re-run: no re-init, no prompt, exit status 0
grep -nE '[a-z0-9]{6}\.[a-z0-9]{16}' vagrant.log vagrant-reprovision.log || echo 'no bootstrap token in the logs'
ls join-command.sh 2>/dev/null || echo 'no join-command.sh in the checkout'
```

Redact any remaining host names, addresses or tokens before attaching such a log to an issue.

## Cluster VM's Lifecycle Commands

To manage the VM's lifecycle, use the following commands:

### Shutdown the Cluster

```shell
vagrant halt
```

### Start the Cluster

```shell
vagrant up
```

### Destroy the Cluster

```shell
vagrant destroy -f
```

> **Warning:** `vagrant destroy -f` permanently deletes all three VirtualBox VMs and everything stored inside them, without asking for confirmation. Use `vagrant status` first to see what exists, and `vagrant halt` if you only want to stop the cluster.

## Some Technical Details

### Vagrantfile

The configuration for these VMs is specified in the `Vagrantfile`. For each VM, we have defined the following settings:

- `vm.network`: The private network IP address of the VM.
- `vm.hostname`: The internal hostname of the VM.
- `vb.name`: The name of the VM as displayed in VirtualBox.
- `vb.memory`: The amount of memory (in MB) allocated to the VM.
- `vb.cpus`: The number of CPUs allocated to the VM.

After running this command, you should have a fully functional Kubernetes cluster ready for use!

### Kubeadm

The cluster setup uses `kubeadm` to initialize the control plane and join worker nodes. The configuration details and initialization commands are managed through the included bash scripts. The `kubeadm` tool is used in the `create_cluster.sh` script to initialize the control plane node with the command `sudo kubeadm init --apiserver-advertise-address=192.168.56.10 --pod-network-cidr=10.244.0.0/16`.
Workers join with `kubeadm join 192.168.56.10:6443 --token <bootstrap token> --discovery-token-ca-cert-hash sha256:<hash>`, run by [`join_worker.sh`](join_worker.sh). See [Worker join credential](#worker-join-credential) for how the token and the hash reach the workers without a join script in the shared folder.
The `kubeadm`,`kubectl`, and `kubelet` tools are also installed on all nodes as part of the provisioning process defined in the `Vagrantfile` and the `kubeadm_kubelet_kubectl.sh` script.



### Kubectl

`kubectl` is the command-line tool that allows users to interact with Kubernetes clusters. It provides a wide range of functionalities that enable users to manage applications, check resource statuses, view logs, and execute administrative tasks on Kubernetes clusters. kubectl communicates with the cluster's API server and sends commands to it, which are then executed on the appropriate cluster nodes. This tool is essential for the operation and management of Kubernetes environments, offering both simplicity for beginners and powerful scripting capabilities for advanced users.

### Kubelet

The kubelet is an essential component that runs on every node in a Kubernetes cluster. It is responsible for ensuring that containers are running in a Pod as specified in the PodSpecs. The kubelet takes a set of PodSpecs that are provided through various mechanisms (primarily through the API server) and ensures that the containers described in those PodSpecs are healthy and running. Additionally, it manages the lifecycle of the containers, including starting, stopping, and maintaining application containers organized through Pod manifests based on system and/or Kubernetes instructions.


### Container Network Interface Plugin

In this project, we use Weave Net as our Container Network Interface (CNI) plugin. Weave Net is a powerful cloud native networking toolkit for Kubernetes. It provides a simple and efficient way to connect PODs across Kubernetes clusters.

[`Network/network_plugin.sh`](../Network/network_plugin.sh) downloads the Weave Net v2.8.1 DaemonSet manifest from the release page, checks its SHA-256 against `versions.sh`, pins its `:latest` image tags to `:2.8.1` and applies it. See the [Network README](../Network/README.md).

This README file provides a clear guide on setting up a Kubernetes cluster using Vagrant and includes links to further resources and detailed steps for installing prerequisites. Adjustments can be made to the scripts or Vagrant configuration based on specific needs or changes in your project structure.