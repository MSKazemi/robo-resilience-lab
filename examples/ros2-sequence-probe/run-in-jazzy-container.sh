#!/usr/bin/env bash
# Run the sequence-probe rclpy adapters inside ONE Jazzy container (publisher and subscriber as two
# processes, --network none, so DDS traffic stays on the container's loopback / shared memory).
# This is the scope of RESULTS-2026-11-20-jazzy-container.md. It says nothing about
# cross-container, cross-node, kind or Kubernetes DDS delivery, latency, jitter or throughput.
#
# Usage: run-in-jazzy-container.sh IMAGE OUT_DIR [CASE...]
#   CASE: ok-string ok-bytes sub-sigterm sub-sigint pub-sigterm (default: all)
# Writes OUT_DIR/<case>/{pub,sub}.jsonl, {pub,sub}.stderr and rc.txt, then runs
# `python3 -m seqprobe analyze` on the host (standard library only) and prints its exit code.
set -euo pipefail

if (( $# < 2 )); then
  echo "usage: $0 IMAGE OUT_DIR [CASE...]" >&2
  exit 2
fi
image=$1
out_root=$2
shift 2
cases=("$@")
if (( ${#cases[@]} == 0 )); then
  cases=(ok-string ok-bytes sub-sigterm sub-sigint pub-sigterm)
fi

probe_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$out_root"
out_root=$(cd "$out_root" && pwd)

# Body executed inside the container; CASE, COUNT and RATE come from the environment.
inner='
source /opt/ros/jazzy/setup.bash
set -u  # after sourcing: setup.bash reads unset variables
export PYTHONDONTWRITEBYTECODE=1
sub_args=(--run-id "$CASE" --expected-count "$COUNT" --msg-type "$MSG_TYPE"
          --reliability reliable --depth 10 --idle-timeout-s 3 --log-file /out/sub.jsonl)
pub_args=(--run-id "$CASE" --count "$COUNT" --rate-hz "$RATE" --payload-bytes 64
          --msg-type "$MSG_TYPE" --reliability reliable --depth 10 --log-file /out/pub.jsonl)
python3 /probe/rclpy_adapters/subscriber_node.py "${sub_args[@]}" 2>/out/sub.stderr &
sub=$!
python3 /probe/rclpy_adapters/publisher_node.py "${pub_args[@]}" 2>/out/pub.stderr &
pub=$!
case "$CASE" in
  sub-sigterm) sleep 4; kill -TERM "$sub" ;;
  sub-sigint)  sleep 4; kill -INT "$sub" ;;
  pub-sigterm) sleep 4; kill -TERM "$pub" ;;
esac
wait "$pub"; pub_rc=$?
wait "$sub"; sub_rc=$?
printf "pub_rc=%s sub_rc=%s\n" "$pub_rc" "$sub_rc" | tee /out/rc.txt
'

domain=40
for case_name in "${cases[@]}"; do
  case "$case_name" in
    ok-string|sub-sigterm|sub-sigint|pub-sigterm) msg_type=string ;;
    ok-bytes) msg_type=bytes ;;
    *) echo "unknown case: $case_name" >&2; exit 2 ;;
  esac
  out="$out_root/$case_name"
  mkdir -p "$out"
  domain=$((domain + 1))
  echo "=== $case_name (msg_type=$msg_type, ROS_DOMAIN_ID=$domain)"
  docker run --rm --network none --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -e CASE="$case_name" -e COUNT=40 -e RATE=10 -e MSG_TYPE="$msg_type" \
    -e ROS_DOMAIN_ID="$domain" \
    -v "$probe_dir:/probe:ro" -v "$out:/out" \
    "$image" bash -c "$inner"
  set +e
  (cd "$probe_dir" && python3 -m seqprobe analyze "$out/sub.jsonl" > "$out/analyze.json")
  analyze_rc=$?
  set -e
  echo "analyze_rc=$analyze_rc" | tee -a "$out/rc.txt"
done
