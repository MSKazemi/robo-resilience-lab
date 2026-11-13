"""Constants of the run-manifest contract ``run-manifest/v1`` (stdlib only).

Data only: no ROS, Kubernetes, model or network code is imported or called.
The contract describes the *metadata that makes one experiment run
reproducible and auditable*; it does not run anything. Every example shipped
in this directory is SYNTHETIC.
"""

from __future__ import annotations

import re

SCHEMA_VERSION = "run-manifest/v1"
RUNFILE_VERSION = "run-metrics/v1"
REGISTRY_VERSION = "metrics-registry/v1"
REPORT_VERSION = "rm-report/v1"

DATA_KINDS = ("synthetic", "simulated", "measured")
RUN_STATUSES = ("completed", "failed", "timed_out", "aborted")
CLOCK_BASES = ("wall", "monotonic")
CLOCK_DOMAINS = ("single_clock", "synchronized", "unsynchronized")
SYNC_METHODS = ("ptp", "ntp", "chrony", "gps", "simulation_clock", "other")
TIERS = ("edge", "cloud", "local")
RELIABILITY = ("reliable", "best_effort")
DURABILITY = ("volatile", "transient_local")
HISTORY = ("keep_last", "keep_all")
ARCHITECTURES = ("x86_64", "aarch64", "riscv64", "other")
MEASUREMENT_KINDS = (
    "ros2_tracing",
    "application_log",
    "simulator_log",
    "prometheus",
    "synthetic_generator",
    "other",
)
DATA_HASH_ROLES = ("input", "config", "output")

# Quantity kinds: unit -> factor to the base unit (s, Hz, GiB, bit/s, fraction).
UNITS = {
    "time": {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9},
    "rate": {"Hz": 1.0},
    "memory": {"GiB": 1.0, "MiB": 1.0 / 1024.0},
    "bandwidth": {"kbit/s": 1e3, "Mbit/s": 1e6, "Gbit/s": 1e9},
    "ratio": {"fraction": 1.0, "percent": 0.01},
}

MAX_QUANTITY = 1e12  # absurdly large values are rejected, not clamped silently
MAX_SEED = 2**63 - 1
MAX_COUNT = 10**9
MAX_FILE_BYTES = 1024 * 1024
MAX_DIAGNOSTICS = 200
MAX_DEPTH = 40
MAX_NODES = 100_000
MAX_LIST = 256
MAX_TEXT = 128

# ASCII-only patterns; always used with ``fullmatch`` (never ``match`` + ``$``).
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+~-]{0,63}")
K8S_RE = re.compile(r"[0-9]+\.[0-9]+(\.[0-9]+)?([-+][0-9A-Za-z.+-]{1,32})?")
TEXT_RE = re.compile(r"[ -~]{1,%d}" % MAX_TEXT)
TOPIC_RE = re.compile(r"/[A-Za-z0-9_]+(/[A-Za-z0-9_]+){0,15}")
SPDX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}")
UNIT_TEXT_RE = re.compile(r"[A-Za-z0-9/^*._-]{1,16}")
PATH_RE = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+){0,15}")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
OCI_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]{1,6})?Z"
)
TS_OTHER_ZONE_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?([+-][0-9]{2}(:?[0-9]{2})?)?"
)

# Whole-value matches only (case-insensitive): licence/id placeholders.
UNDECLARED_LICENSES = frozenset(
    {"unknown", "none", "other", "unlicensed", "tbd", "todo", "n/a", "na", "null", "noassertion", ""}
)
PLACEHOLDER_IDS = frozenset(
    {"todo", "tbd", "none", "null", "nil", "n/a", "na", "changeme", "placeholder", "xxx", "example", "test", "unknown"}
)

# Heuristic secret/address detection. Substring search is correct here (a token
# may sit inside a longer string); this is NOT a substitute for a secret scanner.
SECRET_VALUE_RES = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
)
K8S_JOIN_TOKEN_RE = re.compile(r"[a-z0-9]{6}\.[a-z0-9]{16}")  # whole-value only
SECRET_KEY_RE = re.compile(r"password|passwd|secret|token|credential|kubeconfig|api_?key|private_?key")
IPV4_RE = re.compile(r"(?<![0-9.])([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})(?![0-9])(?!\.[0-9])")

# Diagnostics that are warnings, not errors (a document with only warnings is valid).
WARNING_CODES = frozenset({"SOURCE_DIRTY"})

# code -> (what it means, how to fix it). Stable strings, safe to log.
CODES = {
    # file / document level
    "FILE_UNREADABLE": ("The file could not be read.", "Check the path and permissions."),
    "FILE_TOO_LARGE": ("The file exceeds the size bound.", "Manifests are small metadata files (<= 1 MiB)."),
    "MALFORMED_JSON": ("The file is not valid UTF-8 JSON.", "Fix the syntax error at the reported line/column."),
    "MALFORMED_DOCUMENT": ("The top-level value is not a JSON object.", "Wrap the manifest in one JSON object."),
    "SCHEMA_VERSION_UNKNOWN": (
        "schema_version is not supported by this validator.",
        "Use 'run-manifest/v1' or upgrade the validator; nothing else is checked for an unknown version.",
    ),
    "NESTING_TOO_DEEP": ("The document is nested deeper than the bound.", "Flatten the structure; manifests are shallow."),
    "TOO_MANY_NODES": ("The document has more JSON nodes than the bound.", "Manifests are small metadata files."),
    "INTERNAL_ERROR": ("The validator hit an unexpected internal error.", "Report this with the input; the document is treated as invalid."),
    # generic structure
    "MISSING_FIELD": ("A required field is absent.", "Add the field named in the path."),
    "UNKNOWN_FIELD": ("A field that is not part of the contract.", "Remove or fix the spelling; vendor extensions must start with 'x_'."),
    "FIELD_NOT_ALLOWED": ("A field is present that this configuration forbids.", "Remove it (for example details of a component that is not used)."),
    "DUPLICATE_KEY": ("A JSON object repeats a key.", "Keep one entry; duplicate keys silently drop data in most parsers."),
    "DUPLICATE_ENTRY": ("A list repeats an entry that must be unique.", "Remove or rename the duplicate."),
    "WRONG_TYPE": ("A value has the wrong JSON type.", "Use the type given in the schema (for example a scalar, not a list)."),
    "BOOL_AS_NUMBER": ("A boolean was given where a number is required.", "Use a number; true/false are not 1/0 here."),
    "ENUM_INVALID": ("The value is not one of the allowed choices.", "Use one of the values listed in the message."),
    "EMPTY_STRING": ("A string is empty.", "Provide a non-empty value."),
    "STRING_TOO_LONG": ("A string exceeds the length bound.", "Shorten it."),
    "STRING_INVALID": ("A string has characters outside printable ASCII.", "Use printable ASCII only."),
    "ID_MALFORMED": ("An identifier does not match [A-Za-z0-9][A-Za-z0-9._-]{0,63}.", "Use letters, digits, '.', '_' or '-' (max 64, no spaces)."),
    "VERSION_MALFORMED": ("A version string is malformed.", "Use a version such as '1.29.3' (ASCII, no spaces)."),
    "PLACEHOLDER_VALUE": ("A placeholder was given instead of a real value.", "Provide the real identifier or hash; never a stand-in such as all zeros or TODO."),
    "NON_FINITE_VALUE": ("NaN or Infinity found.", "Use finite numbers only; strict JSON has no NaN/Infinity."),
    "NEGATIVE_VALUE": ("A negative number where only >= 0 makes sense.", "Use a non-negative value."),
    "NON_POSITIVE_VALUE": ("Zero where a value > 0 is required.", "Use a positive value."),
    "VALUE_OUT_OF_RANGE": ("A number is outside the allowed range.", "Use a value inside the range given in the message."),
    "DIM_MISMATCH": ("A list length disagrees with the declared dimension.", "Make the lengths agree."),
    "TIME_ORDER": ("Timestamps or durations are in an impossible order.", "Check start/end, warmup/timeout."),
    # provenance
    "REVISION_MISSING": ("A source/model/dataset revision is absent.", "Record the exact commit hash (40 lowercase hex)."),
    "DIGEST_MISSING": ("An OCI image digest or data hash is absent.", "Record the sha256 digest of the exact image/data used."),
    "HASH_MALFORMED": ("A hash/commit is not lowercase hex of the required length.", "Use 40 hex for commits, 'sha256:'+64 hex for OCI digests, 64 hex for sha256."),
    "LICENSE_MISSING": ("A licence field is absent or empty.", "Add an SPDX identifier such as 'Apache-2.0'."),
    "LICENSE_UNDECLARED": ("The licence is a non-answer (unknown, none, ...).", "Declare the real licence, or do not use the asset."),
    "LICENSE_MALFORMED": ("The licence is not an SPDX-style identifier.", "Use an SPDX identifier (letters, digits, '.', '+', '-')."),
    "SOURCE_DIRTY": ("The source tree was dirty when the run was made.", "Warning only: say so in the report; prefer a clean tree for measured runs."),
    # units / time / clocks
    "UNIT_MISSING": ("A quantity has no unit annotation.", "Write {\"value\": <number>, \"unit\": \"<unit>\"}."),
    "UNIT_NOT_ALLOWED": ("The unit is not valid for this quantity.", "Use one of the units listed in the message."),
    "TIMESTAMP_MALFORMED": ("Not a valid UTC timestamp.", "Use YYYY-MM-DDTHH:MM:SS[.ffffff]Z (no trailing characters)."),
    "TIMESTAMP_NOT_UTC": ("The timestamp has no 'Z' or carries another zone.", "Convert to UTC and end with 'Z'."),
    "CLOCK_MISSING": ("Clock information is absent.", "Declare clock_basis (wall|monotonic) and clock_domain."),
    "CLOCK_SYNC_INCOMPLETE": (
        "The clock-synchronization fields do not match clock_domain.",
        "synchronized needs sync_method and estimated_max_offset; the other domains must not carry them.",
    ),
    "SIMULATION_TIME_MISMATCH": ("Simulation time is declared inconsistently.", "Check clocks.simulation_time against data_kind and simulator."),
    # run semantics
    "WARMUP_EXCEEDS_TIMEOUT": ("warmup is not shorter than timeout.", "Warmup must be < timeout."),
    "TIMEOUT_EXCEEDED": ("A completed run observed longer than its timeout.", "Check observed_duration, or mark the run timed_out."),
    "CENSORING_INCONSISTENT": ("status and censored disagree.", "timed_out/aborted runs are censored; completed runs are not."),
    "CENSOR_REASON_MISSING": ("A censored run has no reason.", "State why observation stopped early."),
    "FAILURE_COUNT_INCONSISTENT": ("A non-completed run has failure_count 0.", "A failed/timed-out/aborted run has at least one failure."),
    "TASK_SUCCESS_INCONSISTENT": ("task_success.applicable and value disagree.", "applicable=false needs value=null; applicable=true needs true/false."),
    "FAULT_INCONSISTENT": ("fault_injection fields disagree with 'applied'.", "applied=true needs kind and duration; applied=false forbids them."),
    "MEASUREMENT_KIND_MISMATCH": ("measurement_source.kind disagrees with data_kind.", "synthetic data uses 'synthetic_generator'; real data must not."),
    "SIMULATOR_REQUIRED": ("data_kind is 'simulated' but no simulator is declared.", "Declare the simulator or change data_kind."),
    # safety
    "ACTUATION_NOT_NONE": ("action_space.actuation is not 'none'.", "This repository only records non-actuating, simulated or offline runs."),
    "ADDRESS_IN_VALUE": ("An IPv4 address was found in a value.", "Use symbolic node names; never publish real robot or host addresses."),
    "SECRET_LIKE_VALUE": ("A value looks like a credential or token.", "Remove it and rotate it if it was real."),
    "SECRET_LIKE_KEY": ("A key name suggests a credential.", "Remove it; manifests never carry secrets."),
    "PATH_UNSAFE": ("A path is absolute, escapes upward or has odd characters.", "Use a relative path of [A-Za-z0-9._-] segments without '..'."),
    # data hash verification (opt-in)
    "HASH_FILE_MISSING": ("A hashed file does not exist under the base directory.", "Fix the path or the base directory."),
    "HASH_MISMATCH": ("The file's sha256 differs from the manifest.", "Regenerate the file or the manifest; do not edit hashes by hand."),
    # run files
    "RUNFILE_VERSION_UNKNOWN": ("run-file schema_version is not supported.", "Use 'run-metrics/v1'."),
    "METRIC_MISSING": ("A registry metric has no entry in the run file.", "Every metric needs an entry (use status not_computable/not_applicable, never omit)."),
    "UNKNOWN_METRIC": ("A metric id is not in the registry.", "Use ids from schema/metrics-registry.v1.json."),
    "STATUS_INVALID": ("The metric status is not allowed here.", "Use ok|censored|not_computable|not_applicable as the registry permits."),
    "VALUE_STATUS_MISMATCH": ("value and status disagree.", "ok needs a value; every other status needs value=null."),
    "METRIC_UNIT_MISMATCH": ("The unit differs from the registry.", "Use the unit the registry defines for this metric."),
    "RATIO_INCONSISTENT": ("A fraction disagrees with its numerator/denominator.", "value must equal numerator/denominator, with denominator > 0 and numerator <= denominator."),
    "RUNFILE_MANIFEST_MISMATCH": ("The run file disagrees with its manifest.", "id, arm, data_kind and manifest sha256 must match the manifest."),
    "CLOCK_REQUIREMENT_VIOLATED": (
        "A metric that needs one clock domain has a value although clocks are unsynchronized.",
        "Set the metric to not_computable with reason clock_unsynchronized.",
    ),
    "DUPLICATE_RUN_ID": ("Two run files share an independent_run_id.", "Each independent run appears exactly once; do not concatenate or repeat run files."),
    "NOT_SYNTHETIC": ("The summary generator only accepts SYNTHETIC fixtures.", "Real data needs a reviewed analysis path (gated on #14/#15/#16)."),
}


def hint_for(code: str) -> str:
    return CODES.get(code, ("", "See README."))[1]
