# Coding-agent implementation handoff: ROS 2 Kubernetes × Hugging Face

> **Status:** proposed work packages. This is an agent-friendly execution guide, **not** a set of implemented CLI commands. Read [AGENTS.md](../AGENTS.md), [the integration roadmap](PROGRAM-ROADMAP-2026-2027.md), [the experiment portfolio](EXPERIMENT-PORTFOLIO.md), the **live** issue, and any overlapping PR before writing code. Preserve existing historical data and provenance. One child issue / one bounded pull request.

> **Status check, 2026-11-20** (PR and issue states read with `gh api`; the live GitHub state wins if it has changed since). Already merged on `main`: PR [#97](https://github.com/MSKazemi/robo-resilience-lab/pull/97) kind demo (2026-10-31, Wave 0A); PRs [#36](https://github.com/MSKazemi/robo-resilience-lab/pull/36), [#91](https://github.com/MSKazemi/robo-resilience-lab/pull/91), [#50](https://github.com/MSKazemi/robo-resilience-lab/pull/50), [#51](https://github.com/MSKazemi/robo-resilience-lab/pull/51) and [#72](https://github.com/MSKazemi/robo-resilience-lab/pull/72) (Wave 0B); W1 via PR [#113](https://github.com/MSKazemi/robo-resilience-lab/pull/113) (issue #55 closed 2026-11-20); W2 via PR [#128](https://github.com/MSKazemi/robo-resilience-lab/pull/128) and W3 via PR [#127](https://github.com/MSKazemi/robo-resilience-lab/pull/127) (both merged 2026-11-20; issues #59 and #60 stay open). PR [#129](https://github.com/MSKazemi/robo-resilience-lab/pull/129) added the synthetic W5 casebook and rule baseline for #84 only; the lexical baseline and any Hugging Face classifier are still open. Still open: PRs #41, #76, #101 and #105. PR #102 no longer resolves on GitHub (HTTP 404 on 2026-11-20). All merged modules are SYNTHETIC or SIMULATED: none of them is a measured result.

## Shared definition of done for every work package

- [ ] State exact repo/branch head, issue number, version pin, inputs/outputs, tests and non-goals.
- [ ] Run fast offline tests that require no secret, model download, Docker privileges, live cluster, physical robot or paid service.
- [ ] Provide a sample synthetic fixture and its license/provenance; do not claim synthetic data represents robotics reality.
- [ ] Guard against expired, repeated, malformed, unauthorized actions; use simulation sink **only** for any robot command.
- [ ] Any optional integration requiring credentials/network/cloud is disabled by default, opt-in, documented, and never exercised from untrusted PRs.
- [ ] Mark each feature status correctly: `proposed`, `implemented/contract-tested`, `container-tested`, `cross-worker-verified`, `benchmarked`, or `published/reviewed`.
- [ ] All new results have reproducible raw data + run manifest. Never replace null entries on the public site with speculative statistics.
- [ ] Human reviews diff, licensing, safety and measurements before merging to `main`.

## Wave 0 — integrate already-open work (human review)

**0A — Cross-worker reference demo, issue #14/#16, PR #97 (merged 2026-10-31)**

Do NOT start a second kind profile. PR #97 is merged; the profile is documented in [KIND-DEMO.md](KIND-DEMO.md). [CI run 40587051848](https://github.com/MSKazemi/robo-resilience-lab/actions/runs/40587051848) (on the PR branch, before the merge) shows actual cross-worker ROS message delivery and a deliberate discovery-failure test with recovery on a hosted amd64 kind cluster. This **does not** validate the Vagrant profile, other network conditions or any model inference. The review points below were written before the merge; keep them for follow-up work on the merged `Makefile`, `kind/ros2k8s-demo.yaml`, ROS 2 image pin, Kubernetes pod placement, Fast DDS Discovery Server and `scripts/verify-ros2.sh` carefully. Ask for actual two-worker test evidence from CI/run logs, negative verification case, CPU/RAM resource requirements, network/port permissions and cleanup behavior. Mark incomplete original Vagrant-specific acceptance conditions as open.

**0B — Documentation reconciliation, PR #91, #36, #50, #51, #72, #76, #101, #105** (#91, #36, #50, #51 and #72 merged; #76, #101 and #105 open)

Order: small corrections first, historical strategies for reference, then broad research documentation, then Jev/HF website after checking overlapping `README.md`, Pages workflow, scenario index and sitemap. Prefer a single canonical research program navigation; do not replace source-of-truth with cloned docs. PR #41 is conflicting, rebase. PR #102 (a mistitled change for RAG issue #65) no longer resolves on GitHub; #65 has no implementing PR, so do not treat it as covered.

## Wave 1 — three safe first-code PRs

### W1 / PR for #55: typed synthetic ROS 2 observation/action contract

*Done:* PR #113 (merged 2026-10-31) added [`examples/cloud-robotics/mock-policy/`](../examples/cloud-robotics/mock-policy/README.md); issue #55 was closed on 2026-11-20. The ROS 2 Jazzy adapter is not part of it.

**Goal:** one deterministic, non-ROS-dependent proof that a mock perception state can be transformed into a typed action proposal for a **simulated** sink. Define versioned JSON schema(s); fixtures `valid`, `missing_camera`, `wrong_state_units`, `nan`, `late`, `replayed`, `stale_revision` as applicable. No `kubectl`, model weights or actuators. Separate schema validation from mock policy.

**Acceptance:** stdlib-first schema validator (or explicitly declared test dependency), reproducible fake policy output, negative tests that fail for wrong versions/fields, documented units and clocks. A valid JSON string alone is insufficient.

### W2 / PR for #59: deterministic reject-on-failure gate

*Merged:* PR #128 (2026-11-20) added [`examples/cloud-robotics/action-gate/`](../examples/cloud-robotics/action-gate/README.md), a simulated gate with a deterministic fault suite. Issue #59 is still open; check its remaining checkboxes before adding to it.

**Goal:** consume W1 proposals and allow/deny only **simulated** dispatch. Enforce finite numbers, exact names/units, value bounds, observation TTL, monotonically advancing sequence/epoch, allowed task ids, authorization and per-action expiry. Reject no-op/hold behaviors explicitly; no implicit fallback to last remote action. Include partition/stale-response tests.

**Acceptance:** tests for TTL, clock skew assumptions, replayed seq, unauthorized model, bad units, failure closure, empty action, and no output side effect. Report actual test invocations; no claim that a simulated gate certifies physical safety.

### W3 / PR for #60: canonical run manifest and offline validation

*Merged:* PR #127 (2026-11-20) added [`examples/run-manifest/`](../examples/run-manifest/README.md) (contract, validator and synthetic summaries). Issue #60 is still open.

**Goal:** minimal JSON Schema and validator for baseline/seed/source SHA/ROS/RMW/network/resource/model+dataset pins/rights/timestamps/raw-artifact hashes, with allowed **mock** case where model ids remain null. Connect to existing result-status registry #88 without inventing a second schema.

**Acceptance:** valid/invalid example manifests, negative tests, deterministic status; a static HTML animation and unreviewed model result must fail publication readiness.

## Wave 2 — first Hugging Face executable baseline

### W4 / PR for #58: immutable Hub artifact cache

**Goal:** offline-first `repo_id + exact commit revision + file hashes + license`; avoid floating `main` or unreviewed `trust_remote_code`. Optional download only in explicit user-run setup, not default CI. Validate cache miss/invalid hash/processor shape mismatch.

**Acceptance:** 100% of recorded HF runs point to immutable commit SHA; CI can test manifest parser with tiny synthetic fixtures and no network.

### W5 / PR for #74 and follow-up #94: read-only fault triage classifier

*Partly merged:* PR #129 (2026-11-20, issue #84) added [`examples/ros2-fault-triage-casebook/`](../examples/ros2-fault-triage-casebook/README.md): a SYNTHETIC casebook and a rule baseline. The lexical baseline, the pinned HF classifier and the measured comparison below are not implemented.

**Goal:** one deterministic family-held-out synthetic incident casebook, rules baseline, simple local lexical baseline, optional cached/pinned HF classifier. Jev adapter remains opt-in and **not** a required part of the science.

**Acceptance:** public split manifest, no leakage, per-class counts, macro-F1, abstention/coverage, p50/p95 latency, raw predictions and a reviewer-readable run artifact. No real remediation or `kubectl` mutation. Register validated results only after #88 review.

## Wave 3 — infrastructure/robot-learning integrations (separate PRs)

### W6 / PR for #56: LeRobotDataset v3 synthetic-action converter

Create/validate exactly one tiny dataset with synchronized **state, camera, action, timestamp and task**. Pin LeRobot release and validate round-trip by loading. Do **not** submit sensor-only rosbag as an action-policy training dataset. Optional Hub upload manual/permission-gated.

### W7 / PR for #61: compatible upstream remote inference server

Only after #57 bridge/embodiment compatibility and #55/#58/#59/#60; reuse upstream LeRobot Zenoh policy server and supported robot/policy contract. No new private RPC protocol. First test simulated client inside isolated local cluster, then measure packet-loss/jitter effects (#62/#96). Never attempt physical actuation.

### W8 / PR for #106: licensed perception benchmark

Start with a no-network fake adapter + annotations and metric tests; then optional pinned Grounding DINO/Depth Anything weights in permitted GPU/CPU environment. Separate relative depth from metric depth and preserve annotation rights.

### W9 / PR for #107: model-serving fairness simulator

Discrete-event CPU-only model-cache/queue simulation and baselines (FIFO/EDF/fair queue); after tests, optional GPU validation. Report worst-client deadline-success and distribution, not only aggregate throughput.

### W10 / research protocol for #104: safe task continuity

Define portable **high-level mission state**, lease epochs/fencing, read-only provenance receipts and simulated task sink. Do not confuse copying a container, model deployment, robot migration and task-state continuity.

## Copyable prompt for Claude Code / Codex (choose one issue only)

```text
Repository: MSKazemi/robo-resilience-lab (public).
Read AGENTS.md, docs/README.md, docs/PROGRAM-ROADMAP-2026-2027.md,
docs/EXPERIMENT-PORTFOLIO.md and the full live GitHub issue before coding.
Work ONLY on issue #58 (immutable Hugging Face artifact manifest and offline cache).
First inspect existing main and open PRs for overlapping changes, including
examples/run-manifest/ (model and dataset revision fields already exist there).
Do not duplicate model-server, HF website or scenario implementations and do not rename the repo.
Respond first with: (1) current observed code/files, (2) minimal plan, (3)
manifest schema and trust boundary, (4) tests and expected acceptance,
(5) files that will change. Then implement only the accepted scope.

Requirements:
- CPU only; Python standard library preferred; no cloud, GPU, robot, secret or HF token.
- Record repo_id + exact commit revision + file hashes + license; reject floating
  `main` revisions and unreviewed `trust_remote_code`.
- Offline-first: CI parses tiny synthetic manifests with no network; any download
  happens only in an explicit, user-run setup step.
- Tests for cache miss, invalid hash and processor shape mismatch.
- Update adjacent docs and preserve historical Foxy/Vagrant examples.
- Give actual test commands/output, limits and compatibility state.
- Open a focused PR for human review. Never self-merge and never invent results.
```

For other issues, replace `#58` and the specific requirements with the relevant wave description. Check the live issue first: #55 is closed, and #59, #60 and #84 already have merged modules. Do not paste general program text into dozens of issue bodies; link this file and the canonical experiment spec instead.
