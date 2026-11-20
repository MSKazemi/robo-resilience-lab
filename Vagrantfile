# -*- mode: ruby -*-
# vi: set ft=ruby :

vm_name_cp = "controlplane"
vm_name_w1 = "worker1"
vm_name_w2 = "worker2"

vm_ip_cp = "192.168.56.10"
vm_ip_w1 = "192.168.56.11"
vm_ip_w2 = "192.168.56.12"

# --- Host sizing and platform guard (see docs/WINDOWS-AND-MAC.md) -----------------
# With no environment variables set, the sizes below are the historical defaults
# (about 30 GB RAM and 18 vCPUs in total). Override them for smaller hosts, e.g.
#   LAB_PROFILE=small vagrant up           (bash/zsh)
#   $env:LAB_PROFILE = "small"; vagrant up  (PowerShell)
# The "small" profile is NOT validated end to end; kubeadm itself needs at least
# 2 vCPUs and 2 GB RAM per node, and the monitoring stack needs more headroom.
require "rbconfig"
require "securerandom"

lab_profiles = {
  "default" => { cp_mem: 18240, cp_cpus: 14, worker_mem: 6096, worker_cpus: 2 },
  "small"   => { cp_mem: 8192,  cp_cpus: 4,  worker_mem: 4096, worker_cpus: 2 },
}

# .strip: cmd.exe's `set X=small && ...` leaves a trailing space in the value.
lab_profile = ENV.fetch("LAB_PROFILE", "default").strip.downcase
unless lab_profiles.key?(lab_profile)
  abort "LAB_PROFILE must be one of: #{lab_profiles.keys.join(', ')} (got '#{lab_profile}')."
end

lab_int = lambda do |name, fallback|
  raw = ENV[name]
  next fallback if raw.nil? || raw.strip.empty?
  abort "#{name} must be a positive whole number (got '#{raw}')." unless raw.strip.match?(/\A[1-9][0-9]*\z/)
  raw.strip.to_i
end

sizes = lab_profiles[lab_profile]
cp_mem     = lab_int.call("LAB_CP_MEMORY_MB",     sizes[:cp_mem])
cp_cpus    = lab_int.call("LAB_CP_CPUS",          sizes[:cp_cpus])
worker_mem = lab_int.call("LAB_WORKER_MEMORY_MB", sizes[:worker_mem])
worker_cpus = lab_int.call("LAB_WORKER_CPUS",     sizes[:worker_cpus])

# ubuntu/jammy64 is published for VirtualBox on x86-64 only. On an Arm host
# (Apple Silicon Macs, Windows on Arm) it cannot boot, so stop early with guidance
# instead of failing in the middle of provisioning. Read-only commands such as
# `vagrant status` and `vagrant destroy` are not blocked.
lab_box = ENV.fetch("LAB_BOX", "ubuntu/jammy64").strip
host_cpu = RbConfig::CONFIG["host_cpu"].to_s.downcase
if host_cpu.match?(/arm|aarch64/) && ENV["LAB_ALLOW_ARM"].to_s.strip != "1" &&
   %w[up reload provision].include?(ARGV[0])
  abort <<~MSG
    This Vagrant lab uses the x86-64 VirtualBox box 'ubuntu/jammy64' and cannot run on an
    Arm host (detected: #{host_cpu}). Use the Docker-based path in docs/WINDOWS-AND-MAC.md
    instead. To experiment anyway, set LAB_BOX to an Arm64 VirtualBox box and LAB_ALLOW_ARM=1;
    that combination is untested.
  MSG
end

# --- Worker join credential (issue #43) ------------------------------------------------
# A fresh kubeadm bootstrap token is generated in memory every time Vagrant loads this file.
# It reaches the guests only as an environment variable of the control-plane and worker
# provisioners, is hidden from Vagrant's output and logs (`sensitive`), and is never written
# to the synced folder. The control plane registers it with a short TTL
# (LAB_BOOTSTRAP_TOKEN_TTL, default 1h). Consequence: provision the control plane and the
# workers in the same command (`vagrant up`, or `vagrant provision controlplane worker1`).
token_chars = [*"a".."z", *"0".."9"]
lab_token_part = ->(n) { Array.new(n) { token_chars[SecureRandom.random_number(token_chars.size)] }.join }
lab_bootstrap_token = "#{lab_token_part.call(6)}.#{lab_token_part.call(16)}"
lab_token_ttl = ENV.fetch("LAB_BOOTSTRAP_TOKEN_TTL", "1h").strip
unless lab_token_ttl.match?(/\A[1-9][0-9]*[smh]\z/)
  abort "LAB_BOOTSTRAP_TOKEN_TTL must look like 30m or 1h (got '#{lab_token_ttl}')."
end
lab_join_env = {
  "LAB_BOOTSTRAP_TOKEN" => lab_bootstrap_token,
  "LAB_BOOTSTRAP_TOKEN_TTL" => lab_token_ttl,
  "LAB_CONTROL_PLANE_IP" => vm_ip_cp,
}

# /etc/hosts entries for all nodes, added once (re-provisioning does not duplicate them).
lab_hosts = { vm_name_cp => vm_ip_cp, vm_name_w1 => vm_ip_w1, vm_name_w2 => vm_ip_w2 }
lab_hosts_script = lab_hosts.map { |name, ip|
  "grep -qxF '#{ip} #{name}' /etc/hosts || echo '#{ip} #{name}' >> /etc/hosts"
}.join("\n")

Vagrant.configure("2") do |config|
  config.vagrant.sensitive = [lab_bootstrap_token]
  config.vm.box = lab_box
  config.vm.synced_folder "./", "/home/vagrant/codes/"
  # Pinned versions for all provisioning scripts: KubernetesClusterSetup/versions.sh
  config.vm.provision "shell", path: "./KubernetesClusterSetup/provision_all.sh"
  config.vm.provision "shell", path: "./KubernetesClusterSetup/CRI_DockerEng_containerd.sh"
  config.vm.provision "shell", path: "./KubernetesClusterSetup/kubeadm_kubelet_kubectl.sh"

  config.vm.provision "shell", inline: "set -eu\n#{lab_hosts_script}"


  # Control Plane Configuration
  config.vm.define vm_name_cp do |cp|
    cp.vm.network "private_network", ip: vm_ip_cp
    cp.vm.hostname = vm_name_cp  # This sets the internal hostname of the VM
    cp.vm.provider "virtualbox" do |vb|
      vb.name = vm_name_cp # This sets the name displayed in VirtualBox
      vb.memory = cp_mem.to_s # This sets the amount of memory for the VM
      vb.cpus = cp_cpus # This sets the number of CPUs for the VM
    end

    # Cluster setup. Readiness is checked inside the scripts with bounded timeouts
    # (API server /readyz, node Ready, Weave Net rollout) instead of fixed sleeps.
    cp.vm.provision "shell", path: "./KubernetesClusterSetup/create_cluster.sh",
      env: lab_join_env, sensitive: true
    cp.vm.provision "shell", path: "./Network/network_plugin.sh"
    cp.vm.provision "shell", path: "./MonitoringSystem/monitoring.sh"
  end

  # Worker Node 1 Configuration
  config.vm.define vm_name_w1 do |w1|
    w1.vm.network "private_network", ip: vm_ip_w1
    w1.vm.hostname = vm_name_w1 # This sets the internal hostname of the VM
    w1.vm.provider "virtualbox" do |vb|
      vb.name = vm_name_w1 # This sets the name displayed in VirtualBox
      vb.memory = worker_mem.to_s # This sets the amount of memory for the VM
      vb.cpus = worker_cpus # This sets the number of CPUs for the VM
    end
    w1.vm.provision "shell", path: "./KubernetesClusterSetup/join_worker.sh",
      env: lab_join_env, sensitive: true
  end


  # Worker Node 2 Configuration
  config.vm.define vm_name_w2 do |w2|
    w2.vm.network "private_network", ip: vm_ip_w2
    w2.vm.hostname = vm_name_w2 # This sets the internal hostname of the VM
    w2.vm.provider "virtualbox" do |vb|
      vb.name = vm_name_w2 # This sets the name displayed in VirtualBox
      vb.memory = worker_mem.to_s # This sets the amount of memory for the VM
      vb.cpus = worker_cpus # This sets the number of CPUs for the VM
    end
    w2.vm.provision "shell", path: "./KubernetesClusterSetup/join_worker.sh",
      env: lab_join_env, sensitive: true
  end


end
 
