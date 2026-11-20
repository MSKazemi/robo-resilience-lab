# Immutable Hugging Face revision pins with an offline cache check (`hf-revision-pins/v1`)

> **SYNTHETIC and offline.** This directory defines a pin-file format for Hugging Face model and dataset revisions, a validator that rejects mutable references, a resolver that maps pins to the on-disk cache layout `huggingface_hub` uses, and a checker that verifies cached files by sha256. Every repo id, revision and file shipped here is **invented**: `rrl-synthetic/*` repos and their commit hashes are not real Hugging Face repos or commits, and the "weights" are a few lines of text. **Nothing is downloaded, no model is loaded, no ROS 2, Kubernetes, GPU or robot is involved, and no credentials are needed or read.**

Addresses part of issue [#58](https://github.com/MSKazemi/robo-resilience-lab/issues/58) (umbrella [#53](https://github.com/MSKazemi/robo-resilience-lab/issues/53)). Python 3 standard library only, CPU only. It does **not** import `huggingface_hub` and opens no network connection (a test patches `socket` to fail and checks that the cache check still passes). The test suite runs on its own; it is **not yet wired into** `scripts/ci/run-offline-checks.sh`.

## Commands (run from the repository root)

```sh
# 1. Validate pin files (exit 0 all valid, 1 at least one invalid, 2 usage or I/O error)
python3 examples/hf-revision-pins/hfpins.py validate examples/hf-revision-pins/fixtures/pins/synthetic-valid.json
python3 examples/hf-revision-pins/hfpins.py validate examples/hf-revision-pins/fixtures/pins/invalid/mutable-main.json   # exit 1
python3 examples/hf-revision-pins/hfpins.py validate --release examples/hf-revision-pins/fixtures/pins/synthetic-valid.json  # exit 1: synthetic
python3 examples/hf-revision-pins/hfpins.py --list-codes

# 2. Build a SYNTHETIC cache in a throwaway directory, then check it
CACHE="$(mktemp -d)/hub"
python3 examples/hf-revision-pins/hfpins.py seed-synthetic examples/hf-revision-pins/fixtures/pins/synthetic-valid.json \
  --source examples/hf-revision-pins/fixtures/synthetic-files --cache-dir "$CACHE"
python3 examples/hf-revision-pins/hfpins.py resolve examples/hf-revision-pins/fixtures/pins/synthetic-valid.json --cache-dir "$CACHE"
HF_HUB_OFFLINE=1 python3 examples/hf-revision-pins/hfpins.py check examples/hf-revision-pins/fixtures/pins/synthetic-valid.json \
  --cache-dir "$CACHE" --require-offline-env

# 3. Regenerate or check the committed SYNTHETIC pin fixtures
python3 examples/hf-revision-pins/fixture_gen.py --check

# 4. Tests (stdlib unittest)
python3 -m unittest discover -s examples/hf-revision-pins/tests -t examples/hf-revision-pins
```

Example output of the mutable-ref case (exit code 1):

```text
FAIL 'examples/hf-revision-pins/fixtures/pins/invalid/mutable-main.json'
  [REVISION_BRANCH] /pins/0/revision: 'main' is a branch and moves on every push; pin the commit hash instead
0 valid, 1 invalid (hf-revision-pins/v1, default mode)
```

## What is in here

| Path | Role |
|---|---|
| `hf_pins.py` | Pin-file contract, strict JSON parsing, validator with typed diagnostics |
| `hf_cache.py` | Cache-directory resolution, pin to cache-path resolver, sha256 checker, synthetic seeder |
| `hfpins.py` | Command line (`validate`, `resolve`, `check`, `seed-synthetic`, `--list-codes`) |
| `fixture_gen.py` | Writes `fixtures/pins/` from `fixtures/synthetic-files/`; `--check` fails on drift |
| `fixtures/synthetic-files/` | Four tiny SYNTHETIC files, one folder per pin id |
| `fixtures/pins/synthetic-valid.json` | Valid SYNTHETIC pin file: one model, one dataset, with file sha256s |
| `fixtures/pins/synthetic-no-hashes.json` | Valid in default mode, rejected in release mode (no file hashes) |
| `fixtures/pins/invalid/` | Mutable-ref and placeholder-license examples, with `expected_codes.json` |
| `tests/` | `unittest` suite: positive, negative, hostile-input, tamper and fail-closed tests |

## Pin-file format

A pin file is one strict JSON object (duplicate keys, `NaN` and `Infinity` are parse errors; files over 1 MiB are refused).

| Field | Required | Rule |
|---|---|---|
| `schema_version` | yes | exactly `hf-revision-pins/v1` |
| `data_kind` | yes | `synthetic` or `real`. Release mode rejects `synthetic` |
| `description` | no | string |
| `pins[]` | yes | 1 to 256 pins |
| `pins[].id` | yes | local lowercase slug, unique in the file (`tiny-policy`) |
| `pins[].repo_id` | yes | `name` or `namespace/name`, the same character rules as `huggingface_hub.utils.validate_repo_id` (ASCII letters, digits, `-`, `_`, `.`; at most 96 characters per part; no `--`, `..`, `.git` suffix or leading/trailing `-`/`.`) |
| `pins[].kind` | yes | `model` or `dataset` (Spaces are out of scope) |
| `pins[].revision` | yes | **exactly 40 lowercase hex characters**, a full commit hash |
| `pins[].license` | yes | the license identifier the repo declares (`apache-2.0`, `mit`, `cc-by-4.0`, `other`, ...). Placeholders such as `unknown`, `none`, `tbd` are rejected |
| `pins[].gated` | no | `true` if the repo requires accepting terms (and therefore a token to prefetch) |
| `pins[].files[]` | no (yes in release mode) | the files the experiment uses. Each: `path` (safe relative POSIX path), `sha256` (64 lowercase hex), optional `size` (bytes), optional `role` (`weights`, `config`, `processor`, `tokenizer`, `data`, `metadata`, `other`) |

The same `(kind, repo_id)` may be pinned only once per file, so an experiment cannot silently load two revisions of one model.

### Why a 40-hex commit and nothing else

`huggingface_hub` treats a revision that matches `[0-9a-f]{40}` as a commit hash and everything else as a ref name that the Hub resolves at download time (`REGEX_COMMIT_HASH` in `huggingface_hub/file_download.py`). Branches (`main`), tags (`v1.0`) and pull-request refs (`refs/pr/3`) can all point at different content tomorrow. An abbreviated hash is not accepted either: the cache folder is named by the full hash, and a prefix is not guaranteed to stay unique. A syntactically valid hash still says nothing about whether that commit exists; only a prefetch (online) or the cache check (offline) can show that.

### Validator codes

Every problem is a diagnostic with a stable code and a JSON-pointer path. The validator never raises on bad input, keeps going after the first problem (up to 200 diagnostics, then `TRUNCATED`), and turns any internal bug into `INTERNAL_ERROR`, which is invalid.

| Code | Meaning |
|---|---|
| `PARSE_ERROR` | not strict JSON, not UTF-8, too large, or unreadable |
| `TYPE_ERROR` | a field has the wrong JSON type |
| `MISSING_FIELD` | a required field is absent |
| `UNKNOWN_FIELD` | a field outside the contract (catches typos such as `revison`) |
| `SCHEMA_VERSION` | wrong `schema_version` |
| `DATA_KIND` | `data_kind` is not `synthetic` or `real` |
| `NO_PINS` | `pins` is empty |
| `TOO_MANY` | more than 256 pins or 4096 files in one pin |
| `PIN_ID` | `id` is not a lowercase slug |
| `DUPLICATE_PIN_ID` | two pins share an `id` |
| `DUPLICATE_REPO` | the same `(kind, repo_id)` is pinned twice |
| `REPO_ID` | not a valid Hugging Face repo id |
| `KIND` | `kind` is not `model` or `dataset` |
| `REVISION_MISSING` | `revision` absent, empty or not a string |
| `REVISION_BRANCH` | a branch name: `main`, `master`, `dev`, `refs/heads/...` and similar |
| `REVISION_PR_REF` | a `refs/pr/N` pull-request ref |
| `REVISION_SHORT_SHA` | an abbreviated commit hash (fewer than 40 hex characters) |
| `REVISION_UPPERCASE` | 40 hex characters with uppercase letters |
| `REVISION_NOT_COMMIT` | any other ref name, such as a tag (`v1.0`), a 64-hex string, or a hash with whitespace |
| `LICENSE` | license missing, malformed or a placeholder |
| `FILES_EMPTY` | `files` present but empty |
| `FILE_PATH` | absolute, `..`, empty segment, backslash, drive letter or control character |
| `DUPLICATE_FILE` | the same path listed twice in one pin |
| `SHA256` | not 64 lowercase hex characters |
| `SIZE` | not a non-negative integer |
| `ROLE` | not one of the documented roles |
| `RELEASE_NO_FILE_HASHES` | release mode: a pin lists no file hashes |
| `RELEASE_SYNTHETIC` | release mode: `data_kind` is `synthetic` |
| `TRUNCATED` | more than 200 diagnostics |
| `INTERNAL_ERROR` | validator bug, reported as invalid (fail closed) |

## Cache layout and resolver

The resolver maps a pin to the layout `huggingface_hub` writes (see the [cache guide](https://huggingface.co/docs/huggingface_hub/guides/manage-cache)):

```text
<cache_dir>/
  models--rrl-synthetic--tiny-policy-fixture/
    blobs/<content id>
    refs/main
    snapshots/da9e025ec4303c3fd76795c8dd1c7837a655c812/config.json  -> ../../blobs/<content id>
  datasets--rrl-synthetic--tiny-episodes-fixture/
    snapshots/e58c656f91505f7a1ed6f60fb456940e364149e8/data/episodes.jsonl
```

The folder name is `<kind>s--` followed by the repo id with `/` replaced by `--`. A pin resolves **only** to `snapshots/<revision>/<path>`. `refs/` is ignored on purpose: `refs/main` is the mutable pointer a pin exists to avoid, and a test shows it cannot stand in for a missing snapshot.

The cache directory is, in order: `--cache-dir`, `HF_HUB_CACHE`, the legacy `HUGGINGFACE_HUB_CACHE`, `HF_HOME/hub`, `XDG_CACHE_HOME/huggingface/hub`, `~/.cache/huggingface/hub`. This mirrors `huggingface_hub/constants.py`.

`seed-synthetic` writes this layout for **synthetic** pin files only. Blobs are named by sha256 (as `huggingface_hub` does for LFS-stored files; for other files it uses the ETag the Hub returns). The checker never relies on blob names, only on file content. Snapshot entries are relative symlinks unless `--copy` is given or symlinks are unavailable. It refuses `data_kind: real`, invalid pin files, sources that do not match their pinned sha256, and existing repo folders.

### Compatibility evidence (one-off, not part of the tests)

On 2026-11-20, during development, a cache produced by `seed-synthetic` was read with a locally available `huggingface_hub` 1.33.0 with `HF_HUB_OFFLINE=1`. `try_to_load_from_cache` returned the expected snapshot path for all 4 files, `hf_hub_download` returned the cached path for one file per repo without network access, `scan_cache_dir` listed both repos with their single revision and no warnings, and `hf_hub_download` raised `LocalEntryNotFoundError` (no download attempt) for an absent file and for an uncached repo. The repo-id rule in `hf_pins.py` agreed with `validate_repo_id` on 25 hand-picked valid and invalid ids, and `repo_folder_name` agreed on 3 cases. The test suite does **not** depend on `huggingface_hub` and does not repeat this check; other `huggingface_hub` versions were not tried.

## Cache checker

`hfpins.py check` (or `hf_cache.check_document`) first validates the pin file. An invalid pin file is reported as `PIN_INVALID` and the cache is **not inspected**. Otherwise every pin is checked: the repo folder exists, the pinned snapshot exists, and each pinned file exists, is a regular file inside the repo folder, has the pinned size and has the pinned sha256. The checker never writes to the cache, so it works on a read-only mount.

| Code | Meaning |
|---|---|
| `PIN_INVALID` | the pin file does not validate; the cache was not inspected |
| `CACHE_DIR_MISSING` | the cache directory does not exist |
| `CACHE_REPO_MISSING` | no folder for this repo (never prefetched, or wrong `kind`) |
| `CACHE_REVISION_MISSING` | the repo folder has no snapshot for the pinned revision |
| `CACHE_REVISION_MISMATCH` | the cache holds other revisions of this repo but not the pinned one (the message lists up to 5) |
| `CACHE_FILE_MISSING` | a pinned file is absent, or a dangling symlink |
| `CACHE_NOT_REGULAR_FILE` | the pinned path is a directory or special file |
| `CACHE_PATH_ESCAPE` | the path resolves outside the repo's cache folder; refused even if the bytes match |
| `CACHE_SIZE_MISMATCH` | file size differs from the pinned size |
| `CACHE_SHA256_MISMATCH` | content differs from the pinned sha256 (tampered or wrong file) |
| `CACHE_READ_ERROR` | the file could not be read, or the checker hit an internal error (fail closed) |
| `OFFLINE_ENV_NOT_SET` | with `--require-offline-env`: `HF_HUB_OFFLINE` (or `TRANSFORMERS_OFFLINE`) is not `1`/`ON`/`YES`/`TRUE` |
| `UNVERIFIED_NO_FILES` | **warning**, not an error: a pin lists no files, so only the snapshot folder was checked. Release mode turns this into `RELEASE_NO_FILE_HASHES` |

`--release` is the release-grade gate: `data_kind` must be `real`, every revision must be a full commit hash, and every pin must list file sha256s that all verify.

## Using it with real assets (operator workflow, not run in this repository)

The steps below describe the intended use. **None of them was run for this change**; they need network access and real Hugging Face repos.

1. On a trusted machine with network access, find the commit you want, for example `HfApi().model_info("<org>/<model>", revision="main").sha` (or `dataset_info` for datasets), and write it into the pin file. For LFS-stored files the Hub reports a sha256 per file (`model_info(..., files_metadata=True)`); for other files, hash them after download.
2. Prefetch exactly that revision with `huggingface_hub.snapshot_download(repo_id, revision=<commit>, repo_type=..., allow_patterns=[...], cache_dir=...)`. Use only public assets, or a gated asset you have accepted the terms for.
3. Run `hfpins.py check --release <pins.json> --cache-dir <dir>` on the prefetched cache.
4. Mount that cache **read-only** where the workload runs, set `HF_HUB_OFFLINE=1` and `HF_HUB_CACHE` to the mount, and run `hfpins.py check --release --require-offline-env` as a start-up gate. With `HF_HUB_OFFLINE=1`, `huggingface_hub` raises (`LocalEntryNotFoundError` in the one-off check above) instead of downloading, so a missing object fails loudly instead of silently fetching `main`.

## Security and licensing checklist

- [ ] Every revision is a 40-hex commit hash; `hfpins.py validate --release` passes.
- [ ] Every pin lists the files the workload loads, with sha256 values; `hfpins.py check --release` passes on the exact cache that will be mounted.
- [ ] `license` matches the license the repo declares, and that license allows your use (research, redistribution, derived weights). The tool checks only that a non-placeholder identifier is present, **not** that it is correct.
- [ ] Gated repos are marked `gated: true`; the terms were accepted by a person, and nothing in the repo or image bakes in the token.
- [ ] Tokens (`HF_TOKEN`) come only from the prefetching operator's local environment, never from a pin file, manifest, image, ConfigMap or log. The workload that reads the offline cache needs no token at all. These tools never read token variables (a test sets a sentinel `HF_TOKEN` and checks it never appears in the output).
- [ ] The cache is mounted read-only in the workload, and the volume has a size limit sized from the pinned files (the sum of `size` values is a lower bound; the cache also stores `refs/` and metadata).
- [ ] The container image digest and source commit are recorded next to the pins, for example in a [`run-manifest/v1`](../run-manifest/README.md) manifest, whose `model` and `dataset` revision fields use the same 40-hex rule. The image's `huggingface_hub` version is recorded, because the cache layout is a `huggingface_hub` convention, not a stable standard.
- [ ] No private sensor data, robot addresses or unlicensed assets are pinned in this public repository.

## What this does and does not show

It shows, by the unit tests: mutable references are rejected with specific codes; a valid synthetic cache passes; an empty cache, a missing revision, a different cached revision, a missing file, a dangling symlink, a symlink escaping the repo folder, a size change and a same-size byte flip each fail with their own code; the checker fails closed on invalid pins, unreadable files and internal errors; no socket is opened.

It does **not** show:

- that any real Hugging Face repo, commit or file works with this tool (none was used);
- that a real prefetch, a real offline model load or a Kubernetes read-only mount works (none was run);
- that a `license` value is correct, or that a gated model's terms were accepted;
- anything about other cache formats (for example `local_dir` downloads, Xet storage internals, `.no_exist/` entries or Windows caches without symlinks beyond the `--copy` path);
- robustness to an attacker who can rewrite both the pin file and the cache. The pin file is the trust anchor: keep it under version control and review changes to it.

Remaining parts of #58 that are not done here: a prefetch tool, the online prefetch test, the end-to-end offline lane with a real tiny public fixture, and Kubernetes manifests for the read-only cache volume.
