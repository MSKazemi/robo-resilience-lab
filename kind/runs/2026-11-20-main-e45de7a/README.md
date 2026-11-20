# kind cross-worker run: `main` at `e45de7a`, 2026-11-20

**Measured, one run, one host.** This folder records one `make verify` and one `make verify-negative` of the kind talker/listener demo ([`docs/KIND-DEMO.md`](../../../docs/KIND-DEMO.md)) on the `main` commit `e45de7a`. Issue [#138](https://github.com/MSKazemi/robo-resilience-lab/issues/138). It extends the earlier `cross-worker verified` record (commit `862b026`, 2026-10-27) to a later `main` commit. It does **not** cover other CNIs, other hosts, the Vagrant/kubeadm profile, a multi-machine cluster, latency or throughput.

## Result

| Check | Exit code | Decisive line |
|---|---|---|
| `make verify` | **0** (5 s wall) | `PASS: ROS 2 messages crossed from ros2k8s-demo-worker to ros2k8s-demo-worker2` |
| `make verify-negative` | **0** (63 s wall) | `PASS: negative test (broken delivery was detected with exit status 1, then recovered)` |
| `make clean` | 0 (5 s wall) | `kind get clusters` afterwards printed `No kind clusters found.` |

`make verify`: the listener heard `Hello World: 9` .. `Hello World: 13` (counter advanced by 4) and the script confirmed that the talker pod logged publishing the newest one. In the negative test, pointing the listener at a dead discovery server (`127.0.0.1:11811`) made verification fail at the delivery check (`FAIL delivery: listener (ros2k8s-demo-worker2) logged no 'I heard' message from the talker (ros2k8s-demo-worker) within 45s`, exit 1). After the restore it passed again (`Hello World: 78` .. `82`). Full output: [`verify.txt`](verify.txt), [`verify-negative.txt`](verify-negative.txt).

## Deviation from `make demo`: the image was built by hand

On this date `make demo` cannot build the image, because two apt pins in `ROS2/Dockerfile` (`ROS_DEMO_NODES_CPP_VERSION`, `ROS2_PULSE_VERSION`) name versions no longer in the ROS apt index. Checked in the pinned base image on the same day: `apt-cache madison` listed only `ros-jazzy-demo-nodes-cpp 0.33.11-1noble.20261105.092654` and `ros-jazzy-ros2-pulse 0.7.0-1noble.20261105.090619`, and a simulated install of the Dockerfile defaults (`apt-get install -s`) exited 100 with `E: Version '0.33.11-1noble.20260920.235420' for 'ros-jazzy-demo-nodes-cpp' was not found` (and the same for `0.5.0-1noble.20260914.160740` of `ros-jazzy-ros2-pulse`). `make demo` itself was not run. Neither the Dockerfile nor the Makefile was changed for this run. Instead the steps of `make demo` were run by hand, in order, with only the build arguments different:

```sh
# 1/5 build, with two build-arg overrides (everything else as in the Dockerfile)
docker build --file ROS2/Dockerfile --tag ros2k8s-demo-ros:local \
  --build-arg ROS_DEMO_NODES_CPP_VERSION=0.33.11-1noble.20261105.092654 \
  --build-arg ROS2_PULSE_VERSION=0.7.0-1noble.20261105.090619 .      # exit 0
# 2/5 to 5/5 exactly as in the Makefile's demo target
kind create cluster --config kind/ros2k8s-demo.yaml --wait 120s        # exit 0, 32 s
kind load docker-image ros2k8s-demo-ros:local --name ros2k8s-demo \
  --nodes ros2k8s-demo-worker,ros2k8s-demo-worker2
kubectl --context kind-ros2k8s-demo apply -f ROS2/jazzy/talker-listener.yaml
for d in discovery-server talker listener; do
  kubectl --context kind-ros2k8s-demo -n ros rollout status deployment/$d --timeout=180s
done                                                                    # steps 3-5: exit 0, 38 s
make verify            # unchanged target; it does not build anything
make verify-negative   # unchanged target
make clean             # deletes cluster ros2k8s-demo and image ros2k8s-demo-ros:local
kind get clusters      # No kind clusters found.
```

The `kindest/node` image and the `ros:jazzy-ros-base-noble` base image were already cached, so no timing here is a cold-start time. The build time was not recorded. The pin drift itself is a separate problem for the Dockerfile owner; this run does not fix it.

## Environment

| Item | Value |
|---|---|
| Commit | `e45de7ae462fb3be63966339be65362a53275b7b` (`origin/main`), clean tree during the run |
| Date (UTC) | cluster created 2026-11-20 01:26:28, `make clean` started 01:31:47 |
| Host | AMD Ryzen 5 PRO 4650U, 12 logical CPUs, 30.6 GiB RAM, Ubuntu 24.04.5 LTS, kernel 7.0.0-43-generic, amd64 |
| Tools | Docker 29.10.0 (client and server), kind v0.33.0 (go1.26.7), kubectl client v1.37.1 |
| Cluster | Kubernetes server v1.37.0, containerd 2.3.4, node image `kindest/node:v1.37.0@sha256:a1ed56cf...` (pinned in `kind/ros2k8s-demo.yaml`), CNI kindnetd (`docker.io/kindest/kindnetd:v20260820-69b56db7`) |
| Demo image | `ros2k8s-demo-ros:local`, local image ID `sha256:b8f1c8b7c098...` (never pushed). Installed: `ros-jazzy-demo-nodes-cpp 0.33.11-1noble.20261105.092654`, `ros-jazzy-rmw-fastrtps-cpp 8.4.4-1noble.20260911.100940`, `ros-jazzy-fastrtps 2.14.7-1noble.20260911.035053`, `ros-jazzy-ros2-pulse 0.7.0-1noble.20261105.090619`, `stress-ng 0.17.06-1build1` |
| DDS setup | As in `ROS2/jazzy/talker-listener.yaml`: Fast DDS (`rmw_fastrtps_cpp`), `ROS_DOMAIN_ID=42`, Fast DDS Discovery Server through a ClusterIP Service |

## Nodes and placement

| Node | Role | Label `ros2k8s-demo/role` | Status |
|---|---|---|---|
| `ros2k8s-demo-control-plane` | control plane | - | Ready |
| `ros2k8s-demo-worker` | worker | `talker` | Ready |
| `ros2k8s-demo-worker2` | worker | `listener` | Ready |

| Pod (during `make verify`) | Node |
|---|---|
| `talker-79db666498-6qhvq` | `ros2k8s-demo-worker` |
| `listener-6789f67db8-x8b45` | `ros2k8s-demo-worker2` |
| `discovery-server-79d9cd87bb-s4qph` | `ros2k8s-demo-worker` |

So talker and listener ran on **different** workers, and the discovery server shared a worker with the talker. The negative test replaced the listener pod twice (`listener-749d978494-dnrff` while broken, `listener-6789f67db8-hmgwp` after the restore), both on `ros2k8s-demo-worker2`.

## Run manifest

[`run-manifest.json`](run-manifest.json) records this run in the [`run-manifest/v1`](../../../examples/run-manifest/README.md) format with `data_kind: measured`. It hashes the kind config, the manifest, the Dockerfile, the verify script and the two logs. Checked with the module's validator (read-only use):

```sh
python3 examples/run-manifest/rm_validate.py kind/runs/2026-11-20-main-e45de7a/run-manifest.json                     # 1 valid, exit 0
python3 examples/run-manifest/rm_validate.py --verify-hashes . kind/runs/2026-11-20-main-e45de7a/run-manifest.json   # 1 valid, exit 0
```

The `x_notes` block explains what the v1 fields mean for this run. In short: v1 requires `timing.action_age_deadline` and an `action_space`, and neither applies to a chatter demo. They were filled with the verify stage timeout (120 s) and the message counter, with `actuation: none`. A valid manifest checks shape and consistency only; it does not prove the values.

## Limits

- **n = 1.** One run on one host. No statistical claim, no latency or throughput.
- The discovery server ran on the talker's worker, so the talker reached it on the same node and the listener reached it across nodes. Other placements were not tried.
- QoS in the manifest: `RELIABLE` and `VOLATILE` were read with `ros2 topic info -v /chatter` from a local talker in a **rebuilt** copy of the image (one container, not the cluster). That rebuild had a different image ID, so it is not byte-identical to the image used in the cluster. `keep_last` with depth 10 is taken from the upstream demo source, not observed (`ros2 topic info` reports history depth as `UNKNOWN`).
- The GitHub Actions workflow for the kind demo was not run or checked for this record.
